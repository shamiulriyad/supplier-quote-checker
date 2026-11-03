"""Step 6: company rules (company-rules.md) as plain Python. Thresholds come from config.yaml.

Every rule returns PASS, FAIL, UNKNOWN (could not be checked) or N/A, with the exact
document text it relied on. UNKNOWN never leads to Approve.
"""
from decimal import Decimal

from .match import pr_text
from .models import ExtractedPack, Field, RuleResult


def _res(rule_id, title, outcome, reason, *evidence) -> RuleResult:
    category = "reject" if rule_id.startswith("R") else "review"
    return RuleResult(rule_id=rule_id, category=category, title=title, outcome=outcome, reason=reason,
                      evidence=[e for e in dict.fromkeys(evidence) if e])


def _why_unknown(name: str, f: Field) -> str:
    label = name.replace("_", " ")
    return f"Could not read the {label} with certainty ({f.note or f.status.lower().replace('_', ' ')})."


def _pct_over(price: Decimal, max_price: Decimal) -> Decimal:
    return ((price - max_price) / max_price * 100).quantize(Decimal("0.01"))


def price_rules(ex: ExtractedPack, pr: dict | None, rules: dict) -> list[RuleResult]:
    t1, t2 = "Price more than 3% above maximum", "Price above maximum (within 3%)"
    price = ex.get("unit_price")
    if pr is None or not price.ok:
        why = "No matching purchase-request line." if pr is None else _why_unknown("unit_price", price)
        return [_res("R1", t1, "UNKNOWN", why, price.quote), _res("N1", t2, "UNKNOWN", why, price.quote)]

    p, mx = Decimal(price.value), pr["max_price"]
    limit = mx * (1 + rules["reject_price_over_pct"] / 100)
    over = _pct_over(p, mx)
    vat_note = " (price includes VAT; compared as quoted)" if ex.get("price_includes_vat").value is True else ""
    ev = (price.quote, pr_text(pr))
    if p > limit:
        return [_res("R1", t1, "FAIL", f"Unit price {p} is {over}% above the maximum {mx} "
                     f"(limit {limit:.3f}){vat_note}.", *ev),
                _res("N1", t2, "N/A", "Covered by R1.")]
    r1 = _res("R1", t1, "PASS", f"Unit price {p} is within {rules['reject_price_over_pct']}% of the maximum {mx}{vat_note}.", *ev)
    if p > mx:
        return [r1, _res("N1", t2, "FAIL", f"Unit price {p} is {over}% above the maximum {mx} "
                         f"(not more than {rules['reject_price_over_pct']}%){vat_note}.", *ev)]
    return [r1, _res("N1", t2, "PASS", f"Unit price {p} is at or below the maximum {mx}{vat_note}.", *ev)]


def advance_rule(ex: ExtractedPack, rules: dict) -> RuleResult:
    t, limit = "Advance payment more than 20%", rules["max_advance_pct"]
    f = ex.get("advance_pct")
    if f.status == "FOUND" and f.value is not None:
        v = Decimal(f.value)
        if v > limit:
            return _res("R2", t, "FAIL", f"Advance payment is {v}% (limit {limit}%).", f.quote)
        return _res("R2", t, "PASS", f"Advance payment is {v}% (limit {limit}%).", f.quote)
    if f.status == "UNCLEAR" and f.value is not None and Decimal(f.value) > limit:
        # value holds the lowest possible reading, so every reading is over the limit
        return _res("R2", t, "FAIL", f"Advance wording is unclear ({f.note}), but even the lowest reading, "
                    f"{f.value}%, is above the {limit}% limit.", f.quote)
    return _res("R2", t, "UNKNOWN", _why_unknown("advance_pct", f), f.quote)


def penalty_clause_rule(ex: ExtractedPack) -> RuleResult:
    t = "No late-delivery penalty clause"
    f = ex.get("penalty_pct")
    if f.status == "NOT_FOUND":
        note = f" ({f.note})" if f.note else ""
        return _res("R3", t, "FAIL", f"The contract has no late-delivery penalty clause{note}.", f.quote)
    if f.status == "FOUND" or f.value is not None:
        return _res("R3", t, "PASS", "A late-delivery penalty clause is present.", f.quote)
    # e.g. the LLM reports a clause the regex check cannot find: do not trust either side
    return _res("R3", t, "UNKNOWN", _why_unknown("penalty clause", f), f.quote)


