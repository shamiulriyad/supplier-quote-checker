"""Queue, sequential batch processing, progress events, human review and persistence (service layer)."""
import threading

import pytest

from checker.models import FIELD_KINDS
from service.batch import Engine, ReviewExpired, validate_pdf
from service.progress import STAGE_NAMES
from service.store import AlreadyReviewed

from .conftest import DATA


@pytest.fixture
def engine(tmp_path):
    eng = Engine(tmp_path / "runtime")
    yield eng
    eng.close()


def queue_of(engine, *packs, extra=()):
    q = engine.new_queue()
    for p in packs:
        q.add_file(DATA / f"supplier-pack-{p}.pdf")
    for name, data in extra:
        q.add_upload(name, data)
    return q


def run(engine, q, mode="regex"):
    return engine.start_batch(list(q.items), mode, run_in_thread=False).snapshot()


# 1. queue management ------------------------------------------------------------------
def test_queue_add_dedupe_remove_clear(engine):
    q = queue_of(engine, "01", "02")
    assert len(q) == 2
    ok, msg = q.add_file(DATA / "supplier-pack-01.pdf")
    assert not ok and "already" in msg
    ok, _ = q.add_upload("copy-of-02.pdf", (DATA / "supplier-pack-02.pdf").read_bytes())
    assert not ok  # same content
    q.add_upload("notes.pdf", b"plain text")
    bad = q.items[-1]
    assert not bad.valid and bad.validation == "Not a PDF file"
    assert q.remove([bad.item_id]) == 1 and len(q) == 2
    q.clear()
    assert len(q) == 0


def test_validate_pdf(tmp_path):
    assert validate_pdf(DATA / "supplier-pack-01.pdf") == (True, "Valid PDF, 1 page")
    empty = tmp_path / "e.pdf"
    empty.write_bytes(b"")
    assert validate_pdf(empty) == (False, "Empty file")
    broken = tmp_path / "b.pdf"
    broken.write_bytes(b"%PDF-1.4 garbage")
    assert validate_pdf(broken) == (False, "Corrupted PDF (cannot be opened)")


def test_empty_queue_cannot_start(engine):
    with pytest.raises(ValueError):
        engine.start_batch([], "regex")


# 2-4. sequential processing, events, status updates -------------------------------------
def test_documents_are_processed_one_after_another(engine):
    snap = run(engine, queue_of(engine, "01", "03", "07"))
    order = [e.document_id for e in snap.log if e.document_id]
    # each document's events form one contiguous block, in queue order
    blocks = [d for i, d in enumerate(order) if i == 0 or order[i - 1] != d]
    assert blocks == [j.document_id for j in snap.jobs]


def test_progress_events_follow_the_real_graph(engine):
    snap = run(engine, queue_of(engine, "01"))
    job = snap.jobs[0]
    events = [e for e in snap.log if e.document_id == job.document_id and e.stage in STAGE_NAMES]
    started = [e.stage for e in events if e.status == "running"]
    assert started == ["parse", "extract", "verify", "match_pr", "check_math", "check_rules", "decide", "report"]
    finished = {e.stage: e for e in events if e.status != "running"}
    assert finished["parse"].metadata["characters"] > 500
    assert finished["verify"].metadata["found"] == len(FIELD_KINDS)
    assert finished["decide"].metadata["decision"] == "APPROVE"
    assert finished["human_review"].status == "skipped"  # not needed for an Approve
    assert all(finished[s].duration_ms is not None for s in STAGE_NAMES if s != "human_review")
    assert {s: st.status for s, st in job.stages.items()} == {
        **{s: "completed" for s in STAGE_NAMES}, "human_review": "skipped"}


def test_status_updates_and_counts(engine):
    snap = run(engine, queue_of(engine, "01", "04", "06"))
    assert [j.status for j in snap.jobs] == ["completed", "waiting_review", "completed"]
    assert [j.decision for j in snap.jobs] == ["APPROVE", "NEEDS_REVIEW", "REJECT"]
    assert snap.status == "finished"
    c = snap.counts()
    assert (c["total"], c["completed"], c["waiting"], c["processing"], c["needs_review"]) == (3, 3, 0, 0, 1)
    paused = snap.jobs[1]
    assert paused.stages["human_review"].status == "waiting" and paused.stages["report"].status == "pending"


