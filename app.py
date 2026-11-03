"""Streamlit demo: run the checker on the sample packs or an uploaded PDF, and review flagged packs.

    streamlit run app.py
"""
import csv
import os
import tempfile
import uuid
from pathlib import Path

import streamlit as st
from langgraph.types import Command

from checker.env import load_dotenv
from checker.config import ROOT, load_config
from checker.graph import build_graph, initial_state
from checker.models import FIELD_KINDS
from checker.report import LABEL

st.set_page_config(page_title="Supplier Quote Checker", page_icon="📄", layout="wide")

load_dotenv()
try:  # Streamlit Cloud: key comes from the app's Secrets
    if "OPENAI_API_KEY" in st.secrets and not os.environ.get("OPENAI_API_KEY"):
        os.environ["OPENAI_API_KEY"] = st.secrets["OPENAI_API_KEY"]
    if "OPENAI_MODEL" in st.secrets:
        os.environ.setdefault("OPENAI_MODEL", st.secrets["OPENAI_MODEL"])
except Exception:
    pass

CFG = load_config()
BADGE = {"APPROVE": "✅ Approve", "REJECT": "❌ Reject", "NEEDS_REVIEW": "🟡 Needs a person to check"}
OUTCOME_ICON = {"FAIL": "❌", "UNKNOWN": "❓", "PASS": "✅", "N/A": "➖"}
STEPS = ["Read PDF", "Extract facts (LLM + regex)", "Verify quotes", "Match purchase request",
         "Re-check the maths", "Apply company rules", "Decide", "Ask a person (if unsure)", "Report"]


@st.cache_data
def expected_answers() -> dict:
    with open(ROOT / "tests" / "expected.csv", encoding="utf-8") as fh:
        return {r["file"]: r["expected_decision"] for r in csv.DictReader(fh)}


if "graph" not in st.session_state:
    st.session_state.graph = build_graph(with_human_review=True)
    st.session_state.runs = []
graph = st.session_state.graph


def run_files(files: list[tuple[str, str]], mode: str) -> None:
    runs = []
    progress = st.progress(0.0, text="Checking…")
    for i, (path, name) in enumerate(files):
        thread = {"configurable": {"thread_id": uuid.uuid4().hex}}
        pack_id = "".join(ch for ch in Path(name).stem if ch.isdigit())[-2:] or str(i + 1)
        graph.invoke(initial_state(path, pack_id=pack_id, source_name=name, mode=mode, ask_human=True), thread)
        runs.append({"name": name, "thread": thread})
        progress.progress((i + 1) / len(files), text=f"Checked {name}")
    progress.empty()
    st.session_state.runs = runs


def state_of(run: dict) -> dict:
    snap = graph.get_state(run["thread"])
    return {**snap.values, "waiting": bool(snap.next)}


# ---------------------------------------------------------------- sidebar
with st.sidebar:
    st.header("Settings")
    has_key = bool(os.environ.get("OPENAI_API_KEY"))
    mode = st.radio(
        "How to read the documents",
        ["auto", "live", "replay", "regex"],
        format_func={"auto": "Auto", "live": "Live: OpenAI + regex check", "replay": "Replay: saved OpenAI answers",
                     "regex": "Offline: regex only"}.get,
        help="Auto uses OpenAI if a key is set, otherwise saved answers, otherwise regex only.",
    )
    st.caption(f"OpenAI key: {'found' if has_key else 'not set'} · model: `{CFG['llm']['model']}`")
    if mode == "live" and not has_key:
        st.warning("No OPENAI_API_KEY: live mode will fall back to 'Needs a person' for every pack.")
    st.divider()
    st.subheader("How it works")
    st.markdown("\n".join(f"{i}. {s}" for i, s in enumerate(STEPS, 1)))
    st.caption("The AI only reads. Every number and every rule is checked by code. "
               "If anything is unclear, a person decides.")

# ---------------------------------------------------------------- main
st.title("📄 Supplier Quote Checker")
st.write("Checks each supplier quotation against the purchase request and the company rules, "
         "and answers **Approve**, **Reject** or **Needs a person to check**, quoting the document as proof.")

source = st.radio("Documents", ["The 10 sample supplier packs", "Upload PDF(s)"], horizontal=True)
files: list[tuple[str, str]] = []
if source.startswith("The 10"):
    files = [(str(p), p.name) for p in sorted(Path(CFG["documents_dir"]).glob("*.pdf"))]
