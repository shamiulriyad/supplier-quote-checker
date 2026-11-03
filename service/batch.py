"""Document queue, sequential batch runner and human-review submission.

The runner processes documents one by one in a background thread so the UI stays usable
(results of finished documents can be opened while the next one runs). The UI only reads
`runner.snapshot()`; it never touches the graph while a document is being processed.
"""
import copy
import hashlib
import sqlite3
import sys
import threading
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

import pdfplumber
from langgraph.checkpoint.sqlite import SqliteSaver
from langgraph.types import Command

from checker.config import load_config
from checker.graph import build_graph, checkpoint_serde, initial_state, report_node
from checker.models import PackResult

from .progress import (COMPLETED, FAILED, PENDING, RUNNING, SKIPPED, STAGE_LABEL, STAGE_NAMES, WAITING,
                       ProgressEvent, stream_document)
from .store import AlreadyReviewed, HistoryStore

MAX_FILE_MB = 10


# ============================================================== queue

@dataclass
class QueueItem:
    item_id: str
    filename: str
    path: str
    size_bytes: int
    source: str  # sample | upload
    valid: bool
    validation: str
    sha256: str


def validate_pdf(path: str | Path) -> tuple[bool, str]:
    """Pre-flight check shown in the queue. The pipeline's parse step still does its own checks."""
    p = Path(path)
    if not p.exists():
        return False, "File not found"
    size = p.stat().st_size
    if size == 0:
        return False, "Empty file"
    if size > MAX_FILE_MB * 1024 * 1024:
        return False, f"Larger than {MAX_FILE_MB} MB"
    with open(p, "rb") as fh:
        if fh.read(5) != b"%PDF-":
            return False, "Not a PDF file"
    try:
        with pdfplumber.open(p) as pdf:
            pages = len(pdf.pages)
    except Exception:
        return False, "Corrupted PDF (cannot be opened)"
    return True, f"Valid PDF, {pages} page{'s' if pages != 1 else ''}"


class DocumentQueue:
    def __init__(self, upload_dir: str | Path):
        self.upload_dir = Path(upload_dir)
        self.items: list[QueueItem] = []

    def __len__(self) -> int:
        return len(self.items)

    def add_file(self, path: str | Path, filename: str | None = None, source: str = "sample") -> tuple[bool, str]:
        p = Path(path)
        name = filename or p.name
        digest = hashlib.sha256(p.read_bytes()).hexdigest() if p.exists() else uuid.uuid4().hex
        if any(i.sha256 == digest for i in self.items):
            return False, f"{name} is already in the queue"
        valid, message = validate_pdf(p)
        self.items.append(QueueItem(uuid.uuid4().hex[:12], name, str(p), p.stat().st_size if p.exists() else 0,
                                    source, valid, message, digest))
        return True, f"Added {name}"

    def add_upload(self, filename: str, data: bytes) -> tuple[bool, str]:
        self.upload_dir.mkdir(parents=True, exist_ok=True)
        digest = hashlib.sha256(data).hexdigest()
        path = self.upload_dir / f"{digest[:16]}.pdf"
        if not path.exists():
            path.write_bytes(data)
        return self.add_file(path, filename=filename, source="upload")

    def remove(self, item_ids: list[str]) -> int:
        before = len(self.items)
        self.items = [i for i in self.items if i.item_id not in set(item_ids)]
        return before - len(self.items)

    def clear(self) -> None:
        self.items = []


# ============================================================== batch state

@dataclass
class StageState:
    status: str = PENDING
    started_at: datetime | None = None
    duration_ms: float | None = None
    message: str = ""
    warning: str | None = None


@dataclass
class DocumentJob:
    document_id: str
    position: int
    filename: str
    path: str
    size_bytes: int
    valid: bool
    validation: str
    thread_id: str
    status: str = "queued"  # queued | running | completed | waiting_review | reviewed | failed | cancelled
    stages: dict[str, StageState] = field(default_factory=lambda: {n: StageState() for n in STAGE_NAMES})
    current_stage: str | None = None
    decision: str | None = None
    human_decision: str | None = None
    supplier: str | None = None
    failed_stage: str | None = None
    error: str | None = None
    warning: str | None = None
    started_at: datetime | None = None
    finished_at: datetime | None = None
    seconds: float | None = None
    characters: int | None = None
    fields_found: int | None = None
    extraction_source: str | None = None
    rules_checked: int | None = None


