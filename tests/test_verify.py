"""The anti-hallucination step: LLM answers must be backed by the document and by the regex check."""
from decimal import Decimal as D

from checker.extract_regex import extract_regex
from checker.models import Field
from checker.verify import merge, merge_field

from .conftest import pack_text

TEXT = pack_text("01")
RX = extract_regex(TEXT)


def llm(value, quote, status="FOUND"):
    return Field(value=value, quote=quote, status=status, source="llm")


def test_agreement_is_trusted():
    f = merge_field("unit_price", llm(D("472.00"), "A Cotton twill fabric, 280 gsm 12,000 metre 472.00 5,664,000.00"),
                    RX.get("unit_price"), TEXT)
    assert f.status == "FOUND" and f.value == D("472.00") and f.source == "llm+regex"


def test_invented_quote_is_caught():
    f = merge_field("advance_pct", llm(D(10), "10% advance, rest on delivery."), RX.get("advance_pct"), TEXT)
    assert f.status == "UNCLEAR"
    assert "agree" in f.note or "quote" in f.note or "only" in f.note


def test_value_not_in_its_own_quote_is_caught():
    quote = "2. Delivery. Delivery within 24 days of receiving the purchase order."
    f = merge_field("delivery_days", llm(14, quote), RX.get("delivery_days"), TEXT)
    assert f.status == "UNCLEAR"


def test_disagreement_becomes_unclear():
    quote = "Balance payable within 45 days after delivery and acceptance of goods."
    f = merge_field("payment_days_min", llm(45, quote), Field(value=40, quote=quote, status="FOUND"), TEXT)
    assert f.status == "UNCLEAR" and "45" in f.note and "40" in f.note


def test_hallucinated_penalty_clause_is_not_trusted():
    text6 = pack_text("06")
    rx6 = extract_regex(text6)
    fake = llm(D("0.5"), "Late delivery penalty of 0.5% of order value per week.")
    f = merge_field("penalty_pct", fake, rx6.get("penalty_pct"), text6)
    assert f.status == "UNCLEAR" and f.value is None


def test_both_not_found_stays_not_found():
    text8 = pack_text("08")
    rx8 = extract_regex(text8)
    f = merge_field("tin", Field(status="NOT_FOUND"), rx8.get("tin"), text8)
    assert f.status == "NOT_FOUND"
    assert f.quote == "Supplier registration details available on request."


def test_weeks_converted_value_matches_quote():
    text9 = pack_text("09")
    rx9 = extract_regex(text9)
    f = merge_field("delivery_days", llm(28, "Delivery within 4 weeks of PO."), rx9.get("delivery_days"), text9)
    assert f.status == "FOUND" and f.value == 28


def test_no_llm_means_regex_only():
    merged = merge(None, RX, TEXT)
    assert {n: f.value for n, f in merged.fields.items()} == {n: f.value for n, f in RX.fields.items()}
    assert merged.get("unit_price").verification == "regex only"
    assert merged.get("penalty_basis").verification in ("regex only", "not found")
