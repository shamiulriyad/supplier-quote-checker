"""Document Processing: build a queue, start a batch, and watch every PDF move through the pipeline.

The batch runs in a background thread (service.batch.BatchRunner). This page only reads the
runner's snapshot; the monitor refreshes itself once per second with st.fragment while the
batch runs, so clicks elsewhere never interrupt processing.
"""
from datetime import datetime
from html import escape
from pathlib import Path

import pandas as pd
import streamlit as st

from checker.config import load_config
from checker.graph import analysis_kind
from service.batch import BatchStartError, BatchState, DocumentJob
from service.progress import STAGE_LABEL, STAGE_NAMES, STAGES

from .. import nav
from ..state import MODES, api_status, external_calls_enabled, get_engine, get_mode, get_queue
from ..theme import (ANALYSIS, STAGE_ICON, analysis_badge, badge, decision_badge, empty_state, explain_error, fmt_bytes, fmt_ms,
                     fmt_seconds, kv, page_header, section, status_badge)

DONE = ("completed", "waiting_review", "reviewed", "failed")


def mode_bar() -> None:
    mode = get_mode()
    label, tone = api_status()
    calls = badge("External API calls on", "accent") if external_calls_enabled(mode) else badge("No API calls", "neutral")
    st.html(f'<div class="si-meta" style="display:flex;gap:10px;align-items:center;flex-wrap:wrap;'
            f'margin-bottom:6px">Mode {badge(MODES[mode][0], "warning" if mode == "replay" else "accent")} {calls} '
            f'OpenAI {badge(label, tone)}<span>{escape(MODES[mode][1])}</span></div>')
    if mode == "replay":
        st.warning("Testing mode: documents are read from **recorded AI responses — not live AI**. "
                   "Turn it off in Settings → Developer / testing options.")


# ============================================================== queue

def render_queue() -> None:
    engine, queue, mode = get_engine(), get_queue(), get_mode()
    section("Add documents")
    tab_samples, tab_upload = st.tabs(["Sample documents", "Upload PDFs"])
    samples = sorted(Path(load_config()["documents_dir"]).glob("*.pdf"))
    with tab_samples:
        picked = st.multiselect("Sample supplier packs", [p.name for p in samples],
                                placeholder="Choose one or more sample documents")
        c1, c2, _ = st.columns([2, 1.3, 3])
        if c1.button(f"Add all {len(samples)} sample documents", icon=":material/library_add:"):
            added = sum(queue.add_file(p)[0] for p in samples)
            st.toast(f"{added} documents added to the queue" if added else "All samples are already queued")
        if c2.button("Add selected", disabled=not picked):
            added = sum(queue.add_file(p)[0] for p in samples if p.name in picked)
            st.toast(f"{added} documents added")
    with tab_upload:
        with st.form("upload-form", clear_on_submit=True, border=False):
            files = st.file_uploader("Supplier quotation PDFs (max 10 MB each)", type=["pdf"],
                                     accept_multiple_files=True)
            if st.form_submit_button("Add to queue", icon=":material/upload_file:") and files:
                results = [queue.add_upload(f.name, f.getvalue()) for f in files]
                skipped = [msg for ok, msg in results if not ok]
                st.toast(f"{sum(ok for ok, _ in results)} uploaded file(s) added"
                         + (f"; {len(skipped)} already queued" if skipped else ""))

    section("Document queue")
    if not queue.items:
        empty_state("The queue is empty",
                    "Add the sample documents or upload PDFs. Processing starts only when you press Start processing.")
        return

    df = pd.DataFrame([{
        "#": f"{n:02d}", "Document": i.filename, "Size": fmt_bytes(i.size_bytes),
        "Source": "Sample" if i.source == "sample" else "Upload",
        "Validation": i.validation, "Status": "Queued" if i.valid else "Queued (invalid)",
    } for n, i in enumerate(queue.items, 1)])
    event = st.dataframe(df, hide_index=True, on_select="rerun", selection_mode="multi-row", key="queue-table")
    selected = [queue.items[r].item_id for r in (event.selection.rows if event else [])]
    invalid = [i for i in queue.items if not i.valid]
    total = sum(i.size_bytes for i in queue.items)
    st.caption(f"{len(queue.items)} documents · {fmt_bytes(total)} · {len(invalid)} failed validation · "
               f"mode: {MODES[mode][0]}")
    if invalid:
        st.warning(f"{len(invalid)} file(s) failed validation. If you keep them, they are still processed and "
                   "recorded as failed (a person then decides). Select and remove them to skip them.")

    busy = bool(engine.runner and engine.runner.is_running())
    c1, c2, c3, _ = st.columns([1.4, 1.4, 1.1, 3])
    if c1.button("Start processing", type="primary", icon=":material/play_arrow:", disabled=busy):
        try:
            with st.spinner("Saving the documents to storage…"):
                engine.start_batch(list(queue.items), mode)
        except BatchStartError as exc:
            st.error(f"{exc} The queue was kept; try again.")
            return
        queue.clear()
        st.session_state.monitor_live = True
        st.session_state.pop("dismissed_batch", None)
        st.rerun()
    if c2.button(f"Remove selected ({len(selected)})", disabled=not selected):
        queue.remove(selected)
        st.rerun()
    if c3.button("Clear queue"):
        queue.clear()
        st.rerun()
    if busy:
        st.info("Another batch is still running. Start this one when it has finished.")


