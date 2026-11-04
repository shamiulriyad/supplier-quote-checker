"""Reusable views: document details, field and rule tables, evidence, document text, PDF preview."""
import io
from html import escape

import pandas as pd
import streamlit as st

from checker.config import load_config
from checker.models import FIELD_KINDS, PackResult
from service.progress import STAGE_LABEL
from service.store import load_result

from .state import get_engine
from .theme import (analysis_badge, decision_badge, explain_error, fmt_bytes, fmt_dt, fmt_ms, fmt_seconds, kv,
                    outcome_badge, quote, section, status_badge)

CATEGORY = {"reject": "Reject rule", "review": "Needs-check rule", "math": "Arithmetic",
            "verification": "Verification check"}
RESULT_LABEL = {"PASS": "PASS", "FAIL": "FAIL", "UNKNOWN": "NEEDS REVIEW", "N/A": "N/A"}
METHOD = {"llm": "LLM", "regex": "Regex", "llm+regex": "LLM + Regex"}


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
            "Method": METHOD.get(f.source, f.source or "—") if f else "—",
            "Page": str(f.page) if f and f.page else "—",
            "Verification": (f.verification or "—") if f else "—",
            "Conflict": "Yes" if f and f.conflict else "",
            "Evidence": (f.quote or "") if f else "",
            "key": name,
        })
    return pd.DataFrame(rows)


def rules_frame(result: PackResult, categories: tuple = ("reject", "review")) -> pd.DataFrame:
    return pd.DataFrame([{"Rule": r.rule_id, "Check": r.title, "Type": CATEGORY.get(r.category, r.category),
                          "Expected": r.expected or "—", "Actual": r.actual or "—",
                          "Result": RESULT_LABEL.get(r.outcome, r.outcome), "Explanation": r.reason}
                         for r in result.rule_results if r.category in categories])


def _status_style(v: str) -> str:
    return {"FOUND": "color:#16A34A", "PASS": "color:#16A34A", "NOT_FOUND": "color:#64748B",
            "UNCLEAR": "color:#D97706;font-weight:600", "UNKNOWN": "color:#D97706;font-weight:600",
            "NEEDS REVIEW": "color:#D97706;font-weight:600", "Yes": "color:#DC2626;font-weight:600",
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
                ("Extraction method", METHOD.get(f.source, f.source or "—")),
                ("Verification", f.verification or "—"),
                ("PDF page", str(f.page) if f.page else "not located on a single page"),
                ("Note", f.note or "—")]))
    if f.quote:
        st.html(quote(f.quote))
    else:
        st.caption("No quote: the value was not found in the document.")
    if f.candidates:
        st.caption("Every reading of this field (kept, never silently replaced):")
        st.dataframe(pd.DataFrame([{"Source": c.source, "Value": fmt_value(c.value), "Evidence": c.quote or "—"}
                                   for c in f.candidates]), hide_index=True)


def _money(v) -> str:
    try:
        return f"{float(v):,.2f}"
    except (TypeError, ValueError):
        return "—"


