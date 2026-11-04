"""Processing History, read from the history store (Supabase PostgreSQL, or local SQLite)."""
from datetime import date, datetime

import pandas as pd
import streamlit as st

from .. import nav
from ..components import render_document
from ..state import MODES, get_engine, get_queue
from ..theme import empty_state, fmt_dt, fmt_seconds, page_header, section


def render() -> None:
    engine = get_engine()
    store = engine.store
    page_header("Processing History", "Previous batches and their documents.")
    if engine.persistent:
        st.caption("Stored in Supabase (PostgreSQL + a private Storage bucket): it survives restarts and redeploys. "
                   "There is no login yet, so everyone using this deployment sees the same history.")
    else:
        st.caption(f"Stored locally ({engine.runtime_dir.name}/app.db and {engine.runtime_dir.name}/files). On "
                   "Streamlit Community Cloud this is lost when the app restarts; configure Supabase to keep it.")
    docs = store.documents()  # one read, used for the interrupted list and the table
    interrupted = store.interrupted(docs)
    if interrupted:
        st.info(f"{len(interrupted)} document(s) were queued or running when the app stopped (marked "
                "*interrupted*). Their PDFs are stored, so they can be processed again.")
        if st.button(f"Re-queue {len(interrupted)} interrupted document(s)", icon=":material/replay:"):
            msgs = engine.requeue([d["document_id"] for d in interrupted], get_queue())
            st.toast("; ".join(msgs)[:300])
            st.session_state.dismissed_batch = engine.runner.batch.batch_id if engine.runner else None
            nav.go("processing")

    batches = store.batches()
    if not batches:
        empty_state("No history yet", "Processed batches will be listed here.")
        return

    def day(iso: str | None) -> date | None:
        return datetime.fromisoformat(iso).date() if iso else None

    first, last = min(day(b["created_at"]) for b in batches), max(day(b["created_at"]) for b in batches)
    f1, f2, f3, f4, f5 = st.columns([1.6, 1.4, 1.2, 1.2, 1.2])
    period = f1.date_input("Date", (first, last), min_value=first, max_value=last)
    suppliers = f2.multiselect("Supplier", sorted({d["supplier"] for d in docs if d.get("supplier")}))
    decisions = f3.multiselect("Decision", ["APPROVE", "REJECT", "NEEDS_REVIEW"])
    modes = f4.multiselect("Mode", list(MODES), format_func=lambda m: MODES[m][0])
    statuses = f5.multiselect("Status", sorted({d["status"] for d in docs}))

    start, end = (period if isinstance(period, tuple) and len(period) == 2 else (first, last))

    def keep(d: dict) -> bool:
        created = day(d["batch_created_at"])
        return (start <= created <= end and (not suppliers or d.get("supplier") in suppliers)
                and (not decisions or d.get("decision") in decisions) and (not modes or d["mode"] in modes)
                and (not statuses or d["status"] in statuses))

    docs = [d for d in docs if keep(d)]
    batch_ids = {d["batch_id"] for d in docs}
    shown = [b for b in batches if b["batch_id"] in batch_ids]

    section(f"Batches ({len(shown)})")
    st.dataframe(pd.DataFrame([{
        "Batch": b["batch_id"], "Started": fmt_dt(b["started_at"] or b["created_at"]), "Mode": MODES[b["mode"]][0],
        "Status": b["status"], "Documents": b["total"], "Approved": b["approved"] or 0, "Rejected": b["rejected"] or 0,
        "Needs review": b["needs_review"] or 0, "With errors": b["failed"] or 0,
        "Avg time": fmt_seconds(b["avg_seconds"]),
    } for b in shown]), hide_index=True)

    section(f"Documents ({len(docs)})")
    if not docs:
        st.caption("No documents match the filters.")
        return
    event = st.dataframe(pd.DataFrame([{
        "Batch": d["batch_id"], "Document": d["filename"], "Supplier": d.get("supplier") or "—",
        "Decision": d.get("decision") or "—", "Human decision": d.get("human_decision") or "—",
        "Status": d["status"], "Mode": MODES[d["mode"]][0], "Time": fmt_seconds(d.get("seconds")),
    } for d in docs]), hide_index=True, on_select="rerun", selection_mode="single-row", key="history-docs")
    rows = event.selection.rows if event else []
    if rows:
        section("Document details")
        render_document(store.document(docs[rows[0]]["document_id"]), key="history")
    else:
        st.caption("Select a document to see its details.")