def registration_rule(ex: ExtractedPack) -> RuleResult:
    t = "Supplier TIN or BIN missing"
    tin, bin_ = ex.get("tin"), ex.get("bin")
    missing = [n for n, f in (("TIN", tin), ("BIN", bin_)) if f.status == "NOT_FOUND"]
    if missing:
        return _res("R4", t, "FAIL", f"Supplier {' and '.join(missing)} not given in the document.",
                    tin.quote, bin_.quote)
    if tin.ok and bin_.ok:
        return _res("R4", t, "PASS", f"TIN {tin.value}, BIN {bin_.value}.", tin.quote, bin_.quote)
    return _res("R4", t, "UNKNOWN", "Could not read the TIN/BIN with certainty.", tin.quote, bin_.quote)


def delivery_rules(ex: ExtractedPack, rules: dict) -> list[RuleResult]:
    t5, t4 = "Delivery later than 45 days", "Delivery in 31 to 45 days"
    f = ex.get("delivery_days")
    if not f.ok:
        why = _why_unknown("delivery_days", f)
        return [_res("R5", t5, "UNKNOWN", why, f.quote), _res("N4", t4, "UNKNOWN", why, f.quote)]
    d, conv = int(f.value), f" ({f.note})" if f.note else ""
    hi, lo = int(rules["max_delivery_days"]), int(rules["review_delivery_from_days"])
    if d > hi:
        return [_res("R5", t5, "FAIL", f"Delivery promised in {d} days{conv}; the limit is {hi} days.", f.quote),
                _res("N4", t4, "N/A", "Covered by R5.")]
    r5 = _res("R5", t5, "PASS", f"Delivery in {d} days{conv}.", f.quote)
    if d >= lo:
        return [r5, _res("N4", t4, "FAIL", f"Delivery promised in {d} days{conv}, which is in the {lo}-{hi} day "
                         "range that needs a person to check.", f.quote)]
    return [r5, _res("N4", t4, "PASS", f"Delivery in {d} days{conv}.", f.quote)]


def quantity_rule(ex: ExtractedPack, pr: dict | None, rules: dict) -> RuleResult:
    t, tol = "Quantity differs by more than 5%", rules["qty_tolerance_pct"]
    f = ex.get("quantity")
    if pr is None or not f.ok:
        return _res("N2", t, "UNKNOWN", "No matching purchase-request line." if pr is None
                    else _why_unknown("quantity", f), f.quote)
    q, req = Decimal(f.value), pr["quantity"]
    diff = ((q - req) / req * 100).quantize(Decimal("0.01"))
    ev = (f.quote, pr_text(pr))
    if abs(diff) > tol:
        return _res("N2", t, "FAIL", f"Quoted quantity {q:,} vs requested {req:,} ({diff:+}%; limit ±{tol}%).", *ev)
    return _res("N2", t, "PASS", f"Quoted quantity {q:,} vs requested {req:,} ({diff:+}%).", *ev)


def payment_rule(ex: ExtractedPack, rules: dict) -> RuleResult:
    t = "Payment terms unclear or outside 30-60 days"
    lo, hi = int(rules["payment_days_min"]), int(rules["payment_days_max"])
    adv, dmin, dmax = ex.get("advance_pct"), ex.get("payment_days_min"), ex.get("payment_days_max")
    problems = []
    if adv.status == "UNCLEAR":
        problems.append(f"advance wording is unclear ({adv.note})")
    if dmin.status == "NOT_FOUND" or dmax.status == "NOT_FOUND":
        problems.append("no payment period after delivery is stated")
    elif dmin.status == "UNCLEAR" or dmax.status == "UNCLEAR":
        problems.append(f"payment period is unclear ({dmin.note or dmax.note})")
        if dmin.value is not None and dmax.value is not None and (int(dmin.value) < lo or int(dmax.value) > hi):
            problems.append(f"it may fall outside {lo}-{hi} days")
    elif int(dmin.value) < lo or int(dmax.value) > hi:
        problems.append(f"payment is due {dmin.value} days after delivery, outside {lo}-{hi} days")
    ev = (adv.quote if adv.status == "UNCLEAR" else None, dmin.quote, dmax.quote)
    if problems:
        return _res("N3", t, "FAIL", "Payment terms: " + "; ".join(problems) + ".", *ev)
    return _res("N3", t, "PASS", f"Payment due {dmin.value} days after delivery (allowed {lo}-{hi}).", dmin.quote)


