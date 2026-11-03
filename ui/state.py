"""Session and process-wide state for the UI."""
import os

import streamlit as st

from checker.config import load_config
from service.batch import DocumentQueue, Engine

MODES = {
    "auto": ("Auto", "OpenAI if an API key is configured, otherwise a saved OpenAI answer, otherwise regex only."),
    "live": ("Live", "OpenAI extraction on every document, cross-validated by the regex extractor. "
                     "Answers are saved for replay."),
    "replay": ("Replay", "Saved OpenAI answers, cross-validated by the regex extractor. No API calls."),
    "regex": ("Offline", "Regex extractor only. No AI and no network calls."),
}


@st.cache_resource(show_spinner=False)
def get_engine() -> Engine:
    """One engine per server process: graph with SQLite checkpoints, history store, active batch."""
    return Engine()


def get_queue() -> DocumentQueue:
    if "queue" not in st.session_state:
        st.session_state.queue = get_engine().new_queue()
    return st.session_state.queue


def get_mode() -> str:
    if "mode" not in st.session_state:
        st.session_state.mode = load_config()["mode"]
    return st.session_state.mode


def api_key_configured() -> bool:
    return bool(os.environ.get("OPENAI_API_KEY"))


def effective_source(mode: str) -> str:
    """What `auto` will actually do right now (mirrors checker.graph.resolve_mode)."""
    if mode != "auto":
        return mode
    return "live" if api_key_configured() else "replay or regex (per document)"


def external_calls_enabled(mode: str) -> bool:
    return mode == "live" or (mode == "auto" and api_key_configured())


def api_status() -> tuple[str, str]:
    """(label, tone). Never contains the key."""
    check = st.session_state.get("api_check")
    if not api_key_configured():
        return "Not configured", "neutral"
    if check is None:
        return "Key configured (not tested)", "accent"
    ok, _msg, _when = check
    return ("Connected", "success") if ok else ("Connection failed", "error")
