"""SQLite persistence. Call ingestion and run completion in one transaction."""
import json
import sqlite3
from collections import Counter
from datetime import date

from .arxiv import arxiv_id, iso, utcnow


def dumps(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True)


def connect(path):
    path.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(path)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys = ON")
    connection.executescript("""
        CREATE TABLE IF NOT EXISTS papers (
            arxiv_id TEXT PRIMARY KEY, metadata_json TEXT NOT NULL,
            selection_json TEXT NOT NULL, first_seen TEXT NOT NULL, last_seen TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS runs (
            id INTEGER PRIMARY KEY AUTOINCREMENT, kind TEXT NOT NULL,
            status TEXT NOT NULL, started_at TEXT NOT NULL, finished_at TEXT,
            since_utc TEXT, until_utc TEXT, config_json TEXT NOT NULL,
            summary_json TEXT, error TEXT
        );
        CREATE TABLE IF NOT EXISTS run_items (
            run_id INTEGER NOT NULL REFERENCES runs(id),
            arxiv_id TEXT NOT NULL REFERENCES papers(arxiv_id),
            action TEXT NOT NULL, metadata_json TEXT NOT NULL, selection_json TEXT NOT NULL,
            PRIMARY KEY (run_id, arxiv_id)
        );
        CREATE TABLE IF NOT EXISTS provenance (
            arxiv_id TEXT NOT NULL REFERENCES papers(arxiv_id),
            origin TEXT NOT NULL, url TEXT NOT NULL, evidence_json TEXT NOT NULL,
            PRIMARY KEY (arxiv_id, origin, url)
        );
        CREATE TABLE IF NOT EXISTS state (key TEXT PRIMARY KEY, value TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS digest_queue (
            arxiv_id TEXT PRIMARY KEY REFERENCES papers(arxiv_id),
            metadata_json TEXT NOT NULL, selection_json TEXT NOT NULL,
            added_at TEXT NOT NULL, last_error TEXT
        );
        CREATE TABLE IF NOT EXISTS pending_metadata (
            arxiv_id TEXT PRIMARY KEY, decision_json TEXT NOT NULL,
            run_id INTEGER NOT NULL REFERENCES runs(id),
            since_utc TEXT NOT NULL, until_utc TEXT NOT NULL, created_at TEXT NOT NULL,
            attempts INTEGER NOT NULL DEFAULT 0, last_attempt TEXT,
            next_retry_at TEXT, last_error TEXT
        );
        CREATE TABLE IF NOT EXISTS digest_processed (
            arxiv_id TEXT NOT NULL, version INTEGER NOT NULL, outcome TEXT NOT NULL,
            processed_at TEXT NOT NULL, PRIMARY KEY (arxiv_id,version)
        );
        CREATE TABLE IF NOT EXISTS editions (day TEXT PRIMARY KEY, payload_json TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS agent_tasks (
            task_id TEXT PRIMARY KEY, kind TEXT NOT NULL, request_json TEXT NOT NULL,
            status TEXT NOT NULL, last_node TEXT, error TEXT, result_json TEXT,
            created_at TEXT NOT NULL, updated_at TEXT NOT NULL
        );
    """)
    if "result_json" not in {row[1] for row in connection.execute("PRAGMA table_info(agent_tasks)")}:
        connection.execute("ALTER TABLE agent_tasks ADD COLUMN result_json TEXT")
        connection.commit()
    _migrate_agent_tasks(connection, path)
    return connection


def _migrate_agent_tasks(connection, path):
    """One-time copy of task metadata out of the legacy checkpoints database."""
    legacy = path.parent / "agent_checkpoints.sqlite3"
    if not legacy.exists():
        return
    connection.execute("ATTACH ? AS legacy", (str(legacy),))
    try:
        found = connection.execute(
            "SELECT 1 FROM legacy.sqlite_master WHERE type='table' AND name='agent_tasks'").fetchone()
        if found:
            wanted = ("task_id", "kind", "request_json", "status", "last_node", "error",
                      "result_json", "created_at", "updated_at")
            have = {row[1] for row in connection.execute("PRAGMA legacy.table_info(agent_tasks)")}
            columns = [column for column in wanted if column in have]
            connection.execute(f"INSERT OR IGNORE INTO agent_tasks ({','.join(columns)}) "
                               f"SELECT {','.join(columns)} FROM legacy.agent_tasks")
        connection.commit()
    finally:
        connection.execute("DETACH legacy")


