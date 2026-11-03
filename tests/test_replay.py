"""Replay is a testing option only: Auto never uses recorded responses, and results say how they were made."""
import pytest

from checker import extract_llm
from checker.extract_regex import extract_regex
from checker.graph import REGEX_ONLY, REPLAY, LIVE, analysis_kind, resolve_mode, run_pack
from checker.parse import read_pdf_text

from .conftest import DATA, expected_rows, llm_answer_from, record

ROWS = expected_rows()


def no_replay_spies(monkeypatch):
    def boom(*a, **k):
        raise AssertionError("a recorded (replay) response was read")
    monkeypatch.setattr(extract_llm, "load_cached", boom)
    monkeypatch.setattr(extract_llm, "has_cached", boom)
    monkeypatch.setattr(extract_llm, "_lookup", boom)


@pytest.fixture
def no_replay(monkeypatch):
    """Fail loudly if anything reads a recorded response."""
    no_replay_spies(monkeypatch)


@pytest.fixture
def api(monkeypatch):
    """A stand-in for the OpenAI API that records every call."""
    calls = []

    def fake_call(text, cfg):
        calls.append(text)
        return llm_answer_from(extract_regex(text))
    monkeypatch.setattr(extract_llm, "call_openai", fake_call)
    monkeypatch.setattr(extract_llm, "save_cached", lambda *a, **k: None)
    return calls


def test_auto_resolves_to_live_or_regex_only(monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "test-key")
    assert resolve_mode("auto") == "live"
    monkeypatch.delenv("OPENAI_API_KEY")
    assert resolve_mode("auto") == "regex"
    assert resolve_mode("replay") == "replay"  # only when asked for explicitly


def test_key_available_calls_the_api(monkeypatch, api, no_replay):
    monkeypatch.setenv("OPENAI_API_KEY", "test-key")
    result = run_pack(str(DATA / "supplier-pack-01.pdf"), mode="auto")
    assert len(api) == 1
    assert result.extraction_source.startswith(LIVE) and analysis_kind(result.extraction_source) == "live"
    assert result.decision == "APPROVE" and not result.errors


def test_no_key_means_regex_only_even_when_a_recording_exists(monkeypatch, api):
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    sample = read_pdf_text(DATA / "supplier-pack-01.pdf")
    record(sample)  # a recording exists for exactly this document...
    no_replay_spies(monkeypatch)  # ...and must not be read
    result = run_pack(str(DATA / "supplier-pack-01.pdf"), mode="auto")
    assert api == []
    assert result.extraction_source == f"{REGEX_ONLY} (no OpenAI API key configured)"
    assert result.decision == "APPROVE" and not result.errors


@pytest.mark.parametrize("key", [True, False], ids=["with-key", "without-key"])
def test_auto_never_uses_replay_on_any_sample(monkeypatch, api, no_replay, key):
    if key:
        monkeypatch.setenv("OPENAI_API_KEY", "test-key")
    else:
        monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    for row in ROWS:
        result = run_pack(str(DATA / row["file"]), mode="auto")
        assert analysis_kind(result.extraction_source) == ("live" if key else "regex")
        assert result.decision == row["expected_decision"]
    assert len(api) == (len(ROWS) if key else 0)


def test_explicit_replay_uses_the_recorded_response_and_says_so(monkeypatch):
    for row in ROWS:  # what earlier live runs would have recorded
        record(read_pdf_text(DATA / row["file"]), row["file"])
    monkeypatch.setenv("OPENAI_API_KEY", "test-key")  # even with a key, replay must not call the API

    def no_api(*a, **k):
        raise AssertionError("replay must not call the API")
    monkeypatch.setattr(extract_llm, "call_openai", no_api)
    for row in ROWS:
        result = run_pack(str(DATA / row["file"]), mode="replay")
        assert result.extraction_source.startswith(REPLAY), result.extraction_source
        assert analysis_kind(result.extraction_source) == "replay"
        assert result.decision == row["expected_decision"]


def test_new_pdf_gets_no_unrelated_recorded_response(monkeypatch, api):
    """A changed document must not pick up another document's recording, in any mode."""
    original = read_pdf_text(DATA / "supplier-pack-01.pdf")
    record(original)  # the original document has a recording
    text = original.replace("QT-2026-901", "QT-2026-999")  # a different document
    assert not extract_llm.has_cached("unused-dir", text)

    replay = run_pack(raw_text=text, mode="replay")
    assert replay.extraction_source == f"{REGEX_ONLY} (no recorded response for this document)"
    assert replay.decision == "NEEDS_REVIEW" and any("No saved LLM answer" in e for e in replay.errors)

    monkeypatch.setenv("OPENAI_API_KEY", "test-key")
    live = run_pack(raw_text=text, mode="auto")
    assert api == [text]  # the API saw this new text; nothing recorded was reused
    assert analysis_kind(live.extraction_source) == "live" and live.decision == "APPROVE"


def test_failed_live_call_is_labelled_regex_only(monkeypatch, no_replay):
    monkeypatch.setenv("OPENAI_API_KEY", "test-key")

    def fail(*a, **k):
        raise extract_llm.LLMUnavailable("timeout")
    monkeypatch.setattr(extract_llm, "call_openai", fail)
    result = run_pack(str(DATA / "supplier-pack-02.pdf"), mode="auto")
    assert result.extraction_source == f"{REGEX_ONLY} (the OpenAI call failed)"
    assert result.decision == "NEEDS_REVIEW"


def test_older_history_labels_are_still_understood():
    assert analysis_kind("OpenAI + regex cross-check") == "live"
    assert analysis_kind("saved OpenAI answer + regex cross-check") == "replay"
    assert analysis_kind("regex only (offline mode)") == "regex"
    assert analysis_kind(None) == "unknown"


def test_replay_is_not_a_normal_mode_in_settings(tmp_path, monkeypatch):
    from streamlit.testing.v1 import AppTest

    from checker.config import load_config

    monkeypatch.setenv("SQC_RUNTIME_DIR", str(tmp_path / "rt"))
    load_config.cache_clear()
    from ui.state import get_engine
    get_engine.clear()

    def script():
        from ui.pages import settings
        settings.render()
    at = AppTest.from_function(script, default_timeout=60).run()
    try:
        assert not at.exception
        main = at.radio[0]
        assert list(main.options) == ["Auto", "Live", "Offline"]  # replay is not offered here
        toggle = at.toggle[0]
        assert "recorded AI responses" in toggle.label and toggle.value is False
        toggle.set_value(True).run()
        assert at.session_state["mode"] == "replay"
        assert any("not live AI" in w.value for w in at.warning)
    finally:
        get_engine().close()
        get_engine.clear()
        load_config.cache_clear()
