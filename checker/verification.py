"""Verification gates, checked in addition to the 14 business rules.

They do not judge the offer; they make sure the facts the decision rests on were actually read and are
consistent. A failed gate sends the document to a person (it can never approve or reject on its own).
"""
from .models import CONFLICT, DOC_CONFLICT, SUSPICIOUS, ExtractedPack, RuleResult

# Without these a quotation cannot be identified or priced, so it can never be approved automatically.
MANDATORY = {
    "supplier_name": "supplier name", "quotation_no": "quotation number", "quote_date": "quotation date",
    "item_code": "item", "quantity": "quantity", "unit_price": "unit price", "total": "total amount",
}


def _gate(rule_id, title, outcome, reason, expected, actual, evidence=()) -> RuleResult:
    return RuleResult(rule_id=rule_id, category="verification", title=title, outcome=outcome, reason=reason,
                      expected=expected, actual=actual, evidence=[e for e in dict.fromkeys(evidence) if e])


def verification_checks(ex: ExtractedPack) -> list[RuleResult]:
    missing = {n: ex.get(n) for n in MANDATORY if not ex.get(n).ok}
    v1 = _gate("V1", "Mandatory information found and verified",
               "FAIL" if missing else "PASS",
               ("Could not verify: " + "; ".join(f"{MANDATORY[n]} ({f.note or f.status.lower()})"
                                                 for n, f in missing.items()) + ".") if missing
               else "Supplier, quotation number and date, item, quantity, unit price and total all verified.",
               "All of: " + ", ".join(MANDATORY.values()),
               ", ".join(MANDATORY[n] for n in missing) + " missing or unverified" if missing else "all present",
               [f.quote for f in missing.values()])

    flagged = {n: f for n, f in ex.fields.items() if f.verification in (CONFLICT, DOC_CONFLICT, SUSPICIOUS)}
    if flagged:
        parts = []
        for n, f in flagged.items():
            seen = ", ".join(dict.fromkeys(f"{c.value} ({c.source})" for c in f.candidates if c.value is not None))
            parts.append(f"{n.replace('_', ' ')}: {f.note}" + (f" [{seen}]" if seen else ""))
        evidence = [c.quote for f in flagged.values() for c in f.candidates if c.quote] or \
                   [f.quote for f in flagged.values()]
        v2 = _gate("V2", "No conflicting or implausible values", "FAIL", "; ".join(parts) + ".",
                   "Every field has one consistent, plausible value",
                   f"{len(flagged)} field(s) flagged: " + ", ".join(n.replace("_", " ") for n in flagged), evidence)
    else:
        v2 = _gate("V2", "No conflicting or implausible values", "PASS", "No field has conflicting readings.",
                   "Every field has one consistent, plausible value", "no conflicts")
    return [v1, v2]
