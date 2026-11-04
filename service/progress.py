"""Real progress events, taken from LangGraph's own task stream.

`graph.stream(..., stream_mode="tasks")` yields one event when a node starts and one when it
finishes (with its output, error and interrupts). Each UI stage is exactly one LangGraph node,
so nothing shown in the monitor is simulated.
"""
import time
from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Callable

# (node name, label, what it does). Order = pipeline order.
STAGES: list[tuple[str, str, str]] = [
    ("parse", "Read & validate PDF", "Open the PDF and extract its text"),
    ("extract", "Extract fields", "Regex extractor, plus OpenAI in live mode (recorded responses only in replay testing mode)"),
    ("verify", "Cross-validate", "Quotes must exist in the PDF; OpenAI and regex must agree"),
    ("match_pr", "Match purchase request", "Find the purchase-request line for the item"),
    ("check_math", "Re-check arithmetic", "Line amount, subtotal, VAT and total"),
    ("check_rules", "Evaluate company rules", "R1-R5 reject rules, N1-N7 and P1 review rules"),
    ("decide", "Decide", "Reject > Needs a person > Approve"),
    ("human_review", "Human review", "Only when the decision is 'Needs a person'"),
    ("report", "Report", "Final result with reasons and evidence"),
]
STAGE_NAMES = [s[0] for s in STAGES]
STAGE_LABEL = {s[0]: s[1] for s in STAGES}

# stage statuses
PENDING, RUNNING, COMPLETED, FAILED, WAITING, SKIPPED = (
    "pending", "running", "completed", "failed", "waiting", "skipped")


@dataclass
class ProgressEvent:
    document_id: str
    filename: str
    stage: str  # a node name, or "batch" / "document" for queue-level events
    status: str  # running | completed | failed | waiting | skipped | info
    message: str
    timestamp: datetime = field(default_factory=datetime.now)
    duration_ms: float | None = None
    metadata: dict[str, Any] = field(default_factory=dict)
    warning: str | None = None


def _count_status(extracted) -> Counter:
    return Counter(f.status for f in extracted.fields.values()) if extracted else Counter()


def describe_output(stage: str, out: dict) -> tuple[str, str, dict, str | None]:
    """Turn a node's real output into (status, message, metadata, warning)."""
    errors = out.get("errors") or []
    trace = out.get("trace") or []
    if out.get("fatal"):
        return FAILED, errors[0] if errors else "Step failed", {}, None
    if trace and all("skipped" in t for t in trace):
        return SKIPPED, "Skipped because an earlier step failed", {}, None

    warning = errors[0] if errors else None
    if stage == "parse":
        text = out.get("raw_text")
        if text is None:
            return COMPLETED, "Text supplied directly", {}, warning
        return COMPLETED, f"Extracted {len(text):,} characters", {"characters": len(text),
                                                                  "lines": text.count("\n") + 1}, warning
    if stage == "extract":
        rx = _count_status(out.get("regex"))
        llm = _count_status(out.get("llm")) if out.get("llm") else None
        meta = {"source": out.get("extraction_source"), "regex_found": rx["FOUND"]}
        msg = f"{out.get('extraction_source')}: regex found {rx['FOUND']} fields"
        if llm is not None:
            meta["llm_found"] = llm["FOUND"]
            msg += f", OpenAI found {llm['FOUND']}"
        return COMPLETED, msg, meta, warning
    if stage == "verify":
        c = _count_status(out.get("extracted"))
        meta = {"found": c["FOUND"], "unclear": c["UNCLEAR"], "not_found": c["NOT_FOUND"]}
        return COMPLETED, (f"{c['FOUND']} fields verified, {c['UNCLEAR']} unclear, "
                           f"{c['NOT_FOUND']} not in document"), meta, warning
    if stage == "match_pr":
        r, pr = out.get("pr_match"), out.get("pr")
        item = f"{pr['pr_number']} item {pr['item']}" if pr else "no purchase-request line"
        return COMPLETED, f"Matched {item} ({r.outcome if r else '?'})", {"outcome": r.outcome if r else None}, warning
    if stage == "check_math":
        c = Counter(m.outcome for m in out.get("math_results", []))
        return COMPLETED, f"{c['PASS']} checks pass, {c['FAIL']} fail, {c['UNKNOWN']} unknown", dict(c), warning
    if stage == "check_rules":
        results = out.get("rule_results", [])
        c = Counter(r.outcome for r in results)
        failed = [r.rule_id for r in results if r.outcome == "FAIL"]
        msg = f"{len(results)} rules: {c['FAIL']} failed, {c['UNKNOWN']} unknown"
        return COMPLETED, msg + (f" ({', '.join(failed)})" if failed else ""), {"failed": failed, **dict(c)}, warning
    if stage == "decide":
        return COMPLETED, f"Decision: {out.get('decision')}", {"decision": out.get("decision")}, warning
    if stage == "human_review":
        h = out.get("human_decision") or {}
        return COMPLETED, f"Person decided {h.get('decision')}", {"human_decision": h.get("decision")}, warning
    if stage == "report":
        r = out.get("result")
        return COMPLETED, "Report ready", {"seconds": r.seconds if r else None}, warning
    return COMPLETED, "Done", {}, warning


@dataclass
class StreamOutcome:
    interrupted: bool = False
    result: Any = None  # PackResult when the graph reached `report`
    decision: str | None = None
    raw_text: str | None = None
    failed_stage: str | None = None


def stream_document(graph, payload, config: dict, document_id: str, filename: str,
                    emit: Callable[[ProgressEvent], None], durability: str | None = None) -> StreamOutcome:
    """Run (or resume) one document through the graph, emitting an event per node start/finish.

    durability="exit" saves the graph state once, when the run ends or pauses for review, instead of
    after every node: one database round trip per document on Supabase instead of ten.
    """
    outcome = StreamOutcome()
    started: dict[str, float] = {}
    extra = {"durability": durability} if durability else {}
    for chunk in graph.stream(payload, config, stream_mode="tasks", **extra):
        name = chunk["name"]
        if "result" not in chunk:  # task started
            started[chunk["id"]] = time.perf_counter()
            emit(ProgressEvent(document_id, filename, name, RUNNING, f"{STAGE_LABEL.get(name, name)} started"))
            continue

        ms = (time.perf_counter() - started.pop(chunk["id"], time.perf_counter())) * 1000
        if chunk.get("interrupts"):
            outcome.interrupted = True
            emit(ProgressEvent(document_id, filename, name, WAITING,
                               "Paused: waiting for a person to approve or reject", duration_ms=ms))
            continue
        if chunk.get("error"):
            outcome.failed_stage = name
            emit(ProgressEvent(document_id, filename, name, FAILED, f"Error: {chunk['error']}", duration_ms=ms))
            continue

        out = chunk["result"] or {}
        status, message, meta, warning = describe_output(name, out)
        if status == FAILED and outcome.failed_stage is None:
            outcome.failed_stage = name
        if "raw_text" in out:
            outcome.raw_text = out["raw_text"]
        if "decision" in out:
            outcome.decision = out["decision"]
        if "result" in out:
            outcome.result = out["result"]
        emit(ProgressEvent(document_id, filename, name, status, message, duration_ms=ms,
                           metadata=meta, warning=warning))
    return outcome
