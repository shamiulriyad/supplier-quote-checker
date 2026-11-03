"""Import the old local history into the current storage backend (Supabase when configured).

    python tools/migrate_local_history.py                  # source: runtime/
    python tools/migrate_local_history.py --source old_runtime --dry-run

Source (pre-Supabase layout): history.db (batches, documents, events), checkpoints.db (LangGraph) and the
PDFs referenced by documents.file_path (normally uploads/ or data/).

- Idempotent: documents get a deterministic UUID from their old id (uuid5) and are skipped if present.
- Preserves batch ids, SHA-256 hashes, result JSON, extracted text, events and human decisions.
- Copies the paused LangGraph state of documents waiting for review, so they can still be reviewed.
- Opens the source read-only and never deletes local files.
- Writes a JSON report next to the source (migration-report-<time>.json).
"""
import argparse
import hashlib
import json
import sqlite3
import sys
import uuid
from collections import defaultdict
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from checker.graph import checkpoint_serde  # noqa: E402
from service.db import to_json  # noqa: E402
from service.files import storage_path_for  # noqa: E402

NAMESPACE = uuid.UUID("6f1c2d64-3a8e-4f0b-9c55-1b7a4e2d9f10")  # fixed: same old id -> same new id, every run
WAITING = ("waiting_review", "reviewing")


def new_document_id(old_id: str) -> str:
    return str(uuid.uuid5(NAMESPACE, old_id))


def _ts(value: str | None) -> str | None:
    """Old timestamps are naive local time; store them with the local offset."""
    if not value:
        return None
    dt = datetime.fromisoformat(value)
    return (dt if dt.tzinfo else dt.astimezone()).isoformat(timespec="seconds")


