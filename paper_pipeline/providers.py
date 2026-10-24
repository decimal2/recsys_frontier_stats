"""Official DeepSeek, Kimi and GLM APIs; no SDK or third-party gateway."""
import http.client
import json
import logging
import os
import re
import time
import urllib.error
import urllib.request

from .arxiv import network_permission_denied, retry_after

OFFICIAL = {
    "deepseek": ("https://api.deepseek.com", "DEEPSEEK_API_KEY"),
    "kimi": ("https://api.moonshot.cn/v1", "MOONSHOT_API_KEY"),
    "glm": ("https://open.bigmodel.cn/api/paas/v4", "ZAI_API_KEY"),
}
LOG = logging.getLogger(__name__)


def redact_secrets(text):
    secrets = {os.environ.get(variable, "").strip() for _, variable in OFFICIAL.values()}
    for secret in sorted(filter(None, secrets), key=len, reverse=True):
        text = text.replace(secret, "[REDACTED]")
    text = re.sub(r"(?i)(Bearer\s+)[^\s\"']+", r"\1[REDACTED]", text)
    return re.sub(r"(https?://)[^/@\s]+:[^/@\s]+@", r"\1[REDACTED]@", text)


def api_error_details(error):
    """Keep bounded, redacted error fields; never dump a response or echoed request."""
    details = {}
    try:
        response = json.loads(error.read(65536))
        detail = response.get("error", response) if isinstance(response, dict) else None
        if isinstance(detail, dict):
            for field in ("message", "type", "code", "param"):
                value = detail.get(field)
                if isinstance(value, (str, int)):
                    details[field] = " ".join(redact_secrets(str(value)).split())[:1000]
    except (ValueError, UnicodeError, OSError):
        pass
    for header in ("x-request-id", "request-id", "x-trace-id"):
        value = (error.headers or {}).get(header)
        if value:
            details["request_id"] = " ".join(redact_secrets(str(value)).split())[:200]
            break
    return json.dumps(details, ensure_ascii=False) if details else "服务端未返回可解析的 JSON 错误字段"


def load_env(path):
    """Read simple KEY=value assignments without shell execution or secret output."""
    if not path.exists():
        return
    allowed = {value[1] for value in OFFICIAL.values()}
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        key, separator, value = line.removeprefix("export ").partition("=")
        key, value = key.strip(), value.strip()
        if separator and key in allowed:
            if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
                value = value[1:-1]
            if value and not os.environ.get(key):
                os.environ[key] = value


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, request, fp, code, msg, headers, newurl):
        raise RuntimeError("Official API redirected; request aborted to protect credentials")


class OfficialAPIError(RuntimeError):
    def __init__(self, provider, status, details):
        self.status = status
        super().__init__(f"{provider} API HTTP {status}; {details}")


