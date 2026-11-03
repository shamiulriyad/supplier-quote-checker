"""Reusable views: document details, field and rule tables, evidence, document text, PDF preview."""
import io
from html import escape
from pathlib import Path

import pandas as pd
import streamlit as st

from checker.models import FIELD_KINDS, PackResult
from service.progress import STAGE_LABEL
from service.store import load_result

from .state import get_engine
from .theme import (SOURCE_LABEL, decision_badge, explain_error, fmt_bytes, fmt_dt, fmt_ms, fmt_seconds, kv,
                    outcome_badge, quote, section, status_badge)

CATEGORY = {"reject": "Reject rule", "review": "Needs-check rule", "math": "Arithmetic"}


def fmt_value(v) -> str:
    if v is None:
        return "—"
    if isinstance(v, bool):
        return "Yes" if v else "No"
    return str(v)


def fields_frame(result: PackResult) -> pd.DataFrame:
    rows = []
    for name in FIELD_KINDS:
        f = result.fields.get(name)
        rows.append({
            "Field": name.replace("_", " ").capitalize(),
            "Extracted value": fmt_value(f.value) if f else "—",
            "Status": f.status if f else "NOT_FOUND",
            "Source": SOURCE_LABEL.get(f.source, f.source.upper() or "—") if f else "—",
            "key": name,
        })
    return pd.DataFrame(rows)


def rules_frame(result: PackResult) -> pd.DataFrame:
    return pd.DataFrame([{"Rule": r.rule_id, "Check": r.title, "Type": CATEGORY.get(r.category, r.category),
                          "Result": r.outcome, "Explanation": r.reason} for r in result.rule_results])


def _status_style(v: str) -> str:
    return {"FOUND": "color:#16A34A", "PASS": "color:#16A34A", "NOT_FOUND": "color:#64748B",
            "UNCLEAR": "color:#D97706;font-weight:600", "UNKNOWN": "color:#D97706;font-weight:600",
            "FAIL": "color:#DC2626;font-weight:600", "N/A": "color:#94A3B8"}.get(v, "")


def styled(df: pd.DataFrame, cols: list[str]):
    return df.style.map(_status_style, subset=cols)


def render_reasons(result: PackResult, empty_text: str = "All rules pass.") -> None:
    """Evidence viewer: every failed or uncertain rule with the exact document text it relied on."""
    if not result.reasons:
        st.success(empty_text)
        return
    for r in result.reasons:
        evidence = "".join(quote(e) for e in r.evidence) or '<div class="si-meta">No quote: ' \
            'this rule is about something that is missing from the document.</div>'
        st.html(f"""<div class="si-rule">
            <div class="si-card-row"><div><b>{escape(r.rule_id)}</b> · {escape(r.title)}
            <span class="si-meta"> · {escape(CATEGORY.get(r.category, r.category))}</span></div>
            {outcome_badge(r.outcome)}</div>
            <div style="margin:6px 0 4px 0;font-size:.86rem">{escape(explain_error(r.reason) if r.rule_id == 'E1'
                                                                       else r.reason)}</div>
            {evidence}</div>""")


def render_fields(result: PackResult, key: str) -> None:
    df = fields_frame(result)
    event = st.dataframe(styled(df.drop(columns=["key"]), ["Status"]), hide_index=True, on_select="rerun",
                         selection_mode="single-row", key=f"fields-{key}", height=420)
    rows = event.selection.rows if event and event.selection else []
    name = df.iloc[rows[0]]["key"] if rows else None
    if not name:
        st.caption("Select a row to see the exact text the value was read from.")
        return
    f = result.fields.get(name)
    st.html(f'<div class="si-section">{escape(name.replace("_", " ").capitalize())}</div>')
    st.html(kv([("Value", fmt_value(f.value)), ("Status", f.status),
                ("Source", SOURCE_LABEL.get(f.source, f.source or "—")), ("Note", f.note or "—")]))
    if f.quote:
        st.html(quote(f.quote))
    else:
        st.caption("No quote: the value was not found in the document.")


