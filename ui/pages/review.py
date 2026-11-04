"""Human Review workspace. Decisions resume the document's paused LangGraph run (interrupt -> Command(resume))."""
from html import escape

import pandas as pd
import streamlit as st

from checker.report import LABEL
from service.batch import ReviewExpired
from service.store import AlreadyReviewed, load_result

from .. import nav
from ..components import fmt_value, render_reasons
from ..state import get_engine
from ..theme import (analysis_badge, badge, decision_badge, empty_state, explain_error, fmt_ago, fmt_dt, kv, page_header,
                     section)

KEY_FIELDS = [("supplier_name", "Supplier"), ("item_code", "Item"), ("description", "Description"),
              ("quantity", "Quantity"), ("unit_price", "Unit price (BDT)"), ("advance_pct", "Advance %"),
              ("payment_days_max", "Payment (days)"), ("delivery_days", "Delivery (days)"),
              ("validity_days", "Validity (days)"), ("tin", "TIN"), ("bin", "BIN")]


def why_not_decided(result) -> list[str]:
    out = []
    for r in result.reasons:
        if r.rule_id == "E1":
            out.append(explain_error(r.reason))
        elif r.outcome == "FAIL" and r.category == "review":
            out.append(f"{r.rule_id} {r.title}: the company rules send this case to a person.")
        elif r.outcome == "FAIL":
            out.append(f"{r.rule_id} {r.title}: failed (shown for completeness).")
        elif r.outcome == "UNKNOWN":
            out.append(f"{r.rule_id} {r.title}: could not be verified from the document, and the system does "
                       "not guess.")
    return out


def render_workspace(doc: dict) -> None:
    engine = get_engine()
    result = load_result(doc)
    flash = st.session_state.pop("review_flash", None)
    if flash:
        st.success(flash)

    st.html(f"""<div class="si-card"><div class="si-card-row"><div>
        <div class="si-doc">{escape(doc['filename'])}</div>
        <div class="si-meta">{escape(doc.get('supplier') or 'Supplier not identified')} · waiting for
        {fmt_ago(doc.get('processed_at'))}</div></div>
        <div>{analysis_badge(doc.get('extraction_source'))} {badge('AI recommendation', 'neutral')}
        {decision_badge(doc.get('decision'))}</div></div>
        <div class="si-meta" style="margin-top:6px">The recommendation is the pipeline's output, not a decision.
        The document's pipeline is paused and continues only when you submit a decision.</div></div>""")

    left, right = st.columns([3, 2], gap="medium")
    with left:
        section("Why the system could not decide")
        for line in why_not_decided(result) if result else ["The document did not produce a result."]:
            st.markdown(f"- {line}")
        section("Failed or uncertain rules · evidence from the PDF")
        if result:
            render_reasons(result)
    with right:
        section("Extracted supplier information")
        if result:
            st.html(kv([(label, f"{fmt_value(result.fields[n].value)}"
                                + ("" if result.fields[n].status == "FOUND" else f" ({result.fields[n].status})"))
                        for n, label in KEY_FIELDS if n in result.fields]))
        section("Your decision")
        if not engine.can_resume(doc["document_id"]):
            st.error("This document's paused pipeline could not be found (for example, the runtime folder was "
                     "deleted). It cannot be resumed; process the document again.")
            return
        with st.form(f"review-form-{doc['document_id']}", clear_on_submit=False):
            decision = st.radio("Decision", ["APPROVE", "REJECT"], index=None, horizontal=True,
                                format_func=lambda d: LABEL[d])
            comment = st.text_area("Explain the reason for your decision.", height=110)
            submitted = st.form_submit_button("Submit Review Decision", type="primary", width="stretch")
        if submitted:
            if decision is None or not comment.strip():
                st.warning("Choose Approve or Reject and explain the reason before submitting.")
                return
            try:
                with st.spinner("Resuming the document's pipeline…"):
                    final = engine.submit_review(doc["document_id"], decision, comment.strip())
            except AlreadyReviewed:
                st.warning("This document has already been reviewed. The queue has been refreshed.")
            except ReviewExpired as exc:
                st.error(str(exc))
                return
            else:
                st.session_state.review_flash = (
                    f"{doc['filename']}: your decision '{LABEL[decision]}' was recorded and the pipeline finished "
                    f"(report generated). System recommendation was '{LABEL[final.decision]}'.")
            st.session_state.pop("review_focus", None)
            st.rerun()


def render() -> None:
    page_header("Human Review", "Documents the system would not decide on its own. Your decision resumes the "
                                "paused pipeline for that document.")
    store = get_engine().store
    docs = store.documents()  # one read for both lists
    pending = store.pending_reviews(docs)
    reviewed = [d for d in docs if d["status"] == "reviewed"]

    m = st.columns(3)
    m[0].metric("Waiting for review", len(pending))
    m[1].metric("Approved by a person", sum(d["human_decision"] == "APPROVE" for d in reviewed))
    m[2].metric("Rejected by a person", sum(d["human_decision"] == "REJECT" for d in reviewed))

    flash = st.session_state.get("review_flash")
    section("Review queue")
    if not pending:
        if flash:
            st.success(st.session_state.pop("review_flash"))
        empty_state("Nothing is waiting for review",
                    "Documents appear here when a needs-check rule fails or a fact cannot be verified.")
    else:
        df = pd.DataFrame([{
            "Document": d["filename"], "Supplier": d.get("supplier") or "—",
            "Reason for review": ", ".join(f"{r.rule_id} {r.title}" for r in (load_result(d).reasons
                                                                               if d.get("result_json") else [])),
            "Review status": "In progress" if d["status"] == "reviewing" else "Waiting",
            "Waiting for": fmt_ago(d.get("processed_at")), "Batch": d["batch_id"],
        } for d in pending])
        event = st.dataframe(df, hide_index=True, on_select="rerun", selection_mode="single-row", key="review-table")
        rows = event.selection.rows if event else []
        doc = pending[rows[0]] if rows else pending[0]
        if not rows:
            st.caption("Showing the oldest item first. Select a row to review a different document.")
        section("Review workspace")
        render_workspace(doc)

    section("Decisions made by people")
    if not reviewed:
        st.caption("No human decisions yet.")
        return
    st.dataframe(pd.DataFrame([{
        "Document": d["filename"], "Supplier": d.get("supplier") or "—", "AI recommendation": d["decision"],
        "Human decision": d["human_decision"], "Comment": d.get("human_comment") or "—",
        "Reviewed": fmt_dt(d.get("reviewed_at")),
    } for d in sorted(reviewed, key=lambda d: d.get("reviewed_at") or "", reverse=True)]), hide_index=True)
    pick = st.selectbox("Open a reviewed document", [d["document_id"] for d in reviewed], index=None,
                        format_func=lambda i: next(d["filename"] + " · " + d["batch_id"] for d in reviewed
                                                   if d["document_id"] == i))
    if pick:
        nav.open_document(pick)