def vat_rule(ex: ExtractedPack, rules: dict) -> RuleResult:
    t, rate = "VAT not shown separately at 15%", rules["vat_rate_pct"]
    inc, vr = ex.get("price_includes_vat"), ex.get("vat_rate")
    if inc.value is True:
        return _res("N5", t, "FAIL", "Prices are quoted including VAT; VAT is not shown separately.", inc.quote)
    if not vr.ok:
        return _res("N5", t, "FAIL", f"VAT is not shown separately ({vr.note}).", vr.quote)
    if Decimal(vr.value) != rate:
        return _res("N5", t, "FAIL", f"VAT is shown at {vr.value}%, not {rate}%.", vr.quote)
    return _res("N5", t, "PASS", f"VAT shown separately at {rate}%.", vr.quote)


def math_rule(math_results: list[RuleResult]) -> RuleResult:
    t = "Numbers do not add up"
    fails = [m for m in math_results if m.outcome == "FAIL"]
    unknown = [m for m in math_results if m.outcome == "UNKNOWN"]
    if fails:
        ev = [e for m in fails for e in m.evidence]
        return _res("N6", t, "FAIL", " ".join(f"{m.title}: {m.reason}" for m in fails), *ev)
    if unknown:
        return _res("N6", t, "UNKNOWN", " ".join(f"{m.title}: {m.reason}" for m in unknown))
    return _res("N6", t, "PASS", "Line amount, subtotal, VAT and total all add up.")


def validity_rule(ex: ExtractedPack, rules: dict) -> RuleResult:
    t, minimum = "Quotation valid for less than 30 days", int(rules["min_validity_days"])
    f = ex.get("validity_days")
    if not f.ok:
        return _res("N7", t, "UNKNOWN", _why_unknown("validity_days", f), f.quote)
    if int(f.value) < minimum:
        return _res("N7", t, "FAIL", f"Quotation is valid for {f.value} days (minimum {minimum}).", f.quote)
    return _res("N7", t, "PASS", f"Quotation is valid for {f.value} days.", f.quote)


def penalty_size_rule(ex: ExtractedPack, rules: dict) -> RuleResult:
    t, minimum = "Penalty at least 0.5% of order value per week", rules["min_penalty_pct_per_week"]
    pct, basis = ex.get("penalty_pct"), ex.get("penalty_basis")
    if pct.status == "NOT_FOUND":
        return _res("P1", t, "N/A", "No penalty clause (see R3).")
    problems = []
    if pct.status == "UNCLEAR":
        problems.append(f"penalty wording is unclear ({pct.note})")
    elif Decimal(pct.value) < minimum:
        problems.append(f"penalty is {pct.value}% per week, below the {minimum}% minimum")
    if not basis.ok:
        problems.append(f"it does not say the percentage is of the order value ({basis.note})")
    elif basis.value != "order value":
        problems.append(f"the percentage is of the {basis.value}, not the order value")
    if problems:
        return _res("P1", t, "FAIL", "Late-delivery penalty: " + "; ".join(problems) + ".", pct.quote)
    return _res("P1", t, "PASS", f"Penalty is {pct.value}% of order value per week.", pct.quote)


def check_rules(ex: ExtractedPack, pr: dict | None, math_results: list[RuleResult], rules: dict) -> list[RuleResult]:
    r1, n1 = price_rules(ex, pr, rules)
    r5, n4 = delivery_rules(ex, rules)
    return [
        r1, advance_rule(ex, rules), penalty_clause_rule(ex), registration_rule(ex), r5,
        n1, quantity_rule(ex, pr, rules), payment_rule(ex, rules), n4, vat_rule(ex, rules),
        math_rule(math_results), validity_rule(ex, rules), penalty_size_rule(ex, rules),
    ]
