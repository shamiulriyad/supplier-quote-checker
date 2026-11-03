"""Step 2b: LLM extractor (OpenAI structured output).

The LLM only copies facts out of the document, each with the exact text it came from.
It never calculates and never decides. Answers are saved to cache/llm so the pipeline
can be re-run later without an API key (replay mode).
"""
import hashlib
import json
import os
import re
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, Field as PydField, ValidationError, create_model

from .models import FIELD_KINDS, ExtractedPack, Field
from .normalize import parse_date

PROMPT_VERSION = "v2"
SRC = "llm"


class LLMUnavailable(Exception):
    """No LLM answer could be obtained (no key, API error, no cached answer...)."""


class LLMField(BaseModel):
    value: str | None
    quote: str | None
    status: Literal["FOUND", "NOT_FOUND", "UNCLEAR"]
    note: str | None


FIELD_GUIDE = {
    "supplier_name": "Supplier company name as printed at the top.",
    "quotation_no": "Quotation number, e.g. QT-2026-901.",
    "quote_date": "Quotation date as written (YYYY-MM-DD).",
    "pr_reference": "Purchase request reference, e.g. PR-2026-114.",
    "item_code": "Item letter in the item table, e.g. A.",
    "description": "Item description exactly as written in the item table.",
    "unit": "Unit of measure in the item table, e.g. metre, piece, cone.",
    "quantity": "Quoted quantity as a plain number without commas, e.g. 12000.",
    "unit_price": "Unit price as a plain number, e.g. 472.00.",
    "price_includes_vat": "Does the UNIT PRICE already include VAT? 'yes' only if the document explicitly says "
                          "the prices are VAT-inclusive (e.g. 'VAT included', 'all taxes included'). 'no' if VAT "
                          "is added on top as a separate line (the grand total including VAT does not make it "
                          "'yes'). Quote the line that shows it.",
    "line_amount": "Amount of the item line as a plain number.",
    "subtotal": "Value on a line labelled 'Subtotal', as a plain number. NOT_FOUND if there is no line "
                "labelled Subtotal (do not use the Total line).",
    "vat_rate": "VAT percentage as a number, e.g. 15. NOT_FOUND if VAT is not shown as a separate line.",
    "vat_amount": "VAT amount as a plain number. NOT_FOUND if VAT is not shown as a separate line.",
    "total": "Grand total as a plain number.",
    "tin": "Supplier TIN number exactly as printed. NOT_FOUND if the number itself is not printed "
           "(e.g. 'details available on request' is NOT_FOUND).",
    "bin": "Supplier BIN number exactly as printed. NOT_FOUND if the number itself is not printed.",
    "advance_pct": "Advance payment percentage, number only. 'No advance' means 0. If the advance is stated "
                   "more than once or in a contradictory way: status UNCLEAR, value = the lowest possible "
                   "reading, and explain the possible readings in note.",
    "payment_days_min": "Days after delivery by which payment is due. A single period gives the same number, "
                        "quote and status for both min and max. A range like '30 to 90 days' gives min 30 / "
                        "max 90 with status UNCLEAR. Vague wording like 'as agreed' is UNCLEAR.",
    "payment_days_max": "Upper end of the payment period; always filled together with payment_days_min.",
    "delivery_days": "Delivery period with its unit exactly as written, e.g. '24 days' or '4 weeks'.",
    "penalty_pct": "Late-delivery penalty percentage per week, number only. NOT_FOUND if the contract has "
                   "no late-delivery penalty clause. UNCLEAR if it is not clearly per week.",
    "penalty_basis": "What the penalty percentage is of, as written, e.g. 'order value'. NOT_FOUND if the "
                     "text does not say.",
    "validity_days": "How long the quotation is valid, with its unit as written, e.g. '45 days'.",
}

LLMExtraction = create_model(
    "LLMExtraction",
    **{name: (LLMField, PydField(description=FIELD_GUIDE[name])) for name in FIELD_KINDS},
)

