"""Step 2a: rule-based extractor. Works offline and is used to cross-check the LLM."""
import re
from decimal import Decimal

from .models import ExtractedPack, Field
from .parse import sentence_at, split_clauses

NUM = r"\d[\d,]*(?:\.\d+)?"
SRC = "regex"


def to_dec(s: str) -> Decimal:
    return Decimal(s.replace(",", ""))


def found(value, quote, note=None) -> Field:
    return Field(value=value, quote=quote, status="FOUND", source=SRC, note=note)


def unclear(value, quote, note) -> Field:
    return Field(value=value, quote=quote, status="UNCLEAR", source=SRC, note=note)


def not_found(note, quote=None) -> Field:
    return Field(value=None, quote=quote, status="NOT_FOUND", source=SRC, note=note)


def _search(pattern, text, flags=re.M):
    return re.search(pattern, text, flags)


def _header(text: str, f: dict) -> None:
    lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
    f["supplier_name"] = found(lines[0], lines[0]) if lines else not_found("document is empty")

    for name, pattern in (
        ("quotation_no", r"Quotation no\.?\s*([A-Z]{2,}-[\w-]+)"),
        ("quote_date", r"\bDate\s+(\d{4}-\d{2}-\d{2})"),
        ("pr_reference", r"Reference\s+(PR-[\w-]+)"),
    ):
        m = _search(pattern, text)
        f[name] = found(m.group(1), m.group(0)) if m else not_found(f"no {name.replace('_', ' ')} found")


def _items(text: str, f: dict) -> None:
    item_re = re.compile(rf"^([A-Z])\s+(.+?)\s+({NUM})\s+([A-Za-z]+)\s+({NUM})\s+({NUM})\s*$", re.M)
    rows = item_re.findall(text)
    lines = [m.group(0).strip() for m in item_re.finditer(text)]
    names = ("item_code", "description", "quantity", "unit", "unit_price", "line_amount")
    if not rows:
        for name in names:
            f[name] = not_found("no item line found in the quotation table")
        return
    code, desc, qty, unit, price, amount = rows[0]
    values = (code, desc.strip(), to_dec(qty), unit, to_dec(price), to_dec(amount))
    for name, value in zip(names, values):
        if len(rows) > 1:
            f[name] = unclear(value, lines[0], f"{len(rows)} item lines found; only single-item quotes are supported")
        else:
            f[name] = found(value, lines[0])


def _totals(text: str, f: dict) -> None:
    m = _search(rf"^Subtotal\s+({NUM})\s*$", text)
    f["subtotal"] = found(to_dec(m.group(1)), m.group(0).strip()) if m else not_found("no subtotal line")

    m = _search(rf"^VAT\s+(\d+(?:\.\d+)?)\s*%\s+({NUM})\s*$", text)
    if m:
        f["vat_rate"] = found(Decimal(m.group(1)), m.group(0).strip())
        f["vat_amount"] = found(to_dec(m.group(2)), m.group(0).strip())
    else:
        f["vat_rate"] = not_found("VAT is not shown as a separate line")
        f["vat_amount"] = not_found("VAT is not shown as a separate line")

    m = _search(rf"^Total\b[^\n]*?(?<![\d,.])({NUM})\s*$", text)
    f["total"] = found(to_dec(m.group(1)), m.group(0).strip()) if m else not_found("no total line")

    inc = _search(r"[^\n]*(VAT included|VAT inclusive|inclusive of VAT|taxes included)[^\n]*", text, re.I | re.M)
    if inc:
        f["price_includes_vat"] = found(True, inc.group(0).strip())
    elif f["vat_rate"].ok:
        f["price_includes_vat"] = found(False, f["vat_rate"].quote)
    else:
        f["price_includes_vat"] = not_found("document does not say whether VAT is included")


def _registration(text: str, f: dict) -> None:
    tin = _search(r"\bTIN\s*:?\s*(\d{6,})", text)
    bin_ = _search(r"\bBIN\s*:?\s*(\d[\d-]{5,}\d)", text)
    hint = _search(r"^[^\n]*(registration|\bTIN\b|\bBIN\b)[^\n]*$", text, re.I | re.M)
    hint_line = hint.group(0).strip() if hint else None
    f["tin"] = found(tin.group(1), hint_line) if tin else not_found("supplier TIN not given", hint_line)
    f["bin"] = found(bin_.group(1), hint_line) if bin_ else not_found("supplier BIN not given", hint_line)


