from decimal import Decimal
from typing import Literal

from pydantic import BaseModel, Field as PydField

Status = Literal["FOUND", "NOT_FOUND", "UNCLEAR"]
Outcome = Literal["PASS", "FAIL", "UNKNOWN", "N/A"]
Decision = Literal["APPROVE", "REJECT", "NEEDS_REVIEW"]
Category = Literal["reject", "review", "math", "verification"]

# What kind of value each field holds. Drives parsing, quote checks and comparison.
FIELD_KINDS: dict[str, str] = {
    "supplier_name": "text",
    "quotation_no": "text",
    "quote_date": "text",
    "pr_reference": "text",
    "item_code": "text",
    "description": "text",
    "unit": "text",
    "quantity": "number",
    "unit_price": "number",
    "price_includes_vat": "bool",
    "line_amount": "number",
    "subtotal": "number",
    "vat_rate": "number",
    "vat_amount": "number",
    "total": "number",
    "currency": "text",
    "tin": "text",
    "bin": "text",
    "advance_pct": "number",
    "payment_days_min": "days",
    "payment_days_max": "days",
    "delivery_days": "days",
    "penalty_pct": "number",
    "penalty_basis": "text",
    "validity_days": "days",
}

# Fields where a wrong value could change the decision. In live/replay mode the LLM and
# the regex extractor must agree on these, otherwise the field becomes UNCLEAR.
CRITICAL_FIELDS = {
    name for name, kind in FIELD_KINDS.items() if kind in ("number", "days", "bool")
} | {"tin", "bin", "penalty_basis", "item_code"}


# How a field's value was checked (set by checker.verify). Never a made-up confidence score.
MATCHED = "matched"                  # LLM and regex read the same value, quote found in the document
SINGLE_SOURCE = "regex only"         # offline: one extractor, quote found in the document
LLM_ONLY = "llm only"                # only the LLM found it (accepted for descriptive fields only)
CONFLICT = "conflict"                # LLM and regex read different values
DOC_CONFLICT = "conflicting values in document"  # the document itself states different values
UNVERIFIED = "unverified"            # quote not in the document, or the value is not in its quote
AMBIGUOUS = "ambiguous in document"  # range, vague or contradictory wording
SUSPICIOUS = "suspicious value"      # impossible value (negative quantity, VAT over 100%, ...)
NOT_IN_DOC = "not found"
TRUSTED = (MATCHED, SINGLE_SOURCE)


class Candidate(BaseModel):
    """One reading of a field: kept so conflicts are shown, never silently resolved."""
    source: str  # "llm" | "regex" | "regex: <method>"
    value: bool | int | Decimal | str | None = None
    quote: str | None = None


class Field(BaseModel):
    value: bool | int | Decimal | str | None = None
    quote: str | None = None  # exact text from the document
    status: Status = "NOT_FOUND"
    source: str = ""
    note: str | None = None
    page: int | None = None  # 1-based page where the quote was found in the PDF text; None if not located
    verification: str = ""
    candidates: list[Candidate] = PydField(default_factory=list)

    @property
    def conflict(self) -> bool:
        return self.verification in (CONFLICT, DOC_CONFLICT)

    @property
    def ok(self) -> bool:
        return self.status == "FOUND" and self.value is not None


class ExtractedPack(BaseModel):
    fields: dict[str, Field] = PydField(default_factory=dict)

    def get(self, name: str) -> Field:
        return self.fields.get(name) or Field(note="not extracted")


class RuleResult(BaseModel):
    rule_id: str
    category: Category
    title: str
    outcome: Outcome
    reason: str
    evidence: list[str] = PydField(default_factory=list)
    expected: str = ""  # the condition the rule requires, in words
    actual: str = ""    # what the document says (or why it could not be read)


class PackResult(BaseModel):
    pack_id: str
    file: str
    supplier: str | None
    item: str | None
    decision: Decision
    reasons: list[RuleResult]
    rule_results: list[RuleResult]
    fields: dict[str, Field]
    extraction_source: str
    errors: list[str]
    human_decision: dict | None = None
    seconds: float = 0.0
    financial: dict = PydField(default_factory=dict)  # quantities, amounts and deviations vs the purchase request
