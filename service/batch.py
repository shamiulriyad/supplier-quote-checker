"""Document queue, sequential batch runner, human-review submission and history maintenance.

The runner processes documents one by one in a background thread so the UI stays usable
(results of finished documents can be opened while the next one runs). The UI only reads
`runner.snapshot()`; it never touches the graph while a document is being processed.

Persistence goes through three parts chosen in service.backend: the history store (PostgreSQL or
SQLite), the file store (Supabase Storage or a local folder) and the LangGraph checkpointer.
"""
import copy
import hashlib
import sys
import threading
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

import pdfplumber
from langgraph.types import Command

from checker import extract_llm
from checker.config import load_config
from checker.graph import build_graph, initial_state, report_node
from checker.models import PackResult

from .backend import BackendSettings, DbLLMCache, build_checkpointer, build_database, build_files, resolve_settings
from .files import FileStore, StorageUnavailable, is_managed_path, storage_path_for
from .progress import (COMPLETED, FAILED, PENDING, RUNNING, SKIPPED, STAGE_LABEL, STAGE_NAMES, WAITING,
                       ProgressEvent, stream_document)
from .store import AlreadyReviewed, HistoryStore

MAX_FILE_MB = 10


# ============================================================== queue

@dataclass
class QueueItem:
    item_id: str
    filename: str
    path: str  # local staging copy, used until the batch starts
    size_bytes: int
    source: str  # sample | upload | requeue
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


def safe_filename(name: str) -> str:
    """Keep only the file name (no folders), printable, at most 200 characters."""
    name = Path(str(name).replace("\\", "/")).name
    name = "".join(ch for ch in name if ch.isprintable()).strip() or "document.pdf"
    return name[:200]


class DocumentQueue:
    def __init__(self, upload_dir: str | Path):
        self.upload_dir = Path(upload_dir)  # local staging only; permanent copies go to the file store
        self.items: list[QueueItem] = []

    def __len__(self) -> int:
        return len(self.items)

    def add_file(self, path: str | Path, filename: str | None = None, source: str = "sample") -> tuple[bool, str]:
        p = Path(path)
        name = safe_filename(filename or p.name)
        if not p.exists():
            return False, f"{name}: file not found"
        return self._add(p, name, source, hashlib.sha256(p.read_bytes()).hexdigest())

    def _add(self, p: Path, name: str, source: str, digest: str) -> tuple[bool, str]:
        if any(i.sha256 == digest for i in self.items):
            return False, f"{name} is already in the queue"
        valid, message = validate_pdf(p)
        self.items.append(QueueItem(uuid.uuid4().hex[:12], name, str(p), p.stat().st_size, source, valid,
                                    message, digest))
        return True, f"Added {name}"

    def add_upload(self, filename: str, data: bytes, source: str = "upload") -> tuple[bool, str]:
        name = safe_filename(filename)
        if not isinstance(data, (bytes, bytearray)) or not data:
            return False, f"{name}: empty upload"
        if len(data) > MAX_FILE_MB * 1024 * 1024:
            return False, f"{name}: larger than {MAX_FILE_MB} MB"
        self.upload_dir.mkdir(parents=True, exist_ok=True)
        digest = hashlib.sha256(data).hexdigest()
        path = self.upload_dir / f"{digest}.pdf"  # file name never comes from the user
        if not path.exists():
            path.write_bytes(data)
        return self._add(path, name, source, digest)  # the bytes are already hashed: no second read

    def remove(self, item_ids: list[str]) -> int:
        before, drop = len(self.items), set(item_ids)
        self.items = [i for i in self.items if i.item_id not in drop]
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
    file_hash: str | None = None
    storage_path: str | None = None
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
    unsaved: bool = False  # result kept in memory because the database could not be reached


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
            "unsaved": sum(j.unsaved for j in self.jobs),
        }

    def elapsed_seconds(self) -> float:
        if not self.started_at:
            return 0.0
        return ((self.finished_at or datetime.now()) - self.started_at).total_seconds()


