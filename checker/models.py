from decimal import Decimal
from typing import Literal

from pydantic import BaseModel, Field as PydField

Status = Literal["FOUND", "NOT_FOUND", "UNCLEAR"]
Outcome = Literal["PASS", "FAIL", "UNKNOWN", "N/A"]
Decision = Literal["APPROVE", "REJECT", "NEEDS_REVIEW"]
Category = Literal["reject", "review", "math"]

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


class Field(BaseModel):
    value: bool | int | Decimal | str | None = None
    quote: str | None = None  # exact text from the document
    status: Status = "NOT_FOUND"
    source: str = ""
    note: str | None = None

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
