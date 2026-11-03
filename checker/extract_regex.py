"""Step 2a: rule-based extractor. Works offline and is used to cross-check the LLM.

Reads several layouts: the item table by its column headers or a fixed line pattern, "Label: value"
lines, numbered or labelled contract terms, and alternative field names. When different readings of the
same field disagree (two different totals, a table price and a "Unit price:" line that differ, ...),
the field becomes UNCLEAR with every reading kept as a candidate: nothing is silently picked.
"""
import re
from datetime import date
from decimal import Decimal

from .models import AMBIGUOUS, DOC_CONFLICT, Candidate, ExtractedPack, Field
from .normalize import (CURRENCY, DATE, MALFORMED_NUMBER, MONEY, NUM, parse_date, to_decimal, unit_name)
from .parse import normalize, sentence_at, split_clauses

SRC = "regex"


def to_dec(s: str) -> Decimal:
    return Decimal(s.replace(",", ""))


def found(value, quote, note=None) -> Field:
    return Field(value=value, quote=quote, status="FOUND", source=SRC, note=note)


def unclear(value, quote, note, verification: str = AMBIGUOUS, candidates=()) -> Field:
    return Field(value=value, quote=quote, status="UNCLEAR", source=SRC, note=note, verification=verification,
                 candidates=list(candidates))


def not_found(note, quote=None) -> Field:
    return Field(value=None, quote=quote, status="NOT_FOUND", source=SRC, note=note)


def _same(a, b) -> bool:
    if isinstance(a, Decimal) or isinstance(b, Decimal):
        try:
            return Decimal(a) == Decimal(b)
        except Exception:
            return False
    return normalize(str(a)) == normalize(str(b))


def pick(readings: list[tuple[str, object, str]], what: str, missing: str) -> Field:
    """One field from several (method, value, quote) readings. Disagreement -> UNCLEAR with all candidates."""
    readings = [r for r in readings if r[1] is not None]
    if not readings:
        return not_found(missing)
    first = readings[0]
    if all(_same(first[1], r[1]) for r in readings[1:]):
        return found(first[1], first[2])
    values = []
    for _m, v, _q in readings:
        if not any(_same(v, x) for x in values):
            values.append(v)
    return unclear(None, first[2], f"the document gives different {what}: " + ", ".join(map(str, values)),
                   DOC_CONFLICT, [Candidate(source=f"regex: {m}", value=v, quote=q) for m, v, q in readings])


def _line_at(text: str, pos: int) -> str:
    start = text.rfind("\n", 0, pos) + 1
    end = text.find("\n", pos)
    return text[start:end if end != -1 else len(text)].strip()


def _lines(text: str) -> list[str]:
    return [ln.strip() for ln in text.splitlines() if ln.strip()]


# ============================================================== header

GENERIC_TITLES = {"quotation", "quote", "price quotation", "sales quotation", "offer", "price offer", "invoice",
                  "proforma invoice", "pro-forma invoice", "quotation / offer", "commercial offer"}


def _header(text: str, f: dict) -> None:
    lines = _lines(text)
    readings = []
    if m := re.search(r"^\s*(?:supplier|vendor|seller|from|company)(?:\s+name)?\s*[:\-]\s*(.+?)\s*$", text, re.I | re.M):
        readings.append(("label", m.group(1).strip(), m.group(0).strip()))
    else:
        top = next((ln for ln in lines[:4] if ln.lower().strip(" :") not in GENERIC_TITLES and ":" not in ln
                    and not re.match(r"^(to|date|ref)\b", ln, re.I)), None)
        if top:
            readings.append(("letterhead", top, top))
    for line in lines[-6:]:  # signature block ("For <Supplier>") near the end only
        if (m := re.match(r"^For\s+([A-Za-z][^\d:]{2,80})$", line)) and len(m.group(1).split()) <= 10:
            readings.append(("signature", m.group(1).strip(), line))
            break
    f["supplier_name"] = pick(readings, "supplier names", "no supplier name found")

    nos = [(m.group(1), m.group(0).strip()) for m in re.finditer(
        r"\b(?:quotation|quote|offer)\s*(?:no\.?|number|#|ref(?:erence)?(?:\s*no\.?)?)\s*[:\-]?\s*"
        r"([A-Z0-9][A-Z0-9\-/]{2,})", text, re.I)]
    f["quotation_no"] = pick([("label", v, q) for v, q in nos], "quotation numbers", "no quotation number found")

    dates = []
    problems = []
    for m in re.finditer(rf"\b(?:quotation\s+|quote\s+|issue\s+|offer\s+)?date[d]?\s*[:\-]?\s*({DATE})", text, re.I):
        iso, problem = parse_date(m.group(1))
        if iso:
            dates.append(("label", iso, m.group(0).strip()))
        else:
            problems.append((problem, m.group(0).strip()))
    if not dates and problems:
        f["quote_date"] = unclear(None, problems[0][1], problems[0][0])
    else:
        f["quote_date"] = pick(dates, "quotation dates", "no quotation date found")

    refs = [(re.sub(r"[-\s]+", "-", m.group(1).upper()), _line_at(text, m.start()))
            for m in re.finditer(r"\b(PR[-\s]?\d{4}[-\s]?\d+)\b", text, re.I)]
    f["pr_reference"] = pick([("reference", v, q) for v, q in refs], "purchase-request references",
                             "no purchase-request reference found")


