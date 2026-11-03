"""Step 3: check the LLM's answer against the document and against the regex extractor.

A value is only trusted (FOUND) when
  1. its quote really appears in the document,
  2. the value really appears in its quote, and
  3. for decision-critical fields, the regex extractor read the same value.
Anything else becomes UNCLEAR, which later means "Needs a person to check".
"""
import re
from decimal import Decimal, InvalidOperation

from .models import CRITICAL_FIELDS, FIELD_KINDS, ExtractedPack, Field
from .parse import normalize, quote_in_text


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
        return normalize(str(f.value)) in normalize(f.quote)
    nums = _numbers_in(f.quote)
    v = Decimal(f.value)
    if kind == "days":  # value may have been converted from weeks or months
        return any(x in nums for x in (v, v / 7, v / 30)) or v.normalize() in nums
    if v == 0 and re.search(r"\bno\b", f.quote, re.I):  # "No advance payment required"
        return True
    return v.normalize() in nums


def same_value(name: str, a, b) -> bool:
    if a is None or b is None:
        return a is b
    kind = FIELD_KINDS[name]
    if kind in ("number", "days"):
        return Decimal(a) == Decimal(b)
    if kind == "bool":
        return a is b
    return normalize(str(a)) == normalize(str(b))


def check_llm_field(name: str, f: Field, text: str) -> Field:
    """Rules 1 and 2: is the LLM's own answer backed by the document?"""
    if f.status == "NOT_FOUND":
        return f
    # An answer without real evidence is thrown away, not just flagged.
    if not quote_in_text(f.quote, text):
        return Field(value=None, quote=None, status="UNCLEAR", source=f.source,
                     note=f"LLM said '{f.value}' but its quote does not appear in the document")
    if f.status == "FOUND" and not value_in_quote(name, f):
        return Field(value=None, quote=f.quote, status="UNCLEAR", source=f.source,
                     note=f"LLM said '{f.value}' but that is not in the text it quoted")
    return f


def merge_field(name: str, llm: Field, rx: Field, text: str) -> Field:
    llm = check_llm_field(name, llm, text)
    both = "llm+regex"

    if name not in CRITICAL_FIELDS:
        # Descriptive fields: a verified LLM answer is enough, else fall back to regex.
        if llm.status == "FOUND":
            return llm.model_copy(update={"source": both if rx.ok and same_value(name, llm.value, rx.value) else "llm"})
        return rx if rx.status != "NOT_FOUND" else llm

    if llm.status == "FOUND" and rx.status == "FOUND":
        if same_value(name, llm.value, rx.value):
            return rx.model_copy(update={"source": both, "quote": llm.quote or rx.quote,
                                         "note": rx.note or llm.note})
        return Field(value=None, quote=llm.quote or rx.quote, status="UNCLEAR", source=both,
                     note=f"LLM read '{llm.value}' but the regex check read '{rx.value}'")

    if llm.status == "NOT_FOUND" and rx.status == "NOT_FOUND":
        return rx.model_copy(update={"source": both, "quote": rx.quote or llm.quote, "note": rx.note or llm.note})

    if "UNCLEAR" in (llm.status, rx.status):
        values = [v for v in (llm.value, rx.value) if v is not None]
        value = None
        if values and FIELD_KINDS[name] in ("number", "days"):
            value = min(Decimal(v) for v in values)  # lowest reading: see rules.advance_rule
            value = int(value) if FIELD_KINDS[name] == "days" else value
        elif values:
            value = values[0]
        notes = "; ".join(dict.fromkeys(n for n in (rx.note, llm.note) if n)) or "unclear in the document"
        return Field(value=value, quote=rx.quote or llm.quote, status="UNCLEAR", source=both, note=notes)

    # one extractor found it, the other did not
    who = "LLM" if llm.status == "FOUND" else "regex check"
    return Field(value=None, quote=llm.quote or rx.quote, status="UNCLEAR", source=both,
                 note=f"only the {who} found this value")


def merge(llm: ExtractedPack | None, rx: ExtractedPack, text: str) -> ExtractedPack:
    if llm is None:
        return rx
    return ExtractedPack(fields={
        name: merge_field(name, llm.get(name), rx.get(name), text) for name in FIELD_KINDS
    })
