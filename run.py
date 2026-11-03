"""Run the checker on supplier packs and print a summary.

    python run.py                         # all PDFs in data/
    python run.py data/supplier-pack-05.pdf --details
    python run.py --mode regex            # offline, no LLM
"""
import argparse
import json
import re
import sys
from pathlib import Path

from checker.env import load_dotenv
from checker.config import ROOT, load_config
from checker.graph import run_pack
from checker.report import LABEL, main_reason, to_markdown


def pack_id(path: Path) -> str:
    m = re.search(r"(\d+)", path.stem)
    return m.group(1) if m else path.stem


def main() -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    load_dotenv()
    cfg = load_config()
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("files", nargs="*", help="PDF files (default: every PDF in data/)")
    ap.add_argument("--mode", choices=["auto", "live", "replay", "regex"], default=None)
    ap.add_argument("--details", action="store_true", help="print reasons and quotes for every pack")
    ap.add_argument("--out", default=str(ROOT / "outputs"), help="folder for results.json and report.md")
    args = ap.parse_args()

    files = [Path(f) for f in args.files] or sorted(Path(cfg["documents_dir"]).glob("*.pdf"))
    results = [run_pack(str(f), pack_id=pack_id(f), mode=args.mode) for f in files]

    print(f"{'Pack':<5} {'Decision':<24} {'Supplier':<30} Reason")
    for r in results:
        print(f"{r.pack_id:<5} {LABEL[r.decision]:<24} {(r.supplier or '?')[:29]:<30} {main_reason(r)}")
    if args.details:
        for r in results:
            print("\n" + to_markdown(r))

    out = Path(args.out)
    out.mkdir(exist_ok=True)
    (out / "results.json").write_text(
        json.dumps([r.model_dump(mode="json") for r in results], indent=2, ensure_ascii=False), encoding="utf-8")
    (out / "report.md").write_text("# Supplier pack decisions\n\n" + "\n".join(to_markdown(r) for r in results),
                                   encoding="utf-8")
    print(f"\nSaved {out / 'results.json'} and {out / 'report.md'}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
