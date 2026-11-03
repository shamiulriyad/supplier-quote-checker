"""Generalisation: layouts and wording the extractor was NOT written against.

Original sample layouts are tested in test_pipeline.py; this file uses tests/fixtures/layouts/
(regenerate with tests/fixtures/make_layouts.py) and inline text. Expected outcomes are derived from the
company rules here in the test, never from tests/expected.csv or recorded LLM answers.
"""
from pathlib import Path

import pytest

from checker import extract_llm
from checker.extract_regex import extract_regex
from checker.graph import run_pack
from checker.parse import normalize, read_pdf_pages

from .conftest import ROOT, llm_answer_from

LAYOUTS = Path(__file__).parent / "fixtures" / "layouts"

CASES = {  # file: (decision, rule ids that must be among the reasons, why)
    "key-value-labels.pdf": ("APPROVE", set(), "every value given as 'Label: value', other wording"),
    "pipe-table-reordered.pdf": ("REJECT", {"R5"}, "columns in a different order; delivery 50 days"),
    "two-pages.pdf": ("APPROVE", set(), "terms on page 2, validity given as a date"),
    "duplicate-totals.pdf": ("NEEDS_REVIEW", {"V2", "V1"}, "two different totals in one document"),
    "conflicting-price.pdf": ("NEEDS_REVIEW", {"V2", "R1"}, "table price and 'Unit price:' line disagree"),
    "missing-number-and-date.pdf": ("NEEDS_REVIEW", {"V1"}, "no quotation number or date: never auto-approve"),
    "invalid-number.pdf": ("NEEDS_REVIEW", {"V1", "R1"}, "price written as '2O5.00' (letter O)"),
    "ambiguous-date.pdf": ("NEEDS_REVIEW", {"V1"}, "05/11/2026: day and month order unclear"),
    "scanned-key-value.pdf": ("NEEDS_REVIEW", {"E1"}, "image only, no text layer"),
}


def run(name, mode="regex"):
    return run_pack(str(LAYOUTS / name), mode=mode)


@pytest.mark.parametrize("name", CASES, ids=[n[:-4] for n in CASES])
def test_unseen_layout_decisions(name):
    decision, rules, _why = CASES[name]
    result = run(name)
    got = {r.rule_id for r in result.reasons}
    assert result.decision == decision, sorted(got)
    assert rules <= got
    if decision == "APPROVE":
        assert not got
    else:
        assert result.reasons and all(r.reason for r in result.reasons)  # every non-approval is explained


# ---------------------------------------------------------------- different labels, names, tables, dates

def test_key_value_layout_is_read_and_normalised():
    f = run("key-value-labels.pdf").fields
    assert f["supplier_name"].value == "Nilphamari Textile Co."  # 'Supplier:' label, confirmed by signature
    assert f["quotation_no"].value == "NT/2026/0457"
    assert f["quote_date"].value == "2026-10-21"  # written 21/10/2026
    assert (f["quantity"].value, f["unit"].value, str(f["unit_price"].value)) == (12000, "metre", "470.00")
    assert f["delivery_days"].value == 21 and "3 weeks" in f["delivery_days"].note
    assert str(f["vat_rate"].value) == "15" and str(f["total"].value) == "6486000.00"
    assert f["tin"].value == "123456789012" and f["bin"].value == "001234567-0101"


def test_reordered_pipe_table_maps_columns_by_header():
    f = run("pipe-table-reordered.pdf").fields
    assert (f["item_code"].value, f["description"].value) == ("B", "Metal zipper, 18 cm")
    assert (f["unit"].value, str(f["unit_price"].value), str(f["line_amount"].value)) == ("piece", "36.50", "730000.00")
    assert f["quote_date"].value == "2026-10-22"  # 'Oct 22, 2026'
    assert (f["advance_pct"].value, f["payment_days_min"].value) == (0, 30)  # 'Net 30 days after delivery'


def test_evidence_page_numbers_come_from_the_real_pages():
    result = run("two-pages.pdf")
    pages = read_pdf_pages(LAYOUTS / "two-pages.pdf")
    f = result.fields
    assert (f["unit_price"].page, f["payment_days_min"].page, f["validity_days"].page) == (1, 2, 2)
    for name, field in f.items():
        if field.quote and field.page:
            assert normalize(field.quote) in normalize(pages[field.page - 1]), name  # never fabricated
    assert f["validity_days"].value == 40 and "2026-10-21 to 2026-11-30" in f["validity_days"].note


def test_no_page_numbers_without_a_pdf():
    text = "\n".join(read_pdf_pages(LAYOUTS / "two-pages.pdf"))
    assert all(f.page is None for f in run_pack(raw_text=text, mode="regex").fields.values())


# ---------------------------------------------------------------- duplicates, conflicts, invalid values

def test_conflicting_values_are_kept_not_replaced():
    total = run("duplicate-totals.pdf").fields["total"]
    assert total.status == "UNCLEAR" and total.value is None
    assert total.verification == "conflicting values in document"
    assert sorted(str(c.value) for c in total.candidates) == ["6513600.00", "6600000.00"]
    assert all(c.quote for c in total.candidates)

    price = run("conflicting-price.pdf").fields["unit_price"]
    assert {str(c.value) for c in price.candidates} == {"472.00", "497.00"}