# ============================================================== item line

ITEM_FIELDS = ("item_code", "description", "quantity", "unit", "unit_price", "line_amount")
_CODE = re.compile(r"^(?:[A-Z]{1,2}\d{0,3}|\d{1,3}[.)]?)$")
_NUM_TOKEN = re.compile(rf"^(?:{CURRENCY})?(?:{NUM})(?:/-)?$")
_STOP = re.compile(r"^\s*(?:sub[\s\-]?total|grand\s+total|total|vat|value\s+added|net\s+amount|supplier|terms|"
                   r"tin\b|bin\b|for\s)", re.I)
# column keywords in a table header, longest first
_HEADER_WORDS = [("desc", r"item\s+description|description|particulars|product|goods"),
                 ("price", r"unit\s*(?:price|rate|cost)|price|rate"),
                 ("amount", r"amount|line\s*total|value|total"),
                 ("qty", r"quantity|qty"),
                 ("unit", r"unit|uom|u/m"),
                 ("code", r"item(?:\s*(?:code|no\.?|#))?|code|sl\.?|s/n")]


def _fixed_line(text: str) -> list[tuple]:
    """The original layout: 'A Description 12,000 metre 472.00 5,664,000.00'."""
    item_re = re.compile(rf"^([A-Z])\s+(.+?)\s+({NUM})\s+([A-Za-z]+)\s+({NUM})\s+({NUM})\s*$", re.M)
    return [(m.group(1), m.group(2).strip(), to_dec(m.group(3)), unit_name(m.group(4)) or m.group(4),
             to_dec(m.group(5)), to_dec(m.group(6)), m.group(0).strip()) for m in item_re.finditer(text)]


def _header_columns(line: str) -> list[str] | None:
    """Column order from a header line such as 'Description | Qty | Unit | Rate (BDT) | Amount'."""
    pattern = "|".join(f"(?P<{name}>\\b(?:{rx})\\b)" for name, rx in _HEADER_WORDS)
    cols = []
    for m in re.finditer(pattern, line, re.I):
        name = m.lastgroup
        if name not in cols:
            cols.append(name)
    numeric = [c for c in cols if c in ("qty", "price", "amount")]
    return cols if len(numeric) == 3 and "desc" in cols else None


def _cell_name(cell: str) -> str | None:
    cols = _header_columns_any(cell)
    return cols[0] if cols else None


def _header_columns_any(line: str) -> list[str]:
    pattern = "|".join(f"(?P<{name}>\\b(?:{rx})\\b)" for name, rx in _HEADER_WORDS)
    return [m.lastgroup for m in re.finditer(pattern, line, re.I)]


def _pipe_table(lines: list[str], i: int) -> tuple[list[tuple], str | None]:
    """'Description | Item | Qty | ...' tables: map cells to columns by the header cells."""
    names = [_cell_name(c) for c in lines[i].split("|")]
    rows, problem = [], None
    for row in lines[i + 1:]:
        if not row.strip() or _STOP.match(row) or "|" not in row:
            break
        cells = [c.strip() for c in row.split("|")]
        if set(row.strip()) <= set("|-+= "):
            continue  # separator line
        cell = {n: c for n, c in zip(names, cells) if n}
        bad = [c for c in cell.values() if MALFORMED_NUMBER.match(c.replace(" ", ""))]
        if bad:
            problem = f"unreadable number '{bad[0]}' in the item table"
        values = {k: to_decimal(cell.get(k, "")) for k in ("qty", "price", "amount")}
        qty_cell = cell.get("qty", "")
        if values["qty"] is None and (m := re.fullmatch(rf"({NUM})\s*([A-Za-z]+)", qty_cell)):
            values["qty"] = to_dec(m.group(1))
            cell.setdefault("unit", m.group(2))
        if None in values.values() and not bad:
            continue  # not an item row
        # a row with an unreadable number is kept: its other cells are still evidence
        code = cell.get("code", "")
        rows.append((code.rstrip(".)") if _CODE.match(code or "") else None, cell.get("desc") or None, values["qty"],
                     unit_name(cell.get("unit")), values["price"], values["amount"], row.strip()))
    return rows, problem


