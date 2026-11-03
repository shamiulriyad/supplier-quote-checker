"""The fixed pipeline, wired with LangGraph.

parse -> extract -> verify -> match_pr -> check_math -> check_rules -> decide -> [human_review] -> report

Every step does one job and writes its result into the shared state. If a step crashes the
error is recorded, the remaining steps are skipped and the pack goes to a person.
"""
import operator
from decimal import Decimal
import os
import time
from functools import lru_cache, wraps
from pathlib import Path
from typing import Annotated, Any, TypedDict

from langgraph.checkpoint.memory import InMemorySaver
from langgraph.checkpoint.serde.jsonplus import JsonPlusSerializer
from langgraph.graph import END, START, StateGraph
from langgraph.types import interrupt

from . import extract_llm
from .config import load_config
from .decide import decide
from .extract_regex import extract_regex
from .match import load_purchase_request, match_pr
from .math_check import check_math
from .models import ExtractedPack, PackResult, RuleResult
from .parse import DocumentError, read_pdf_pages
from .rules import check_rules
from .currency import convert_to_base
from .verification import verification_checks
from .verify import merge

# How each document was read. The first words are what the UI shows; the part in brackets is the detail.
LIVE = "Live AI Analysis"
REGEX_ONLY = "AI unavailable — Regex-only analysis"
REPLAY = "Recorded AI Response — Not Live"
SOURCE_LABELS = {
    "live": f"{LIVE} (OpenAI + regex cross-check)",
    "replay": f"{REPLAY} (recorded test fixture + regex cross-check)",
    "regex": f"{REGEX_ONLY} (offline mode selected)",
    "auto_no_key": f"{REGEX_ONLY} (no OpenAI API key configured)",
    "live_failed": f"{REGEX_ONLY} (the OpenAI call failed)",
    "replay_missing": f"{REGEX_ONLY} (no recorded response for this document)",
}


def analysis_kind(source: str | None) -> str:
    """'live' | 'replay' | 'regex' | 'unknown' from an extraction_source (also understands pre-2.1 labels)."""
    s = source or ""
    if s.startswith(LIVE) or s.startswith("OpenAI + regex"):
        return "live"
    if s.startswith(REPLAY) or s.startswith("saved OpenAI"):
        return "replay"
    if s.startswith(REGEX_ONLY) or s.startswith("regex only"):
        return "regex"
    return "unknown"


class PackState(TypedDict, total=False):
    # input
    pack_id: str
    pdf_path: str
    source_name: str
    raw_text: str
    pages: list[str] | None
    mode: str
    ask_human: bool
    # produced by the steps
    regex: ExtractedPack
    llm: ExtractedPack | None
    extraction_source: str
    extracted: ExtractedPack
    pr: dict | None
    conversion: dict | None
    pr_match: RuleResult
    math_results: list[RuleResult]
    rule_results: list[RuleResult]
    decision: str
    reasons: list[RuleResult]
    human_decision: dict | None
    result: PackResult
    fatal: bool
    errors: Annotated[list[str], operator.add]
    trace: Annotated[list[str], operator.add]
    started: float


@lru_cache
def _purchase_request(path: str) -> dict:
    return load_purchase_request(path)


def step(name: str, always_run: bool = False):
    """Wrap a node: skip after a fatal error, catch exceptions, record timing."""
    def deco(fn):
        @wraps(fn)
        def node(state: PackState) -> dict:
            if state.get("fatal") and not always_run:
                return {"trace": [f"{name}: skipped (earlier step failed)"]}
            t0 = time.perf_counter()
            try:
                out = fn(state) or {}
            except Exception as exc:  # never let one bad document crash the run
                out = {"errors": [f"{name} step failed: {exc}"], "fatal": True}
            out["trace"] = out.get("trace", []) + [f"{name}: {(time.perf_counter() - t0) * 1000:.0f} ms"]
            return out
        return node
    return deco


@step("parse")
def parse_node(state: PackState) -> dict:
    if "raw_text" in state:  # text given directly (tests)
        if not state["raw_text"].strip():
            raise DocumentError("Document has no readable text.")
        return {"pages": None}  # no PDF, so no page numbers
    pages = read_pdf_pages(state["pdf_path"])
    return {"raw_text": "\n".join(pages), "pages": pages}


def resolve_mode(requested: str) -> str:
    """Auto = live AI when an OpenAI key is configured, otherwise regex only.

    Auto NEVER uses recorded responses: replay runs only when "replay" is requested explicitly
    (a developer / testing option).
    """
    if requested != "auto":
        return requested
    return "live" if os.environ.get("OPENAI_API_KEY") else "regex"


