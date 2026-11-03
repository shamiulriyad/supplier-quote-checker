"""SQLite history: batches, documents (with their full PackResult) and processing events.

One short-lived connection per call keeps it safe to use from the worker thread and the UI.
"""
import json
import sqlite3
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path

from checker.models import PackResult

SCHEMA = """
CREATE TABLE IF NOT EXISTS batches (
    batch_id     TEXT PRIMARY KEY,
    created_at   TEXT NOT NULL,
    started_at   TEXT,
    finished_at  TEXT,
    mode         TEXT NOT NULL,
    status       TEXT NOT NULL,          -- queued | running | finished | cancelled | interrupted
    total        INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS documents (
    document_id      TEXT PRIMARY KEY,
    batch_id         TEXT NOT NULL REFERENCES batches(batch_id),
    position         INTEGER NOT NULL,
    filename         TEXT NOT NULL,
    file_path        TEXT,
    size_bytes       INTEGER,
    status           TEXT NOT NULL,      -- queued | running | completed | waiting_review | reviewed | failed | cancelled | interrupted
    supplier         TEXT,
    item             TEXT,
    decision         TEXT,               -- the system's decision
    extraction_source TEXT,
    seconds          REAL,
    processed_at     TEXT,
    failed_stage     TEXT,
    error            TEXT,
    thread_id        TEXT,
    raw_text         TEXT,
    result_json      TEXT,
    human_decision   TEXT,
    human_comment    TEXT,
    reviewed_at      TEXT
);
CREATE TABLE IF NOT EXISTS events (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    batch_id     TEXT NOT NULL,
    document_id  TEXT,
    ts           TEXT NOT NULL,
    stage        TEXT NOT NULL,
    status       TEXT NOT NULL,
    message      TEXT NOT NULL,
    duration_ms  REAL,
    warning      TEXT,
    metadata     TEXT
);
CREATE INDEX IF NOT EXISTS idx_documents_batch ON documents(batch_id);
CREATE INDEX IF NOT EXISTS idx_events_doc ON events(document_id);
"""


def now() -> str:
    return datetime.now().isoformat(timespec="seconds")


class AlreadyReviewed(Exception):
    """The document is not waiting for a review (already decided, or never paused)."""