else:
    uploads = st.file_uploader("Supplier quotation PDF", type="pdf", accept_multiple_files=True)
    tmp = Path(tempfile.gettempdir()) / "sqc_uploads"
    tmp.mkdir(exist_ok=True)
    for up in uploads or []:
        path = tmp / f"{uuid.uuid4().hex}.pdf"
        path.write_bytes(up.getvalue())
        files.append((str(path), up.name))

if st.button("Run check", type="primary", disabled=not files):
    run_files(files, mode)

runs = [(r, state_of(r)) for r in st.session_state.runs]
if not runs:
    st.info("Choose documents and press **Run check**.")
    st.stop()

# ---------------------------------------------------------------- summary
expected = expected_answers()
rows, matches, compared = [], 0, 0
for run, s in runs:
    human = (s.get("human_decision") or {}).get("decision")
    exp = expected.get(run["name"])
    if exp:
        compared += 1
        matches += exp == s.get("decision")
    rows.append({
        "Pack": run["name"],
        "Supplier": str(s["extracted"].get("supplier_name").value or "?") if s.get("extracted") else "?",
        "Decision": BADGE[s["decision"]],
        "Reason": "; ".join(f"{r.rule_id} {r.title}" for r in s.get("reasons", [])) or "All rules pass",
        "Person": ("⏳ waiting" if s["waiting"] else (LABEL.get(human, human) if human else "")),
        **({"Expected": BADGE[exp]} if exp else {}),
    })

c1, c2, c3, c4 = st.columns(4)
c1.metric("Approve", sum(s["decision"] == "APPROVE" for _, s in runs))
c2.metric("Reject", sum(s["decision"] == "REJECT" for _, s in runs))
c3.metric("Needs a person", sum(s["decision"] == "NEEDS_REVIEW" for _, s in runs))
if compared:
    c4.metric("Matches expected answer", f"{matches}/{compared}")
st.dataframe(rows, hide_index=True)

# ---------------------------------------------------------------- details
st.subheader("Details")
for run, s in runs:
    title = f"{run['name']} — {BADGE[s['decision']]}" + ("  ⏳ waiting for a person" if s["waiting"] else "")
    with st.expander(title, expanded=s["waiting"]):
        st.caption(f"Extraction: {s.get('extraction_source', '?')}")
        for e in s.get("errors", []):
            st.error(e)

        tab_reasons, tab_fields, tab_rules, tab_steps, tab_text = st.tabs(
            ["Reasons", "Extracted facts", "All rule checks", "Pipeline steps", "Document text"])

        with tab_reasons:
            if not s.get("reasons"):
                st.success("All rules pass.")
            for r in s.get("reasons", []):
                kind = {"reject": "Reject rule", "review": "Needs-check rule"}.get(r.category, r.category)
                st.markdown(f"{OUTCOME_ICON[r.outcome]} **{r.rule_id} · {r.title}** ({kind})  \n{r.reason}")
                for ev in r.evidence:
                    st.markdown(f"> {ev}")

        with tab_fields:
            ex = s.get("extracted")
            if ex:
                st.dataframe([{"Field": n, "Value": "" if ex.get(n).value is None else str(ex.get(n).value),
                               "Status": ex.get(n).status, "Source": ex.get(n).source,
                               "Quote from document": ex.get(n).quote or "", "Note": ex.get(n).note or ""}
                              for n in FIELD_KINDS], hide_index=True)

        with tab_rules:
            st.dataframe([{"Rule": r.rule_id, "Check": r.title, "Result": f"{OUTCOME_ICON[r.outcome]} {r.outcome}",
                           "Why": r.reason} for r in s.get("rule_results", [])],
                         hide_index=True)

        with tab_steps:
            st.code("\n".join(s.get("trace", [])) or "no steps recorded")

        with tab_text:
            st.code(s.get("raw_text", ""), language=None)

        if s["waiting"]:
            st.markdown("#### Your decision")
            with st.form(f"review-{run['thread']['configurable']['thread_id']}"):
                choice = st.radio("Decision", ["APPROVE", "REJECT"], format_func=LABEL.get, horizontal=True)
                comment = st.text_input("Comment (why)")
                if st.form_submit_button("Submit decision"):
                    graph.invoke(Command(resume={"decision": choice, "comment": comment}), run["thread"])
                    st.rerun()
        elif s.get("human_decision"):
            h = s["human_decision"]
            st.info(f"Person's decision: **{LABEL[h['decision']]}** — {h.get('comment') or 'no comment'}")
