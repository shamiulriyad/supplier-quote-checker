"""Step 7: turn rule results into one decision. Order matters: Reject > Needs review > Approve.

Deterministic: only the rule and verification results of this document decide. Approve is possible only
when nothing failed and nothing is unknown, so missing or unverified information can never approve.
"""
from .models import Decision, RuleResult


def decide(rule_results: list[RuleResult], errors: list[str]) -> tuple[Decision, list[RuleResult]]:
    rejects = [r for r in rule_results if r.category == "reject" and r.outcome == "FAIL"]
    reviews = [r for r in rule_results if r.category in ("review", "verification") and r.outcome == "FAIL"]
    unknown = [r for r in rule_results if r.outcome == "UNKNOWN"]
    reasons = rejects + reviews + unknown  # always list every problem, not just the first

    if errors:  # a step failed, so even a Reject is not trustworthy: a person decides
        reasons += [RuleResult(rule_id="E1", category="review", title="Processing problem", outcome="UNKNOWN",
                               reason=e) for e in errors]
        return "NEEDS_REVIEW", reasons
    if rejects:
        return "REJECT", reasons
    if reviews or unknown:
        return "NEEDS_REVIEW", reasons
    return "APPROVE", reasons