def _table(text: str) -> tuple[list[tuple], str | None]:
    """Rows under a header, read by the header's column order. Returns (rows, problem)."""
    lines = text.splitlines()
    for i, line in enumerate(lines):
        cols = _header_columns(line)
        if not cols:
            continue
        if line.count("|") >= 2:
            rows, problem = _pipe_table(lines, i)
            if rows or problem:
                return rows, problem
            continue
        numeric_order = [c for c in cols if c in ("qty", "price", "amount")]
        desc_first = cols.index("desc") < cols.index(numeric_order[0])
        # a first column of short codes ("A", "B2", "1.") if the header has a code column or starts with Item/Sl/No
        has_code = "code" in cols or bool(re.match(r"\s*(?:item|sl\.?|s/n|no\.?|#|code)\b", line, re.I))
        rows, problem = [], None
        for row in lines[i + 1:]:
            if not row.strip() or _STOP.match(row):
                break
            tokens = row.replace("|", " ").split()
            nums = [k for k, t in enumerate(tokens) if _NUM_TOKEN.match(t)]
            bad = [t for t in tokens if MALFORMED_NUMBER.match(t)]
            if bad:
                problem = f"unreadable number '{bad[0]}' in the item table"
            if len(nums) < 3:
                continue
            take = nums[-3:] if desc_first else nums[:3]
            values = dict(zip(numeric_order, (to_decimal(tokens[k]) for k in take)))
            qty_pos = take[numeric_order.index("qty")]
            unit_pos = next((k for k in (qty_pos + 1, qty_pos - 1) if 0 <= k < len(tokens)
                             and k not in take and unit_name(tokens[k])), None)
            code = tokens[0] if has_code and _CODE.match(tokens[0]) and 0 not in take else None
            used = set(take) | ({unit_pos} if unit_pos is not None else set()) | ({0} if code else set())
            span = range(0, take[0]) if desc_first else range(take[-1] + 1, len(tokens))
            desc = " ".join(tokens[k] for k in span if k not in used).strip(" |")
            rows.append((code.rstrip(".)") if code else None, desc or None, values["qty"],
                         unit_name(tokens[unit_pos]) if unit_pos is not None else None,
                         values["price"], values["amount"], row.strip()))
        if rows or problem:
            return rows, problem
    return [], None


def _key_values(text: str) -> dict:
    """'Quantity: 12,000 metres', 'Unit price: BDT 472.00', 'Amount: ...' lines."""
    out = {}
    if m := re.search(r"^\s*item\s*(?:code|no\.?|#)?\s*[:\-]\s*([A-Z]{1,2}\d{0,3})\b.*$", text, re.I | re.M):
        out["item_code"] = (m.group(1).upper(), m.group(0).strip())
    if m := re.search(r"^\s*(?:item\s+)?(?:description|product|particulars)\s*[:\-]\s*(.+?)\s*$", text, re.I | re.M):
        out["description"] = (m.group(1), m.group(0).strip())
    if m := re.search(rf"^\s*(?:qty|quantity)\s*[:\-]\s*({NUM})\s*([A-Za-z]+)?", text, re.I | re.M):
        out["quantity"] = (to_dec(m.group(1)), m.group(0).strip())
        if unit_name(m.group(2)):
            out["unit"] = (unit_name(m.group(2)), m.group(0).strip())
    if m := re.search(rf"^\s*(?:unit\s*(?:price|rate|cost)|rate|price\s+per\s+\w+)\s*(?:\([^)]*\))?\s*[:\-]\s*{MONEY}",
                      text, re.I | re.M):
        out["unit_price"] = (to_dec(m.group("num")), m.group(0).strip())
    if m := re.search(rf"^\s*(?:amount|line\s*total|item\s*total)\s*(?:\([^)]*\))?\s*[:\-]\s*{MONEY}", text, re.I | re.M):
        out["line_amount"] = (to_dec(m.group("num")), m.group(0).strip())
    return out


