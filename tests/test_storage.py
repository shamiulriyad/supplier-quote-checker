"""Persistent storage: file stores, database layer, Supabase backend (mocked), Clear History, recovery, migration.

No Supabase credentials are needed: Supabase Storage is replaced by FakeSupabaseClient and the database
runs on the SQLite dialect of the same store code. tests/integration/ runs against a real project.
"""
import json
import sqlite3
import uuid
from datetime import datetime, timezone
from decimal import Decimal

import pytest

from checker import extract_llm
from checker.graph import build_graph, checkpoint_serde, initial_state, report_node
from service.backend import BackendConfigError, DbLLMCache, resolve_settings
from service.batch import BatchStartError, Engine
from service.db import PostgresDatabase, SqliteDatabase, _normalize
from service.files import (LocalFileStore, StorageUnavailable, SupabaseFileStore, is_managed_path,
                           storage_path_for)

from .conftest import DATA

PDF01 = (DATA / "supplier-pack-01.pdf").read_bytes()
PATH01 = storage_path_for(__import__("hashlib").sha256(PDF01).hexdigest())


# ============================================================== fake Supabase Storage

class FakeBucketAPI:
    def __init__(self, owner):
        self.o = owner

    def _maybe_fail(self, op):
        if self.o.fail.get(op, 0) > 0:
            self.o.fail[op] -= 1
            raise ConnectionError(f"simulated network error during {op}")

    def exists(self, path):
        self._maybe_fail("exists")
        return path in self.o.objects

    def upload(self, path, data, file_options=None):
        self._maybe_fail("upload")
        if path in self.o.objects:
            raise Exception("409 Duplicate: The resource already exists")
        self.o.objects[path] = bytes(data)
        self.o.options[path] = file_options
        self.o.uploads += 1

    def download(self, path):
        self._maybe_fail("download")
        if path not in self.o.objects:
            raise Exception("404 Object not found")
        return self.o.objects[path]

    def remove(self, paths):
        self._maybe_fail("remove")
        for p in paths:
            if p in self.o.undeletable:
                raise ConnectionError("simulated failure deleting " + p)
        for p in paths:
            self.o.objects.pop(p, None)
        return [{"name": p} for p in paths]


class FakeStorage:
    def __init__(self):
        self.objects, self.options, self.fail, self.undeletable = {}, {}, {}, set()
        self.uploads = 0
        self.buckets = {}

    def from_(self, bucket):
        return FakeBucketAPI(self)

    def get_bucket(self, bucket):
        if bucket not in self.buckets:
            raise Exception("Bucket not found")
        return type("Bucket", (), {"public": self.buckets[bucket]["public"]})()

    def create_bucket(self, bucket, options=None):
        self.buckets[bucket] = dict(options or {})
        return {"name": bucket}


class FakeSupabaseClient:
    def __init__(self):
        self.storage = FakeStorage()


@pytest.fixture
def fake_files():
    client = FakeSupabaseClient()
    store = SupabaseFileStore("https://example.supabase.co", "service-key", "supplier-documents", client=client)
    store.ensure_bucket()
    return store, client.storage


@pytest.fixture
def engine(tmp_path, fake_files, monkeypatch):
    monkeypatch.setattr("service.files.time.sleep", lambda s: None)  # no real back-off waits
    files, _storage = fake_files
    eng = Engine(tmp_path / "rt", db=SqliteDatabase(tmp_path / "rt" / "app.db"), files=files)
    eng.cache_dir = str(tmp_path / "llm-cache")
    yield eng
    eng.close()
    extract_llm.set_cache_backend(None)


def queue(eng, *packs, uploads=()):
    q = eng.new_queue()
    for p in packs:
        q.add_file(DATA / f"supplier-pack-{p}.pdf")
    for name, data in uploads:
        q.add_upload(name, data)
    return q


def run(eng, q, mode="regex"):
    return eng.start_batch(list(q.items), mode, run_in_thread=False).snapshot()


# ============================================================== configuration

def test_settings_local_when_nothing_configured():
    assert resolve_settings({}).kind == "local"


def test_settings_supabase_when_all_configured():
    s = resolve_settings({"SUPABASE_URL": "u", "SUPABASE_KEY": "k", "SUPABASE_DB_URL": "d"})
    assert (s.kind, s.bucket) == ("supabase", "supplier-documents")


def test_partial_configuration_is_refused_and_names_missing_without_values():
    with pytest.raises(BackendConfigError) as err:
        resolve_settings({"SUPABASE_URL": "https://x.supabase.co", "SUPABASE_KEY": "secret-value"})
    assert "SUPABASE_DB_URL" in str(err.value) and "secret-value" not in str(err.value)


