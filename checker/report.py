"""Step 9 helpers: human-readable output."""
from .models import PackResult

LABEL = {"APPROVE": "Approve", "REJECT": "Reject", "NEEDS_REVIEW": "Needs a person to check"}


def main_reason(r: PackResult) -> str:
    if not r.reasons:
        return "All rules pass"
    return "; ".join(f"{x.rule_id} {x.title}" for x in r.reasons)


def to_markdown(r: PackResult) -> str:
    lines = [f"### Pack {r.pack_id}: {r.supplier or r.file} -> **{LABEL[r.decision]}**", ""]
    lines.append(f"- File: `{r.file}`  |  Item: {r.item or 'unknown'}  |  Extraction: {r.extraction_source}")
    if r.human_decision:
        lines.append(f"- Person's decision: **{r.human_decision.get('decision')}**"
                     f" ({r.human_decision.get('comment') or 'no comment'})")
    lines.append("")
    if not r.reasons:
        lines.append("All rules pass.")
    for x in r.reasons:
        kind = {"reject": "Reject rule", "review": "Needs-check rule"}.get(x.category, x.category)
        lines.append(f"- **{x.rule_id} ({kind}, {x.outcome})**: {x.reason}")
        for e in x.evidence:
            lines.append(f"  > \"{e}\"")
    return "\n".join(lines) + "\n"