def _items(text: str, f: dict) -> None:
    fixed = _fixed_line(text)
    table, problem = _table(text)
    kv = _key_values(text)
    row_sets = [("table", table), ("fixed layout", fixed)]
    multi = next(((m, rows) for m, rows in row_sets if len(rows) > 1), None)
    if multi:
        method, rows = multi
        for k, name in enumerate(ITEM_FIELDS):
            f[name] = unclear(rows[0][k], rows[0][-1], f"{len(rows)} item lines found; only single-item quotes "
                              "are supported")
        return
    for k, name in enumerate(ITEM_FIELDS):
        readings = [(method, rows[0][k], rows[0][-1]) for method, rows in row_sets if rows]
        if name in kv:
            readings.append(("label", kv[name][0], kv[name][1]))
        f[name] = pick(readings, f"{name.replace('_', ' ')} values", "no item line found in the quotation")
        if problem and name in ("quantity", "unit_price", "line_amount") and not f[name].ok:
            f[name] = unclear(None, None, problem)


# ============================================================== totals

def _amount_lines(pattern: str, text: str) -> list[tuple]:
    return [(m, m.group(0).strip()) for m in re.finditer(pattern, text, re.I | re.M)]


def _totals(text: str, f: dict) -> None:
    subs = _amount_lines(rf"^\s*(?:sub[\s\-]?total|net\s+amount|amount\s+before\s+vat|total\s+before\s+vat)\b"
                         rf"[^\n]*?{MONEY}\s*$", text)
    f["subtotal"] = pick([("label", to_dec(m.group("num")), q) for m, q in subs], "subtotals", "no subtotal line")

    vats = _amount_lines(rf"^\s*(?:vat|value\s+added\s+tax)\b[^\n%]*?(?P<rate>\d+(?:\.\d+)?)\s*%[^\n]*?{MONEY}\s*$",
                         text)
    if vats:
        f["vat_rate"] = pick([("label", Decimal(m.group("rate")), q) for m, q in vats], "VAT rates", "")
        f["vat_amount"] = pick([("label", to_dec(m.group("num")), q) for m, q in vats], "VAT amounts", "")
    else:
        f["vat_rate"] = not_found("VAT is not shown as a separate line")
        f["vat_amount"] = not_found("VAT is not shown as a separate line")

    totals = _amount_lines(rf"^\s*(?:grand\s+total|total\s+amount(?:\s+payable)?|total\s+payable|total)\b"
                           rf"(?!\s*(?:before|excl|excluding|ex\.?\s*vat))[^\n]*?(?<![\d,.]){MONEY}\s*$", text)
    f["total"] = pick([("label", to_dec(m.group("num")), q) for m, q in totals], "totals", "no total line")

    inc = re.search(r"[^\n]*(VAT included|VAT inclusive|inclusive of (?:all )?(?:VAT|taxes)|taxes included|"
                    r"including VAT|incl\.? VAT)[^\n]*", text, re.I)
    if inc:
        f["price_includes_vat"] = found(True, inc.group(0).strip())
    elif f["vat_rate"].ok:
        f["price_includes_vat"] = found(False, f["vat_rate"].quote)
    else:
        f["price_includes_vat"] = not_found("document does not say whether VAT is included")


# ============================================================== registration

def _registration(text: str, f: dict) -> None:
    hint = re.search(r"^[^\n]*(registration|\bTIN\b|\bBIN\b)[^\n]*$", text, re.I | re.M)
    hint_line = hint.group(0).strip() if hint else None
    for name, pattern, label in (
        ("tin", r"\b(?:e-?TIN|TIN)\b(?:\s*(?:no\.?|number|#))?\s*[:\-]?\s*(\d[\d\- ]{8,}\d)", "TIN"),
        ("bin", r"\bBIN\b(?:\s*(?:no\.?|number|#))?\s*[:\-]?\s*(\d[\d\-]{5,}\d)", "BIN"),
    ):
        hits = [(m.group(1).strip(), m.group(0).strip()) for m in re.finditer(pattern, text, re.I)]
        if hits:
            quote = hint_line if hint_line and normalize(hits[0][1]) in normalize(hint_line) else hits[0][1]
            f[name] = pick([("label", v, quote if i == 0 else q) for i, (v, q) in enumerate(hits)],
                           f"{label} numbers", "")
        else:
            f[name] = not_found(f"supplier {label} not given", hint_line)


