"""Step 5: re-do the arithmetic on the quotation. All maths is done here in code, never by the LLM."""
from decimal import Decimal

from .models import ExtractedPack, RuleResult


def _r(rule_id, title, outcome, reason, evidence=()) -> RuleResult:
    return RuleResult(rule_id=rule_id, category="math", title=title, outcome=outcome,
                      reason=reason, evidence=[e for e in dict.fromkeys(evidence) if e])


def _need(ex: ExtractedPack, *names):
    """Values if every field is FOUND, else None."""
    fields = [ex.get(n) for n in names]
    if all(f.ok for f in fields):
        return [Decimal(f.value) for f in fields]
    return None


def _missing(ex, *names) -> str:
    return ", ".join(n.replace("_", " ") for n in names if not ex.get(n).ok)


def check_math(ex: ExtractedPack, rules: dict) -> list[RuleResult]:
    tol = rules["math_tolerance"]
    vat_pct = rules["vat_rate_pct"]
    q = lambda *names: [ex.get(n).quote for n in names]
    out = []

    # M1 quantity x unit price = line amount
    t = "Line amount = quantity x unit price"
    v = _need(ex, "quantity", "unit_price", "line_amount")
    if v is None:
        out.append(_r("M1", t, "UNKNOWN", f"Missing {_missing(ex, 'quantity', 'unit_price', 'line_amount')}."))
    else:
        qty, price, amount = v
        expected = qty * price
        ok = abs(expected - amount) <= tol
        out.append(_r("M1", t, "PASS" if ok else "FAIL",
                      f"{qty:,} x {price} = {expected:,.2f}; document says {amount:,.2f}"
                      + ("" if ok else f" (difference {amount - expected:+,.2f})."), q("line_amount")))

    vat_separate = ex.get("price_includes_vat").value is False

    # M2 subtotal = line amount (single-item quotes)
    t = "Subtotal = sum of line amounts"
    if not ex.get("subtotal").ok and not vat_separate:
        out.append(_r("M2", t, "N/A", "No subtotal line (prices quoted including VAT)."))
    else:
        v = _need(ex, "line_amount", "subtotal")
        if v is None:
            out.append(_r("M2", t, "UNKNOWN", f"Missing {_missing(ex, 'line_amount', 'subtotal')}."))
        else:
            amount, sub = v
            ok = abs(amount - sub) <= tol
            out.append(_r("M2", t, "PASS" if ok else "FAIL",
                          f"Line amount {amount:,.2f}, subtotal {sub:,.2f}.", q("line_amount", "subtotal")))

    # M3 VAT amount = subtotal x VAT rate,  M4 total = subtotal + VAT
    if vat_separate:
        v = _need(ex, "subtotal", "vat_amount")
        if v is None:
            out.append(_r("M3", "VAT amount = subtotal x 15%", "UNKNOWN",
                          f"Missing {_missing(ex, 'subtotal', 'vat_amount')}."))
        else:
            sub, vat = v
            expected = (sub * vat_pct / 100).quantize(Decimal("0.01"))
            ok = abs(expected - vat) <= tol
            out.append(_r("M3", "VAT amount = subtotal x 15%", "PASS" if ok else "FAIL",
                          f"{vat_pct}% of {sub:,.2f} = {expected:,.2f}; document says {vat:,.2f}.",
                          q("vat_amount")))
        v = _need(ex, "subtotal", "vat_amount", "total")
        if v is None:
            out.append(_r("M4", "Total = subtotal + VAT", "UNKNOWN",
                          f"Missing {_missing(ex, 'subtotal', 'vat_amount', 'total')}."))
        else:
            sub, vat, total = v
            ok = abs(sub + vat - total) <= tol
            out.append(_r("M4", "Total = subtotal + VAT", "PASS" if ok else "FAIL",
                          f"{sub:,.2f} + {vat:,.2f} = {sub + vat:,.2f}; document says {total:,.2f}.", q("total")))
    else:
        v = _need(ex, "line_amount", "total")
        if v is None:
            out.append(_r("M4", "Total = line amount", "UNKNOWN", f"Missing {_missing(ex, 'line_amount', 'total')}."))
        else:
            amount, total = v
            ok = abs(amount - total) <= tol
            out.append(_r("M4", "Total = line amount", "PASS" if ok else "FAIL",
                          f"Line amount {amount:,.2f}, total {total:,.2f}.", q("total")))
    return out