def start_run(connection, kind, config, since=None, until=None):
    cursor = connection.execute(
        "INSERT INTO runs(kind,status,started_at,since_utc,until_utc,config_json) VALUES (?,?,?,?,?,?)",
        (kind, "running", iso(utcnow()), iso(since) if since else None,
         iso(until) if until else None, dumps(config)))
    connection.commit()
    return cursor.lastrowid


def queue_metadata(connection, run_id, decision, since, until):
    """Persist the model's evidence before trying the independent metadata API."""
    connection.execute("""INSERT OR IGNORE INTO pending_metadata
        (arxiv_id,decision_json,run_id,since_utc,until_utc,created_at)
        VALUES (?,?,?,?,?,?)""", (decision["arxiv_id"], dumps(decision), run_id,
                                 iso(since), iso(until), iso(utcnow())))


def pending_metadata(connection):
    rows = connection.execute("SELECT * FROM pending_metadata ORDER BY created_at,arxiv_id")
    result = []
    for row in rows:
        item = dict(row)
        item["decision"] = json.loads(item.pop("decision_json"))
        result.append(item)
    return result


def defer_metadata(connection, identifiers, error, next_retry_at, *, attempted=True):
    connection.executemany("""UPDATE pending_metadata SET attempts=attempts+?,
        last_attempt=CASE WHEN ? THEN ? ELSE last_attempt END,next_retry_at=?,last_error=? WHERE arxiv_id=?""",
        [(int(attempted), attempted, iso(utcnow()), iso(next_retry_at), str(error), identifier)
         for identifier in identifiers])


def save_paper(connection, run_id, incoming, config, selection_override=None):
    identifier = incoming["arxiv_id"]
    row = connection.execute("SELECT metadata_json,selection_json FROM papers WHERE arxiv_id=?", (identifier,)).fetchone()
    old = json.loads(row[0]) if row else None
    paper = incoming
    if old:
        if old["origin"] == "arxiv" and incoming["origin"] != "arxiv":
            paper = old  # History imports must never overwrite primary metadata.
        elif old["origin"] == incoming["origin"] == "arxiv" and incoming["version"] < old["version"]:
            paper = old  # Replaying a cache must not roll a newer version back.
        elif old["origin"] == incoming["origin"] == "recsys_frontier" and old["source_date"] > incoming["source_date"]:
            paper = old
        if paper is incoming and incoming["origin"] == "arxiv" and not incoming["companies"] and old["company_evidence_kind"] == "history_report":
            # Missing API affiliations do not erase an explicitly labeled secondary-source claim.
            paper = {**incoming, "companies": old["companies"], "company_evidence_kind": "history_report",
                     "company_evidence": old["company_evidence"],
                     "company_source_url": old.get("company_source_url", old.get("source_url"))}
    action = "new" if old is None else ("updated" if dumps(paper) != dumps(old) else "unchanged")
    use_override = (selection_override is not None and paper["origin"] == incoming["origin"]
                    and paper["version"] == incoming["version"])
    if use_override:
        selection = selection_override
    elif old is not None:
        selection = json.loads(row[1])  # Kept the stored snapshot; keep its selection too.
    else:
        # Offline imports carry no topical judgment; only the agent selects.
        selection = {"selected": False, "bucket": "unreviewed", "score": 0, "tags": [],
                     "reasons": [], "matched_terms": [], "text_basis": "offline_import"}
    now = iso(utcnow())
    connection.execute("""
        INSERT INTO papers VALUES (?,?,?,?,?) ON CONFLICT(arxiv_id) DO UPDATE SET
        metadata_json=excluded.metadata_json, selection_json=excluded.selection_json, last_seen=excluded.last_seen
    """, (identifier, dumps(paper), dumps(selection), now, now))
    source_url = incoming.get("source_url", incoming["url"])
    evidence = {"company_evidence": incoming.get("company_evidence", []),
                "company_evidence_kind": incoming.get("company_evidence_kind", "unknown"),
                "source_date": incoming.get("source_date"), "recorded_at": now}
    connection.execute("""
        INSERT INTO provenance VALUES (?,?,?,?) ON CONFLICT(arxiv_id,origin,url)
        DO UPDATE SET evidence_json=excluded.evidence_json
    """, (identifier, incoming["origin"], source_url, dumps(evidence)))
    # Repeated IDs in an input keep the strongest action, and the latest metadata snapshot.
    previous = connection.execute("SELECT action FROM run_items WHERE run_id=? AND arxiv_id=?",
                                  (run_id, identifier)).fetchone()
    if previous and previous[0] in {"new", "updated"} and action == "unchanged":
        action = previous[0]
    if previous and previous[0] == "new":
        action = "new"
    connection.execute("INSERT OR REPLACE INTO run_items VALUES (?,?,?,?,?)",
                       (run_id, identifier, action, dumps(paper), dumps(selection)))
    if paper["origin"] == "arxiv" and (action != "unchanged" or use_override):
        enqueue(connection, paper, selection)