# ============================================================== terms

def _sentences_with(text: str, pattern: str) -> str | None:
    """All sentences of the document that match, joined (used when there is no clause for a term)."""
    flat = re.sub(r"\s+", " ", text)
    hits = [s.strip() for s in re.split(r"(?<=[.;])\s+", flat) if re.search(pattern, s, re.I)]
    return hits[0] if len(hits) == 1 else (" ".join(hits) if hits else None)


_DELIVERY_BASIS = re.compile(r"(?:after|of|from|following)\s+(?:the\s+)?(?:date\s+of\s+)?(?:delivery|receipt of "
                             r"(?:the\s+)?goods|acceptance)", re.I)
_OTHER_BASIS = re.compile(r"(?:after|of|from|following)\s+(?:the\s+)?(?:date\s+of\s+)?(invoice|bill|PO\b|purchase "
                          r"order|order)", re.I)
_PO_BASIS = re.compile(r"(?:of|from|after|following)\s+(?:the\s+)?(?:date\s+of\s+|receipt\s+of\s+|receiving\s+)?"
                       r"(?:the\s+)?(?:PO\b|purchase\s+order|order)", re.I)


def _payment(text: str, clauses: dict, f: dict) -> None:
    p = clauses.get("payment") or _sentences_with(text, r"\bpayment\b|\bpayable\b|\bnet\s+\d+")
    if not p:
        for name in ("advance_pct", "payment_days_min", "payment_days_max"):
            f[name] = not_found("no payment terms found")
        return

    # advance
    advs = {}
    for m in re.finditer(r"(\d+(?:\.\d+)?)\s*%\s*(?:advance|upfront|down\s*payment|(?:payable\s+)?with\s+(?:the\s+)?"
                         r"(?:purchase\s+order|PO\b|order))|advance\s+(?:payment\s+)?(?:of\s+)?(\d+(?:\.\d+)?)\s*%",
                         p, re.I):
        advs[m.start()] = (Decimal(m.group(1) or m.group(2)), m)
    no_adv = re.search(r"\bno\s+advance|without\s+(?:any\s+)?advance", p, re.I)
    mentions = [advs[k] for k in sorted(advs)]
    if no_adv and not mentions:
        f["advance_pct"] = found(Decimal(0), sentence_at(p, no_adv.start()))
    elif len(mentions) == 1:
        f["advance_pct"] = found(mentions[0][0], sentence_at(p, mentions[0][1].start()))
    elif mentions:
        vals = [v for v, _ in mentions]
        quote = sentence_at(p, mentions[0][1].start(), mentions[-1][1].end())
        f["advance_pct"] = unclear(
            min(vals), quote,
            f"advance is stated {len(vals)} times ({', '.join(f'{v}%' for v in vals)}); "
            f"it could mean {min(vals)}% or {sum(vals)}% in total",
        )
    else:
        f["advance_pct"] = not_found("payment terms do not mention an advance", p)

    # days after delivery
    rng = re.search(r"(\d+)\s*(?:to|-|–)\s*(\d+)\s*days", p, re.I)
    singles = list(re.finditer(r"(?:within\s+|net\s+)(\d+)\s*(?:days?|\b)|(\d+)\s*days?\s+(?:after|of|from|following)",
                               p, re.I))
    vague = re.search(r"as agreed|to be agreed|mutually|negotiable|upon agreement", p, re.I)
    if rng:
        lo, hi = int(rng.group(1)), int(rng.group(2))
        note = f"payment period is a range ({lo} to {hi} days)" + (" and left to agreement" if vague else "")
        q = sentence_at(p, rng.start())
        f["payment_days_min"] = unclear(lo, q, note)
        f["payment_days_max"] = unclear(hi, q, note)
        return
    days = sorted({int(m.group(1) or m.group(2)) for m in singles})
    if not days:
        f["payment_days_min"] = not_found("no payment period in days", p)
        f["payment_days_max"] = not_found("no payment period in days", p)
        return
    q = sentence_at(p, singles[0].start(), singles[-1].end())
    problem = None
    if len(days) > 1:
        problem = "several different payment periods are stated"
    elif vague:
        problem = "payment period is left to agreement"
    elif not _DELIVERY_BASIS.search(q):
        other = _OTHER_BASIS.search(q)
        problem = (f"the payment period is counted from the {other.group(1).lower()}, not from delivery" if other
                   else "it does not say that the days count from delivery")
    if problem:
        f["payment_days_min"] = unclear(days[0], q, problem)
        f["payment_days_max"] = unclear(days[-1], q, problem)
    else:
        f["payment_days_min"] = found(days[0], q)
        f["payment_days_max"] = found(days[0], q)