SYSTEM_PROMPT = """You extract facts from one supplier quotation for a procurement check.

Rules:
1. Only report what is written in the document. Never guess, infer or fill in typical values.
2. For every field give `quote`: ONE continuous piece of text copied character-for-character from the
   document that contains the value. Never join a column header to a value and never stitch separate parts
   together. For a value in the item table, quote the whole table row. For a value in the terms, quote the
   sentence it is in.
3. status FOUND: clearly stated. NOT_FOUND: not in the document (value null). UNCLEAR: stated but ambiguous,
   contradictory, a range or vague; put your best reading in value and explain in note.
4. Do not calculate anything and do not judge whether the quote is acceptable. Copy numbers as printed,
   without thousands separators.
5. note is null unless you need to explain something."""


def cache_key(text: str) -> str:
    return hashlib.sha256(f"{PROMPT_VERSION}|{text}".encode("utf-8")).hexdigest()[:16]


# ---- answer cache -----------------------------------------------------------------------
# Answers are saved per document text. Where they are saved is pluggable: local JSON files
# (default, cache_dir) or the llm_cache table when Supabase is configured (set_cache_backend).
# They are read only in replay (testing) mode. FIXTURES_DIR is an optional, git-ignored local folder of
# extra read-only recordings; nothing ships in the repository.

FIXTURES_DIR = Path(__file__).resolve().parent.parent / "data" / "llm-replay"


class FileCache:
    def __init__(self, cache_dir: str | Path):
        self.dir = Path(cache_dir)

    def get(self, key: str) -> dict | None:
        path = self.dir / f"{key}.json"
        return json.loads(path.read_text(encoding="utf-8"))["response"] if path.exists() else None

    def put(self, key: str, response: dict, model: str, document: str) -> None:
        self.dir.mkdir(parents=True, exist_ok=True)
        payload = {"document": document, "model": model, "prompt_version": PROMPT_VERSION,
                   "created": datetime.now(timezone.utc).isoformat(timespec="seconds"), "response": response}
        (self.dir / f"{key}.json").write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")

    def delete(self, keys: list[str]) -> int:
        n = 0
        for key in keys:
            path = self.dir / f"{key}.json"
            if path.exists():
                path.unlink()
                n += 1
        return n


_backend = None  # anything with get(key) / put(key, response, model, document) / delete(keys)


def set_cache_backend(backend) -> None:
    global _backend
    _backend = backend


def cache_backend(cache_dir: str):
    return _backend or FileCache(cache_dir)


def _lookup(cache_dir: str, text: str) -> dict | None:
    key = cache_key(text)
    found = cache_backend(cache_dir).get(key)
    return found if found is not None else FileCache(FIXTURES_DIR).get(key)


def has_cached(cache_dir: str, text: str) -> bool:
    return _lookup(cache_dir, text) is not None


def load_cached(cache_dir: str, text: str) -> dict:
    response = _lookup(cache_dir, text)
    if response is None:
        raise LLMUnavailable("No saved LLM answer for this document (run once in live mode to create it).")
    return response


def save_cached(cache_dir: str, text: str, response: dict, model: str, source_name: str) -> None:
    cache_backend(cache_dir).put(cache_key(text), response, model, source_name)


def _client(llm_cfg: dict, timeout: float | None = None, max_retries: int | None = None):
    import openai

    return openai.OpenAI(
        timeout=timeout or llm_cfg["timeout_seconds"],
        max_retries=llm_cfg["max_retries"] if max_retries is None else max_retries,
        # gzip only: brotli decoding breaks with older brotlicffi builds (e.g. Anaconda's 1.1)
        default_headers={"Accept-Encoding": "gzip, deflate"},
    )


