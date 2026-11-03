"""Score the checker against the expected answers in tests/expected.csv.

    python evaluate.py               # mode from config.yaml (auto)
    python evaluate.py --mode regex  # offline
    python evaluate.py --mode live   # OpenAI + regex cross-check
    python evaluate.py --mode regex --dir other/pdfs --expected other/pdfs/expected.csv   # another set

A pack counts as right when the decision matches AND every expected rule is among the reasons
(so a right answer for the wrong reason does not count).
"""
import argparse
import csv
import sys
import time

from checker.env import load_dotenv
from checker.config import ROOT, load_config
from checker.graph import run_pack
from checker.report import LABEL


def main() -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    load_dotenv()
    ap = argparse.ArgumentParser()
    ap.add_argument("--mode", choices=["auto", "live", "replay", "regex"], default=None)
    ap.add_argument("--dir", default=None, help="folder with the PDFs (default: data/)")
    ap.add_argument("--expected", default=str(ROOT / "tests" / "expected.csv"), help="answer key CSV")
    args = ap.parse_args()
    cfg = load_config()
    folder = args.dir or cfg["documents_dir"]

    with open(args.expected, encoding="utf-8") as fh:
        expected = list(csv.DictReader(fh))
    width = max(len(r.get("pack") or r["file"]) for r in expected) + 1

    decision_ok = reason_ok = 0
    times = []
    print(f"{'Pack':<{width}} {'Expected':<24} {'Got':<24} {'Decision':<9} {'Reason':<7} Rules found")
    for row in expected:
        t0 = time.perf_counter()
        name = row.get("pack") or row["file"]
        r = run_pack(f"{folder}/{row['file']}", pack_id=name, mode=args.mode)
        times.append(time.perf_counter() - t0)
        want = set(filter(None, row["expected_rules"].split(";")))
        got = {x.rule_id for x in r.reasons}
        d_ok = r.decision == row["expected_decision"]
        # an Approve must have no reasons at all; otherwise every expected rule must be among the reasons
        r_ok = d_ok and want <= got and (row["expected_decision"] != "APPROVE" or not got)
        decision_ok += d_ok
        reason_ok += r_ok
        print(f"{name:<{width}} {LABEL[row['expected_decision']]:<24} {LABEL[r.decision]:<24} "
              f"{'yes' if d_ok else 'NO':<9} {'yes' if r_ok else 'NO':<7} {', '.join(sorted(got)) or '-'}"
              f"   [{r.extraction_source}]")
        for e in r.errors:
            print(f"      error: {e}")

    n = len(expected)
    print(f"\nDecision accuracy: {decision_ok}/{n}")
    print(f"Decision + reason accuracy: {reason_ok}/{n}")
    print(f"Average time per pack: {sum(times) / n:.2f}s")
    return 0 if reason_ok == n else 1


if __name__ == "__main__":
    sys.exit(main())
