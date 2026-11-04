"""History store: batches, documents (with their full PackResult), events, step checkpoints, LLM cache.

Runs on Supabase PostgreSQL or local SQLite through service.db (same SQL). Reads return the column
names the UI has always used (document_id, filename, status, raw_text, ...).
"""
from checker.models import PackResult

from .db import Database, as_json, now_iso, to_json


class AlreadyReviewed(Exception):
    """The document is not waiting for a review (already decided, or never paused)."""


# old name -> column
_COLUMNS = {"status": "processing_status", "raw_text": "extracted_text", "filename": "original_filename"}

DOC_SELECT = """
SELECT d.*, d.id AS document_id, d.original_filename AS filename, d.processing_status AS status,
       d.extracted_text AS raw_text, b.mode AS mode, b.created_at AS batch_created_at
FROM documents d JOIN batches b ON b.batch_id = d.batch_id
"""
VISIBLE = "d.processing_status <> 'deleting'"
REVIEW_STATUSES = ("waiting_review", "reviewing")  # waiting for a person

EVENT_INSERT = "INSERT INTO document_events (batch_id, document_id, event_type, event_data, created_at)"
STEP_UPSERT = "INSERT INTO pipeline_checkpoints (document_id, step_name, status, checkpoint_data, created_at, updated_at)"
STEP_CONFLICT = ("ON CONFLICT (document_id, step_name) DO UPDATE SET status = excluded.status, "
                 "checkpoint_data = excluded.checkpoint_data, updated_at = excluded.updated_at")


def _event_row(batch_id: str, e) -> tuple:
    ts = e.timestamp.astimezone().isoformat(timespec="seconds")
    data = {"stage": e.stage, "status": e.status, "message": e.message, "duration_ms": e.duration_ms,
            "warning": e.warning, "metadata": e.metadata, "filename": e.filename}
    return (batch_id, e.document_id or None, f"{e.stage}.{e.status}", to_json(data), ts)


