"""Boundary tests for every company rule, starting from the clean Pack 01 and changing one value."""
from decimal import Decimal as D

import pytest

from checker.decide import decide
from checker.match import load_purchase_request, match_pr
from checker.config import load_config
from checker.math_check import check_math
from checker.models import Field
from checker.rules import check_rules

from .conftest import with_fields


def outcomes(ex, rules):
    pr, x1 = match_pr(ex, load_purchase_request(load_config()["purchase_request"]))
    results = check_rules(ex, pr, check_math(ex, rules), rules) + [x1]
    return {r.rule_id: r.outcome for r in results}


def decision(ex, rules):
    pr, x1 = match_pr(ex, load_purchase_request(load_config()["purchase_request"]))
    return decide(check_rules(ex, pr, check_math(ex, rules), rules) + [x1], [])[0]


def test_clean_pack_approves(base_pack, rules):
    assert decision(base_pack, rules) == "APPROVE"


# Item A max price is 480.00, so +3% = 494.40
@pytest.mark.parametrize("price, r1, n1", [
    ("480.00", "PASS", "PASS"),
    ("480.01", "PASS", "FAIL"),
    ("494.40", "PASS", "FAIL"),   # exactly 3% above: not "more than 3%"
    ("494.41", "FAIL", "N/A"),
])
def test_price_thresholds(base_pack, rules, price, r1, n1):
    p = D(price)
    ex = with_fields(base_pack, unit_price=p, line_amount=p * 12000, subtotal=p * 12000,
                     vat_amount=(p * 12000 * D("0.15")).quantize(D("0.01")),
                     total=p * 12000 + (p * 12000 * D("0.15")).quantize(D("0.01")))
    o = outcomes(ex, rules)
    assert (o["R1"], o["N1"]) == (r1, n1)


@pytest.mark.parametrize("advance, outcome", [(0, "PASS"), (20, "PASS"), (21, "FAIL")])
def test_advance(base_pack, rules, advance, outcome):
    assert outcomes(with_fields(base_pack, advance_pct=D(advance)), rules)["R2"] == outcome


def test_unclear_advance_rejects_only_if_every_reading_is_too_high(base_pack, rules):
    high = Field(value=D(40), quote="q", status="UNCLEAR", note="stated twice")
    low = Field(value=D(10), quote="q", status="UNCLEAR", note="stated twice")
    assert outcomes(with_fields(base_pack, advance_pct=high), rules)["R2"] == "FAIL"
    assert outcomes(with_fields(base_pack, advance_pct=low), rules)["R2"] == "UNKNOWN"


@pytest.mark.parametrize("days, r5, n4", [(30, "PASS", "PASS"), (31, "PASS", "FAIL"), (45, "PASS", "FAIL"),
                                          (46, "FAIL", "N/A")])
def test_delivery(base_pack, rules, days, r5, n4):
    o = outcomes(with_fields(base_pack, delivery_days=days), rules)
    assert (o["R5"], o["N4"]) == (r5, n4)


@pytest.mark.parametrize("qty, outcome", [(12600, "PASS"), (12601, "FAIL"), (11400, "PASS"), (11399, "FAIL")])
def test_quantity_5_percent(base_pack, rules, qty, outcome):
    q = D(qty)
    amount = q * D("472.00")
    vat = (amount * D("0.15")).quantize(D("0.01"))
    ex = with_fields(base_pack, quantity=q, line_amount=amount, subtotal=amount, vat_amount=vat, total=amount + vat)
    assert outcomes(ex, rules)["N2"] == outcome


@pytest.mark.parametrize("lo, hi, outcome", [(30, 30, "PASS"), (60, 60, "PASS"), (29, 29, "FAIL"), (61, 61, "FAIL")])
def test_payment_days(base_pack, rules, lo, hi, outcome):
    assert outcomes(with_fields(base_pack, payment_days_min=lo, payment_days_max=hi), rules)["N3"] == outcome


def test_payment_range_needs_check(base_pack, rules):
    ex = with_fields(base_pack,
                     payment_days_min=Field(value=30, quote="q", status="UNCLEAR", note="range"),
                     payment_days_max=Field(value=90, quote="q", status="UNCLEAR", note="range"))
    assert outcomes(ex, rules)["N3"] == "FAIL"
    assert decision(ex, rules) == "NEEDS_REVIEW"


@pytest.mark.parametrize("days, outcome", [(29, "FAIL"), (30, "PASS")])
def test_validity(base_pack, rules, days, outcome):
    assert outcomes(with_fields(base_pack, validity_days=days), rules)["N7"] == outcome


@pytest.mark.parametrize("pct, outcome", [("0.5", "PASS"), ("0.49", "FAIL")])
def test_penalty_size(base_pack, rules, pct, outcome):
    assert outcomes(with_fields(base_pack, penalty_pct=D(pct)), rules)["P1"] == outcome


def test_missing_penalty_clause_rejects(base_pack, rules):
    ex = with_fields(base_pack, penalty_pct=Field(status="NOT_FOUND"), penalty_basis=Field(status="NOT_FOUND"))
    assert outcomes(ex, rules)["R3"] == "FAIL"
    assert decision(ex, rules) == "REJECT"


def test_missing_bin_rejects(base_pack, rules):
    assert outcomes(with_fields(base_pack, bin=Field(status="NOT_FOUND")), rules)["R4"] == "FAIL"


def test_vat_rules(base_pack, rules):
    assert outcomes(with_fields(base_pack, vat_rate=D(10)), rules)["N5"] == "FAIL"
    assert outcomes(with_fields(base_pack, price_includes_vat=True), rules)["N5"] == "FAIL"


def test_wrong_line_amount(base_pack, rules):
    o = outcomes(with_fields(base_pack, line_amount=D("5666000.00")), rules)
    assert o["N6"] == "FAIL"


def test_unreadable_price_is_never_approved(base_pack, rules):
    ex = with_fields(base_pack, unit_price=Field(status="UNCLEAR", note="LLM and regex disagree"))
    o = outcomes(ex, rules)
    assert o["R1"] == "UNKNOWN"
    assert decision(ex, rules) == "NEEDS_REVIEW"


def test_pipeline_error_overrides_reject(base_pack, rules):
    ex = with_fields(base_pack, advance_pct=D(50))
    pr, x1 = match_pr(ex, load_purchase_request(load_config()["purchase_request"]))
    results = check_rules(ex, pr, check_math(ex, rules), rules) + [x1]
    assert decide(results, [])[0] == "REJECT"
    assert decide(results, ["LLM failed"])[0] == "NEEDS_REVIEW"
