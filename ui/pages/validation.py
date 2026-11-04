"""Validation Results: every rule outcome for every document of a batch."""
import pandas as pd
import streamlit as st

from service.store import load_result

from .. import nav
from ..charts import rule_failures_chart
from ..components import styled
from ..state import get_engine
from ..theme import empty_state, page_header, section
from .documents import batch_picker

RULE_ORDER = ["R1", "R2", "R3", "R4", "R5", "N1", "N2", "N3", "N4", "N5", "N6", "N7", "P1", "X1"]


def render() -> None:
    page_header("Validation Results", "Company-rule outcomes per document: PASS, FAIL, UNKNOWN (could not be "
                                      "verified) or N/A.")
    store = get_engine().store
    batches = store.batches()
    if not batches:
        empty_state("No results yet", "Run a batch on the Document Processing page to see rule outcomes.")
        return
    batch_id = batch_picker("validation-batch", allow_all=False, batches=batches)
    docs = [d for d in store.documents(batch_id) if d.get("result_json")]
    results = [(d, load_result(d)) for d in docs]
    if not results:
        empty_state("No finished documents in this batch", "Results appear as soon as each document finishes.")
        return

    titles: dict[str, tuple[str, str]] = {}
    rows = []
    for d, r in results:
        row = {"Document": d["filename"], "Decision": d.get("decision")}
        for rr in r.rule_results:
            titles[rr.rule_id] = (rr.title, rr.category)
            row[rr.rule_id] = rr.outcome
        rows.append(row)
    cols = [c for c in RULE_ORDER if c in titles] + sorted(set(titles) - set(RULE_ORDER))
    matrix = pd.DataFrame(rows, columns=["Document", "Decision"] + cols).fillna("—")

    section("Rule matrix")
    st.dataframe(styled(matrix, cols), hide_index=True)
    st.caption("R = reject rule, N/P = needs-a-person rule, X = purchase-request match. "
               "Documents whose processing failed have no rule results.")

    section("Rule failures in this batch")
    counts = []
    for rid in cols:
        fails = sum(1 for _d, r in results for rr in r.rule_results if rr.rule_id == rid and rr.outcome == "FAIL")
        unknown = sum(1 for _d, r in results for rr in r.rule_results
                      if rr.rule_id == rid and rr.outcome == "UNKNOWN")
        counts.append({"rule": rid, "title": titles[rid][0], "category": titles[rid][1], "fail": fails,
                       "unknown": unknown})
    c1, c2 = st.columns([3, 2])
    with c1:
        if any(c["fail"] for c in counts):
            st.plotly_chart(rule_failures_chart(counts), config={"displayModeBar": False})
        else:
            st.success("No rule failed in this batch.")
    with c2:
        st.dataframe(pd.DataFrame([{"Rule": c["rule"], "Check": c["title"], "Failed": c["fail"],
                                    "Unknown": c["unknown"]} for c in counts]), hide_index=True, height=420)

    section("Open a document")
    pick = st.selectbox("Document", [d["document_id"] for d, _ in results],
                        format_func=lambda i: next(d["filename"] for d, _ in results if d["document_id"] == i))
    if st.button("Open evidence and details", icon=":material/open_in_new:"):
        nav.open_document(pick)