def check_connection(llm_cfg: dict) -> tuple[bool, str]:
    """One cheap API call (model lookup) to confirm the key and model work. Never returns the key."""
    if not os.environ.get("OPENAI_API_KEY"):
        return False, "OPENAI_API_KEY is not set."
    import openai

    try:
        _client(llm_cfg, timeout=15, max_retries=0).models.retrieve(llm_cfg["model"])
    except openai.AuthenticationError:
        return False, "The API key was rejected (authentication failed)."
    except openai.NotFoundError:
        return False, f"The key works, but model '{llm_cfg['model']}' is not available to it."
    except Exception as exc:
        return False, f"Could not reach OpenAI: {type(exc).__name__}"
    return True, f"Connected. Model '{llm_cfg['model']}' is available."


def call_openai(text: str, llm_cfg: dict) -> dict:
    if not os.environ.get("OPENAI_API_KEY"):
        raise LLMUnavailable("OPENAI_API_KEY is not set.")
    import openai

    client = _client(llm_cfg)
    kwargs = {
        "model": llm_cfg["model"],
        "messages": [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": f"<document>\n{text}\n</document>"},
        ],
        "response_format": LLMExtraction,
    }
    if llm_cfg.get("temperature") is not None:
        kwargs["temperature"] = llm_cfg["temperature"]

    last_error = None
    for _ in range(2):  # one retry if the answer does not fit the schema
        try:
            completion = client.chat.completions.parse(**kwargs)
        except openai.BadRequestError as exc:
            if "temperature" in str(exc) and "temperature" in kwargs:
                kwargs.pop("temperature")  # some models only allow the default
                continue
            raise LLMUnavailable(f"OpenAI rejected the request: {exc}") from exc
        except (ValidationError, openai.LengthFinishReasonError) as exc:
            last_error = exc
            continue
        except openai.OpenAIError as exc:
            raise LLMUnavailable(f"OpenAI call failed: {exc}") from exc
        message = completion.choices[0].message
        if message.refusal or message.parsed is None:
            last_error = message.refusal or "empty answer"
            continue
        return message.parsed.model_dump()
    raise LLMUnavailable(f"LLM did not return a valid answer: {last_error}")


# ---- turn the LLM's strings into typed Fields -------------------------------------------

def _number(s: str) -> Decimal:
    m = re.search(r"-?\d[\d,]*(?:\.\d+)?", s)
    if not m:
        raise InvalidOperation(s)
    return Decimal(m.group(0).replace(",", ""))


def _days(s: str) -> tuple[int, str | None]:
    n = int(_number(s))
    unit = s.lower()
    if "week" in unit:
        return n * 7, f"converted from {n} weeks"
    if "month" in unit:
        return n * 30, f"converted from {n} months (30 days each)"
    return n, None


def _to_field(name: str, raw: dict) -> Field:
    status, value, note = raw["status"], raw.get("value"), raw.get("note")
    base = {"quote": raw.get("quote"), "source": SRC}
    if status == "NOT_FOUND" or value in (None, ""):
        if status == "FOUND":
            return Field(value=None, status="UNCLEAR", note="LLM said FOUND but gave no value", **base)
        return Field(value=None, status=status, note=note, **base)

    kind = FIELD_KINDS[name]
    try:
        if kind == "number":
            parsed = _number(value)
        elif kind == "days":
            parsed, conv = _days(value)
            note = "; ".join(x for x in (note, conv) if x) or None
        elif kind == "bool":
            low = value.strip().lower()
            if low not in ("yes", "no", "true", "false"):
                return Field(value=None, status="UNCLEAR", note=f"unexpected answer '{value}'", **base)
            parsed = low in ("yes", "true")
        elif name == "quote_date":  # same schema as the regex extractor: ISO date, or unclear
            parsed, problem = parse_date(value.strip())
            if parsed is None:
                return Field(value=None, status="UNCLEAR", note=problem, **base)
        else:
            parsed = value.strip()
    except (InvalidOperation, ValueError):
        return Field(value=None, status="UNCLEAR", note=f"could not read a number from '{value}'", **base)
    return Field(value=parsed, status=status, note=note, **base)


def to_extracted(response: dict) -> ExtractedPack:
    return ExtractedPack(fields={name: _to_field(name, response[name]) for name in FIELD_KINDS if name in response})
