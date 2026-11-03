"""Step 3: check every extracted value against the document and against the other extractor.

A value is only trusted (FOUND) when
  1. its quote really appears in the document,
  2. the value really appears in its quote,
  3. the LLM and the regex extractor read the same value (live mode), and
  4. the value is possible (no negative quantity, VAT over 100 %, ...).
Anything else becomes UNCLEAR, which later means "Needs a person to check". Conflicting readings are
kept as candidates, never silently replaced. Each field also records how it was verified and the PDF
page its quote was found on.
"""
import re
from decimal import Decimal, InvalidOperation

from .models import (CONFLICT, CRITICAL_FIELDS, DOC_CONFLICT, FIELD_KINDS, LLM_ONLY, MATCHED, NOT_IN_DOC,
                     SINGLE_SOURCE, SUSPICIOUS, UNVERIFIED, AMBIGUOUS, Candidate, ExtractedPack, Field)
from .normalize import parse_date
from .parse import locate_page, normalize, quote_in_text

# plausible ranges: outside them a value is "suspicious" and goes to a person
LIMITS = {
    "quantity": (Decimal("0.0001"), None), "unit_price": (Decimal("0.0001"), None),
    "line_amount": (Decimal("0.0001"), None), "subtotal": (Decimal("0.0001"), None),
    "total": (Decimal("0.0001"), None), "vat_amount": (Decimal(0), None),
    "vat_rate": (Decimal(0), Decimal(100)), "advance_pct": (Decimal(0), Decimal(100)),
    "penalty_pct": (Decimal(0), Decimal(100)),
    "payment_days_min": (0, 3650), "payment_days_max": (0, 3650), "delivery_days": (0, 3650),
    "validity_days": (0, 3650),
}


def _numbers_in(text: str) -> set[Decimal]:
    out = set()
    for m in re.findall(r"\d[\d,]*(?:\.\d+)?", text):
        try:
            out.add(Decimal(m.replace(",", "")).normalize())
        except InvalidOperation:
            pass
    return out


def value_in_quote(name: str, f: Field) -> bool:
    if f.value is None or not f.quote:
        return False
    kind = FIELD_KINDS[name]
    if kind == "bool":
        return True  # yes/no is a reading of the quote, not a copy of it
    if kind == "text":
        if name == "quote_date" and parse_date(str(f.value))[0]:
            return any(parse_date(d)[0] == parse_date(str(f.value))[0]
                       for d in re.findall(r"[\w./\-, ]{6,24}", f.quote) if parse_date(d.strip())[0]) \
                or normalize(str(f.value)) in normalize(f.quote)
        return normalize(str(f.value)) in normalize(f.quote)
    nums = _numbers_in(f.quote)
    v = Decimal(f.value)
    if kind == "days":  # value may have been converted from weeks or months
        return any(x in nums for x in (v, v / 7, v / 30)) or v.normalize() in nums
    if v == 0 and re.search(r"\bno\b", f.quote, re.I):  # "No advance payment required"
        return True
    return v.normalize() in nums or (v / 7).normalize() in nums  # per-day penalty converted to per week


def _loose(s) -> str:
    return re.sub(r"[^a-z0-9]", "", normalize(str(s)))


def same_value(name: str, a, b) -> bool:
    if a is None or b is None:
        return a is b
    kind = FIELD_KINDS[name]
    if kind in ("number", "days"):
        return Decimal(a) == Decimal(b)
    if kind == "bool":
        return a is b
    if name == "quote_date":
        da, db = parse_date(str(a))[0], parse_date(str(b))[0]
        if da and db:
            return da == db
    return _loose(a) == _loose(b)


def check_llm_field(name: str, f: Field, text: str) -> Field:
    """Rules 1 and 2: is the LLM's own answer backed by the document?"""
    if f.status == "NOT_FOUND":
        return f
    if not quote_in_text(f.quote, text):
        return Field(value=None, quote=None, status="UNCLEAR", source=f.source, verification=UNVERIFIED,
                     note=f"LLM said '{f.value}' but its quote does not appear in the document",
                     candidates=[Candidate(source="llm", value=f.value, quote=f.quote)])
    if f.status == "FOUND" and not value_in_quote(name, f):
        return Field(value=None, quote=f.quote, status="UNCLEAR", source=f.source, verification=UNVERIFIED,
                     note=f"LLM said '{f.value}' but that is not in the text it quoted",
                     candidates=[Candidate(source="llm", value=f.value, quote=f.quote)])
    return f


