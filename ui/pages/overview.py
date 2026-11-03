"""Overview dashboard. Every number is computed from the history store (real processed documents)."""
from collections import Counter

import pandas as pd
import streamlit as st

from service.store import load_result

from .. import nav
from ..charts import decisions_chart, history_chart, review_chart, rule_failures_chart
from ..state import get_engine
from ..theme import empty_state, fmt_dt, fmt_seconds, page_header, section

PROCESSED = ("completed", "waiting_review", "reviewing", "reviewed", "failed")


def render() -> None:
    c1, c2 = st.columns([4, 1.3], vertical_alignment="bottom")
    with c1:
        page_header("Supplier Intelligence Dashboard",
                    "Intelligent supplier quotation analysis and compliance verification.")
    with c2:
        if st.button("New Document Analysis", type="primary", icon=":material/add:", width="stretch"):
            nav.go("processing")

    store = get_engine().store
    docs = [d for d in store.documents() if d["status"] in PROCESSED]
    if not docs:
        empty_state("No documents processed yet",
                    "Start a new document analysis to see decisions, review workload and rule statistics here.")
        return

    decisions = Counter(d["decision"] for d in docs if d.get("decision"))
    with_errors = sum(1 for d in docs if d["status"] == "failed" or d.get("failed_stage") or d.get("error"))
    times = [d["seconds"] for d in docs if d.get("seconds") is not None]
    m = st.columns(6)
    m[0].metric("Total documents", len(docs))
    m[1].metric("Approved", decisions["APPROVE"])
    m[2].metric("Rejected", decisions["REJECT"])
    m[3].metric("Needs human review", decisions["NEEDS_REVIEW"])
    m[4].metric("Processing success rate", f"{(len(docs) - with_errors) / len(docs):.0%}",
                help="Documents processed without any pipeline error (e.g. unreadable PDF, AI extraction failure).")
    m[5].metric("Avg processing time", fmt_seconds(sum(times) / len(times)) if times else "—")
    st.caption("Approved / Rejected / Needs human review are the system's decisions. "
               "Decisions made by a person are shown under Human review below.")

    section("Recent processing activity")
    recent = sorted(docs, key=lambda d: (d.get("processed_at") or "", d["document_id"]), reverse=True)[:10]
    st.dataframe(pd.DataFrame([{
        "Document": d["filename"], "Supplier": d.get("supplier") or "—",
        "Status": d["status"].replace("_", " "), "Processing time": fmt_seconds(d.get("seconds")),
        "Decision": d.get("decision") or "—", "Human decision": d.get("human_decision") or "—",
        "Processed": fmt_dt(d.get("processed_at")),
    } for d in recent]), hide_index=True)

    section("Processing statistics")
    left, right = st.columns(2, gap="medium")
    with left:
        st.markdown("**System decisions**")
        st.plotly_chart(decisions_chart(dict(decisions)), config={"displayModeBar": False})
    with right:
        st.markdown("**Human review**")
        waiting = sum(1 for d in docs if d["status"] in ("waiting_review", "reviewing"))
        human = Counter(d.get("human_decision") for d in docs if d.get("human_decision"))
        if waiting or human:
            st.plotly_chart(review_chart(waiting, human["APPROVE"], human["REJECT"]),
                            config={"displayModeBar": False})
        else:
            st.caption("No document has needed a person yet.")

    left, right = st.columns(2, gap="medium")
    with left:
        st.markdown("**Documents per batch**")
        st.plotly_chart(history_chart(store.batches()[:12]), config={"displayModeBar": False})
    with right:
        st.markdown("**Rule failures (all documents)**")
        fails, titles = Counter(), {}
        for d in docs:
            r = load_result(d)
            for rr in (r.rule_results if r else []):
                titles[rr.rule_id] = rr.title
                if rr.outcome == "FAIL":
                    fails[rr.rule_id] += 1
        if fails:
            st.plotly_chart(rule_failures_chart([{"rule": k, "title": titles[k], "fail": v}
                                                 for k, v in fails.items()]), config={"displayModeBar": False})
        else:
            st.caption("No rule has failed yet.")