def _payment(clauses: dict, f: dict) -> None:
    p = clauses.get("payment")
    if not p:
        for name in ("advance_pct", "payment_days_min", "payment_days_max"):
            f[name] = not_found("no payment clause")
        return

    # advance
    no_adv = re.search(r"no advance", p, re.I)
    advs = list(re.finditer(r"(\d+(?:\.\d+)?)\s*%\s+advance", p, re.I))
    if no_adv and not advs:
        f["advance_pct"] = found(Decimal(0), sentence_at(p, no_adv.start()))
    elif len(advs) == 1:
        f["advance_pct"] = found(Decimal(advs[0].group(1)), sentence_at(p, advs[0].start()))
    elif advs:
        vals = [Decimal(m.group(1)) for m in advs]
        quote = sentence_at(p, advs[0].start(), advs[-1].end())
        f["advance_pct"] = unclear(
            min(vals), quote,
            f"advance is stated {len(vals)} times ({', '.join(f'{v}%' for v in vals)}); "
            f"it could mean {min(vals)}% or {sum(vals)}% in total",
        )
    else:
        f["advance_pct"] = not_found("payment clause does not mention an advance", p)

    # days after delivery
    rng = re.search(r"(\d+)\s*(?:to|-)\s*(\d+)\s*days", p, re.I)
    singles = list(re.finditer(r"within\s+(\d+)\s*days", p, re.I))
    vague = re.search(r"as agreed|to be agreed|mutually|negotiable|upon agreement", p, re.I)
    if rng:
        lo, hi = int(rng.group(1)), int(rng.group(2))
        note = f"payment period is a range ({lo} to {hi} days)" + (" and left to agreement" if vague else "")
        q = sentence_at(p, rng.start())
        f["payment_days_min"] = unclear(lo, q, note)
        f["payment_days_max"] = unclear(hi, q, note)
    elif len({m.group(1) for m in singles}) == 1:
        days = int(singles[0].group(1))
        q = sentence_at(p, singles[0].start())
        make = (lambda v: unclear(v, q, "payment period is left to agreement")) if vague else (lambda v: found(v, q))
        f["payment_days_min"] = make(days)
        f["payment_days_max"] = make(days)
    elif singles:
        q = sentence_at(p, singles[0].start(), singles[-1].end())
        days = sorted(int(m.group(1)) for m in singles)
        f["payment_days_min"] = unclear(days[0], q, "several different payment periods are stated")
        f["payment_days_max"] = unclear(days[-1], q, "several different payment periods are stated")
    else:
        f["payment_days_min"] = not_found("no payment period in days", p)
        f["payment_days_max"] = not_found("no payment period in days", p)


def _duration_days(clause: str, pattern_prefix: str) -> tuple[int | None, re.Match | None, str | None]:
    m = re.search(pattern_prefix + r"\s*(\d+)\s*(days?|weeks?|months?)", clause, re.I)
    if not m:
        return None, None, None
    n, unit = int(m.group(1)), m.group(2).lower()
    if unit.startswith("week"):
        return n * 7, m, f"converted from {n} weeks"
    if unit.startswith("month"):
        return n * 30, m, f"converted from {n} months (30 days each)"
    return n, m, None


def _delivery(clauses: dict, f: dict) -> None:
    d = clauses.get("delivery")
    if not d:
        f["delivery_days"] = not_found("no delivery clause")
        return
    days, m, note = _duration_days(d, r"within")
    if days is None:
        f["delivery_days"] = not_found("delivery clause has no period in days or weeks", d)
    else:
        f["delivery_days"] = found(days, sentence_at(d, m.start()), note)


def _penalty(clauses: dict, f: dict) -> None:
    clause = clauses.get("delay") or next(
        (c for c in clauses.values() if re.search(r"penalty|late delivery|delay in delivery", c, re.I)), None
    )
    if not clause:
        present = ", ".join(c.title() for c in clauses) or "none"
        note = f"clauses present: {present}"
        f["penalty_pct"] = not_found(note)
        f["penalty_basis"] = not_found(note)
        return
    m = re.search(r"(\d+(?:\.\d+)?)\s*%", clause)
    if not m:
        f["penalty_pct"] = unclear(None, clause, "penalty clause has no percentage")
        f["penalty_basis"] = not_found("penalty clause has no percentage", clause)
        return
    q = sentence_at(clause, m.start())
    if re.search(r"per week|each (full )?week|a week|weekly", clause, re.I):
        f["penalty_pct"] = found(Decimal(m.group(1)), q)
    else:
        f["penalty_pct"] = unclear(Decimal(m.group(1)), q, "penalty is not stated per week")
    basis = re.search(r"of (?:the )?(order value|contract value|po value|invoice value)", clause, re.I)
    f["penalty_basis"] = (
        found(basis.group(1).lower(), q) if basis
        else not_found("penalty does not say what it is a percentage of", q)
    )


def _validity(text: str, clauses: dict, f: dict) -> None:
    v = clauses.get("validity") or text
    days, m, note = _duration_days(v, r"valid (?:for|till|until)")
    if days is None:
        f["validity_days"] = not_found("no quotation validity period")
    else:
        f["validity_days"] = found(days, sentence_at(v, m.start()) if v is not text else m.group(0), note)


def extract_regex(text: str) -> ExtractedPack:
    f: dict[str, Field] = {}
    clauses = split_clauses(text)
    _header(text, f)
    _items(text, f)
    _totals(text, f)
    _registration(text, f)
    _payment(clauses, f)
    _delivery(clauses, f)
    _penalty(clauses, f)
    _validity(text, clauses, f)
    return ExtractedPack(fields=f)