@step("extract")
def extract_node(state: PackState) -> dict:
    cfg = load_config()
    text = state["raw_text"]
    requested = state.get("mode") or cfg["mode"]
    mode = resolve_mode(requested)
    label = "auto_no_key" if requested == "auto" and mode == "regex" else mode
    out: dict[str, Any] = {"regex": extract_regex(text), "llm": None, "extraction_source": SOURCE_LABELS[label]}
    if mode in ("live", "replay"):
        try:
            if mode == "live":
                response = extract_llm.call_openai(text, cfg["llm"])
                extract_llm.save_cached(cfg["cache_dir"], text, response, cfg["llm"]["model"],
                                        state.get("source_name", ""))
            else:  # explicit testing mode: a recorded response for exactly this text, never another document's
                response = extract_llm.load_cached(cfg["cache_dir"], text)
            out["llm"] = extract_llm.to_extracted(response)
        except Exception as exc:  # LLMUnavailable, network or library errors: keep the regex result
            out["errors"] = [f"LLM extraction unavailable, only one extractor was used: {exc}"]
            out["extraction_source"] = SOURCE_LABELS["live_failed" if mode == "live" else "replay_missing"]
    return out


@step("verify")
def verify_node(state: PackState) -> dict:
    extracted = merge(state.get("llm"), state["regex"], state["raw_text"], state.get("pages"))
    out: dict[str, Any] = {"extracted": extracted}
    if all(extracted.get(n).status == "NOT_FOUND" for n in ("item_code", "quantity", "unit_price")):
        # Without an item line, "missing TIN" or "missing penalty" would only mean "this is not a quote".
        out["errors"] = ["Document does not look like a supplier quotation (no item, quantity or price found)."]
    return out


@step("match_pr")
def match_node(state: PackState) -> dict:
    pr, result = match_pr(state["extracted"], _purchase_request(load_config()["purchase_request"]))
    return {"pr": pr, "pr_match": result}


@step("check_math")
def math_node(state: PackState) -> dict:
    return {"math_results": check_math(state["extracted"], load_config()["rules"])}


@step("check_rules")
def rules_node(state: PackState) -> dict:
    cfg = load_config()
    # price rules compare in the purchase request's currency; the stored fields keep the quoted amounts
    in_base, conversion = convert_to_base(state["extracted"], cfg)
    results = check_rules(in_base, state.get("pr"), state["math_results"], cfg["rules"])
    return {"rule_results": results + [state["pr_match"]] + verification_checks(state["extracted"]),
            "conversion": conversion}


@step("decide", always_run=True)
def decide_node(state: PackState) -> dict:
    decision, reasons = decide(state.get("rule_results", []), state.get("errors", []))
    return {"decision": decision, "reasons": reasons}


def human_review_node(state: PackState) -> dict:
    """Pause the pipeline and wait for a person. Resumed with Command(resume={...})."""
    answer = interrupt({
        "pack_id": state.get("pack_id"),
        "system_decision": state["decision"],
        "reasons": [r.model_dump() for r in state["reasons"]],
    })
    return {"human_decision": answer, "trace": ["human_review: answered"]}


@step("report", always_run=True)
def report_node(state: PackState) -> dict:
    ex = state.get("extracted") or state.get("regex") or ExtractedPack()
    supplier, item = ex.get("supplier_name"), ex.get("item_code")
    desc = ex.get("description")
    result = PackResult(
        pack_id=state.get("pack_id", ""),
        file=state.get("source_name") or Path(state.get("pdf_path", "")).name,
        supplier=str(supplier.value) if supplier.value else None,
        item=f"{item.value} - {desc.value}" if item.value else None,
        decision=state["decision"],
        reasons=state.get("reasons", []),
        rule_results=state.get("rule_results", []),
        fields=ex.fields,
        extraction_source=state.get("extraction_source", "none"),
        errors=state.get("errors", []),
        human_decision=state.get("human_decision"),
        seconds=round(time.perf_counter() - state.get("started", time.perf_counter()), 2),
        financial=financial_summary(ex, state.get("pr"), state.get("conversion")),
    )
    return {"result": result}


def _num(f):
    return Decimal(f.value) if f.ok else None