class HistoryStore:
    def __init__(self, db: Database):
        self.db = db

    # ---------------------------------------------------------------- batches & documents
    def create_batch(self, batch_id: str, mode: str, documents: list[dict]) -> None:
        """Insert a batch and its documents in one transaction (idempotent if retried)."""
        ts = now_iso()

        def tx(t):
            t.execute("INSERT INTO batches (batch_id, mode, status, total, created_at, updated_at) "
                      "VALUES (?, ?, 'queued', ?, ?, ?) ON CONFLICT (batch_id) DO NOTHING",
                      (batch_id, mode, len(documents), ts, ts))
            t.insert_many(
                "INSERT INTO documents (id, batch_id, position, file_hash, original_filename, storage_path, "
                "size_bytes, thread_id, processing_status, created_at, updated_at)",
                [(d["document_id"], batch_id, d["position"], d.get("file_hash"), d["filename"],
                  d.get("storage_path"), d.get("size_bytes"), d.get("thread_id"), "queued", ts, ts)
                 for d in documents],
                "ON CONFLICT (id) DO NOTHING")
        self.db.run(tx)

    def update_batch(self, batch_id: str, **cols) -> None:
        self._update("batches", "batch_id", batch_id, cols)

    def update_document(self, document_id: str, **cols) -> None:
        self._update("documents", "id", document_id, {_COLUMNS.get(k, k): v for k, v in cols.items()})

    def _update(self, table: str, key: str, value: str, cols: dict, tx=None) -> None:
        if not cols:
            return
        cols = {**cols, "updated_at": now_iso()}
        sql = f"UPDATE {table} SET {', '.join(f'{k} = ?' for k in cols)} WHERE {key} = ?"
        params = (*[to_json(v) if isinstance(v, (dict, list)) else v for v in cols.values()], value)
        if tx is not None:
            tx.execute(sql, params)
        else:
            self.db.execute(sql, params)

    def save_result(self, document_id: str, result: PackResult, status: str, raw_text: str | None,
                    failed_stage: str | None = None, *, batch_id: str | None = None, events=(),
                    steps: dict | None = None) -> None:
        """Result, the document's events and its step statuses: committed together."""
        cols = {"processing_status": status, "supplier": result.supplier, "item": result.item,
                "decision": result.decision, "extraction_source": result.extraction_source,
                "seconds": result.seconds, "processed_at": now_iso(), "failed_stage": failed_stage,
                "error": "; ".join(result.errors) or None, "extracted_text": raw_text,
                "result_json": result.model_dump_json()}
        self._save_document(document_id, cols, batch_id, events, steps)

    def _save_document(self, document_id: str, cols: dict, batch_id: str | None, events, steps: dict | None) -> None:
        """Update the document row, add its events and upsert its step statuses in one transaction."""
        def tx(t):
            self._update("documents", "id", document_id, cols, tx=t)
            if batch_id and events:
                t.insert_many(EVENT_INSERT, [_event_row(batch_id, e) for e in events])
            if steps:
                self._write_steps(t, document_id, steps)
        self.db.run(tx)

    def add_event(self, batch_id: str, event) -> None:
        self.add_events(batch_id, [event])

    def add_events(self, batch_id: str, events) -> None:
        rows = [_event_row(batch_id, e) for e in events]
        if rows:
            self.db.run(lambda t: t.insert_many(EVENT_INSERT, rows))

    def save_steps(self, document_id: str, steps: dict) -> None:
        self.db.run(lambda t: self._write_steps(t, document_id, steps))

    @staticmethod
    def _write_steps(t, document_id: str, steps: dict) -> None:
        ts = now_iso()
        t.insert_many(STEP_UPSERT, [(document_id, name, s["status"], to_json(s.get("data") or {}), ts, ts)
                                    for name, s in steps.items()], STEP_CONFLICT)

    # ---------------------------------------------------------------- human review
    def claim_review(self, document_id: str) -> None:
        """Atomically move waiting_review -> reviewing. Blocks a second submission of the same review."""
        n = self.db.execute("UPDATE documents SET processing_status = 'reviewing', updated_at = ? "
                            "WHERE id = ? AND processing_status = 'waiting_review'", (now_iso(), document_id))
        if n != 1:
            raise AlreadyReviewed(document_id)

    def release_review(self, document_id: str) -> None:
        self.update_document(document_id, status="waiting_review")

    def save_review(self, document_id: str, decision: str, comment: str, result: PackResult | None,
                    *, batch_id: str | None = None, events=(), steps: dict | None = None) -> None:
        cols = {"processing_status": "reviewed", "human_decision": decision, "human_comment": comment,
                "reviewed_at": now_iso()}
        if result is not None:
            cols["result_json"] = result.model_dump_json()
        self._save_document(document_id, cols, batch_id, events, steps)

    # ---------------------------------------------------------------- restart / recovery
    def mark_stale(self) -> int:
        """After a restart, anything that was running can no longer finish. Say so instead of hiding it."""
        ts = now_iso()

        def tx(t):
            n = t.execute("UPDATE documents SET processing_status = 'interrupted', updated_at = ?, error = "
                          "COALESCE(error, 'The app stopped before this document finished.') "
                          "WHERE processing_status IN ('queued', 'running')", (ts,))
            t.execute("UPDATE batches SET status = 'interrupted', updated_at = ?, finished_at = "
                      "COALESCE(finished_at, ?) WHERE status IN ('queued', 'running')", (ts, ts))
            t.execute("UPDATE documents SET processing_status = 'waiting_review', updated_at = ? "
                      "WHERE processing_status = 'reviewing'", (ts,))
            return n
        return self.db.run(tx)

    # ---------------------------------------------------------------- deletion (Clear History)
    def deletion_candidates(self) -> list[dict]:
        return self.db.query("SELECT id, batch_id, storage_path, thread_id, extracted_text, processing_status "
                             "FROM documents")

    def mark_deleting(self, document_ids: list[str]) -> None:
        if document_ids:
            ts = now_iso()
            self.db.run(lambda t: t.executemany(
                "UPDATE documents SET processing_status = 'deleting', updated_at = ? WHERE id = ?",
                [(ts, i) for i in document_ids]))

    def storage_paths_still_used(self, excluding: list[str]) -> set[str]:
        """Storage paths referenced by documents that are NOT being deleted (never delete those files)."""
        rows = self.db.query("SELECT id, storage_path FROM documents WHERE storage_path IS NOT NULL")
        skip = set(excluding)
        return {r["storage_path"] for r in rows if r["id"] not in skip}

    def delete_documents(self, document_ids: list[str]) -> None:
        """Delete rows of these documents (events and step checkpoints first), then empty batches."""
        if not document_ids:
            return

        def tx(t):
            for table, col in (("document_events", "document_id"), ("pipeline_checkpoints", "document_id"),
                               ("documents", "id")):
                t.executemany(f"DELETE FROM {table} WHERE {col} = ?", [(i,) for i in document_ids])
            t.execute("DELETE FROM document_events WHERE document_id IS NULL AND batch_id NOT IN "
                      "(SELECT batch_id FROM documents)")
            t.execute("DELETE FROM batches WHERE batch_id NOT IN (SELECT batch_id FROM documents)")
        self.db.run(tx)

    def pending_deletion(self) -> int:
        return self.db.query("SELECT COUNT(*) AS n FROM documents WHERE processing_status = 'deleting'")[0]["n"]

    # ---------------------------------------------------------------- LLM cache (Supabase backend)
    def cache_get(self, key: str) -> dict | None:
        rows = self.db.query("SELECT response FROM llm_cache WHERE cache_key = ?", (key,))
        return as_json(rows[0]["response"]) if rows else None

    def cache_put(self, key: str, response: dict, model: str, document: str, prompt_version: str) -> None:
        self.db.execute("INSERT INTO llm_cache (cache_key, model, prompt_version, document, response, created_at) "
                        "VALUES (?, ?, ?, ?, ?, ?) ON CONFLICT (cache_key) DO UPDATE SET response = excluded.response, "
                        "model = excluded.model", (key, model, prompt_version, document, to_json(response), now_iso()))

    def cache_delete(self, keys: list[str]) -> int:
        if not keys:
            return 0
        return self.db.run(lambda t: sum(t.execute("DELETE FROM llm_cache WHERE cache_key = ?", (k,)) for k in keys))

    # ---------------------------------------------------------------- reads
    def batches(self) -> list[dict]:
        def count(cond: str) -> str:
            return f"SUM(CASE WHEN {cond} THEN 1 ELSE 0 END)"
        return self.db.query(f"""
            SELECT b.*, {count("d.decision = 'APPROVE'")} AS approved, {count("d.decision = 'REJECT'")} AS rejected,
              {count("d.decision = 'NEEDS_REVIEW'")} AS needs_review,
              {count("d.processing_status = 'failed' OR d.failed_stage IS NOT NULL")} AS failed,
              {count("d.processing_status IN ('completed','waiting_review','reviewing','reviewed','failed')")}
                AS processed,
              AVG(d.seconds) AS avg_seconds
            FROM batches b JOIN documents d ON d.batch_id = b.batch_id AND d.processing_status <> 'deleting'
            GROUP BY b.batch_id, b.mode, b.status, b.total, b.started_at, b.finished_at, b.created_at, b.updated_at
            ORDER BY b.created_at DESC, b.batch_id DESC""")

    def documents(self, batch_id: str | None = None) -> list[dict]:
        sql, args = DOC_SELECT + f" WHERE {VISIBLE}", []
        if batch_id:
            sql += " AND d.batch_id = ?"
            args.append(batch_id)
        return self.db.query(sql + " ORDER BY b.created_at DESC, b.batch_id DESC, d.position", args)

    def document(self, document_id: str) -> dict | None:
        rows = self.db.query(DOC_SELECT + " WHERE d.id = ?", (document_id,))
        return rows[0] if rows else None

    # `docs`: rows already read with documents(), so a page that needs both does not query twice.
    def pending_reviews(self, docs: list[dict] | None = None) -> list[dict]:
        docs = self.documents() if docs is None else docs
        pending = [d for d in docs if d["status"] in REVIEW_STATUSES]
        return sorted(pending, key=lambda d: (d.get("processed_at") or "", d["document_id"]))  # oldest first

    def count_pending_reviews(self) -> int:
        """len(pending_reviews()) without reading every document row (the sidebar shows it on every page)."""
        return self.db.query("SELECT COUNT(*) AS n FROM documents d JOIN batches b ON b.batch_id = d.batch_id "
                             "WHERE d.processing_status IN ('waiting_review', 'reviewing')")[0]["n"]

    def interrupted(self, docs: list[dict] | None = None) -> list[dict]:
        docs = self.documents() if docs is None else docs
        return [d for d in docs if d["status"] == "interrupted" and d.get("storage_path")]

    def events(self, document_id: str | None = None, batch_id: str | None = None) -> list[dict]:
        sql, args = "SELECT * FROM document_events WHERE 1 = 1", []
        if document_id:
            sql += " AND document_id = ?"
            args.append(document_id)
        if batch_id:
            sql += " AND batch_id = ?"
            args.append(batch_id)
        out = []
        for r in self.db.query(sql + " ORDER BY id", args):
            data = as_json(r["event_data"]) or {}
            out.append({"id": r["id"], "batch_id": r["batch_id"], "document_id": r["document_id"],
                        "ts": r["created_at"], "event_type": r["event_type"], "stage": data.get("stage"),
                        "status": data.get("status"), "message": data.get("message", ""),
                        "duration_ms": data.get("duration_ms"), "warning": data.get("warning"),
                        "metadata": data.get("metadata") or {}})
        return out

    def steps(self, document_id: str) -> dict[str, dict]:
        rows = self.db.query("SELECT step_name, status, checkpoint_data, updated_at FROM pipeline_checkpoints "
                             "WHERE document_id = ?", (document_id,))
        return {r["step_name"]: {"status": r["status"], "data": as_json(r["checkpoint_data"]) or {},
                                 "updated_at": r["updated_at"]} for r in rows}


def load_result(doc: dict) -> PackResult | None:
    raw = doc.get("result_json")
    if not raw:
        return None
    return PackResult.model_validate(raw) if isinstance(raw, dict) else PackResult.model_validate_json(raw)
