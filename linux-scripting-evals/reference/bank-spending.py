#!/usr/bin/env python3
"""Print spending per category per month as CSV (month,category,amount) from bank JSON exports.

Spending = outgoing amounts minus refunds in the same category and month; income
categories (net money in) are left out. Split transactions count per split. The month
comes from each transaction's date, not from the file it's in.
Reference answer for eval 5, used to validate checks/bank.py.
"""
import csv
import json
import sys
from collections import defaultdict
from decimal import Decimal
from pathlib import Path


def main():
    folder = Path(sys.argv[1] if len(sys.argv) > 1 else Path.home() / "finance/exports")
    totals = defaultdict(Decimal)
    files = sorted(folder.glob("*.json"))
    if not files:
        sys.exit(f"no .json files in {folder}")
    for f in files:
        try:
            data = json.loads(f.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as e:
            sys.exit(f"{f}: {e}")
        for t in data.get("transactions", []):
            parts = t.get("splits") or [t]
            for p in parts:
                cat = p.get("category") or "Uncategorized"
                totals[(t["date"][:7], cat)] -= Decimal(str(p["amount"]))
    out = csv.writer(sys.stdout, lineterminator="\n")
    out.writerow(["month", "category", "amount"])
    for (month, cat), v in sorted(totals.items()):
        if v > 0:
            out.writerow([month, cat, f"{v:.2f}"])


if __name__ == "__main__":
    main()