def steps_of(job: DocumentJob) -> dict:
    """Per-step status for pipeline_checkpoints."""
    out = {}
    for name, s in job.stages.items():
        out[name] = {"status": s.status, "data": {
            "message": s.message, "duration_ms": s.duration_ms, "warning": s.warning, "thread_id": job.thread_id,
            "started_at": s.started_at.astimezone().isoformat(timespec="seconds") if s.started_at else None}}
    return out


# ============================================================== engine

class ReviewExpired(Exception):
    """The paused pipeline for this document no longer exists, so it cannot be resumed."""


class BatchStartError(Exception):
    """The batch could not be saved; nothing was processed and the queue is unchanged."""


@dataclass
class ClearReport:
    documents_deleted: int = 0
    files_deleted: int = 0
    files_failed: list[str] = field(default_factory=list)
    checkpoints_deleted: int = 0
    checkpoints_failed: int = 0
    cache_entries_deleted: int = 0
    documents_kept: int = 0  # rows kept (marked 'deleting') because their file could not be deleted

    @property
    def complete(self) -> bool:
        return not self.files_failed and not self.checkpoints_failed and not self.documents_kept


class Engine:
    """One per process: storage backends, the compiled graph and the active runner."""

    def __init__(self, runtime_dir: str | Path | None = None, *, settings: BackendSettings | None = None,
                 db=None, files: FileStore | None = None, checkpointer=None):
        # The worker thread is CPU-bound in offline/replay mode (PDF parsing). A shorter GIL switch interval
        # keeps the web server and UI threads responsive while it runs (Python's "convoy effect").
        sys.setswitchinterval(0.001)
        cfg = load_config()
        runtime = Path(runtime_dir or cfg["runtime_dir"])
        runtime.mkdir(parents=True, exist_ok=True)
        self.runtime_dir = runtime
        self.upload_dir = runtime / "uploads"
        self.cache_dir = cfg["cache_dir"]
        self.settings = settings or (BackendSettings("local") if db is not None else resolve_settings())
        self._closers = []

        db = db or build_database(self.settings, runtime)
        self._closers.append(db.close)
        self.store = HistoryStore(db)
        self.files = files or build_files(self.settings, runtime)
        if checkpointer is None:
            checkpointer, closer = build_checkpointer(self.settings, runtime)
            self._closers.append(closer)
        self.checkpointer = checkpointer
        self.graph = build_graph(with_human_review=True, checkpointer=checkpointer)
        self.graph_lock = threading.RLock()
        # Supabase: LLM answers in the llm_cache table; local: JSON files in cache/llm (not in git)
        extract_llm.set_cache_backend(DbLLMCache(self.store) if self.settings.kind == "supabase" else None)
        self.stale_on_start = self.store.mark_stale()
        self.legacy_history = (runtime / "history.db").exists()  # pre-Supabase local history, importable
        self.runner: "BatchRunner | None" = None

    @property
    def persistent(self) -> bool:
        return self.settings.kind == "supabase"

    def close(self) -> None:
        for close in self._closers:
            try:
                close()
            except Exception:
                pass

    def new_queue(self) -> DocumentQueue:
        return DocumentQueue(self.upload_dir)

    def status(self) -> dict:
        db_ok, db_msg = self.store.db.check()
        files_ok, files_msg = self.files.check()
        return {"backend": self.settings.label, "persistent": self.persistent, "database": (db_ok, db_msg),
                "files": (files_ok, files_msg), "pending_deletion": self.store.pending_deletion()}

    # -------------------------------------------------------- batches
    def start_batch(self, items: list[QueueItem], mode: str, run_in_thread: bool = True) -> "BatchRunner":
        """Store the PDFs, then record the batch. Either both succeed or nothing is left behind."""
        if self.runner and self.runner.is_running():
            raise RuntimeError("A batch is already running.")
        if not items:
            raise ValueError("The queue is empty.")
        batch_id = datetime.now().strftime("%Y%m%d-%H%M%S-") + uuid.uuid4().hex[:4]
        jobs, new_uploads = [], []
        try:
            for n, item in enumerate(items, 1):
                data = Path(item.path).read_bytes()
                path = storage_path_for(item.sha256)
                if self.files.put(path, data, item.filename):
                    new_uploads.append(path)
                jobs.append(DocumentJob(document_id=str(uuid.uuid4()), position=n, filename=item.filename,
                                        path=item.path, size_bytes=item.size_bytes, valid=item.valid,
                                        validation=item.validation, thread_id=uuid.uuid4().hex,
                                        file_hash=item.sha256, storage_path=path))
            self.store.create_batch(batch_id, mode, [
                {"document_id": j.document_id, "position": j.position, "filename": j.filename,
                 "size_bytes": j.size_bytes, "file_hash": j.file_hash, "storage_path": j.storage_path,
                 "thread_id": j.thread_id}
                for j in jobs])
        except Exception as exc:
            # Compensation: files uploaded in THIS attempt did not exist before, so no record can use them.
            leftover = self.files.delete(new_uploads) if new_uploads else []
            note = f" {len(leftover)} uploaded file(s) could not be removed again." if leftover else ""
            raise BatchStartError(f"The batch could not be saved, so nothing was processed: {exc}.{note}") from exc

        runner = BatchRunner(self, BatchState(batch_id=batch_id, mode=mode, jobs=jobs))
        self.runner = runner
        if run_in_thread:
            runner.start()
        else:
            runner.run()
        return runner

    def requeue(self, document_ids: list[str], queue: DocumentQueue) -> list[str]:
        """Put interrupted (or any stored) documents back in the queue, from the permanent file store."""
        messages = []
        for doc_id in document_ids:
            doc = self.store.document(doc_id)
            if not doc or not doc.get("storage_path"):
                messages.append(f"{doc_id}: no stored file")
                continue
            try:
                data = self.files.get(doc["storage_path"])
            except Exception as exc:
                messages.append(f"{doc['filename']}: could not download ({exc})")
                continue
            added, message = queue.add_upload(doc["filename"], data, source="requeue")
            messages.append(message)
            if added and doc["status"] == "interrupted":
                self.store.update_document(doc_id, status="cancelled", error="Re-queued into a new batch")
        return messages

    def read_pdf(self, doc: dict) -> bytes:
        """Original PDF bytes for preview: from the file store (server side, never a public URL)."""
        if not doc.get("storage_path"):
            raise FileNotFoundError("No stored file for this document.")
        return self.files.get(doc["storage_path"])

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
                                          cfg, document_id, doc["filename"], events.append, durability="exit")
        except Exception:
            self.store.release_review(document_id)
            raise
        steps = {e.stage: {"status": e.status, "data": {"message": e.message, "duration_ms": e.duration_ms}}
                 for e in events if e.stage in STAGE_NAMES and e.status != RUNNING}
        self.store.save_review(document_id, decision, comment, outcome.result, batch_id=doc["batch_id"],
                               events=events, steps=steps)
        if self.runner:
            self.runner.apply_review(document_id, decision, events)
        return outcome.result

    # -------------------------------------------------------- Clear History
    def clear_history(self) -> ClearReport:
        """Delete every document with its events, step checkpoints, graph state, PDF and cached LLM answer.

        Order: mark rows 'deleting' (hidden) -> delete files -> delete graph threads and cache entries ->
        delete rows. If a file cannot be deleted, its rows stay marked so the next Clear History retries.
        """
        if self.runner and self.runner.is_running():
            raise RuntimeError("Wait for the running batch to finish before clearing history.")
        report = ClearReport()
        docs = self.store.deletion_candidates()
        ids = [d["id"] for d in docs]
        self.store.mark_deleting(ids)

        still_used = self.store.storage_paths_still_used(excluding=ids)
        paths = sorted({d["storage_path"] for d in docs if is_managed_path(d["storage_path"])} - still_used)
        report.files_failed = self.files.delete(paths)
        report.files_deleted = len(paths) - len(report.files_failed)

        for thread_id in {d["thread_id"] for d in docs if d.get("thread_id")}:
            try:
                with self.graph_lock:
                    self.checkpointer.delete_thread(thread_id)
                report.checkpoints_deleted += 1
            except Exception:
                report.checkpoints_failed += 1

        keys = {extract_llm.cache_key(d["extracted_text"]) for d in docs if d.get("extracted_text")}
        report.cache_entries_deleted = extract_llm.cache_backend(self.cache_dir).delete(sorted(keys))

        failed = set(report.files_failed)
        delete_ids = [d["id"] for d in docs if d["storage_path"] not in failed]
        report.documents_kept = len(ids) - len(delete_ids)
        self.store.delete_documents(delete_ids)
        report.documents_deleted = len(delete_ids)
        if self.runner and not self.runner.is_running():
            self.runner = None
        return report