def render_decision(result: PackResult) -> None:
    """Document information, final decision with reasons, financial analysis, rule-by-rule analysis."""
    f = result.fields
    val = lambda n: fmt_value(f[n].value) if n in f and f[n].value is not None else "not verified"  # noqa: E731
    left, right = st.columns([3, 2], gap="medium")
    with left:
        section("Document information")
        st.html(kv([("Supplier", val("supplier_name")), ("Quotation number", val("quotation_no")),
                    ("Date", val("quote_date")), ("Purchase request", val("pr_reference")),
                    ("Item", result.item or "not verified"), ("Analysis", ("html", analysis_badge(result.extraction_source)))]))
    with right:
        section("Final decision")
        st.html(f'<div style="font-size:1.05rem;margin-bottom:6px">{decision_badge(result.decision)}</div>')
        if result.reasons:
            for r in result.reasons:
                text = explain_error(r.reason) if r.rule_id == "E1" else r.reason
                st.markdown(f"- **{r.rule_id}** {RESULT_LABEL.get(r.outcome, r.outcome)}: {text}")
        else:
            st.caption("Approved: all 14 rules pass and every mandatory fact was verified in the document.")
        st.caption("Decided by fixed code from this document's verified facts. The AI only reads the document.")

    fin = result.financial or {}
    section("Financial analysis")
    if fin:
        cur = fin.get("currency")
        foreign = bool(cur) and cur != "BDT"
        doc_cur = cur or "currency unclear"
        if foreign and fin.get("exchange_rate"):
            conversion = f"1 {cur} = {fin['exchange_rate']} BDT (company rate, as of {fin.get('rates_as_of') or '—'})"
        elif foreign:
            conversion = f"no company rate for {cur}: not converted"
        else:
            conversion = fin.get("currency_note") or "no conversion needed"
        rows = [("Currency", cur or "unclear", conversion, "")]
        if foreign:
            rows.append((f"Unit price ({cur}, as quoted)", fmt_value(fin.get("unit_price_quoted")), "", ""))
        rows += [
            ("Quantity", f"{fmt_value(fin.get('quantity'))} {fin.get('unit') or ''}".strip(),
             f"{fmt_value(fin.get('requested_quantity'))} requested" if fin.get("requested_quantity") else "—",
             f"{fin['quantity_deviation_pct']}%" if fin.get("quantity_deviation_pct") else "—"),
            ("Unit price (BDT)" + (", converted" if foreign else ""), fmt_value(fin.get("unit_price")),
             f"max {fin.get('max_unit_price')} (reject above {fin.get('reject_above_price')})"
             if fin.get("max_unit_price") else "—",
             f"{fin['price_deviation_pct']}%" if fin.get("price_deviation_pct") else "—"),
            (f"Line amount ({doc_cur})", _money(fin.get("line_amount")),
             f"quantity × price = {_money(fin.get('computed_line_amount'))}", ""),
            (f"Subtotal ({doc_cur})", _money(fin.get("subtotal")), "", ""),
            ("VAT", f"{fmt_value(fin.get('vat_rate'))}% = {_money(fin.get('vat_amount'))}" if fin.get("vat_rate")
             else ("included in prices" if fin.get("price_includes_vat") else "not shown"),
             f"{load_config()['rules']['vat_rate_pct']}% required", ""),
            (f"Total ({doc_cur})", _money(fin.get("total")), "", ""),
        ]
        st.dataframe(pd.DataFrame(rows, columns=["Item", "Document", "Purchase request / check", "Deviation"]),
                     hide_index=True)
    else:
        st.caption("No financial data (the document could not be read).")

    missing = [n for n, x in f.items() if x.status == "NOT_FOUND"]
    flagged = [(n, x) for n, x in f.items() if x.status == "UNCLEAR"]
    if missing or flagged:
        section("Missing or conflicting fields")
        if flagged:
            st.dataframe(pd.DataFrame([{"Field": n.replace("_", " "), "Problem": x.verification or "unclear",
                                        "Detail": x.note or ""} for n, x in flagged]), hide_index=True)
        if missing:
            st.caption("Not in the document: " + ", ".join(n.replace("_", " ") for n in missing))

    section("Business rule analysis (14 rules)")
    df = rules_frame(result)
    if df.empty:
        st.info("Rules were not evaluated (an earlier step failed).")
    else:
        st.dataframe(styled(df.drop(columns=["Type"]), ["Result"]), hide_index=True,
                     height=35 * (len(df) + 1) + 3)  # all 14 rows visible without scrolling
    gates = rules_frame(result, ("verification",))
    if not gates.empty:
        section("Verification checks")
        st.dataframe(styled(gates.drop(columns=["Type"]), ["Result"]), hide_index=True)


@st.cache_data(show_spinner=False, max_entries=64)
def pdf_pages_png(storage_path: str, _doc: dict) -> list[bytes]:
    """Render up to 5 pages. Keyed by storage path, which is content-addressed (the file never changes)."""
    import pdfplumber

    data = get_engine().read_pdf(_doc)  # downloaded server-side; no public URL is ever created
    pages = []
    with pdfplumber.open(io.BytesIO(data)) as pdf:
        for page in pdf.pages[:5]:
            buf = io.BytesIO()
            page.to_image(resolution=110).original.save(buf, format="PNG")
            pages.append(buf.getvalue())
    return pages


def render_pdf_preview(doc: dict) -> None:
    if not doc.get("storage_path"):
        st.info("No stored copy of this file is available for preview.")
        return
    try:
        pages = pdf_pages_png(doc["storage_path"], doc)
    except FileNotFoundError:
        st.info("The stored file was not found (it may have been deleted).")
        return
    except Exception as exc:
        name = type(exc).__name__
        if "pdf" in str(exc).lower() or name in ("PDFSyntaxError", "PSEOF", "PdfminerException"):
            st.warning("This file cannot be rendered as a PDF (it may be corrupted).")
        else:
            st.warning(f"The preview could not be loaded from storage ({name}). Try again in a moment.")
        return
    for png in pages:
        st.image(png, width="stretch")


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
            ("Analysis", ("html", analysis_badge(doc.get("extraction_source")))),
            ("Mode / extraction detail", f"{doc.get('mode', '-')} · {doc.get('extraction_source') or '-'}"),
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

    tabs = st.tabs(["Decision", "Evidence", "Extraction evidence", "Rule validation", "Document text",
                    "PDF preview", "Processing log"])
    with tabs[0]:
        render_decision(result)
    with tabs[1]:
        section("Failed or uncertain rules")
        render_reasons(result)
    with tabs[2]:
        render_fields(result, key)
    with tabs[3]:
        df = rules_frame(result, ("reject", "review", "verification"))
        if df.empty:
            st.info("Rules were not evaluated (an earlier step failed).")
        else:
            st.dataframe(styled(df, ["Result"]), hide_index=True)
    with tabs[4]:
        text = doc.get("raw_text")
        if text:
            st.caption(f"{len(text):,} characters extracted. Use the copy button at the top right of the box.")
            with st.expander("Full extracted text", expanded=True):
                st.code(text, language=None, wrap_lines=True)
        else:
            st.info("No text could be extracted from this file.")
    with tabs[5]:
        render_pdf_preview(doc)
    with tabs[6]:
        render_events(doc["document_id"])