@dataclass
class BatchState:
    batch_id: str
    mode: str
    jobs: list[DocumentJob]
    created_at: datetime = field(default_factory=datetime.now)
    status: str = "queued"  # queued | running | finished | cancelled
    started_at: datetime | None = None
    finished_at: datetime | None = None
    cancel_requested: bool = False
    log: list[ProgressEvent] = field(default_factory=list)

    def counts(self) -> dict[str, int]:
        done = ("completed", "waiting_review", "reviewed", "failed")
        return {
            "total": len(self.jobs),
            "completed": sum(j.status in done for j in self.jobs),
            "processing": sum(j.status == "running" for j in self.jobs),
            "waiting": sum(j.status == "queued" for j in self.jobs),
            "failed": sum(j.status == "failed" or j.failed_stage is not None for j in self.jobs),
            "needs_review": sum(j.status == "waiting_review" for j in self.jobs),
            "cancelled": sum(j.status == "cancelled" for j in self.jobs),
        }

    def elapsed_seconds(self) -> float:
        if not self.started_at:
            return 0.0
        return ((self.finished_at or datetime.now()) - self.started_at).total_seconds()


# ============================================================== engine

class ReviewExpired(Exception):
    """The paused pipeline for this document no longer exists, so it cannot be resumed."""


class Engine:
    """One per process: the compiled graph (SQLite checkpoints), the history store and the active runner."""

    def __init__(self, runtime_dir: str | Path | None = None):
        # The worker thread is CPU-bound in offline/replay mode (PDF parsing). A shorter GIL switch interval
        # keeps the web server and UI threads responsive while it runs (Python's "convoy effect").
        sys.setswitchinterval(0.001)
        runtime = Path(runtime_dir or load_config()["runtime_dir"])
        runtime.mkdir(parents=True, exist_ok=True)
        self.runtime_dir = runtime
        self.upload_dir = runtime / "uploads"
        self.store = HistoryStore(runtime / "history.db")
        self.stale_on_start = self.store.mark_stale()
        self._cp_conn = sqlite3.connect(runtime / "checkpoints.db", check_same_thread=False)
        self.graph = build_graph(with_human_review=True,
                                 checkpointer=SqliteSaver(self._cp_conn, serde=checkpoint_serde()))
        self.graph_lock = threading.RLock()
        self.runner: "BatchRunner | None" = None

    def close(self) -> None:
        self._cp_conn.close()

    def new_queue(self) -> DocumentQueue:
        return DocumentQueue(self.upload_dir)

    # -------------------------------------------------------- batches
    def start_batch(self, items: list[QueueItem], mode: str, run_in_thread: bool = True) -> "BatchRunner":
        if self.runner and self.runner.is_running():
            raise RuntimeError("A batch is already running.")
        if not items:
            raise ValueError("The queue is empty.")
        batch_id = datetime.now().strftime("%Y%m%d-%H%M%S-") + uuid.uuid4().hex[:4]
        jobs = [DocumentJob(document_id=f"{batch_id}-{n:02d}", position=n, filename=i.filename, path=i.path,
                            size_bytes=i.size_bytes, valid=i.valid, validation=i.validation,
                            thread_id=uuid.uuid4().hex) for n, i in enumerate(items, 1)]
        self.store.create_batch(batch_id, mode, [
            {"document_id": j.document_id, "position": j.position, "filename": j.filename,
             "file_path": j.path, "size_bytes": j.size_bytes} for j in jobs])
        runner = BatchRunner(self, BatchState(batch_id=batch_id, mode=mode, jobs=jobs))
        self.runner = runner
        if run_in_thread:
            runner.start()
        else:
            runner.run()
        return runner

    # -------------------------------------------------------- human review
    def can_resume(self, document_id: str) -> bool:
        doc = self.store.document(document_id)
        if not doc or not doc.get("thread_id"):
            return False
        with self.graph_lock:
            snap = self.graph.get_state({"configurable": {"thread_id": doc["thread_id"]}})
        return "human_review" in (snap.next or ())

    def submit_review(self, document_id: str, decision: str, comment: str) -> PackResult:
        """Resume the paused graph of one document with a person's decision. Safe against double submits."""
        if decision not in ("APPROVE", "REJECT"):
            raise ValueError("Decision must be APPROVE or REJECT.")
        doc = self.store.document(document_id)
        if doc is None:
            raise KeyError(document_id)
        self.store.claim_review(document_id)  # raises AlreadyReviewed on a second submission
        events: list[ProgressEvent] = []
        try:
            with self.graph_lock:
                cfg = {"configurable": {"thread_id": doc["thread_id"]}}
                if "human_review" not in (self.graph.get_state(cfg).next or ()):
                    raise ReviewExpired("This document's paused pipeline was not found; process it again.")
                outcome = stream_document(self.graph, Command(resume={"decision": decision, "comment": comment}),
                                          cfg, document_id, doc["filename"], events.append)
        except Exception:
            self.store.release_review(document_id)
            raise
        for e in events:
            self.store.add_event(doc["batch_id"], e)
        self.store.save_review(document_id, decision, comment, outcome.result)
        if self.runner:
            self.runner.apply_review(document_id, decision, events)
        return outcome.result


