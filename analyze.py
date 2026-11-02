"""Run the DocIntel pipeline locally on a file, no agent needed.

    python analyze.py samples/invoice-sliced.pdf
    python analyze.py samples/po-tobii.pdf --json
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from docintel import analyze_document
from docintel.config import get_settings
from docintel.format import format_report
from docintel.mineru import MinerUError


def main() -> int:
    parser = argparse.ArgumentParser(description="Analyze a document with MinerU + DocIntel rules.")
    parser.add_argument("path", type=Path)
    parser.add_argument("--type", choices=["invoice", "purchase-order", "contract", "general"], help="skip auto-classification")
    parser.add_argument("--json", action="store_true", help="print the raw JSON result")
    args = parser.parse_args()

    try:
        result = analyze_document(args.path.read_bytes(), args.path.name, args.type, get_settings())
    except MinerUError as exc:
        print(f"MinerU error: {exc}", file=sys.stderr)
        return 1

    print(json.dumps(result, indent=2, ensure_ascii=False) if args.json else format_report(result))
    return 0


if __name__ == "__main__":
    sys.exit(main())