def _cands(llm: Field, rx: Field) -> list[Candidate]:
    out = list(rx.candidates) if rx.candidates else (
        [Candidate(source="regex", value=rx.value, quote=rx.quote)] if rx.status != "NOT_FOUND" else [])
    out += list(llm.candidates) if llm.candidates else (
        [Candidate(source="llm", value=llm.value, quote=llm.quote)] if llm.status != "NOT_FOUND" else [])
    return out


def merge_field(name: str, llm: Field, rx: Field, text: str) -> Field:
    llm = check_llm_field(name, llm, text)
    both = "llm+regex"

    if llm.status == "FOUND" and rx.status == "FOUND":
        if same_value(name, llm.value, rx.value):
            keep = rx if name in CRITICAL_FIELDS else llm  # regex value is already normalised for numbers
            return keep.model_copy(update={"source": both, "quote": llm.quote or rx.quote,
                                           "note": rx.note or llm.note, "verification": MATCHED,
                                           "candidates": _cands(llm, rx)})
        return Field(value=None, quote=llm.quote or rx.quote, status="UNCLEAR", source=both, verification=CONFLICT,
                     note=f"LLM read '{llm.value}' but the regex check read '{rx.value}'", candidates=_cands(llm, rx))

    if llm.status == "NOT_FOUND" and rx.status == "NOT_FOUND":
        return rx.model_copy(update={"source": both, "quote": rx.quote or llm.quote, "note": rx.note or llm.note,
                                     "verification": NOT_IN_DOC})

    if name not in CRITICAL_FIELDS and llm.status == "FOUND" and rx.status == "NOT_FOUND":
        # descriptive fields (supplier name, date, description...): one verified reading is enough
        return llm.model_copy(update={"verification": LLM_ONLY, "candidates": _cands(llm, rx)})
    if name not in CRITICAL_FIELDS and rx.status == "FOUND" and llm.status == "NOT_FOUND":
        return rx.model_copy(update={"verification": SINGLE_SOURCE, "candidates": _cands(llm, rx)})

    if "UNCLEAR" in (llm.status, rx.status):
        values = [v for v in (llm.value, rx.value) if v is not None]
        value = None
        if values and FIELD_KINDS[name] in ("number", "days"):
            value = min(Decimal(v) for v in values)  # lowest reading: see rules.advance_rule
            value = int(value) if FIELD_KINDS[name] == "days" else value
        elif values:
            value = values[0]
        notes = "; ".join(dict.fromkeys(n for n in (rx.note, llm.note) if n)) or "unclear in the document"
        verification = next((v for v in (rx.verification, llm.verification) if v in (DOC_CONFLICT, UNVERIFIED)),
                             AMBIGUOUS)
        return Field(value=value, quote=rx.quote or llm.quote, status="UNCLEAR", source=both, note=notes,
                     verification=verification, candidates=_cands(llm, rx))

    # one extractor found it, the other did not
    who = "LLM" if llm.status == "FOUND" else "regex check"
    return Field(value=None, quote=llm.quote or rx.quote, status="UNCLEAR", source=both, verification=CONFLICT,
                 note=f"only the {who} found this value", candidates=_cands(llm, rx))


def _offline(f: Field) -> Field:
    if f.verification:
        return f
    status = {"FOUND": SINGLE_SOURCE, "NOT_FOUND": NOT_IN_DOC}.get(f.status, AMBIGUOUS)
    return f.model_copy(update={"verification": status})


def sanity(name: str, f: Field) -> Field:
    """Rule 4: an impossible value is never trusted."""
    if not f.ok or name not in LIMITS:
        return f
    lo, hi = LIMITS[name]
    v = Decimal(f.value)
    if (lo is not None and v < lo) or (hi is not None and v > hi):
        bounds = f"{lo} to {hi}" if hi is not None else f"above {lo}"
        return f.model_copy(update={"status": "UNCLEAR", "verification": SUSPICIOUS,
                                    "note": f"{v} is not a plausible {name.replace('_', ' ')} (expected {bounds})"})
    return f


def merge(llm: ExtractedPack | None, rx: ExtractedPack, text: str, pages: list[str] | None = None) -> ExtractedPack:
    if llm is None:
        fields = {name: _offline(rx.get(name)) for name in FIELD_KINDS}
    else:
        fields = {name: merge_field(name, llm.get(name), rx.get(name), text) for name in FIELD_KINDS}
    out = {}
    for name, f in fields.items():
        f = sanity(name, f)
        out[name] = f.model_copy(update={"page": locate_page(f.quote, pages)})
    return ExtractedPack(fields=out)