# ============================================================== runner

class BatchRunner:
    def __init__(self, engine: Engine, batch: BatchState):
        self.engine = engine
        self.batch = batch
        self._lock = threading.Lock()
        self._thread: threading.Thread | None = None
        self._buffers: dict[str, list[ProgressEvent]] = {}  # per-document events, saved with the result
        self._unsaved: dict[str, dict] = {}  # results the database did not accept yet

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
            if event.document_id:
                self._buffers.setdefault(event.document_id, []).append(event)
        if not event.document_id:  # batch-level events are rare: save them right away
            try:
                self.engine.store.add_event(self.batch.batch_id, event)
            except Exception:
                pass  # still visible in the live log; the batch row records the outcome
        time.sleep(0.002)  # yield the GIL so the UI can render this event

    def _take_events(self, document_id: str) -> list[ProgressEvent]:
        with self._lock:
            return self._buffers.pop(document_id, [])

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

    # -------------------------------------------------------- saving
    def _save(self, job: DocumentJob, **payload) -> None:
        """Save a document's outcome; if the database is unreachable keep it for retry_unsaved()."""
        try:
            self.engine.store.save_result(job.document_id, batch_id=self.batch.batch_id, **payload)
            self._set(job, unsaved=False)
            self._unsaved.pop(job.document_id, None)
        except Exception as exc:
            self._unsaved[job.document_id] = payload
            self._set(job, unsaved=True)
            with self._lock:
                self.batch.log.append(ProgressEvent(job.document_id, job.filename, "document", FAILED,
                                                    f"Result could not be saved yet ({exc}); kept in memory"))

    def retry_unsaved(self) -> int:
        """Try again to save results the database refused earlier. Returns how many are still unsaved."""
        for job in [j for j in self.batch.jobs if j.document_id in self._unsaved]:
            self._save(job, **self._unsaved[job.document_id])
        return len(self._unsaved)

    # -------------------------------------------------------- work
    def run(self) -> None:
        b, store = self.batch, self.engine.store
        with self._lock:
            b.status, b.started_at = "running", datetime.now()
        try:
            store.update_batch(b.batch_id, status="running", started_at=b.started_at.astimezone().isoformat())
        except Exception:
            pass
        self._emit(ProgressEvent("", "", "batch", "info",
                                 f"Batch processing started: {len(b.jobs)} documents, mode '{b.mode}'"))
        try:
            for job in b.jobs:
                if b.cancel_requested:
                    self._cancel_remaining()
                    break
                self._process(job)
        finally:
            if self._unsaved:
                self.retry_unsaved()
            c = b.counts()
            with self._lock:
                b.status = "cancelled" if b.cancel_requested else "finished"
                b.finished_at = datetime.now()
            try:
                store.update_batch(b.batch_id, status=b.status, finished_at=b.finished_at.astimezone().isoformat())
            except Exception:
                pass
            self._emit(ProgressEvent("", "", "batch", "info",
                                     f"Batch {b.status}: {c['completed']} of {c['total']} documents processed, "
                                     f"{c['needs_review']} waiting for a person, {c['failed']} with errors"
                                     + (f", {c['unsaved']} not saved yet" if c["unsaved"] else "")))

    def _cancel_remaining(self) -> None:
        for job in self.batch.jobs:
            if job.status == "queued":
                self._set(job, status="cancelled")
                self._emit(ProgressEvent(job.document_id, job.filename, "document", "skipped",
                                         "Not processed (batch cancelled)"))
                try:
                    self.engine.store.update_document(job.document_id, status="cancelled")
                    self.engine.store.add_events(self.batch.batch_id, self._take_events(job.document_id))
                except Exception:
                    pass

    def _process(self, job: DocumentJob) -> None:
        store, b = self.engine.store, self.batch
        t0 = time.perf_counter()
        # 'running' lives in memory only (the monitor reads it); the database keeps 'queued' until the result
        # is saved, which a restart also reports as interrupted. Saves a round trip per document.
        self._set(job, status="running", started_at=datetime.now(), current_stage=None)
        self._emit(ProgressEvent(job.document_id, job.filename, "document", "info",
                                 f"{job.filename} selected ({job.position} of {len(b.jobs)})"))
        cfg = {"configurable": {"thread_id": job.thread_id}}
        path = job.path
        if not Path(path).exists():  # staging copy gone (e.g. after a restart): use the permanent copy
            path = str(self.engine.upload_dir / f"{job.file_hash}.pdf")
            Path(path).parent.mkdir(parents=True, exist_ok=True)
            Path(path).write_bytes(self.engine.files.get(job.storage_path))
        state = initial_state(path, pack_id=f"{job.position:02d}", source_name=job.filename,
                              mode=b.mode, ask_human=True)
        try:
            with self.engine.graph_lock:
                outcome = stream_document(self.engine.graph, state, cfg, job.document_id, job.filename,
                                          self._emit, durability="exit")
                values = self.engine.graph.get_state(cfg).values if outcome.interrupted else None
        except Exception as exc:  # the graph itself failed: record it and move on to the next document
            secs = round(time.perf_counter() - t0, 2)
            self._set(job, status="failed", error=str(exc), finished_at=datetime.now(), seconds=secs,
                      failed_stage=job.current_stage or "pipeline")
            self._emit(ProgressEvent(job.document_id, job.filename, "document", FAILED,
                                     f"Processing failed: {exc}. Continuing with the next document."))
            try:
                store.update_document(job.document_id, status="failed", error=str(exc), seconds=secs,
                                      failed_stage=job.failed_stage, processed_at=datetime.now().astimezone().isoformat())
                store.add_events(b.batch_id, self._take_events(job.document_id))
                store.save_steps(job.document_id, steps_of(job))
            except Exception:
                pass
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
        verdict = {"waiting_review": "waiting for a person", "failed": "failed"}.get(status, "completed")
        self._emit(ProgressEvent(job.document_id, job.filename, "document",
                                 WAITING if outcome.interrupted else (FAILED if status == "failed" else COMPLETED),
                                 f"Decision: {result.decision} ({verdict}) in {secs:.1f}s",
                                 metadata={"decision": result.decision}))
        self._save(job, result=result, status=status, raw_text=outcome.raw_text, failed_stage=outcome.failed_stage,
                   events=self._take_events(job.document_id), steps=steps_of(job))


__all__ = ["AlreadyReviewed", "BatchRunner", "BatchStartError", "BatchState", "ClearReport", "DocumentJob",
           "DocumentQueue", "Engine", "QueueItem", "ReviewExpired", "StageState", "StorageUnavailable",
           "validate_pdf", "STAGE_LABEL"]