_DURATION = re.compile(r"(\d+)\s*(working\s+days|business\s+days|calendar\s+days|days?|weeks?|months?)", re.I)


def _to_days(n: int, unit: str) -> tuple[int | None, str | None]:
    u = unit.lower()
    if u.startswith(("working", "business")):
        return None, f"{n} {u}: calendar days are not known"
    if u.startswith("week"):
        return n * 7, f"converted from {n} weeks"
    if u.startswith("month"):
        return n * 30, f"converted from {n} months (30 days each)"
    return n, None


def _delivery(text: str, clauses: dict, f: dict) -> None:
    d = clauses.get("delivery") or _sentences_with(text, r"\bdeliver(?:y|ed)?\b[^.]*\d|\blead\s+time\b")
    if not d:
        f["delivery_days"] = not_found("no delivery terms found")
        return
    hits = list(_DURATION.finditer(d))
    if not hits:
        f["delivery_days"] = not_found("delivery terms have no period in days or weeks", d)
        return
    m = hits[0]
    days, note = _to_days(int(m.group(1)), m.group(2))
    q = sentence_at(d, m.start())
    others = {_to_days(int(h.group(1)), h.group(2))[0] for h in hits[1:]} - {days}
    if days is None:
        f["delivery_days"] = unclear(None, q, note)
    elif others:
        f["delivery_days"] = unclear(days, q, "several different delivery periods are stated")
    elif not _PO_BASIS.search(q):
        f["delivery_days"] = unclear(days, q, "it does not say that the period counts from the purchase order")
    else:
        f["delivery_days"] = found(days, q, note)


def _penalty(text: str, clauses: dict, f: dict) -> None:
    clause = clauses.get("delay") or next(
        (c for c in clauses.values() if re.search(r"penalty|late delivery|delay in delivery|liquidated", c, re.I)),
        None) or _sentences_with(text, r"penalty|liquidated damages")
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
    pct = Decimal(m.group(1))
    if re.search(r"per week|each (full )?week|a week|weekly|per\s+completed\s+week", clause, re.I):
        f["penalty_pct"] = found(pct, q)
    elif re.search(r"per day|each day|a day|daily", clause, re.I):
        f["penalty_pct"] = found(pct * 7, q, f"converted from {pct}% per day")
    else:
        f["penalty_pct"] = unclear(pct, q, "penalty is not stated per week")
    basis = re.search(r"of (?:the )?(order value|contract value|po value|invoice value|order amount)", clause, re.I)
    f["penalty_basis"] = (
        found(basis.group(1).lower().replace("order amount", "order value"), q) if basis
        else not_found("penalty does not say what it is a percentage of", q)
    )


def _validity(text: str, clauses: dict, f: dict) -> None:
    v = clauses.get("validity") or text
    m = re.search(r"(?:valid(?:ity)?(?:\s+period)?\s*(?:for|of|:|-)?\s*)(\d+)\s*(days?|weeks?|months?)", v, re.I)
    if m:
        days, note = _to_days(int(m.group(1)), m.group(2))
        f["validity_days"] = found(days, sentence_at(v, m.start()) if v is not text else m.group(0), note)
        return
    m = re.search(rf"valid\s+(?:until|till|up\s+to|through)\s+({DATE})", v, re.I)
    if m:
        q = sentence_at(v, m.start()) if v is not text else m.group(0)
        until, problem = parse_date(m.group(1))
        start = f["quote_date"].value if f["quote_date"].ok else None
        if until and start:
            days = (date.fromisoformat(until) - date.fromisoformat(start)).days
            f["validity_days"] = found(days, q, f"computed: {start} to {until}")
        else:
            f["validity_days"] = unclear(None, q, problem or "valid-until date given, but the quotation date is "
                                                            "not known")
        return
    f["validity_days"] = not_found("no quotation validity period")


def extract_regex(text: str) -> ExtractedPack:
    f: dict[str, Field] = {}
    clauses = split_clauses(text)
    _header(text, f)
    _items(text, f)
    _totals(text, f)
    _registration(text, f)
    _payment(text, clauses, f)
    _delivery(text, clauses, f)
    _penalty(text, clauses, f)
    _validity(text, clauses, f)
    return ExtractedPack(fields=f)