@st.cache_data(show_spinner=False, max_entries=64)
def pdf_pages_png(path: str, mtime: float) -> list[bytes]:
    import pdfplumber

    pages = []
    with pdfplumber.open(path) as pdf:
        for page in pdf.pages[:5]:
            buf = io.BytesIO()
            page.to_image(resolution=110).original.save(buf, format="PNG")
            pages.append(buf.getvalue())
    return pages


def render_pdf_preview(path: str | None) -> None:
    if not path or not Path(path).exists():
        st.info("The original file is no longer available for preview.")
        return
    try:
        for png in pdf_pages_png(path, Path(path).stat().st_mtime):
            st.image(png, width="stretch")
    except Exception:
        st.warning("This file cannot be rendered as a PDF (it may be corrupted).")


def render_events(document_id: str) -> None:
    events = get_engine().store.events(document_id=document_id)
    if not events:
        st.caption("No processing events recorded.")
        return
    rows = [{"Time": e["ts"][11:19], "Stage": STAGE_LABEL.get(e["stage"], e["stage"].capitalize()),
             "Status": e["status"], "Duration": fmt_ms(e["duration_ms"]), "Message": e["message"]}
            for e in events if e["status"] != "running"]
    st.dataframe(pd.DataFrame(rows), hide_index=True)


def render_document(doc: dict, key: str = "doc") -> None:
    """Full inspection view for one processed document (a row from the history store)."""
    result = load_result(doc)
    human = doc.get("human_decision")
    st.html(f"""<div class="si-card"><div class="si-card-row">
        <div><div class="si-doc">{escape(doc['filename'])}</div>
        <div class="si-meta">{escape(doc.get('supplier') or 'Supplier not identified')} ·
        {escape(doc.get('item') or 'item unknown')}</div></div>
        <div>{status_badge(doc['status'])}</div></div></div>""")

    c1, c2 = st.columns([3, 2])
    with c1:
        st.html(kv([
            ("File size", fmt_bytes(doc.get("size_bytes"))),
            ("Processed", fmt_dt(doc.get("processed_at"))),
            ("Processing time", fmt_seconds(doc.get("seconds"))),
            ("Mode / extraction", f"{doc.get('mode', '-')} · {doc.get('extraction_source') or '-'}"),
            ("Batch", doc["batch_id"]),
        ]))
    with c2:
        rows = [("System decision", ("html", decision_badge(doc.get("decision"))))]
        if human:
            rows.append(("Human decision", ("html", decision_badge(human))))
            rows.append(("Reviewer comment", doc.get("human_comment") or "—"))
            rows.append(("Reviewed", fmt_dt(doc.get("reviewed_at"))))
        elif doc["status"] in ("waiting_review", "reviewing"):
            rows.append(("Human decision", ("html", status_badge("waiting_review"))))
        st.html(kv(rows))

    for err in (doc.get("error") or "").split("; "):
        if err:
            st.warning(explain_error(err))

    if result is None:
        st.info("This document has no result (it did not finish processing).")
        return

    tabs = st.tabs(["Evidence", "Extracted fields", "Rule validation", "Document text", "PDF preview",
                    "Processing log"])
    with tabs[0]:
        section("Failed or uncertain rules")
        render_reasons(result)
    with tabs[1]:
        render_fields(result, key)
    with tabs[2]:
        df = rules_frame(result)
        if df.empty:
            st.info("Rules were not evaluated (an earlier step failed).")
        else:
            st.dataframe(styled(df, ["Result"]), hide_index=True)
    with tabs[3]:
        text = doc.get("raw_text")
        if text:
            st.caption(f"{len(text):,} characters extracted. Use the copy button at the top right of the box.")
            with st.expander("Full extracted text", expanded=True):
                st.code(text, language=None, wrap_lines=True)
        else:
            st.info("No text could be extracted from this file.")
    with tabs[4]:
        render_pdf_preview(doc.get("file_path"))
    with tabs[5]:
        render_events(doc["document_id"])
