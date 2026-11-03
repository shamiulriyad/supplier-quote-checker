"""All Documents: every processed document, with a full inspection view."""
import pandas as pd
import streamlit as st

from .. import nav
from ..components import render_document
from ..state import get_engine
from ..theme import empty_state, fmt_dt, fmt_seconds, page_header, section


def batch_picker(key: str, allow_all: bool = True) -> str | None:
    batches = get_engine().store.batches()
    if not batches:
        return None
    labels = {b["batch_id"]: f"{b['batch_id']} · {b['total']} docs · {b['mode']} · {b['status']}" for b in batches}
    options = ([None] if allow_all else []) + list(labels)
    return st.selectbox("Batch", options, key=key,
                        format_func=lambda b: "All batches" if b is None else labels[b],
                        index=1 if allow_all else 0)


def documents_table(docs: list[dict], key: str) -> dict | None:
    df = pd.DataFrame([{
        "Document": d["filename"], "Supplier": d.get("supplier") or "—", "System decision": d.get("decision") or "—",
        "Human decision": d.get("human_decision") or "—", "Status": (d["status"] or "").replace("_", " "),
        "Time": fmt_seconds(d.get("seconds")), "Processed": fmt_dt(d.get("processed_at")),
        "Batch": d["batch_id"],
    } for d in docs])
    event = st.dataframe(df, hide_index=True, on_select="rerun", selection_mode="single-row", key=key)
    rows = event.selection.rows if event else []
    return docs[rows[0]] if rows else None


def render() -> None:
    page_header("All Documents", "Every processed supplier document. Select one to inspect fields, rules and evidence.")
    store = get_engine().store
    requested = st.session_state.pop("open_document", None)
    if requested:
        st.session_state.documents_focus = requested

    if not store.batches():
        empty_state("No documents yet", "Process documents on the Document Processing page; they will appear here.")
        if st.button("Go to Document Processing", type="primary"):
            nav.go("processing")
        return

    c1, c2 = st.columns([2, 3])
    with c1:
        batch_id = batch_picker("documents-batch")
    with c2:
        query = st.text_input("Search by file or supplier", placeholder="e.g. pack-05 or Meghna")
    docs = store.documents(batch_id)
    if query:
        q = query.lower()
        docs = [d for d in docs if q in d["filename"].lower() or q in (d.get("supplier") or "").lower()]

    section(f"{len(docs)} documents")
    picked = documents_table(docs, key="documents-table")
    focus_id = picked["document_id"] if picked else st.session_state.get("documents_focus")
    doc = store.document(focus_id) if focus_id else None
    if picked:
        st.session_state.documents_focus = picked["document_id"]
    if doc is None:
        st.caption("Select a document in the table to see its details.")
        return
    section("Document details")
    render_document(doc, key="documents")