def test_runs_in_background_thread_and_snapshot_is_live(engine):
    q = queue_of(engine, "01", "02", "03")
    runner = engine.start_batch(list(q.items), "regex")
    seen = runner.snapshot()  # readable while it runs
    assert seen.batch_id == runner.batch.batch_id
    runner.join(timeout=60)
    assert not runner.is_running()
    assert runner.snapshot().counts()["completed"] == 3


def test_second_batch_refused_while_running(engine):
    gate = threading.Event()
    q = queue_of(engine, "01")
    runner = engine.start_batch(list(q.items), "regex", run_in_thread=False)
    runner.batch.status = "running"  # pretend still running
    runner._thread = threading.Thread(target=gate.wait)
    runner._thread.start()
    with pytest.raises(RuntimeError):
        engine.start_batch(list(q.items), "regex")
    gate.set()


# 5-6. failures and preserved results ----------------------------------------------------
def test_failed_document_does_not_lose_other_results(engine):
    q = queue_of(engine, "01", extra=[("broken.pdf", b"not a pdf")])
    q.add_file(DATA / "supplier-pack-06.pdf")
    snap = run(engine, q)
    good1, bad, good2 = snap.jobs
    assert bad.failed_stage == "parse" and bad.stages["parse"].status == "failed"
    assert bad.stages["extract"].status == "skipped"
    assert bad.status == "waiting_review"  # a person decides, never silently dropped
    assert (good1.decision, good2.decision) == ("APPROVE", "REJECT")
    stored = {d["filename"]: d for d in engine.store.documents(snap.batch_id)}
    assert stored["supplier-pack-01.pdf"]["decision"] == "APPROVE"
    assert stored["supplier-pack-06.pdf"]["decision"] == "REJECT"
    assert "Could not open PDF" in stored["broken.pdf"]["error"]
    assert snap.counts()["failed"] == 1


def test_graph_crash_is_recorded_and_batch_continues(engine, monkeypatch):
    import service.batch as batch_mod
    real = batch_mod.stream_document
    calls = {"n": 0}

    def flaky(*args, **kwargs):
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("simulated graph failure")
        return real(*args, **kwargs)

    monkeypatch.setattr(batch_mod, "stream_document", flaky)
    snap = run(engine, queue_of(engine, "01", "02"))
    assert snap.jobs[0].status == "failed" and "simulated" in snap.jobs[0].error
    assert snap.jobs[1].status == "completed" and snap.jobs[1].decision == "APPROVE"
    assert engine.store.document(snap.jobs[0].document_id)["status"] == "failed"


def test_cancel_stops_between_documents(engine):
    q = queue_of(engine, "01", "02", "03")
    runner = engine.start_batch(list(q.items), "regex", run_in_thread=False)  # already finished
    assert runner.snapshot().status == "finished"
    # cancel before a run: nothing after the request is processed
    q = queue_of(engine, "04", "05", "06")
    from service.batch import BatchRunner, BatchState, DocumentJob
    jobs = [DocumentJob(f"x-{i}", i, it.filename, it.path, it.size_bytes, True, "", f"t-{i}")
            for i, it in enumerate(q.items, 1)]
    engine.store.create_batch("x", "regex", [{"document_id": j.document_id, "position": j.position,
                                              "filename": j.filename, "file_path": j.path,
                                              "size_bytes": j.size_bytes} for j in jobs])
    r = BatchRunner(engine, BatchState("x", "regex", jobs))
    real_process = r._process

    def process_then_cancel(job):
        real_process(job)
        r.cancel()

    r._process = process_then_cancel
    r.run()
    snap = r.snapshot()
    assert [j.status for j in snap.jobs] == ["waiting_review", "cancelled", "cancelled"]
    assert snap.status == "cancelled"