def enqueue(connection, paper, selection):
    if not selection["selected"]:
        connection.execute("DELETE FROM digest_queue WHERE arxiv_id=?", (paper["arxiv_id"],))
        return
    if connection.execute("SELECT 1 FROM digest_processed WHERE arxiv_id=? AND version=?",
                          (paper["arxiv_id"], paper["version"])).fetchone():
        return
    existing = connection.execute("SELECT metadata_json FROM digest_queue WHERE arxiv_id=?", (paper["arxiv_id"],)).fetchone()
    if existing and json.loads(existing[0])["version"] > paper["version"]:
        return
    connection.execute("""INSERT INTO digest_queue VALUES (?,?,?,?,NULL)
        ON CONFLICT(arxiv_id) DO UPDATE SET metadata_json=excluded.metadata_json,
        selection_json=excluded.selection_json""",
        (paper["arxiv_id"], dumps(paper), dumps(selection), iso(utcnow())))


def history_paper(item):
    identifier, _ = arxiv_id(item["url"])
    source_date = date.fromisoformat(item["date"]).isoformat()
    if not item["title"].strip():
        raise ValueError("Missing history title: " + identifier)
    companies = sorted(set(item.get("companies", [])))
    return {"arxiv_id": identifier, "version": 0, "title": item["title"], "abstract": "",
            "published": None, "updated": None, "authors": [], "affiliations": [], "categories": [],
            "doi": "", "comment": "", "journal_ref": "", "url": "https://arxiv.org/abs/" + identifier,
            "pdf_url": "https://arxiv.org/pdf/" + identifier, "origin": "recsys_frontier",
            "source_url": item["page_url"], "source_date": source_date,
            "companies": companies, "company_evidence_kind": "history_report" if companies else "unknown",
            "company_evidence": {"source_raw": item.get("source_raw", ""),
                                 "evidence": item.get("evidence", ""), "notes": item.get("notes", ""),
                                 "correction": item.get("correction", "")}}


def run_summary(connection, run_id):
    rows = connection.execute("SELECT action,selection_json FROM run_items WHERE run_id=?", (run_id,)).fetchall()
    actions = Counter(row["action"] for row in rows)
    selected = [row for row in rows if json.loads(row["selection_json"])["selected"]]
    return {"unique_seen": len(rows), "actions": dict(actions), "selected_seen": len(selected),
            "preferred_team_seen": sum(json.loads(row["selection_json"]).get("team_priority") == "preferred" for row in selected),
            "report_papers": sum(row["action"] != "unchanged" for row in selected)}


def finish_run(connection, run_id, summary):
    status = "partial" if summary.get("metadata_failed") else "completed"
    connection.execute("UPDATE runs SET status=?,finished_at=?,summary_json=?,error=NULL WHERE id=?",
                       (status, iso(utcnow()), dumps(summary), run_id))


def fail_run(connection, run_id, error):
    connection.execute("UPDATE runs SET status='failed',finished_at=?,error=? WHERE id=?",
                       (iso(utcnow()), str(error), run_id))
    connection.commit()

