"""Step 4: find the purchase-request line this quote is for, and check it really matches."""
import csv
import re
from decimal import Decimal

from .models import ExtractedPack, RuleResult


def load_purchase_request(path: str) -> dict[str, dict]:
    with open(path, encoding="utf-8-sig", newline="") as fh:
        rows = list(csv.DictReader(fh))
    return {
        row["Item"].strip(): {
            "pr_number": row["PR number"].strip(),
            "item": row["Item"].strip(),
            "description": row["Description"].strip(),
            "unit": row["Unit"].strip(),
            "quantity": Decimal(row["Quantity requested"].replace(",", "")),
            "max_price": Decimal(row["Maximum unit price (BDT)"].replace(",", "")),
        }
        for row in rows
    }


def pr_text(pr: dict) -> str:
    return (f"{pr['pr_number']} item {pr['item']} \"{pr['description']}\": "
            f"{pr['quantity']:,} {pr['unit']}, max {pr['max_price']} BDT")


def _simple(s: str) -> str:
    return re.sub(r"[^a-z0-9]", "", s.lower())


def match_pr(ex: ExtractedPack, pr_lines: dict[str, dict]) -> tuple[dict | None, RuleResult]:
    pr, result = _match_pr(ex, pr_lines)
    show = lambda n: str(ex.get(n).value) if ex.get(n).ok else f"{n.replace('_', ' ')} unclear/missing"  # noqa: E731
    expected = (f"Item {pr['item']}, unit {pr['unit']}, reference {pr['pr_number']}" if pr
                else "An item of the purchase request, with matching unit and reference")
    actual = f"item {show('item_code')}, unit {show('unit')}, reference {show('pr_reference')}"
    return pr, result.model_copy(update={"expected": expected, "actual": actual})


def _match_pr(ex: ExtractedPack, pr_lines: dict[str, dict]) -> tuple[dict | None, RuleResult]:
    title = "Quote matches a purchase-request line"
    code = ex.get("item_code")
    if not code.ok:
        return None, RuleResult(rule_id="X1", category="review", title=title, outcome="UNKNOWN",
                                reason="Could not read which item the quote is for.",
                                evidence=[code.quote] if code.quote else [])
    pr = pr_lines.get(str(code.value))
    if pr is None:
        return None, RuleResult(rule_id="X1", category="review", title=title, outcome="FAIL",
                                reason=f"Item {code.value} is not in the purchase request.",
                                evidence=[code.quote])

    problems, unknown = [], []
    checks = (
        ("description", pr["description"]),
        ("unit", pr["unit"]),
        ("pr_reference", pr["pr_number"]),
    )
    for name, expected in checks:
        f = ex.get(name)
        if not f.ok:
            unknown.append(name.replace("_", " "))
        elif _simple(str(f.value)) != _simple(expected):
            problems.append(f"{name.replace('_', ' ')} is \"{f.value}\" but the purchase request says \"{expected}\"")

    evidence = [q for q in {ex.get(n).quote for n, _ in checks} if q] + [pr_text(pr)]
    if problems:
        return pr, RuleResult(rule_id="X1", category="review", title=title, outcome="FAIL",
                              reason="; ".join(problems).capitalize() + ".", evidence=evidence)
    if unknown:
        return pr, RuleResult(rule_id="X1", category="review", title=title, outcome="UNKNOWN",
                              reason=f"Could not confirm: {', '.join(unknown)}.", evidence=evidence)
    return pr, RuleResult(rule_id="X1", category="review", title=title, outcome="PASS",
                          reason=f"Matches {pr['pr_number']} item {pr['item']}.", evidence=evidence)
