"""Settings: processing mode, AI connection, rule thresholds (read-only), storage."""
from datetime import datetime

import pandas as pd
import streamlit as st

from checker.config import load_config
from checker.extract_llm import check_connection
from service import __version__

from ..state import MODES, PUBLIC_MODES, api_key_configured, api_status, effective_source, external_calls_enabled, get_engine, \
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
    testing = current == "replay"
    mode = st.radio("How documents are read", PUBLIC_MODES,
                    index=PUBLIC_MODES.index(current) if current in PUBLIC_MODES else 0,
                    format_func=lambda m: MODES[m][0], captions=[MODES[m][1] for m in PUBLIC_MODES],
                    disabled=busy or testing)
    if not testing and mode != current:
        st.session_state.mode = mode
        st.rerun()
    with st.expander("Developer / testing options", expanded=testing):
        st.write("Replay runs the pipeline on **recorded** OpenAI responses (the 10 sample packs only) so tests and "
                 "demos are repeatable without API calls. It is not live AI: results are labelled "
                 "**Recorded AI Response — Not Live**. Documents without a recording get regex-only analysis; "
                 "another document's recording is never reused.")
        use_replay = st.toggle("Use recorded AI responses (replay mode)", value=testing, disabled=busy)
        if use_replay != testing:
            st.session_state.mode = "replay" if use_replay else "auto"
            st.rerun()
    if testing:
        st.warning("Testing mode is on: documents are read from recorded AI responses, not live AI.")
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

    render_storage(engine, busy)


def render_storage(engine, busy: bool) -> None:
    section("Storage")
    store = engine.store
    st.html(kv([
        ("Backend", engine.settings.label),
        ("Persistent", ("html", badge("Yes: survives restarts and redeploys", "success") if engine.persistent
                        else badge("Only on this machine (temporary on Streamlit Cloud)", "warning"))),
        ("PDF files", f"Private bucket '{engine.settings.bucket}'" if engine.persistent
         else str(engine.runtime_dir / "files")),
        ("Batches stored", str(len(store.batches()))),
        ("Waiting for review", str(len(store.pending_reviews()))),
        ("Version", __version__),
    ]))
    if st.button("Test storage connection", help="Runs one database query and one bucket lookup."):
        with st.spinner("Checking…"):
            st.session_state.storage_check = engine.status()
    check = st.session_state.get("storage_check")
    if check:
        for label, (ok, msg) in (("Database", check["database"]), ("Files", check["files"])):
            (st.success if ok else st.error)(f"{label}: {msg}")
    if not engine.persistent:
        st.caption("Set SUPABASE_URL, SUPABASE_KEY and SUPABASE_DB_URL to keep history and PDFs permanently "
                   "(README, Supabase setup).")
    if engine.legacy_history:
        st.info("An older local history (runtime/history.db) was found. Import it with "
                "`python tools/migrate_local_history.py`.")

    render_clear_history(engine, busy)


def render_clear_history(engine, busy: bool) -> None:
    store = engine.store
    pending = store.pending_deletion()
    with st.expander("Clear processing history", expanded=bool(st.session_state.get("confirm_clear"))):
        docs = store.documents()
        files = {d["storage_path"] for d in docs if d.get("storage_path")}
        st.write(f"Permanently deletes **{len(docs)} documents** in **{len(store.batches())} batches**, their "
                 f"processing events and step checkpoints, the paused review pipelines, **{len(files)} stored "
                 "PDF files** and the cached AI answers for these documents. Files and records that belong to "
                 "nothing in this history are never touched.")
        if pending:
            st.warning(f"{pending} document(s) from an earlier clear are still marked for deletion "
                       "(their files could not be deleted). Clearing again retries them.")
        if busy:
            st.info("Wait for the running batch to finish first.")
            return
        if not st.session_state.get("confirm_clear"):
            if st.button("Clear history…", disabled=not (docs or pending)):
                st.session_state.confirm_clear = True
                st.rerun()
            return
        typed = st.text_input("Type DELETE to confirm", key="clear-confirm-text")
        c1, c2, _ = st.columns([1.4, 1, 3])
        if c2.button("Cancel"):
            st.session_state.confirm_clear = False
            st.rerun()
        if c1.button("Permanently delete", type="primary", disabled=typed.strip() != "DELETE"):
            with st.spinner("Deleting…"):
                try:
                    report = engine.clear_history()
                except Exception as exc:
                    st.error(f"Clear History stopped: {exc}. Nothing that was already deleted can be restored; "
                             "run it again to finish.")
                    return
            st.session_state.confirm_clear = False
            st.session_state.pop("dismissed_batch", None)
            st.session_state.clear_report = report
            st.rerun()
    report = st.session_state.pop("clear_report", None)
    if report:
        msg = (f"Deleted {report.documents_deleted} documents, {report.files_deleted} files, "
               f"{report.checkpoints_deleted} review pipelines and {report.cache_entries_deleted} cached answers.")
        if report.complete:
            st.success(msg)
        else:
            st.warning(msg + f" Not finished: {len(report.files_failed)} file(s) and {report.checkpoints_failed} "
                       f"pipeline(s) could not be deleted; {report.documents_kept} document(s) stay marked for "
                       "deletion. Run Clear History again to retry.")