# ============================================================== monitor pieces

def _stage_position(job: DocumentJob) -> str:
    if job.status == "queued":
        return "Queued: waiting for earlier documents"
    if job.status == "cancelled":
        return "Not processed (batch cancelled)"
    if job.status == "running" and job.current_stage:
        n = STAGE_NAMES.index(job.current_stage) + 1
        return f"Step {n} of {len(STAGE_NAMES)} · {STAGE_LABEL[job.current_stage]}"
    if job.status == "running":
        return "Starting"
    if job.status == "failed" and job.failed_stage:
        return f"Failed at {STAGE_LABEL.get(job.failed_stage, job.failed_stage)} · {fmt_seconds(job.seconds)}"
    return f"Finished in {fmt_seconds(job.seconds)}"


def doc_card(job: DocumentJob, active: bool) -> str:
    segs = "".join(f'<div class="si-seg {job.stages[n].status}" title="{escape(STAGE_LABEL[n])}: '
                   f'{escape(STAGE_ICON[job.stages[n].status][2])}"></div>' for n in STAGE_NAMES)
    right = status_badge(job.status)
    if job.decision and job.status != "running":
        right = decision_badge(job.decision) + " " + right
    sub = job.supplier or ("" if job.valid else f"Validation: {job.validation}")
    return f"""<div class="si-card{' active' if active else ''}">
      <div class="si-card-row"><div><span class="si-meta">#{job.position:02d}</span>
      <span class="si-doc"> {escape(job.filename)}</span>
      <div class="si-meta">{fmt_bytes(job.size_bytes)}{' · ' + escape(sub) if sub else ''}</div></div>
      <div>{right}</div></div>
      <div class="si-track">{segs}</div>
      <div class="si-meta">{escape(_stage_position(job))}</div></div>"""


def steps_list(job: DocumentJob) -> str:
    rows = []
    for name, label, _desc in STAGES:
        s = job.stages[name]
        glyph, cls, status_label = STAGE_ICON[s.status]
        when = s.started_at.strftime("%H:%M:%S") if s.started_at else ""
        dur = fmt_ms(s.duration_ms) if s.status not in ("running", "pending") else (
            "running…" if s.status == "running" else "")
        msg = s.message if s.status not in ("pending",) else ""
        warn = f'<div class="si-step-warn">{escape(explain_error(s.warning) or "")}</div>' if s.warning else ""
        rows.append(f"""<li class="si-step"><span class="si-ico {cls}" title="{status_label}">{glyph}</span>
            <div><div class="si-step-name">{escape(label)}
            <span class="si-meta"> · {status_label}</span></div>
            <div class="si-step-msg">{escape(msg)}</div>{warn}</div>
            <div class="si-step-time">{when}<br>{dur}</div></li>""")
    return f'<ul class="si-steps">{"".join(rows)}</ul>'


