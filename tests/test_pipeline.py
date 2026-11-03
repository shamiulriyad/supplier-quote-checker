"""End-to-end tests of the LangGraph pipeline on the 10 sample packs and on bad input."""
from decimal import Decimal as D

import pytest
from langgraph.types import Command

from checker import extract_llm
from checker.extract_regex import extract_regex
from checker.graph import build_graph, initial_state, run_pack

from .conftest import DATA, expected_rows, llm_answer_from

ROWS = expected_rows()


def check(result, row):
    want = set(filter(None, row["expected_rules"].split(";")))
    got = {r.rule_id for r in result.reasons}
    assert result.decision == row["expected_decision"], result.reasons
    assert want <= got
    if not want:
        assert not got
    # every reason that failed on document content carries an exact quote (except "clause is absent")
    for r in result.reasons:
        if r.outcome == "FAIL" and r.rule_id not in ("R3",):
            assert r.evidence, r


@pytest.mark.parametrize("row", ROWS, ids=[r["pack"] for r in ROWS])
def test_offline_mode_scores_10_of_10(row):
    check(run_pack(str(DATA / row["file"]), pack_id=row["pack"], mode="regex"), row)


@pytest.mark.parametrize("row", ROWS, ids=[r["pack"] for r in ROWS])
def test_llm_mode_with_agreeing_llm(row, fake_llm):
    fake_llm(lambda text: llm_answer_from(extract_regex(text)))
    result = run_pack(str(DATA / row["file"]), pack_id=row["pack"], mode="live")
    assert result.extraction_source.startswith("OpenAI")
    assert not result.errors
    check(result, row)


def test_llm_failure_goes_to_a_person(fake_llm):
    def boom(text):
        raise extract_llm.LLMUnavailable("timeout")
    fake_llm(boom)
    result = run_pack(str(DATA / "supplier-pack-03.pdf"), mode="live")  # a clear Reject on its own
    assert result.decision == "NEEDS_REVIEW"
    assert any("timeout" in e for e in result.errors)


def test_hallucinating_llm_cannot_approve_pack_06(fake_llm):
    def lie(text):
        answer = llm_answer_from(extract_regex(text))
        answer["penalty_pct"] = {"value": "0.5", "quote": "Late delivery penalty of 0.5% of order value per week.",
                                 "status": "FOUND", "note": None}
        answer["penalty_basis"] = {"value": "order value", "quote": "0.5% of order value per week",
                                   "status": "FOUND", "note": None}
        return answer
    fake_llm(lie)
    result = run_pack(str(DATA / "supplier-pack-06.pdf"), mode="live")
    assert result.decision == "NEEDS_REVIEW"
    assert result.fields["penalty_pct"].status == "UNCLEAR"
    assert {r.rule_id: r.outcome for r in result.rule_results}["R3"] == "UNKNOWN"


def test_garbage_file_does_not_crash(tmp_path):
    bad = tmp_path / "broken.pdf"
    bad.write_bytes(b"this is not a pdf")
    result = run_pack(str(bad), mode="regex")
    assert result.decision == "NEEDS_REVIEW" and result.errors


def test_text_that_is_not_a_quote_is_not_rejected():
    result = run_pack(raw_text="Dear sir, please find our company profile attached.", mode="regex")
    assert result.decision == "NEEDS_REVIEW"
    assert any("does not look like" in e for e in result.errors)


def test_empty_text():
    assert run_pack(raw_text="   ", mode="regex").decision == "NEEDS_REVIEW"


def test_same_input_same_output():
    runs = [run_pack(str(DATA / "supplier-pack-09.pdf"), mode="regex").model_dump(exclude={"seconds"})
            for _ in range(3)]
    assert runs[0] == runs[1] == runs[2]


def test_human_review_pauses_and_resumes():
    graph = build_graph(with_human_review=True)
    cfg = {"configurable": {"thread_id": "pack-04"}}
    state = initial_state(str(DATA / "supplier-pack-04.pdf"), pack_id="04", mode="regex", ask_human=True)
    paused = graph.invoke(state, cfg)
    assert "__interrupt__" in paused and "result" not in paused
    assert paused["__interrupt__"][0].value["system_decision"] == "NEEDS_REVIEW"

    done = graph.invoke(Command(resume={"decision": "APPROVE", "comment": "2% over is fine this time"}), cfg)
    assert done["result"].decision == "NEEDS_REVIEW"  # the system's own answer is kept
    assert done["result"].human_decision["decision"] == "APPROVE"


def test_clear_decisions_do_not_pause():
    graph = build_graph(with_human_review=True)
    cfg = {"configurable": {"thread_id": "pack-01"}}
    out = graph.invoke(initial_state(str(DATA / "supplier-pack-01.pdf"), mode="regex", ask_human=True), cfg)
    assert out["result"].decision == "APPROVE" and "__interrupt__" not in out
