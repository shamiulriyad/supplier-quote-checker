"""Session and process-wide state for the UI."""
import os

import streamlit as st

from checker.config import load_config
from checker.graph import resolve_mode
from service.batch import DocumentQueue, Engine

MODES = {
    "auto": ("Auto", "Live AI analysis (OpenAI) when an API key is configured, otherwise regex-only analysis. "
                     "Never uses recorded responses."),
    "live": ("Live", "OpenAI on every document, cross-validated by the regex extractor. Without a key, documents "
                     "fall back to regex-only analysis and go to a person."),
    "regex": ("Offline", "Regex extractor only. No AI and no network calls."),
    "replay": ("Replay (testing)", "TESTING ONLY: recorded OpenAI responses for the 10 sample packs. Not live AI; "
                                   "results are labelled 'Recorded AI Response — Not Live'."),
}
PUBLIC_MODES = ["auto", "live", "regex"]  # replay is a developer / testing option, see Settings


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
    """What `auto` will actually do right now (decided by checker.graph.resolve_mode)."""
    if mode != "auto":
        return mode
    return "live AI" if resolve_mode(mode) == "live" else "regex-only (no OpenAI API key)"


def external_calls_enabled(mode: str) -> bool:
    return resolve_mode(mode) == "live"


def api_status() -> tuple[str, str]:
    """(label, tone). Never contains the key."""
    check = st.session_state.get("api_check")
    if not api_key_configured():
        return "Not configured", "neutral"
    if check is None:
        return "Key configured (not tested)", "accent"
    ok, _msg, _when = check
    return ("Connected", "success") if ok else ("Connection failed", "error")