# ============================================================== database layer

def test_postgres_dialect_uses_psycopg_placeholders():
    db = PostgresDatabase.__new__(PostgresDatabase)
    assert db.sql("SELECT * FROM documents WHERE id = ? AND batch_id = ?") == \
        "SELECT * FROM documents WHERE id = %s AND batch_id = %s"


def test_rows_are_normalized_for_the_ui():
    aware = datetime(2026, 11, 3, 4, 0, 0, tzinfo=timezone.utc)
    row = _normalize({"created_at": aware, "seconds": Decimal("0.25"), "id": uuid.UUID(int=1),
                      "reviewed_at": "2026-11-03T04:00:00+00:00"})
    local = aware.astimezone().replace(tzinfo=None).isoformat(timespec="seconds")
    assert row == {"created_at": local, "seconds": 0.25, "id": str(uuid.UUID(int=1)), "reviewed_at": local}


def test_postgres_schema_file_covers_the_store_tables():
    sql = (DATA.parent / "supabase" / "migrations" / "20261103000000_initial_schema.sql").read_text()
    for table in ("batches", "documents", "document_events", "pipeline_checkpoints", "llm_cache"):
        assert f"create table if not exists {table}" in sql
    assert "unique (batch_id, file_hash)" in sql and "enable row level security" in sql
    bucket = (DATA.parent / "supabase" / "migrations" / "20261103000100_storage_bucket.sql").read_text()
    assert "false" in bucket.split("values")[1].split(")")[0]  # public = false


# ============================================================== file stores

def test_local_store_is_content_addressed_and_atomic(tmp_path):
    store = LocalFileStore(tmp_path)
    assert store.put(PATH01, PDF01) is True
    assert store.put(PATH01, PDF01) is False  # duplicate
    assert store.get(PATH01) == PDF01
    assert not list(tmp_path.rglob("*.part"))


@pytest.mark.parametrize("bad", ["../secrets.env", "pdfs/../../x.pdf", "other/ab/" + "a" * 64 + ".pdf", ""])
def test_unmanaged_paths_are_refused(tmp_path, bad):
    assert not is_managed_path(bad)
    with pytest.raises(ValueError):
        LocalFileStore(tmp_path).put(bad, b"x")


def test_supabase_store_dedupes_and_keeps_filename_metadata(fake_files):
    files, storage = fake_files
    assert files.put(PATH01, PDF01, "supplier-pack-01.pdf") is True
    assert files.put(PATH01, PDF01, "renamed.pdf") is False
    assert storage.uploads == 1
    assert storage.options[PATH01]["metadata"] == {"original_filename": "supplier-pack-01.pdf"}
    assert storage.options[PATH01]["content-type"] == "application/pdf"
    assert files.get(PATH01) == PDF01


def test_supabase_store_retries_transient_errors(fake_files, monkeypatch):
    monkeypatch.setattr("service.files.time.sleep", lambda s: None)
    files, storage = fake_files
    storage.fail = {"upload": 2}
    assert files.put(PATH01, PDF01) is True  # third attempt succeeds
    storage.fail = {"download": 5}
    with pytest.raises(StorageUnavailable):
        files.get(PATH01)


def test_bucket_is_created_private_and_public_buckets_are_refused():
    client = FakeSupabaseClient()
    store = SupabaseFileStore("u", "k", "supplier-documents", client=client)
    store.ensure_bucket()
    assert client.storage.buckets["supplier-documents"]["public"] is False
    client.storage.buckets["supplier-documents"]["public"] = True
    assert store.check()[0] is False
    with pytest.raises(StorageUnavailable):
        store.ensure_bucket()


# ============================================================== processing persistence

def test_batch_uploads_pdfs_and_persists_results(engine, fake_files):
    _files, storage = fake_files
    snap = run(engine, queue(engine, "01", "04"))
    assert storage.uploads == 2
    docs = engine.store.documents(snap.batch_id)
    assert [d["decision"] for d in docs] == ["APPROVE", "NEEDS_REVIEW"]
    d = docs[0]
    assert d["storage_path"] == PATH01 and d["file_hash"] in PATH01 and d["filename"] == "supplier-pack-01.pdf"
    result = json.loads(d["result_json"])
    business = [r for r in result["rule_results"] if r["category"] != "verification"]
    gates = [r for r in result["rule_results"] if r["category"] == "verification"]
    assert len(result["fields"]) == 24 and len(business) == 14 and [g["rule_id"] for g in gates] == ["V1", "V2"]
    assert result["fields"]["unit_price"]["quote"].startswith("A Cotton twill")
    assert len(d["raw_text"]) > 500
    assert engine.read_pdf(d) == PDF01  # preview source