class OfficialClient:
    def __init__(self, config, provider=None):
        self.provider = provider or config["llm"]["provider"]
        if self.provider not in OFFICIAL:
            raise ValueError("Choose one official provider: deepseek, kimi, glm")
        self.base_url, self.key_env = OFFICIAL[self.provider]
        self.settings = config["llm"]
        self.profile = self.settings["providers"][self.provider]
        self.model = self.profile["model"]

    def require_key(self):
        key = os.environ.get(self.key_env, "").strip()
        if not key:
            raise RuntimeError(f"未配置 {self.key_env}；请在本机环境或 pipeline 的 .env 文件中配置。")
        return key

    def validate_generation(self):
        limit = self.profile.get("max_output")
        value = self.settings["max_tokens"]
        if type(value) is not int or value < 1:
            raise ValueError("llm.max_tokens 必须为正整数")
        if limit is not None and value > limit:
            raise ValueError(f"llm.max_tokens={value} 超过 {self.model} 的最大输出上限 {limit}；"
                             "max_tokens 控制生成长度，不是输入上下文长度。")

    def post(self, path, payload, warn_statuses=()):
        key = self.require_key()
        opener = urllib.request.build_opener(NoRedirect())
        for attempt in range(self.settings["retries"] + 1):
            LOG.info("官方 API 请求 provider=%s path=%s model=%s max_tokens=%s attempt=%s/%s", self.provider, path,
                     self.model, payload.get("max_tokens"), attempt + 1, self.settings["retries"] + 1)
            request = urllib.request.Request(self.base_url + path,
                data=json.dumps(payload, ensure_ascii=False).encode(), method="POST",
                headers={"Authorization": "Bearer " + key, "Content-Type": "application/json"})
            try:
                with opener.open(request, timeout=self.settings["timeout_seconds"]) as response:
                    result = json.loads(response.read())
                if not isinstance(result, dict):
                    raise ValueError("Official API returned a non-object response")
                LOG.info("官方 API 响应成功 provider=%s path=%s", self.provider, path)
                return result
            except urllib.error.HTTPError as error:
                status = error.code
                delay = max(2 ** (attempt + 1), retry_after((error.headers or {}).get("Retry-After")))
                try:
                    details = api_error_details(error)
                finally:
                    error.close()
                if status not in {408, 429, 500, 502, 503, 504} or attempt == self.settings["retries"]:
                    level = logging.WARNING if status in warn_statuses else logging.ERROR
                    LOG.log(level, "官方 API 失败 provider=%s path=%s HTTP=%s attempt=%s details=%s", self.provider, path,
                            status, attempt + 1, details, exc_info=True)
                    raise OfficialAPIError(self.provider, status, details) from None
                LOG.warning("官方 API 限流或临时错误 provider=%s HTTP=%s，%.1f 秒后重试 details=%s", self.provider, status, delay, details)
                time.sleep(delay)
            except (urllib.error.URLError, TimeoutError, ConnectionError, PermissionError,
                    http.client.HTTPException) as error:  # IncompleteRead/RemoteDisconnected: response cut off mid-stream.
                if network_permission_denied(error):
                    LOG.error("官方 API 网络权限被阻止 provider=%s", self.provider, exc_info=True)
                    raise RuntimeError(f"{self.provider} API 请求被当前执行环境的网络权限阻止（EPERM/EACCES）；尚无法验证密钥，请在允许联网的环境运行。") from None
                if attempt == self.settings["retries"]:
                    LOG.error("官方 API 网络连接失败，重试已用尽 provider=%s", self.provider, exc_info=True)
                    raise RuntimeError(f"{self.provider} API 网络连接失败；未完成的论文可重试。") from None
                LOG.warning("官方 API 网络连接失败 provider=%s，%s 秒后重试", self.provider, 2 ** (attempt + 1), exc_info=True)
                time.sleep(2 ** (attempt + 1))
        raise RuntimeError("Official API retry exhausted")

    def tools(self, messages, definitions, max_tokens):
        """Keep reasoning_content in memory for subsequent thinking-model tool turns."""
        self.validate_generation()
        response = self.post("/chat/completions", {
            "model": self.model, "messages": messages, "tools": definitions,
            "tool_choice": "auto", "stream": False, "max_tokens": max_tokens,
            **self.profile.get("parameters", {})})
        try:
            choice = response["choices"][0]
            if choice["finish_reason"] not in {"stop", "tool_calls"}:
                raise ValueError("Agent output incomplete: " + str(choice["finish_reason"]))
            raw = choice["message"]
            if raw.get("role", "assistant") != "assistant":
                raise ValueError("Expected an assistant tool response")
            message = {"role": "assistant", "content": raw.get("content")}
            if "reasoning_content" in raw:
                message["reasoning_content"] = raw["reasoning_content"]
            calls = raw.get("tool_calls") or []
            if not isinstance(calls, list):
                raise ValueError("Invalid tool_calls")
            identifiers = set()
            for call in calls:
                if (call["type"] != "function" or not isinstance(call["id"], str) or not call["id"]
                        or call["id"] in identifiers or not isinstance(call["function"]["name"], str)
                        or not isinstance(call["function"]["arguments"], str)):
                    raise ValueError("Invalid function tool call")
                identifiers.add(call["id"])
            if calls:
                message["tool_calls"] = calls
        except (KeyError, IndexError, TypeError, AttributeError) as error:
            raise ValueError("Unexpected tool completion format") from error
        return message, {"provider": self.provider, "model": response.get("model", self.model),
                         "request_id": response.get("id"), "usage": response.get("usage", {})}

    def json(self, system, user):
        self.validate_generation()
        response = self.post("/chat/completions", {
            "model": self.model, "messages": [{"role": "system", "content": system},
                                               {"role": "user", "content": user}],
            "response_format": {"type": "json_object"}, "stream": False,
            "max_tokens": self.settings["max_tokens"], **self.profile.get("parameters", {})})
        try:
            choice = response["choices"][0]
            if choice["finish_reason"] != "stop":
                raise ValueError("Model output incomplete: " + str(choice["finish_reason"]))
            content = choice["message"]["content"]
            result = json.loads(content)
            if not isinstance(result, dict):
                raise ValueError("Expected a JSON object")
        except (KeyError, IndexError, TypeError) as error:
            raise ValueError("Unexpected chat completion format") from error
        return result, {"provider": self.provider, "model": response.get("model", self.model),
                        "request_id": response.get("id"), "usage": response.get("usage", {})}


class DeepSeekSearch:
    def __init__(self, config):
        self.client = OfficialClient(config, "deepseek")

    def search(self, query, count=10):
        if not query.strip() or len(query) > 128 or not 1 <= count <= 50:
            raise ValueError("DeepSeek search requires query length 1..128 and count 1..50")
        result = self.client.post("/v1/tools/search", {"query": query, "count": count, "need_content": False})
        if not isinstance(result.get("results"), list):
            raise ValueError("Invalid DeepSeek Search response")
        return result["results"]

    def fetch(self, url):
        # A missing/unfetchable page is a routine outcome, not a client failure.
        result = self.client.post("/v1/tools/fetch", {"url": url}, warn_statuses={400, 403, 404, 422})
        if not isinstance(result.get("content"), str) or not result["content"].strip():
            raise ValueError("DeepSeek Fetch returned no content")
        return result