def current_details(job: DocumentJob) -> str:
    src = job.extraction_source or ""
    kind = analysis_kind(src)
    provider = {"live": "OpenAI (live call)", "replay": "Recorded OpenAI response (no API call)",
                "regex": "None (regex only)"}.get(kind, "Not started" if job.status in ("queued", "running") else "—")
    elapsed = job.seconds if job.seconds is not None else (
        (datetime.now() - job.started_at).total_seconds() if job.started_at else None)
    rules = job.stages["check_rules"]
    rule_progress = {"pending": "Not started", "running": "Evaluating…"}.get(
        rules.status, rules.message or rules.status.capitalize())
    return kv([
        ("Status", ("html", status_badge(job.status))),
        ("Current stage", STAGE_LABEL.get(job.current_stage, "—") if job.status == "running" else "—"),
        ("Extracted text", f"{job.characters:,} characters" if job.characters is not None else "—"),
        ("Fields verified", f"{job.fields_found} of 24" if job.fields_found is not None else "—"),
        ("Analysis", ("html", analysis_badge(src)) if src else "—"),
        ("AI provider", provider),
        ("Model", load_config()["llm"]["model"] if kind in ("live", "replay") else "—"),
        ("Extraction detail", src or "—"),
        ("Processing time", fmt_seconds(elapsed)),
        ("Rule evaluation", rule_progress),
        ("Decision", ("html", decision_badge(job.decision)) if job.decision else "—"),
    ])


def log_html(snap: BatchState, limit: int = 300) -> str:
    rows = []
    for e in reversed(snap.log[-limit:]):
        if e.status == "running":
            continue  # start events are visible in the step list; the log keeps outcomes
        cls = {"failed": "si-log-failed", "waiting": "si-log-waiting"}.get(e.status, "")
        if e.stage == "batch":
            cls = "si-log-batch"
        who = f"{escape(e.filename)} · " if e.filename and e.stage not in ("document",) else ""
        stage = f"{STAGE_LABEL[e.stage]}: " if e.stage in STAGE_LABEL else ""
        dur = f" ({fmt_ms(e.duration_ms)})" if e.duration_ms is not None else ""
        warn = f' <span class="si-log-waiting">⚠ {escape(e.warning)}</span>' if e.warning else ""
        rows.append(f'<div class="si-log-row"><span class="si-log-ts">{e.timestamp:%H:%M:%S}</span>'
                    f'<span class="{cls}">{who}{escape(stage)}{escape(e.message)}{dur}{warn}</span></div>')
    return f'<div class="si-log">{"".join(rows)}</div>'


# ============================================================== monitor