def test_same_pdf_in_a_later_batch_is_not_uploaded_again(engine, fake_files):
    _files, storage = fake_files
    run(engine, queue(engine, "01"))
    run(engine, queue(engine, "01"))
    assert storage.uploads == 1
    assert len(engine.store.documents()) == 2  # two runs, one stored file


def test_events_and_step_checkpoints_are_saved_per_document(engine):
    snap = run(engine, queue(engine, "06"))
    doc_id = snap.jobs[0].document_id
    events = engine.store.events(document_id=doc_id)
    assert [e["stage"] for e in events if e["status"] == "running"][:3] == ["parse", "extract", "verify"]
    assert events[-1]["message"].startswith("Decision: REJECT")
    steps = engine.store.steps(doc_id)
    assert steps["check_rules"]["status"] == "completed" and steps["human_review"]["status"] == "skipped"
    assert steps["parse"]["data"]["thread_id"] == snap.jobs[0].thread_id


# ============================================================== failure handling

def test_storage_failure_saves_nothing_and_removes_new_uploads(engine, fake_files):
    _files, storage = fake_files
    q = queue(engine, "01", "02")
    storage.fail = {"exists": 0, "upload": 0}
    real_upload = FakeBucketAPI.upload
    calls = {"n": 0}

    def flaky(self, path, data, file_options=None):
        calls["n"] += 1
        if calls["n"] > 1:  # first file uploads, second keeps failing
            raise ConnectionError("network down")
        return real_upload(self, path, data, file_options)

    FakeBucketAPI.upload = flaky
    try:
        with pytest.raises(BatchStartError):
            engine.start_batch(list(q.items), "regex", run_in_thread=False)
    finally:
        FakeBucketAPI.upload = real_upload
    assert storage.objects == {}  # the first file was removed again
    assert engine.store.documents() == [] and len(q) == 2


def test_database_failure_after_upload_removes_the_upload(engine, fake_files, monkeypatch):
    _files, storage = fake_files

    def broken(*a, **k):
        raise sqlite3.OperationalError("disk I/O error")

    monkeypatch.setattr(engine.store, "create_batch", broken)
    with pytest.raises(BatchStartError):
        engine.start_batch(list(queue(engine, "01").items), "regex", run_in_thread=False)
    assert storage.objects == {}


def test_unsaved_result_is_kept_and_saved_later(engine, monkeypatch):
    real = engine.store.save_result
    monkeypatch.setattr(engine.store, "save_result", lambda *a, **k: (_ for _ in ()).throw(
        sqlite3.OperationalError("database unavailable")))
    runner = engine.start_batch(list(queue(engine, "03").items), "regex", run_in_thread=False)
    snap = runner.snapshot()
    assert snap.jobs[0].unsaved and snap.counts()["unsaved"] == 1
    assert engine.store.document(snap.jobs[0].document_id)["decision"] is None
    monkeypatch.setattr(engine.store, "save_result", real)
    assert runner.retry_unsaved() == 0
    assert engine.store.document(snap.jobs[0].document_id)["decision"] == "REJECT"


def test_interrupted_documents_can_be_requeued_from_storage(engine):
    snap = run(engine, queue(engine, "02"))
    doc_id = snap.jobs[0].document_id
    engine.store.update_document(doc_id, status="interrupted")
    q = engine.new_queue()
    msgs = engine.requeue([doc_id], q)
    assert len(q) == 1 and q.items[0].source == "requeue" and q.items[0].filename == "supplier-pack-02.pdf"
    assert "Added" in msgs[0]
    assert engine.store.document(doc_id)["status"] == "cancelled"


def test_upload_validation(engine):
    q = engine.new_queue()
    assert q.add_upload("../../evil.pdf", b"%PDF-1.4 tiny")[0]
    assert q.items[0].filename == "evil.pdf"
    assert not q.add_upload("big.pdf", b"x" * (11 * 1024 * 1024))[0]
    assert not q.add_upload("empty.pdf", b"")[0]


# ============================================================== review + restart

def test_review_resume_after_restart_with_supabase_style_storage(tmp_path, fake_files, monkeypatch):
    files, _ = fake_files
    first = Engine(tmp_path / "rt", db=SqliteDatabase(tmp_path / "rt" / "app.db"), files=files)
    snap = run(first, queue(first, "04"))
    doc_id = snap.jobs[0].document_id
    first.close()
    second = Engine(tmp_path / "rt", db=SqliteDatabase(tmp_path / "rt" / "app.db"), files=files)
    try:
        assert second.can_resume(doc_id)
        result = second.submit_review(doc_id, "APPROVE", "fine")
        assert result.human_decision["decision"] == "APPROVE"
        assert second.store.steps(doc_id)["report"]["status"] == "completed"
    finally:
        second.close()


