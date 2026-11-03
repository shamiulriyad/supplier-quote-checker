"""Streamlit AppTest checks: pages render real backend data, and the queue -> monitor -> review flow works."""
import time

import pytest
from streamlit.testing.v1 import AppTest

from checker.config import load_config

from .conftest import DATA


@pytest.fixture
def ui(tmp_path, monkeypatch):
    """Fresh runtime folder and engine; no API key so nothing can call OpenAI."""
    monkeypatch.setenv("SQC_RUNTIME_DIR", str(tmp_path / "rt"))
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    load_config.cache_clear()
    from ui.state import get_engine
    get_engine.clear()
    yield get_engine
    get_engine().close()
    get_engine.clear()
    load_config.cache_clear()


def page(name: str) -> AppTest:
    def script(page_name):
        from ui.theme import inject_css
        import importlib
        inject_css()
        importlib.import_module(f"ui.pages.{page_name}").render()
    at = AppTest.from_function(script, args=(name,), default_timeout=60)
    at.session_state["mode"] = "regex"
    return at


def process(get_engine, *packs):
    engine = get_engine()
    q = engine.new_queue()
    for p in packs:
        q.add_file(DATA / f"supplier-pack-{p}.pdf")
    return engine.start_batch(list(q.items), "regex", run_in_thread=False).snapshot()


def html_text(at) -> str:
    return " ".join(e.proto.body for e in at.get("html"))


def metric(at, label):
    return next(m.value for m in at.metric if m.label == label)


def test_queue_then_batch_then_monitor(ui):
    at = page("processing").run()
    assert not at.exception
    assert "The queue is empty" in html_text(at)

    next(b for b in at.button if b.label.startswith("Add all")).click().run()
    table = at.dataframe[0].value
    assert len(table) == 10 and list(table["Status"].unique()) == ["Queued"]
    assert ui().runner is None  # nothing starts before the user presses Start

    next(b for b in at.button if b.label == "Start processing").click().run()
    runner = ui().runner
    runner.join(timeout=120)
    at = page("processing").run()  # a new browser session sees the same (process-wide) batch
    assert not at.exception
    assert metric(at, "Total documents") == "10"
    assert metric(at, "Completed") == "10"
    assert metric(at, "Waiting in queue") == "0"
    results = at.dataframe[0].value
    assert len(results) == 10
    assert list(results["Decision"]) == ["APPROVE", "APPROVE", "REJECT", "NEEDS_REVIEW", "REJECT", "REJECT",
                                         "NEEDS_REVIEW", "REJECT", "NEEDS_REVIEW", "REJECT"]


def test_monitor_shows_partial_results_before_batch_ends(ui):
    engine = ui()
    q = engine.new_queue()
    for p in ("01", "03", "05"):
        q.add_file(DATA / f"supplier-pack-{p}.pdf")
    # set up a batch, process only the first document, leave the rest queued (= mid-batch)
    from service.batch import BatchRunner, BatchState, DocumentJob
    jobs = [DocumentJob(f"b-{i}", i, it.filename, it.path, it.size_bytes, True, it.validation, f"t-{i}")
            for i, it in enumerate(q.items, 1)]
    engine.store.create_batch("b", "regex", [{"document_id": j.document_id, "position": j.position,
                                              "filename": j.filename, "file_path": j.path,
                                              "size_bytes": j.size_bytes} for j in jobs])
    runner = BatchRunner(engine, BatchState("b", "regex", jobs))
    runner.batch.status = "running"
    runner._process(jobs[0])
    engine.runner = runner

    at = page("processing").run()
    assert not at.exception
    assert metric(at, "Completed") == "1" and metric(at, "Waiting in queue") == "2"
    results = at.dataframe[0].value
    assert list(results["Document"]) == ["supplier-pack-01.pdf"]  # first result visible while others wait
    runner.batch.status = "finished"


def test_review_page_resumes_graph(ui):
    snap = process(ui, "04", "01")
    at = page("review").run()
    assert not at.exception
    assert metric(at, "Waiting for review") == "1"
    at.radio[0].set_value("APPROVE")
    at.text_area[0].input("Within tolerance for this supplier")
    next(b for b in at.button if b.label == "Submit Review Decision").click().run()
    assert not at.exception
    doc = ui().store.document(snap.jobs[0].document_id)
    assert doc["status"] == "reviewed" and doc["human_decision"] == "APPROVE"
    assert metric(at, "Waiting for review") == "0"
    assert metric(at, "Approved by a person") == "1"


def test_review_requires_decision_and_comment(ui):
    snap = process(ui, "07")
    at = page("review").run()
    next(b for b in at.button if b.label == "Submit Review Decision").click().run()
    assert any("explain the reason" in w.value.lower() for w in at.warning)
    assert ui().store.document(snap.jobs[0].document_id)["status"] == "waiting_review"


def test_document_details_show_all_fields_and_rules(ui):
    snap = process(ui, "09")
    at = page("documents")
    at.session_state["open_document"] = snap.jobs[0].document_id
    at.run()
    assert not at.exception
    frames = [d.value for d in at.dataframe]
    fields = next(f for f in frames if "Extracted value" in f.columns)
    assert len(fields) == 24
    rules = next(f for f in frames if "Explanation" in f.columns)
    assert set(rules[rules["Result"] == "FAIL"]["Rule"]) == {"N3", "N6"}
    assert [t.label for t in at.tabs] == ["Evidence", "Extracted fields", "Rule validation", "Document text",
                                          "PDF preview", "Processing log"]


def test_overview_metrics_come_from_processed_documents(ui):
    process(ui, "01", "02", "03", "04")
    at = page("overview").run()
    assert not at.exception
    assert metric(at, "Total documents") == "4"
    assert metric(at, "Approved") == "2"
    assert metric(at, "Rejected") == "1"
    assert metric(at, "Needs human review") == "1"
    assert metric(at, "Processing success rate") == "100%"


def test_other_pages_render_with_data(ui):
    process(ui, "05", "08")
    for name in ("validation", "history", "settings"):
        at = page(name).run()
        assert not at.exception, name
    matrix = page("validation").run().dataframe[0].value
    assert list(matrix["Decision"]) == ["REJECT", "REJECT"] and matrix.loc[1, "R4"] == "FAIL"


def test_pages_have_empty_states(ui):
    for name in ("overview", "documents", "validation", "review", "history"):
        at = page(name).run()
        assert not at.exception, name


def test_full_app_navigation_loads(ui):
    at = AppTest.from_file("app.py", default_timeout=60).run()
    assert not at.exception
    time.sleep(0)
