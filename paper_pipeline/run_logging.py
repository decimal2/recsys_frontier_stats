"""One UTF-8 log per invocation, grouped by the Beijing start date."""
import logging
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path

from .providers import redact_secrets

BEIJING = timezone(timedelta(hours=8))


class RedactingFormatter(logging.Formatter):
    def formatTime(self, record, datefmt=None):
        return datetime.fromtimestamp(record.created, BEIJING).isoformat(timespec="milliseconds")

    def format(self, record):
        text = super().format(record)
        # Resolve at format time: .env may be loaded after logging is initialized.
        return redact_secrets(text)


@contextmanager
def run_logging(directory):
    started = datetime.now(BEIJING)
    directory = Path(directory) / started.strftime("%Y-%m-%d")
    directory.mkdir(parents=True, exist_ok=True)
    name, sequence = started.strftime("%H-%M-%S"), 0
    while True:
        suffix = f"-{sequence}" if sequence else ""
        path = directory / f"{name}{suffix}.log"
        try:
            # Exclusive creation also prevents simultaneous processes from sharing a log.
            file_handler = logging.FileHandler(path, mode="x", encoding="utf-8")
            break
        except FileExistsError:
            sequence += 1
    console_handler = logging.StreamHandler()  # stderr; stdout remains machine-readable JSON.
    handlers = [file_handler, console_handler]
    formatter = RedactingFormatter("%(asctime)s [%(levelname)s] %(name)s: %(message)s")
    logger = logging.getLogger("paper_pipeline")
    previous = logger.handlers[:], logger.level, logger.propagate, logger.disabled
    for handler in handlers:
        handler.setFormatter(formatter)
        handler.setLevel(logging.INFO)
    logger.handlers, logger.level, logger.propagate, logger.disabled = handlers, logging.INFO, False, False
    try:
        yield path
    finally:
        logger.handlers, logger.level, logger.propagate, logger.disabled = previous
        for handler in handlers:
            handler.close()