def _monitor_body() -> None:
    engine = get_engine()
    runner = engine.runner
    snap = runner.snapshot()
    live = runner.is_running()
    if not live and st.session_state.get("monitor_live"):
        st.session_state.monitor_live = False
        st.rerun()  # whole page once, to stop polling and refresh the sidebar

    c = snap.counts()
    finished_docs = c["completed"]
    pct = finished_docs / c["total"] if c["total"] else 0
    title = {"running": "Processing supplier documents", "queued": "Starting batch",
             "finished": "Batch finished", "cancelled": "Batch cancelled"}.get(snap.status, snap.status)
    st.html(f'<div class="si-progress-label"><span><b style="color:#172033">{title}</b> · batch {snap.batch_id}'
            f' · mode {MODES.get(snap.mode, (snap.mode,))[0]}</span><span>{pct:.0%}</span></div>')
    st.progress(pct, text=f"{finished_docs} of {c['total']} documents completed")

    m = st.columns(6)
    m[0].metric("Total documents", c["total"])
    m[1].metric("Completed", c["completed"])
    m[2].metric("Processing now", c["processing"])
    m[3].metric("Waiting in queue", c["waiting"])
    m[4].metric("Failed", c["failed"])
    m[5].metric("Elapsed", fmt_seconds(snap.elapsed_seconds()))

    b1, b2, b3, _ = st.columns([1.5, 1.6, 1.8, 3])
    if live:
        if b1.button("Cancel processing", icon=":material/stop_circle:", disabled=snap.cancel_requested,
                     help="Stops after the document that is running now. A document is never stopped half-way."):
            runner.cancel()
        if snap.cancel_requested:
            b2.caption("Stopping after the current document…")
    else:
        if b1.button("New analysis", type="primary", icon=":material/add:"):
            st.session_state.dismissed_batch = snap.batch_id
            st.rerun()
    if c["needs_review"] and b3.button(f"Open human review ({c['needs_review']})", icon=":material/how_to_reg:"):
        nav.go("review")

    if c["unsaved"]:
        st.warning(f"{c['unsaved']} result(s) could not be saved to the database yet (connection problem). "
                   "They are kept in memory; nothing is lost while the app keeps running.")
        if st.button("Retry saving", key="retry-unsaved"):
            left = runner.retry_unsaved()
            st.toast("All results saved" if not left else f"{left} still not saved")

    running = next((j for j in snap.jobs if j.status == "running"), None)
    last_done = next((j for j in reversed(snap.jobs) if j.status in DONE), None)
    left, right = st.columns([5, 4], gap="medium")
    with left:
        section(f"Documents ({c['total']}) · processed one at a time, in order")
        st.html("".join(doc_card(j, j is running) for j in snap.jobs))
    with right:
        options = ["Follow current document"] + [f"#{j.position:02d} {j.filename}" for j in snap.jobs]
        choice = st.selectbox("Inspect document", options, key="monitor-focus")
        if choice == options[0]:
            focus = running or last_done or snap.jobs[0]
        else:
            focus = snap.jobs[options.index(choice) - 1]
        section(f"#{focus.position:02d} {focus.filename}")
        st.html(current_details(focus))
        st.html(steps_list(focus))
        if focus.error:
            for err in focus.error.split("; "):
                st.warning(explain_error(err))
        section("Live processing log · newest first")
        with st.container(height=320, border=True):
            st.html(log_html(snap))

    done = [j for j in snap.jobs if j.status in DONE]
    section(f"Results so far ({len(done)} of {c['total']})")
    if not done:
        st.caption("The first result appears here as soon as the first document finishes.")
        return
    df = pd.DataFrame([{"#": f"{j.position:02d}", "Document": j.filename, "Supplier": j.supplier or "—",
                        "Analysis": ANALYSIS[analysis_kind(j.extraction_source)][0],
                        "Decision": j.decision or "—", "Human decision": j.human_decision or "—",
                        "Status": j.status.replace("_", " "), "Time": fmt_seconds(j.seconds)} for j in done])
    event = st.dataframe(df, hide_index=True, on_select="rerun", selection_mode="single-row", key="results-table")
    rows = event.selection.rows if event else []
    if rows and st.button(f"Open details for {done[rows[0]].filename}", icon=":material/open_in_new:"):
        nav.open_document(done[rows[0]].document_id)
    elif not rows:
        st.caption("Select a row to open the full details (fields, rules, evidence) — available while the batch runs.")


def render_monitor() -> None:
    runner = get_engine().runner
    live = runner.is_running()
    if live:
        st.session_state.monitor_live = True
    st.fragment(_monitor_body, run_every=1.0 if live else None)()


# ============================================================== page

def render() -> None:
    page_header("Document Processing",
                "Queue supplier PDFs, start the analysis, and follow every document through the pipeline.")
    mode_bar()
    runner = get_engine().runner
    if runner and (runner.is_running() or st.session_state.get("dismissed_batch") != runner.batch.batch_id):
        render_monitor()
    else:
        render_queue()