# ============================================================== runner

class BatchRunner:
    def __init__(self, engine: Engine, batch: BatchState):
        self.engine = engine
        self.batch = batch
        self._lock = threading.Lock()
        self._thread: threading.Thread | None = None

    # -------------------------------------------------------- control
    def start(self) -> None:
        self._thread = threading.Thread(target=self.run, name=f"batch-{self.batch.batch_id}", daemon=True)
        self._thread.start()

    def join(self, timeout: float | None = None) -> None:
        if self._thread:
            self._thread.join(timeout)

    def is_running(self) -> bool:
        return self.batch.status in ("queued", "running") and (self._thread is None or self._thread.is_alive())

    def cancel(self) -> None:
        """Stop after the document that is running now. Never stops a document half-way."""
        with self._lock:
            if self.batch.status not in ("queued", "running") or self.batch.cancel_requested:
                return
            self.batch.cancel_requested = True
        self._emit(ProgressEvent("", "", "batch", "info", "Cancel requested: stopping after the current document"))

    def snapshot(self) -> BatchState:
        with self._lock:
            return copy.deepcopy(self.batch)

    # -------------------------------------------------------- events
    def _emit(self, event: ProgressEvent) -> None:
        with self._lock:
            self.batch.log.append(event)
            job = next((j for j in self.batch.jobs if j.document_id == event.document_id), None)
            if job and event.stage in job.stages:
                st = job.stages[event.stage]
                if event.status == RUNNING:
                    st.status, st.started_at, st.message = RUNNING, event.timestamp, event.message
                    job.current_stage = event.stage
                else:
                    st.status, st.duration_ms, st.message, st.warning = (
                        event.status, event.duration_ms, event.message, event.warning)
                    if event.warning:
                        job.warning = event.warning
                    meta = event.metadata
                    if event.stage == "parse" and "characters" in meta:
                        job.characters = meta["characters"]
                    if event.stage == "extract":
                        job.extraction_source = meta.get("source")
                    if event.stage == "verify" and "found" in meta:
                        job.fields_found = meta["found"]
                    if event.stage == "check_rules":
                        job.rules_checked = sum(meta.get(k, 0) for k in ("PASS", "FAIL", "UNKNOWN", "N/A"))
                    if event.stage == "decide":
                        job.decision = meta.get("decision")
        self.engine.store.add_event(self.batch.batch_id, event)
        time.sleep(0.002)  # yield the GIL so the UI can render this event

    def apply_review(self, document_id: str, decision: str, events: list[ProgressEvent]) -> None:
        with self._lock:
            job = next((j for j in self.batch.jobs if j.document_id == document_id), None)
            if job is None:
                return
            job.status, job.human_decision = "reviewed", decision
            for e in events:
                self.batch.log.append(e)
                if e.stage in job.stages and e.status != RUNNING:
                    job.stages[e.stage] = StageState(e.status, e.timestamp, e.duration_ms, e.message, e.warning)

    def _set(self, job: DocumentJob, **values) -> None:
        with self._lock:
            for k, v in values.items():
                setattr(job, k, v)

    # -------------------------------------------------------- work
    def run(self) -> None:
        b, store = self.batch, self.engine.store
        with self._lock:
            b.status, b.started_at = "running", datetime.now()
        store.update_batch(b.batch_id, status="running", started_at=b.started_at.isoformat(timespec="seconds"))
        self._emit(ProgressEvent("", "", "batch", "info",
                                 f"Batch processing started: {len(b.jobs)} documents, mode '{b.mode}'"))
        try:
            for job in b.jobs:
                if b.cancel_requested:
                    self._cancel_remaining()
                    break
                self._process(job)
        finally:
            c = b.counts()
            with self._lock:
                b.status = "cancelled" if b.cancel_requested else "finished"
                b.finished_at = datetime.now()
            store.update_batch(b.batch_id, status=b.status, finished_at=b.finished_at.isoformat(timespec="seconds"))
            self._emit(ProgressEvent("", "", "batch", "info",
                                     f"Batch {b.status}: {c['completed']} of {c['total']} documents processed, "
                                     f"{c['needs_review']} waiting for a person, {c['failed']} with errors"))

    def _cancel_remaining(self) -> None:
        for job in self.batch.jobs:
            if job.status == "queued":
                self._set(job, status="cancelled")
                self.engine.store.update_document(job.document_id, status="cancelled")
                self._emit(ProgressEvent(job.document_id, job.filename, "document", "skipped",
                                         "Not processed (batch cancelled)"))

    def _process(self, job: DocumentJob) -> None:
        store, b = self.engine.store, self.batch
        t0 = time.perf_counter()
        self._set(job, status="running", started_at=datetime.now(), current_stage=None)
        store.update_document(job.document_id, status="running", thread_id=job.thread_id)
        self._emit(ProgressEvent(job.document_id, job.filename, "document", "info",
                                 f"{job.filename} selected ({job.position} of {len(b.jobs)})"))
        cfg = {"configurable": {"thread_id": job.thread_id}}
        state = initial_state(job.path, pack_id=f"{job.position:02d}", source_name=job.filename,
                              mode=b.mode, ask_human=True)
        try:
            with self.engine.graph_lock:
                outcome = stream_document(self.engine.graph, state, cfg, job.document_id, job.filename, self._emit)
                values = self.engine.graph.get_state(cfg).values if outcome.interrupted else None
        except Exception as exc:  # the graph itself failed: record it and move on to the next document
            secs = round(time.perf_counter() - t0, 2)
            self._set(job, status="failed", error=str(exc), finished_at=datetime.now(), seconds=secs,
                      failed_stage=job.current_stage or "pipeline")
            store.update_document(job.document_id, status="failed", error=str(exc), seconds=secs,
                                  failed_stage=job.failed_stage, processed_at=datetime.now().isoformat("T", "seconds"))
            self._emit(ProgressEvent(job.document_id, job.filename, "document", FAILED,
                                     f"Processing failed: {exc}. Continuing with the next document."))
            return

        secs = round(time.perf_counter() - t0, 2)
        result = outcome.result
        if outcome.interrupted:  # the report node has not run yet; build the system's result from the state
            result = report_node(values)["result"]
        result = result.model_copy(update={"seconds": secs})
        status = "waiting_review" if outcome.interrupted else ("failed" if outcome.failed_stage else "completed")
        if outcome.decision != "NEEDS_REVIEW":
            self._emit(ProgressEvent(job.document_id, job.filename, "human_review", SKIPPED,
                                     f"Not needed (decision: {outcome.decision})"))
        self._set(job, status=status, decision=result.decision, supplier=result.supplier, seconds=secs,
                  finished_at=datetime.now(), failed_stage=outcome.failed_stage, current_stage=None,
                  error="; ".join(result.errors) or None)
        store.save_result(job.document_id, result, status, outcome.raw_text, outcome.failed_stage)
        verdict = {"waiting_review": "waiting for a person", "failed": "failed"}.get(status, "completed")
        self._emit(ProgressEvent(job.document_id, job.filename, "document",
                                 WAITING if outcome.interrupted else (FAILED if status == "failed" else COMPLETED),
                                 f"Decision: {result.decision} ({verdict}) in {secs:.1f}s",
                                 metadata={"decision": result.decision}))


__all__ = ["AlreadyReviewed", "BatchRunner", "BatchState", "DocumentJob", "DocumentQueue", "Engine",
           "QueueItem", "ReviewExpired", "StageState", "validate_pdf", "STAGE_LABEL"]
