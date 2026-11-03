"""Every document is decided from its own content. Stored results are outputs only, never inputs.

The history store is the same code on Supabase PostgreSQL and on SQLite, so these run on SQLite.
"""
from pathlib import Path

import pytest

from service.batch import Engine

from .conftest import DATA

LAYOUTS = Path(__file__).parent / "fixtures" / "layouts"


@pytest.fixture
def engine(tmp_path):
    eng = Engine(tmp_path / "rt")
    yield eng
    eng.close()


def run(eng, *paths):
    q = eng.new_queue()
    for p in paths:
        q.add_file(p)
    return eng.start_batch(list(q.items), "regex", run_in_thread=False).snapshot()


def test_stored_result_is_never_reused_as_a_decision(engine):
    first = run(engine, DATA / "supplier-pack-03.pdf").jobs[0]
    assert first.decision == "REJECT"
    # tamper with the stored output: if anything read it back, the next run would say APPROVE
    engine.store.db.execute("UPDATE documents SET decision = 'APPROVE', result_json = NULL WHERE id = ?",
                            (first.document_id,))
    second = run(engine, DATA / "supplier-pack-03.pdf").jobs[0]
    assert second.decision == "REJECT"
    assert engine.store.document(second.document_id)["decision"] == "REJECT"


def test_processing_reads_nothing_from_the_history_store(engine, monkeypatch):
    def forbidden(*a, **k):
        raise AssertionError("the pipeline read stored history")
    for name in ("documents", "document", "batches", "events", "steps", "pending_reviews", "cache_get"):
        monkeypatch.setattr(engine.store, name, forbidden)
    snap = run(engine, DATA / "supplier-pack-01.pdf", DATA / "supplier-pack-06.pdf")
    assert [j.decision for j in snap.jobs] == ["APPROVE", "REJECT"]


def test_uploads_in_one_batch_are_decided_independently(engine):
    snap = run(engine, LAYOUTS / "key-value-labels.pdf", LAYOUTS / "pipe-table-reordered.pdf",
               LAYOUTS / "missing-number-and-date.pdf")
    assert [j.decision for j in snap.jobs] == ["APPROVE", "REJECT", "NEEDS_REVIEW"]
    suppliers = [engine.store.document(j.document_id)["supplier"] for j in snap.jobs]
    assert suppliers == ["Nilphamari Textile Co.", "Rangpur Trims House", "Karnaphuli Cartons"]


def test_stored_result_keeps_full_evidence(engine):
    import json
    job = run(engine, LAYOUTS / "two-pages.pdf").jobs[0]
    stored = json.loads(engine.store.document(job.document_id)["result_json"])
    price = stored["fields"]["unit_price"]
    assert (price["page"], price["verification"]) == (1, "regex only") and price["quote"]
    assert stored["fields"]["validity_days"]["page"] == 2
    rules = {r["rule_id"]: r for r in stored["rule_results"]}
    assert rules["R1"]["expected"].startswith("Unit price") and rules["R1"]["actual"].startswith("93.00")
    assert stored["financial"]["max_unit_price"] == "95.00" and stored["financial"]["price_deviation_pct"] == "-2.11"