def test_invalid_number_is_not_guessed():
    result = run("invalid-number.pdf")
    f = result.fields
    assert f["unit_price"].status == "UNCLEAR" and "2O5.00" in f["unit_price"].note
    assert f["item_code"].value == "C" and f["quantity"].value == 800  # the rest of the row is still read
    assert {r.rule_id: r.outcome for r in result.rule_results}["R1"] == "UNKNOWN"


def test_ambiguous_date_is_not_guessed():
    f = run("ambiguous-date.pdf").fields
    assert f["quote_date"].status == "UNCLEAR" and "day and month order unclear" in f["quote_date"].note
    assert str(f["penalty_pct"].value) == "0.7" and "per day" in f["penalty_pct"].note


@pytest.mark.parametrize("text, field, status, note_part", [
    ("Payment terms: Net 45 days from the date of invoice.", "payment_days_min", "UNCLEAR", "from the invoice"),
    ("Payment terms: payment within 30 days.", "payment_days_min", "UNCLEAR", "does not say"),
    ("Delivery: within 20 working days of PO.", "delivery_days", "UNCLEAR", "working days"),
    ("Delivery: 25 days.", "delivery_days", "UNCLEAR", "does not say"),
    ("This quotation is valid until 30/11/2026.", "validity_days", "UNCLEAR", "quotation date"),
    ("Quotation no. Q-1 Date: 31/02/2026", "quote_date", "UNCLEAR", "not a valid date"),
])
def test_wording_variants_are_flagged_not_guessed(text, field, status, note_part):
    f = extract_regex(text).get(field)
    assert f.status == status and note_part in (f.note or ""), f


def test_lakh_number_format():
    text = "Sub-total: Tk 56,64,000.00\nVAT @ 15%: Tk 8,49,600.00\nGrand Total: Tk 65,13,600.00"
    f = extract_regex(text).fields
    assert (str(f["subtotal"].value), str(f["vat_amount"].value), str(f["total"].value)) == \
        ("5664000.00", "849600.00", "6513600.00")


def test_different_supplier_names_are_read_from_the_document():
    names = {run(n).fields["supplier_name"].value for n in
             ("key-value-labels.pdf", "pipe-table-reordered.pdf", "two-pages.pdf")}
    assert names == {"Nilphamari Textile Co.", "Rangpur Trims House", "Jamuna Packaging Co"}


# ---------------------------------------------------------------- live LLM on an unseen layout (API stubbed)

def _live(monkeypatch, answer):
    monkeypatch.setenv("OPENAI_API_KEY", "test-key")
    monkeypatch.setattr(extract_llm, "call_openai", lambda text, cfg: answer(text))
    monkeypatch.setattr(extract_llm, "save_cached", lambda *a, **k: None)


def test_live_mode_cross_checks_unseen_layout(monkeypatch):
    _live(monkeypatch, lambda text: llm_answer_from(extract_regex(text)))
    result = run("key-value-labels.pdf", mode="live")
    assert result.decision == "APPROVE"
    assert result.fields["unit_price"].verification == "matched"
    assert {c.source for c in result.fields["unit_price"].candidates} == {"llm", "regex"}


def test_llm_cannot_overrule_the_rules(monkeypatch):
    """The LLM says delivery is 30 days; the document says 50. Result: a person decides, never Approve."""
    def lie(text):
        answer = llm_answer_from(extract_regex(text))
        answer["delivery_days"] = {"value": "30 days", "quote": "- Delivery: within 50 days from PO.",
                                   "status": "FOUND", "note": None}
        return answer
    _live(monkeypatch, lie)
    result = run("pipe-table-reordered.pdf", mode="live")
    assert result.decision != "APPROVE"
    assert result.fields["delivery_days"].verification == "unverified"  # 30 is not in the quoted text


def test_llm_and_regex_disagreement_goes_to_a_person(monkeypatch):
    def other_price(text):
        answer = llm_answer_from(extract_regex(text))
        answer["unit_price"] = {"value": "36.00", "quote": "Metal zipper, 18 cm | B | 20,000 | pcs | 36.50 | 730,000.00",
                                "status": "FOUND", "note": None}
        return answer
    _live(monkeypatch, other_price)
    result = run("pipe-table-reordered.pdf", mode="live")
    price = result.fields["unit_price"]
    assert price.status == "UNCLEAR" and price.verification in ("unverified", "conflict")
    assert result.decision != "APPROVE"


# ---------------------------------------------------------------- no answer keys or recordings at runtime

def test_runtime_code_never_reads_answer_keys_or_recordings():
    runtime = [*(ROOT / "checker").glob("*.py"), *(ROOT / "service").glob("*.py"),
               *(ROOT / "ui").rglob("*.py"), ROOT / "app.py"]
    for path in runtime:
        source = path.read_text(encoding="utf-8")
        assert "expected.csv" not in source, path
    # recordings are only reachable through explicit replay mode
    graph = (ROOT / "checker" / "graph.py").read_text(encoding="utf-8")
    assert graph.count("load_cached") == 1 and 'if mode == "live"' in graph