# ============================================================== LLM cache

def test_database_llm_cache_and_fixture_fallback(engine, tmp_path, monkeypatch):
    text = "some document text"
    extract_llm.set_cache_backend(DbLLMCache(engine.store))
    assert not extract_llm.has_cached("unused", text)
    extract_llm.save_cached("unused", text, {"ok": True}, "gpt-x", "a.pdf")
    assert extract_llm.load_cached("unused", text) == {"ok": True}
    assert engine.store.cache_get(extract_llm.cache_key(text)) == {"ok": True}
    # an optional local fixtures folder is read after the database
    fixtures = tmp_path / "fixtures"
    monkeypatch.setattr(extract_llm, "FIXTURES_DIR", fixtures)
    extract_llm.FileCache(fixtures).put(extract_llm.cache_key("other text"), {"from": "fixture"}, "m", "d")
    assert extract_llm.load_cached("unused", "other text") == {"from": "fixture"}


# ============================================================== Clear History

def test_clear_history_deletes_everything_it_owns(engine, fake_files, tmp_path, monkeypatch):
    _files, storage = fake_files
    fixtures = tmp_path / "fixtures"
    monkeypatch.setattr(extract_llm, "FIXTURES_DIR", fixtures)
    extract_llm.set_cache_backend(None)  # this test checks the local file cache
    snap = run(engine, queue(engine, "01", "04"))
    paused = snap.jobs[1]
    text = engine.store.document(snap.jobs[0].document_id)["raw_text"]
    cache = extract_llm.FileCache(engine.cache_dir)
    cache.put(extract_llm.cache_key(text), {"x": 1}, "m", "d")
    extract_llm.FileCache(fixtures).put(extract_llm.cache_key(text), {"x": 1}, "m", "d")  # read-only fixture
    storage.objects["pdfs/zz/unrelated.pdf"] = b"not ours"  # must survive

    report = engine.clear_history()
    assert report.complete and report.documents_deleted == 2 and report.files_deleted == 2
    assert report.cache_entries_deleted == 1 and report.checkpoints_deleted == 2
    assert engine.store.documents() == [] and engine.store.batches() == []
    assert engine.store.events() == [] and engine.store.steps(paused.document_id) == {}
    assert list(storage.objects) == ["pdfs/zz/unrelated.pdf"]
    assert not engine.can_resume(paused.document_id)
    assert extract_llm.FileCache(extract_llm.FIXTURES_DIR).get(extract_llm.cache_key(text))  # fixture kept


def test_clear_history_partial_failure_is_retried(engine, fake_files):
    _files, storage = fake_files
    run(engine, queue(engine, "01", "02"))
    storage.undeletable = {PATH01}
    report = engine.clear_history()
    assert not report.complete and report.files_failed == [PATH01] and report.documents_kept == 1
    assert engine.store.documents() == []  # hidden while marked 'deleting'
    assert engine.store.pending_deletion() == 1
    storage.undeletable = set()
    report = engine.clear_history()
    assert report.complete and engine.store.pending_deletion() == 0 and storage.objects == {}


def test_clear_history_refused_while_a_batch_runs(engine):
    runner = engine.start_batch(list(queue(engine, "01").items), "regex", run_in_thread=False)
    runner.batch.status = "running"
    with pytest.raises(RuntimeError):
        engine.clear_history()
    runner.batch.status = "finished"


# ============================================================== migration of the old local history

OLD_SCHEMA = """
CREATE TABLE batches (batch_id TEXT PRIMARY KEY, created_at TEXT NOT NULL, started_at TEXT, finished_at TEXT,
    mode TEXT NOT NULL, status TEXT NOT NULL, total INTEGER NOT NULL);
CREATE TABLE documents (document_id TEXT PRIMARY KEY, batch_id TEXT NOT NULL, position INTEGER NOT NULL,
    filename TEXT NOT NULL, file_path TEXT, size_bytes INTEGER, status TEXT NOT NULL, supplier TEXT, item TEXT,
    decision TEXT, extraction_source TEXT, seconds REAL, processed_at TEXT, failed_stage TEXT, error TEXT,
    thread_id TEXT, raw_text TEXT, result_json TEXT, human_decision TEXT, human_comment TEXT, reviewed_at TEXT);
CREATE TABLE events (id INTEGER PRIMARY KEY AUTOINCREMENT, batch_id TEXT NOT NULL, document_id TEXT,
    ts TEXT NOT NULL, stage TEXT NOT NULL, status TEXT NOT NULL, message TEXT NOT NULL, duration_ms REAL,
    warning TEXT, metadata TEXT);
"""


