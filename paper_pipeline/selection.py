"""Company matching against author affiliations; topical relevance is decided by the agent."""
import re
import unicodedata


def normalize(text):
    text = unicodedata.normalize("NFKC", text).casefold()
    return re.sub(r"[^\w]+", " ", text).strip()


def matches(text, terms):
    normalized = " " + normalize(text) + " "
    return [term for term in terms if " " + normalize(term) + " " in normalized]


def affiliation_companies(affiliations, config):
    aliases = {name: name for name in config["companies"]}
    aliases.update({alias: name for alias, name in config.get("company_aliases", {}).items()
                    if name in config["companies"]})
    evidence = []
    for affiliation in affiliations:
        for alias in matches(affiliation, aliases):
            evidence.append({"company": aliases[alias], "affiliation": affiliation})
    evidence = [dict(pair) for pair in sorted({tuple(sorted(e.items())) for e in evidence})]
    return sorted({e["company"] for e in evidence}), evidence


def organization_details(paper, analysis, config):
    """Return allowlisted companies from author affiliations, never from topic mentions."""
    model_names = [entry["organization"] for entry in analysis.get("affiliations", [])]
    for names, basis in (
        (model_names, "全文机构行（模型提取）"),
        (paper.get("affiliations", []), "arXiv 作者机构字段"),
        (paper.get("companies", []) if paper.get("company_evidence_kind") == "arxiv_affiliation" else [],
         "arXiv 公司机构字段"),
    ):
        companies, _ = affiliation_companies(names, config)
        if companies:
            return {"organizations": companies, "organization_basis": basis}
    return {"organizations": [], "organization_basis": "作者机构中未确认目标大公司"}