def financial_summary(ex: ExtractedPack, pr: dict | None, conversion: dict | None = None) -> dict:
    """Numbers for the Financial Analysis view: what the document says, what the maths gives, what was requested.

    Read-only: computed from this document's verified fields and its purchase-request line; it does not decide.
    """
    q, price = _num(ex.get("quantity")), _num(ex.get("unit_price"))
    conv = conversion or {}
    rate = Decimal(conv["rate"]) if conv.get("rate") else None
    if conversion is not None and rate is None:
        price = None  # unclear currency or no company rate: not comparable with the purchase request
    elif rate is not None and price is not None:
        price = price * rate
    out = {
        "currency": conv.get("currency"), "exchange_rate": conv.get("rate"), "rates_as_of": conv.get("rates_as_of"),
        "currency_note": conv.get("note"),
        "unit_price_quoted": _num(ex.get("unit_price")), "total_quoted": _num(ex.get("total")),
        "quantity": q, "unit": ex.get("unit").value, "unit_price": price,
        "line_amount": _num(ex.get("line_amount")), "subtotal": _num(ex.get("subtotal")),
        "vat_rate": _num(ex.get("vat_rate")), "vat_amount": _num(ex.get("vat_amount")),
        "total": _num(ex.get("total")), "price_includes_vat": ex.get("price_includes_vat").value,
        "unit_price_bdt": price.quantize(Decimal("0.01")) if price is not None and price == price.quantize(Decimal("0.01"))
        else (price.quantize(Decimal("0.0001")) if price is not None else None),
        "computed_line_amount": (q * _num(ex.get("unit_price"))).quantize(Decimal("0.01"))
        if q is not None and _num(ex.get("unit_price")) is not None else None,
    }
    if pr:
        limit = pr["max_price"] * (1 + load_config()["rules"]["reject_price_over_pct"] / 100)
        out.update({
            "pr_number": pr["pr_number"], "pr_item": pr["item"], "requested_quantity": pr["quantity"],
            "max_unit_price": pr["max_price"], "reject_above_price": limit.quantize(Decimal("0.001")),
            "price_deviation_pct": ((price - pr["max_price"]) / pr["max_price"] * 100).quantize(Decimal("0.01"))
            if price is not None else None,
            "quantity_deviation_pct": ((q - pr["quantity"]) / pr["quantity"] * 100).quantize(Decimal("0.01"))
            if q is not None else None,
        })
    return {k: (str(v) if isinstance(v, Decimal) else v) for k, v in out.items()}


def route_after_decide(state: PackState) -> str:
    if state["decision"] == "NEEDS_REVIEW" and state.get("ask_human"):
        return "human_review"
    return "report"


def checkpoint_serde() -> JsonPlusSerializer:
    """Serializer that is allowed to restore our pydantic models from a checkpoint."""
    return JsonPlusSerializer(allowed_msgpack_modules=[
        ("checker.models", "ExtractedPack"), ("checker.models", "Field"),
        ("checker.models", "RuleResult"), ("checker.models", "PackResult"), ("checker.models", "Candidate"),
    ])


def build_graph(with_human_review: bool = False, checkpointer=None):
    """Compile the pipeline. Human review needs a checkpointer (in-memory unless one is given)."""
    g = StateGraph(PackState)
    for name, fn in (("parse", parse_node), ("extract", extract_node), ("verify", verify_node),
                     ("match_pr", match_node), ("check_math", math_node), ("check_rules", rules_node),
                     ("decide", decide_node), ("human_review", human_review_node), ("report", report_node)):
        g.add_node(name, fn)
    g.add_edge(START, "parse")
    for a, b in (("parse", "extract"), ("extract", "verify"), ("verify", "match_pr"),
                 ("match_pr", "check_math"), ("check_math", "check_rules"), ("check_rules", "decide")):
        g.add_edge(a, b)
    g.add_conditional_edges("decide", route_after_decide, ["human_review", "report"])
    g.add_edge("human_review", "report")
    g.add_edge("report", END)

    if not with_human_review:
        return g.compile()
    return g.compile(checkpointer=checkpointer or InMemorySaver(serde=checkpoint_serde()))


def initial_state(pdf_path: str | None = None, *, raw_text: str | None = None, pack_id: str = "",
                  source_name: str = "", mode: str | None = None, ask_human: bool = False) -> PackState:
    state: PackState = {"pack_id": pack_id, "source_name": source_name or (Path(pdf_path).name if pdf_path else ""),
                        "ask_human": ask_human, "errors": [], "trace": [], "started": time.perf_counter()}
    if pdf_path:
        state["pdf_path"] = str(pdf_path)
    if raw_text is not None:
        state["raw_text"] = raw_text
    if mode:
        state["mode"] = mode
    return state


_GRAPH = None


def run_pack(pdf_path: str | None = None, **kwargs) -> PackResult:
    """Run one pack straight through (no human pause). Used by the CLI, evaluation and tests."""
    global _GRAPH
    if _GRAPH is None:
        _GRAPH = build_graph(with_human_review=False)
    return _GRAPH.invoke(initial_state(pdf_path, **kwargs))["result"]