def _read_only(path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(f"file:{path.as_posix()}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    return conn


def copy_thread(source_saver, target_saver, thread_id: str) -> bool:
    """Copy every checkpoint (oldest first) and pending write of one thread. False if nothing to copy."""
    cfg = {"configurable": {"thread_id": thread_id}}
    if target_saver.get_tuple(cfg) is not None:
        return False  # already there
    tuples = list(source_saver.list(cfg))[::-1]
    if not tuples:
        return False
    for t in tuples:
        parent = t.parent_config or {"configurable": {"thread_id": thread_id, "checkpoint_ns": ""}}
        target_saver.put(parent, t.checkpoint, t.metadata, t.checkpoint.get("channel_versions", {}))
        by_task = defaultdict(list)
        for task_id, channel, value in t.pending_writes or []:
            by_task[task_id].append((channel, value))
        for task_id, writes in by_task.items():
            target_saver.put_writes(t.config, writes, task_id)
    return True


def migrate(engine, source: Path, dry_run: bool = False) -> dict:
    report = {"source": str(source), "target": engine.settings.label, "dry_run": dry_run,
              "started": datetime.now().isoformat(timespec="seconds"), "batches": 0, "documents_migrated": 0,
              "documents_skipped": 0, "events": 0, "files_uploaded": 0, "files_deduplicated": 0,
              "files_missing": [], "reviews_resumable": 0, "reviews_not_resumable": [], "errors": []}
    history = source / "history.db"
    if not history.exists():
        report["errors"].append(f"{history} not found")
        return report
    src = _read_only(history)
    cols = {r["name"] for r in src.execute("PRAGMA table_info(documents)")}
    if "document_id" not in cols:
        report["errors"].append(f"{history} is not the old history format (no documents.document_id)")
        return report

    source_saver = None
    if (source / "checkpoints.db").exists():
        from langgraph.checkpoint.sqlite import SqliteSaver
        source_saver = SqliteSaver(_read_only(source / "checkpoints.db"), serde=checkpoint_serde())

    store, db = engine.store, engine.store.db
    batches = [dict(r) for r in src.execute("SELECT * FROM batches ORDER BY created_at")]
    docs = [dict(r) for r in src.execute("SELECT * FROM documents ORDER BY batch_id, position")]
    events = defaultdict(list)
    for r in src.execute("SELECT * FROM events ORDER BY id"):
        events[r["document_id"]].append(dict(r))

    known_batches = {b["batch_id"] for b in store.batches()}
    for b in batches:
        if b["batch_id"] in known_batches:
            continue
        report["batches"] += 1
        if not dry_run:
            db.execute("INSERT INTO batches (batch_id, mode, status, total, started_at, finished_at, created_at, "
                       "updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?) ON CONFLICT (batch_id) DO NOTHING",
                       (b["batch_id"], b["mode"], b["status"], b["total"], _ts(b["started_at"]),
                        _ts(b["finished_at"]), _ts(b["created_at"]), _ts(b["created_at"])))
            batch_events = [e for e in events.get(None, []) if e["batch_id"] == b["batch_id"]]
            store.db.run(lambda t, rows=batch_events, bid=b["batch_id"]: t.executemany(
                "INSERT INTO document_events (batch_id, document_id, event_type, event_data, created_at) "
                "VALUES (?, NULL, ?, ?, ?)", [_event(bid, e) for e in rows]))

    for d in docs:
        nid = new_document_id(d["document_id"])
        try:
            if store.document(nid):
                report["documents_skipped"] += 1
            else:
                _migrate_document(engine, d, nid, events.get(d["document_id"], []), report, dry_run)
            if d["status"] in WAITING and d.get("thread_id") and source_saver and not dry_run:
                copy_thread(source_saver, engine.checkpointer, d["thread_id"])
                if engine.can_resume(nid):
                    report["reviews_resumable"] += 1
                else:
                    report["reviews_not_resumable"].append(d["filename"])
        except Exception as exc:  # keep going; the run can be repeated
            report["errors"].append(f"{d['document_id']} ({d['filename']}): {exc}")

    report["finished"] = datetime.now().isoformat(timespec="seconds")
    if not dry_run:
        out = source / f"migration-report-{datetime.now():%Y%m%d-%H%M%S}.json"
        out.write_text(json.dumps(report, indent=2), encoding="utf-8")
        report["report_file"] = str(out)
    return report


def _event(batch_id: str, e: dict) -> tuple:
    data = {"stage": e["stage"], "status": e["status"], "message": e["message"], "duration_ms": e["duration_ms"],
            "warning": e["warning"], "metadata": json.loads(e["metadata"] or "{}"), "legacy_event_id": e["id"]}
    return (batch_id, f"{e['stage']}.{e['status']}", to_json(data), _ts(e["ts"]))


def _migrate_document(engine, d: dict, nid: str, doc_events: list, report: dict, dry_run: bool) -> None:
    file_hash = storage_path = None
    path = Path(d["file_path"]) if d.get("file_path") else None
    if path and not path.is_absolute():
        path = ROOT / path
    if path and path.exists():
        data = path.read_bytes()
        file_hash = hashlib.sha256(data).hexdigest()
        storage_path = storage_path_for(file_hash)
        if not dry_run:
            created = engine.files.put(storage_path, data, d["filename"])
            report["files_uploaded" if created else "files_deduplicated"] += 1
    else:
        report["files_missing"].append(d["filename"])

    report["documents_migrated"] += 1
    report["events"] += len(doc_events)
    if dry_run:
        return
    status = "interrupted" if d["status"] in ("queued", "running") else d["status"]

    def tx(t):
        t.execute(
            "INSERT INTO documents (id, batch_id, position, file_hash, original_filename, storage_path, size_bytes, "
            "supplier, item, decision, extraction_source, extracted_text, result_json, processing_status, "
            "failed_stage, error, seconds, thread_id, human_decision, human_comment, reviewed_at, processed_at, "
            "legacy_id, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, "
            "?, ?, ?, ?, ?) ON CONFLICT (id) DO NOTHING",
            (nid, d["batch_id"], d["position"], file_hash, d["filename"], storage_path, d["size_bytes"],
             d["supplier"], d["item"], d["decision"], d["extraction_source"], d["raw_text"], d["result_json"],
             status, d["failed_stage"], d["error"], d["seconds"], d["thread_id"], d["human_decision"],
             d["human_comment"], _ts(d["reviewed_at"]), _ts(d["processed_at"]), d["document_id"],
             _ts(d["processed_at"]) or datetime.now().astimezone().isoformat(), datetime.now().astimezone().isoformat()))
        t.executemany("INSERT INTO document_events (batch_id, document_id, event_type, event_data, created_at) "
                      "VALUES (?, ?, ?, ?, ?)",
                      [(d["batch_id"], nid, *_event(d["batch_id"], e)[1:]) for e in doc_events])
    engine.store.db.run(tx)


def main() -> int:
    from checker.env import load_dotenv
    from service.batch import Engine

    load_dotenv()
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--source", default=str(ROOT / "runtime"), help="folder with history.db and checkpoints.db")
    ap.add_argument("--dry-run", action="store_true", help="report what would be migrated, change nothing")
    args = ap.parse_args()
    engine = Engine()
    try:
        print(f"Target: {engine.settings.label}")
        report = migrate(engine, Path(args.source), dry_run=args.dry_run)
    finally:
        engine.close()
    for k, v in report.items():
        print(f"  {k}: {v}")
    return 1 if report["errors"] else 0


if __name__ == "__main__":
    sys.exit(main())