class HistoryStore:
    def __init__(self, path: str | Path):
        self.path = str(path)
        Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        with self._conn() as c:
            c.executescript(SCHEMA)

    @contextmanager
    def _conn(self):
        conn = sqlite3.connect(self.path, timeout=30)
        conn.row_factory = sqlite3.Row
        try:
            with conn:
                yield conn
        finally:
            conn.close()

    # ---------------------------------------------------------------- writes
    def create_batch(self, batch_id: str, mode: str, documents: list[dict]) -> None:
        with self._conn() as c:
            c.execute("INSERT INTO batches VALUES (?,?,?,?,?,?,?)",
                      (batch_id, now(), None, None, mode, "queued", len(documents)))
            c.executemany(
                "INSERT INTO documents (document_id, batch_id, position, filename, file_path, size_bytes, status)"
                " VALUES (:document_id, :batch_id, :position, :filename, :file_path, :size_bytes, 'queued')",
                [{**d, "batch_id": batch_id} for d in documents])

    def update_batch(self, batch_id: str, **cols) -> None:
        self._update("batches", "batch_id", batch_id, cols)

    def update_document(self, document_id: str, **cols) -> None:
        self._update("documents", "document_id", document_id, cols)

    def _update(self, table: str, key: str, value: str, cols: dict) -> None:
        if not cols:
            return
        sets = ", ".join(f"{k} = ?" for k in cols)
        with self._conn() as c:
            c.execute(f"UPDATE {table} SET {sets} WHERE {key} = ?", (*cols.values(), value))

    def save_result(self, document_id: str, result: PackResult, status: str, raw_text: str | None,
                    failed_stage: str | None = None) -> None:
        self.update_document(
            document_id, status=status, supplier=result.supplier, item=result.item, decision=result.decision,
            extraction_source=result.extraction_source, seconds=result.seconds, processed_at=now(),
            failed_stage=failed_stage, error="; ".join(result.errors) or None, raw_text=raw_text,
            result_json=result.model_dump_json(),
        )

    def add_event(self, batch_id: str, event) -> None:
        with self._conn() as c:
            c.execute("INSERT INTO events (batch_id, document_id, ts, stage, status, message, duration_ms, warning,"
                      " metadata) VALUES (?,?,?,?,?,?,?,?,?)",
                      (batch_id, event.document_id or None, event.timestamp.isoformat(timespec="seconds"),
                       event.stage, event.status, event.message, event.duration_ms, event.warning,
                       json.dumps(event.metadata, default=str)))

    def claim_review(self, document_id: str) -> None:
        """Atomically move waiting_review -> reviewing. Blocks a second submission of the same review."""
        with self._conn() as c:
            cur = c.execute("UPDATE documents SET status = 'reviewing' WHERE document_id = ? "
                            "AND status = 'waiting_review'", (document_id,))
            if cur.rowcount != 1:
                raise AlreadyReviewed(document_id)

    def release_review(self, document_id: str) -> None:
        self.update_document(document_id, status="waiting_review")

    def save_review(self, document_id: str, decision: str, comment: str, result: PackResult | None) -> None:
        cols = {"status": "reviewed", "human_decision": decision, "human_comment": comment, "reviewed_at": now()}
        if result is not None:
            cols["result_json"] = result.model_dump_json()
        self.update_document(document_id, **cols)

    def mark_stale(self) -> int:
        """After a restart, anything that was running can no longer finish. Say so instead of hiding it."""
        with self._conn() as c:
            n = c.execute("UPDATE documents SET status = 'interrupted', error = COALESCE(error, "
                          "'The app stopped before this document finished.') "
                          "WHERE status IN ('queued', 'running')").rowcount
            c.execute("UPDATE batches SET status = 'interrupted', finished_at = COALESCE(finished_at, ?) "
                      "WHERE status IN ('queued', 'running')", (now(),))
            c.execute("UPDATE documents SET status = 'waiting_review' WHERE status = 'reviewing'")
        return n

    def clear(self) -> None:
        with self._conn() as c:
            c.executescript("DELETE FROM events; DELETE FROM documents; DELETE FROM batches;")

    # ---------------------------------------------------------------- reads
    def batches(self) -> list[dict]:
        with self._conn() as c:
            rows = c.execute("""
                SELECT b.*,
                  SUM(d.decision = 'APPROVE') AS approved, SUM(d.decision = 'REJECT') AS rejected,
                  SUM(d.decision = 'NEEDS_REVIEW') AS needs_review, SUM(d.status = 'failed' OR d.failed_stage IS NOT NULL) AS failed,
                  SUM(d.status IN ('completed','waiting_review','reviewed','failed')) AS processed,
                  AVG(d.seconds) AS avg_seconds
                FROM batches b LEFT JOIN documents d ON d.batch_id = b.batch_id
                GROUP BY b.batch_id ORDER BY b.created_at DESC, b.rowid DESC""").fetchall()
        return [dict(r) for r in rows]

    def documents(self, batch_id: str | None = None) -> list[dict]:
        sql = ("SELECT d.*, b.mode, b.created_at AS batch_created_at FROM documents d "
               "JOIN batches b ON b.batch_id = d.batch_id")
        args: tuple = ()
        if batch_id:
            sql += " WHERE d.batch_id = ?"
            args = (batch_id,)
        sql += " ORDER BY b.created_at DESC, b.rowid DESC, d.position"
        with self._conn() as c:
            return [dict(r) for r in c.execute(sql, args).fetchall()]

    def document(self, document_id: str) -> dict | None:
        with self._conn() as c:
            row = c.execute("SELECT d.*, b.mode, b.created_at AS batch_created_at FROM documents d "
                            "JOIN batches b ON b.batch_id = d.batch_id WHERE d.document_id = ?",
                            (document_id,)).fetchone()
        return dict(row) if row else None

    def pending_reviews(self) -> list[dict]:
        pending = [d for d in self.documents() if d["status"] in ("waiting_review", "reviewing")]
        return sorted(pending, key=lambda d: (d.get("processed_at") or "", d["document_id"]))  # oldest first

    def events(self, document_id: str | None = None, batch_id: str | None = None) -> list[dict]:
        sql, args = "SELECT * FROM events WHERE 1=1", []
        if document_id:
            sql += " AND document_id = ?"
            args.append(document_id)
        if batch_id:
            sql += " AND batch_id = ?"
            args.append(batch_id)
        with self._conn() as c:
            rows = c.execute(sql + " ORDER BY id", args).fetchall()
        return [{**dict(r), "metadata": json.loads(r["metadata"] or "{}")} for r in rows]


def load_result(doc: dict) -> PackResult | None:
    return PackResult.model_validate_json(doc["result_json"]) if doc.get("result_json") else None
