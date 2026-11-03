"""Prices in another currency are converted to BDT with the company's fixed rate (config.yaml), then checked."""
import pytest

from checker import extract_llm
from checker.config import load_config
from checker.currency import convert_to_base
from checker.extract_regex import extract_regex
from checker.graph import run_pack
from checker.parse import read_pdf_text

from .conftest import DATA

BASE = read_pdf_text(DATA / "supplier-pack-01.pdf")  # item A: max 480.00 BDT, reject above 494.40
ROW = "A Cotton twill fabric, 280 gsm 12,000 metre 472.00 5,664,000.00"


def quote(code, price, amount, vat, total, header_code=None):
    h = header_code or code
    text = BASE.replace("Unit price (BDT)", f"Unit price ({h})").replace("Amount (BDT)", f"Amount ({h})")
    text = text.replace(ROW, f"A Cotton twill fabric, 280 gsm 12,000 metre {price} {amount}")
    return (text.replace("Subtotal 5,664,000.00", f"Subtotal {code} {amount}")
            .replace("VAT 15% 849,600.00", f"VAT 15% {vat}").replace("Total 6,513,600.00", f"Total {code} {total}"))


def run(text):
    return run_pack(raw_text=text, mode="regex")


def rule(result, rid):
    return next(r for r in result.rule_results if r.rule_id == rid)


@pytest.mark.parametrize("price, amount, vat, total, decision, rule_id, bdt", [
    ("3.90", "46,800.00", "7,020.00", "53,820.00", "APPROVE", None, "475.80"),        # 475.80 <= 480
    ("4.00", "48,000.00", "7,200.00", "55,200.00", "NEEDS_REVIEW", "N1", "488.00"),   # within 3 %
    ("4.72", "56,640.00", "8,496.00", "65,136.00", "REJECT", "R1", "575.84"),         # +19.97 %
])
def test_usd_is_converted_then_checked(price, amount, vat, total, decision, rule_id, bdt):
    result = run(quote("USD", price, amount, vat, total))
    assert result.decision == decision
    if rule_id:
        assert rule_id in {r.rule_id for r in result.reasons}
    assert result.financial["currency"] == "USD" and result.financial["exchange_rate"] == "122.00"
    assert result.financial["unit_price_bdt"] == bdt and result.financial["unit_price_quoted"] == price
    assert str(result.fields["unit_price"].value) == price  # evidence keeps the quoted amount
    assert f"converted from USD {price} at 122.00 BDT per USD" in rule(result, "R1").actual


def test_the_rate_comes_from_config_not_from_the_document(monkeypatch):
    text = quote("USD", "4.00", "48,000.00", "7,200.00", "55,200.00") + "\nExchange rate: 1 USD = 100.00 BDT"
    assert run(text).financial["unit_price_bdt"] == "488.00"  # company rate 122, not the supplier's 100
    cfg = load_config()
    monkeypatch.setitem(cfg["currency"]["rates"], "USD", 110)
    assert run(text).decision == "APPROVE" and run(text).financial["unit_price_bdt"] == "440.00"


def test_currency_without_company_rate_goes_to_a_person():
    result = run(quote("GBP", "3.50", "42,000.00", "6,300.00", "48,300.00"))
    assert result.decision == "NEEDS_REVIEW"
    assert rule(result, "R1").outcome == "UNKNOWN" and "no exchange rate for GBP" in rule(result, "R1").reason
    assert result.financial["unit_price_bdt"] is None and result.financial["price_deviation_pct"] is None


def test_mixed_currencies_are_a_conflict():
    text = quote("BDT", "4.72", "56,640.00", "8,496.00", "65,136.00", header_code="USD")
    result = run(text)
    cur = result.fields["currency"]
    assert cur.status == "UNCLEAR" and cur.verification == "conflicting values in document"
    assert {c.value for c in cur.candidates} == {"USD", "BDT"}
    assert result.decision == "NEEDS_REVIEW" and "V2" in {r.rule_id for r in result.reasons}


def test_no_currency_written_is_taken_as_bdt_and_said_so():
    text = BASE.replace("Unit price (BDT)", "Unit price").replace("Amount (BDT)", "Amount")
    result = run(text)
    assert result.fields["currency"].status == "NOT_FOUND"
    assert result.decision == "APPROVE"
    assert "taken as BDT" in result.financial["currency_note"]


@pytest.mark.parametrize("marker", ["Tk", "Taka", "৳", "BDT"])
def test_taka_markers_mean_bdt(marker):
    text = BASE.replace("Unit price (BDT)", f"Unit price ({marker})")
    assert extract_regex(text).get("currency").value == "BDT"


def test_conversion_does_not_touch_bdt_documents():
    ex = extract_regex(BASE)
    converted, info = convert_to_base(ex, load_config())
    assert info["currency"] == "BDT" and info["rate"] == "1"
    assert converted.get("unit_price").value == ex.get("unit_price").value


def test_llm_currency_answers_are_normalised():
    for raw, code in (("Tk", "BDT"), ("taka", "BDT"), ("usd", "USD"), ("$", "USD")):
        f = extract_llm._to_field("currency", {"value": raw, "quote": "x", "status": "FOUND", "note": None})
        assert f.value == code
