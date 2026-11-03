"""Sidebar footer: processing mode, AI provider/model, API status, version."""
from html import escape

import streamlit as st

from checker.config import load_config
from service import __version__

from .state import MODES, api_status, get_engine, get_mode
from .theme import badge


def render_sidebar_footer() -> None:
    engine = get_engine()
    mode = get_mode()
    label, tone = api_status()
    runner = engine.runner
    activity = ""
    if runner and runner.is_running():
        c = runner.snapshot().counts()
        activity = f'<div>{badge(f"Processing {c["completed"]}/{c["total"]}", "accent")}</div>'
    waiting = len(engine.store.pending_reviews())
    reviews = f'<div>{badge(f"{waiting} waiting for review", "warning")}</div>' if waiting else ""
    with st.sidebar:
        st.divider()
        st.html(f"""
        <div class="si-foot">
          {activity}{reviews}
          <div>Mode <b>{escape(MODES[mode][0])}</b></div>
          <div>AI provider <b>OpenAI</b> · <b>{escape(load_config()['llm']['model'])}</b></div>
          <div>API {badge(label, tone)}</div>
          <div>Version <b>{__version__}</b></div>
        </div>""")