# 7-9. human review ----------------------------------------------------------------------
def test_review_interrupt_and_resume(engine):
    snap = run(engine, queue_of(engine, "04"))
    doc_id = snap.jobs[0].document_id
    assert [d["document_id"] for d in engine.store.pending_reviews()] == [doc_id]
    assert engine.store.pending_reviews(engine.store.documents()) == engine.store.pending_reviews()
    assert engine.store.count_pending_reviews() == 1
    assert engine.can_resume(doc_id)

    result = engine.submit_review(doc_id, "APPROVE", "2% over is acceptable this month")
    assert result.decision == "NEEDS_REVIEW"  # the system's own decision is kept
    assert result.human_decision == {"decision": "APPROVE", "comment": "2% over is acceptable this month"}
    doc = engine.store.document(doc_id)
    assert (doc["status"], doc["human_decision"]) == ("reviewed", "APPROVE")
    assert engine.store.pending_reviews() == []
    assert engine.store.count_pending_reviews() == 0
    assert not engine.can_resume(doc_id)
    stages = [e["stage"] for e in engine.store.events(document_id=doc_id) if e["status"] == "completed"]
    assert stages[-2:] == ["human_review", "report"]
    assert engine.runner.snapshot().jobs[0].status == "reviewed"


def test_duplicate_review_is_blocked(engine):
    snap = run(engine, queue_of(engine, "07"))
    doc_id = snap.jobs[0].document_id
    engine.submit_review(doc_id, "REJECT", "quantity too low")
    with pytest.raises(AlreadyReviewed):
        engine.submit_review(doc_id, "APPROVE", "second click")
    assert engine.store.document(doc_id)["human_decision"] == "REJECT"


def test_concurrent_claim_only_one_wins(engine):
    snap = run(engine, queue_of(engine, "09"))
    doc_id = snap.jobs[0].document_id
    engine.store.claim_review(doc_id)
    with pytest.raises(AlreadyReviewed):
        engine.store.claim_review(doc_id)


def test_invalid_review_input(engine):
    snap = run(engine, queue_of(engine, "04"))
    with pytest.raises(ValueError):
        engine.submit_review(snap.jobs[0].document_id, "MAYBE", "")
    assert engine.store.document(snap.jobs[0].document_id)["status"] == "waiting_review"


def test_failed_resume_releases_the_claim(engine, monkeypatch):
    snap = run(engine, queue_of(engine, "04"))
    doc_id = snap.jobs[0].document_id
    monkeypatch.setattr(engine.graph, "get_state", lambda cfg: type("S", (), {"next": ()})())
    with pytest.raises(ReviewExpired):
        engine.submit_review(doc_id, "APPROVE", "x")
    assert engine.store.document(doc_id)["status"] == "waiting_review"


# 10. recovery after restart -------------------------------------------------------------
def test_pending_review_survives_restart(tmp_path):
    first = Engine(tmp_path / "rt")
    snap = run(first, queue_of(first, "04", "01"))
    doc_id = snap.jobs[0].document_id
    first.close()

    second = Engine(tmp_path / "rt")  # new process: memory is gone, SQLite is not
    try:
        assert [d["document_id"] for d in second.store.pending_reviews()] == [doc_id]
        assert second.store.document(snap.jobs[1].document_id)["decision"] == "APPROVE"
        assert second.can_resume(doc_id)
        result = second.submit_review(doc_id, "REJECT", "after restart")
        assert result.human_decision["decision"] == "REJECT"
    finally:
        second.close()


def test_unfinished_work_is_marked_interrupted_on_restart(tmp_path):
    first = Engine(tmp_path / "rt")
    first.store.create_batch("b1", "regex", [{"document_id": "b1-01", "position": 1, "filename": "a.pdf",
                                              "file_path": "a.pdf", "size_bytes": 1}])
    first.store.update_document("b1-01", status="running")
    first.close()
    second = Engine(tmp_path / "rt")
    try:
        assert second.stale_on_start == 1
        assert second.store.document("b1-01")["status"] == "interrupted"
        assert second.store.batches()[0]["status"] == "interrupted"
    finally:
        second.close()