def make_old_runtime(root):
    """The pre-Supabase layout: history.db + checkpoints.db (SqliteSaver) + uploads/, one paused review."""
    from langgraph.checkpoint.sqlite import SqliteSaver

    root.mkdir(parents=True)
    (root / "uploads").mkdir()
    db = sqlite3.connect(root / "history.db")
    db.executescript(OLD_SCHEMA)
    db.execute("INSERT INTO batches VALUES ('20261101-100000-abcd', '2026-11-01T10:00:00', '2026-11-01T10:00:01', "
               "'2026-11-01T10:00:05', 'regex', 'finished', 2)")
    saver = SqliteSaver(sqlite3.connect(root / "checkpoints.db", check_same_thread=False), serde=checkpoint_serde())
    graph = build_graph(True, saver)
    for pos, pack in ((1, "01"), (2, "04")):
        upload = root / "uploads" / f"pack{pack}.pdf"
        upload.write_bytes((DATA / f"supplier-pack-{pack}.pdf").read_bytes())
        thread = f"old-thread-{pack}"
        cfg = {"configurable": {"thread_id": thread}}
        out = graph.invoke(initial_state(str(upload), mode="regex", ask_human=True), cfg)
        result = out.get("result") or report_node(graph.get_state(cfg).values)["result"]
        status = "completed" if "result" in out else "waiting_review"
        doc_id = f"20261101-100000-abcd-{pos:02d}"
        db.execute("INSERT INTO documents VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                   (doc_id, "20261101-100000-abcd", pos, f"supplier-pack-{pack}.pdf", str(upload), upload.stat().st_size,
                    status, result.supplier, result.item, result.decision, result.extraction_source, 0.3,
                    "2026-11-01T10:00:03", None, None, thread, "text", result.model_dump_json(), None, None, None))
        db.execute("INSERT INTO events (batch_id, document_id, ts, stage, status, message, duration_ms, warning, "
                   "metadata) VALUES (?, ?, '2026-11-01T10:00:02', 'parse', 'completed', 'Extracted text', 12, NULL, '{}')",
                   ("20261101-100000-abcd", doc_id))
    db.execute("INSERT INTO events (batch_id, document_id, ts, stage, status, message, duration_ms, warning, metadata) "
               "VALUES ('20261101-100000-abcd', NULL, '2026-11-01T10:00:00', 'batch', 'info', 'Batch started', "
               "NULL, NULL, '{}')")
    db.commit()
    db.close()
    (root / "uploads" / "pack01.pdf")  # left in place on purpose


def test_migration_imports_old_history_idempotently(engine, tmp_path, fake_files):
    import tools.migrate_local_history as mig

    _files, storage = fake_files
    old = tmp_path / "old-runtime"
    make_old_runtime(old)

    dry = mig.migrate(engine, old, dry_run=True)
    assert dry["documents_migrated"] == 2 and engine.store.documents() == [] and storage.objects == {}

    report = mig.migrate(engine, old)
    assert not report["errors"], report["errors"]
    assert (report["batches"], report["documents_migrated"], report["files_uploaded"]) == (1, 2, 2)
    assert report["reviews_resumable"] == 1 and report["events"] == 2
    docs = {d["legacy_id"]: d for d in engine.store.documents()}
    old_id = "20261101-100000-abcd-02"
    paused = docs[old_id]
    assert paused["document_id"] == mig.new_document_id(old_id) and paused["status"] == "waiting_review"
    assert paused["batch_id"] == "20261101-100000-abcd" and paused["storage_path"] in storage.objects
    assert len(json.loads(paused["result_json"])["fields"]) == 24
    assert [e["message"] for e in engine.store.events(document_id=paused["document_id"])] == ["Extracted text"]

    again = mig.migrate(engine, old)  # second run changes nothing
    assert again["documents_skipped"] == 2 and again["documents_migrated"] == 0 and again["batches"] == 0
    assert len(engine.store.documents()) == 2 and len(engine.store.events()) == 3
    assert (old / "history.db").exists() and (old / "uploads" / "pack01.pdf").exists()  # source untouched

    result = engine.submit_review(paused["document_id"], "REJECT", "migrated review")
    assert result.human_decision["decision"] == "REJECT"


def test_migration_reports_missing_source(engine, tmp_path):
    import tools.migrate_local_history as mig

    assert mig.migrate(engine, tmp_path / "nothing")["errors"]
