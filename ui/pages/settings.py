"""Settings: processing mode, AI connection, rule thresholds (read-only), storage."""
from datetime import datetime

import pandas as pd
import streamlit as st

from checker.config import load_config
from checker.extract_llm import check_connection
from service import __version__

from ..state import MODES, api_key_configured, api_status, effective_source, external_calls_enabled, get_engine, \
    get_mode
from ..theme import badge, kv, page_header, section

RULE_HELP = {
    "reject_price_over_pct": "R1 reject / N1 review: % above the maximum unit price",
    "max_advance_pct": "R2 reject: maximum advance payment %",
    "max_delivery_days": "R5 reject: latest delivery (days after PO)",
    "review_delivery_from_days": "N4 review: delivery from this many days",
    "qty_tolerance_pct": "N2 review: allowed quantity difference %",
    "payment_days_min": "N3 review: earliest payment (days after delivery)",
    "payment_days_max": "N3 review: latest payment (days after delivery)",
    "vat_rate_pct": "N5 review: required VAT rate %",
    "min_validity_days": "N7 review: minimum quotation validity (days)",
    "min_penalty_pct_per_week": "P1 review: minimum late-delivery penalty % per week",
    "math_tolerance": "N6 review: rounding tolerance (BDT)",
}


def render() -> None:
    page_header("Settings", "Processing mode, AI connection and the company rules in use.")
    cfg = load_config()
    engine = get_engine()
    busy = bool(engine.runner and engine.runner.is_running())

    section("Processing mode")
    current = get_mode()
    mode = st.radio("How documents are read", list(MODES), index=list(MODES).index(current),
                    format_func=lambda m: MODES[m][0], captions=[MODES[m][1] for m in MODES], disabled=busy)
    if mode != current:
        st.session_state.mode = mode
        st.rerun()
    if busy:
        st.caption("The mode cannot be changed while a batch is running; the running batch keeps its mode.")
    if mode == "live" and not api_key_configured():
        st.warning("Live mode needs an OpenAI API key. Without one, every document is analysed with the regex "
                   "extractor only and sent to a person.")

    section("AI provider")
    label, tone = api_status()
    st.html(kv([
        ("Provider", "OpenAI"),
        ("Model", cfg["llm"]["model"]),
        ("API key", "Configured (hidden)" if api_key_configured() else "Not configured"),
        ("Connection", ("html", badge(label, tone))),
        ("External API calls", ("html", badge("Enabled", "accent") if external_calls_enabled(mode)
                                else badge("Disabled", "neutral"))),
        ("Extraction right now", effective_source(mode)),
    ]))
    check = st.session_state.get("api_check")
    if check:
        st.caption(f"Last test {check[2]}: {check[1]}")
    if st.button("Test connection", disabled=not api_key_configured(),
                 help="Makes one small API call (model lookup). No document data is sent."):
        with st.spinner("Contacting OpenAI…"):
            ok, msg = check_connection(cfg["llm"])
        st.session_state.api_check = (ok, msg, datetime.now().strftime("%H:%M:%S"))
        st.rerun()
    st.caption("Set the key in a .env file (OPENAI_API_KEY=...) locally, or in Secrets on Streamlit Cloud. "
               "Change the model with OPENAI_MODEL. The key is never shown in the app.")

    section("Company rules in use (config.yaml)")
    st.dataframe(pd.DataFrame([{"Setting": k, "Value": str(v), "Used by": RULE_HELP.get(k, "")}
                               for k, v in cfg["rules"].items()]), hide_index=True)
    st.caption("Rules are code in checker/rules.py; the thresholds above come from config.yaml (read-only here).")

    section("Storage")
    store = engine.store
    st.html(kv([("History database", str(engine.runtime_dir / "history.db")),
                ("Review checkpoints", str(engine.runtime_dir / "checkpoints.db")),
                ("Batches stored", str(len(store.batches()))),
                ("Waiting for review", str(len(store.pending_reviews()))),
                ("Version", __version__)]))
    with st.expander("Clear processing history"):
        st.write("Deletes all batches, documents and events from the history database. "
                 "Documents waiting for review will no longer be listed.")
        sure = st.checkbox("I understand this cannot be undone", disabled=busy)
        if st.button("Clear history", disabled=not sure or busy):
            store.clear()
            st.session_state.pop("dismissed_batch", None)
            st.success("History cleared.")
