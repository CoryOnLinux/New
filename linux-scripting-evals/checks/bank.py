#!/usr/bin/env python3
"""Eval 5 (bank JSON to CSV): run the produced script on the sample exports and check the CSV.

The exports sit in $HOME/finance/exports (HOME is a temp folder), so a script with
that default path works too. Expected totals are computed here from the same files,
under either reasonable reading of "spending":
  debits:  sum of outgoing amounts only (refunds ignored)
  net:     outgoing minus refunds per category, income categories left out
Long CSV (month,category,amount) and wide CSV (category rows, month columns) are both accepted.
"""
import csv
import io
import json
import re
import shutil
from collections import defaultdict
from decimal import Decimal, InvalidOperation
from pathlib import Path

from common import FILES, Report, env_with, expand, install_script, parser, response_checks, run, tail, workdir

UNCAT = "<uncategorized>"


def expected(folder):
    debits, net = defaultdict(Decimal), defaultdict(Decimal)
    income = set()
    for f in sorted(folder.glob("*.json")):
        for t in json.loads(f.read_text())["transactions"]:
            month = t["date"][:7]
            parts = t.get("splits") or [{"amount": t["amount"], "category": t.get("category")}]
            for p in parts:
                cat = p.get("category") or UNCAT
                amt = Decimal(str(p["amount"]))
                if amt < 0:
                    debits[(month, cat)] += -amt
                net[(month, cat)] -= amt
    for (m, c), v in list(net.items()):
        if v < 0:          # money in, not spending (salary)
            income.add(c)
            del net[(m, c)]
    return dict(debits), dict(net), income


def parse_csv(text):
    rows = list(csv.reader(io.StringIO(text)))
    rows = [r for r in rows if any(c.strip() for c in r)]
    if len(rows) < 2:
        return None, "fewer than 2 CSV rows"
    head = [h.strip().lower() for h in rows[0]]
    cells = {}
    month_rx = re.compile(r"^\d{4}-\d{2}$")
    if any("month" in h for h in head) and any("categ" in h for h in head):
        mi = next(i for i, h in enumerate(head) if "month" in h)
        ci = next(i for i, h in enumerate(head) if "categ" in h)
        ai = next((i for i, h in enumerate(head) if i not in (mi, ci)), None)
        for r in rows[1:]:
            if len(r) != len(head):
                return None, f"row with {len(r)} fields under a {len(head)}-column header: {r}"
            cells[(r[mi].strip()[:7], r[ci].strip())] = r[ai].strip()
        return cells, "long"
    months = [i for i, h in enumerate(head) if month_rx.match(h)]
    if months:
        for r in rows[1:]:
            if len(r) != len(head):
                return None, f"row with {len(r)} fields under a {len(head)}-column header: {r}"
            for i in months:
                if r[i].strip():
                    cells[(head[i], r[0].strip())] = r[i].strip()
        return cells, "wide"
    return None, f"can't tell the layout from the header {rows[0]}"


def to_dec(s):
    try:
        return Decimal(s.replace("€", "").replace(" ", ""))
    except InvalidOperation:
        return None


def compare(got, want, known_cats, income):
    """Missing / wrong cells of `want` in `got`; abs() so either sign convention passes."""
    unc = {k for k in got if k[1] not in known_cats and k[1] not in income}
    norm = {}
    for (m, c), v in got.items():
        key = (m, UNCAT) if (m, c) in unc else (m, c)
        norm[key] = v
    bad = []
    for k, v in want.items():
        g = norm.get(k)
        if g is None:
            if v != 0:
                bad.append(f"{k[0]} {k[1]}: missing (want {v})")
        elif g is None or to_dec(g) is None or abs(abs(to_dec(g)) - v) > Decimal("0.005"):
            bad.append(f"{k[0]} {k[1]}: {g} (want {v})")
    extra = [f"{m} {c}: {v}" for (m, c), v in norm.items() if (m, c) not in want and c not in income
             and to_dec(v) not in (None, Decimal(0))]
    return bad + [f"unexpected {e}" for e in extra]


def main():
    args = parser(__doc__).parse_args()
    rep = Report("bank-csv")
    work = workdir(args.keep)
    home = work / "home"
    exports = home / "finance/exports"
    shutil.copytree(FILES / "bank", exports)
    script = install_script(args.script, work)
    env = env_with([], HOME=home, LC_ALL="C.UTF-8")

    r = run(expand(args.cmd, script=script, dir=exports), env, cwd=work, timeout=60)
    rep.check("runs", r.returncode == 0, f"exit {r.returncode}" + (f": {tail(r.stderr, 3)}" if r.returncode else ""))
    got, layout = parse_csv(r.stdout or "")
    if got is None:
        rep.add("FAIL", "csv-parses", f"{layout}; stdout starts: {(r.stdout or '')[:120]!r}")
        response_checks(rep, args.response)
        raise SystemExit(rep.finish(args.json))
    rep.add("PASS", "csv-parses", f"{layout} layout, {len(got)} cells; stdout is only CSV")

    debits, net, income = expected(exports)
    known = {c for _, c in debits} - {UNCAT}
    bad_d, bad_n = compare(got, debits, known, income), compare(got, net, known, income)
    which = "debits" if not bad_d else "net" if not bad_n else None
    rep.check("totals-match", which is not None,
              f"matches the '{which}' reading" if which else
              "neither reading matches; closest: " + "; ".join((bad_d if len(bad_d) <= len(bad_n) else bad_n)[:5]))

    july_books = got.get(("2026-07", "Books, Media"))
    rep.check("splits-counted", july_books is not None and to_dec(july_books) is not None
              and abs(to_dec(july_books)) == Decimal("40.00"),
              f"2026-07 'Books, Media' = {july_books} (from a split transaction)")
    rep.check("month-from-date", ("2026-07", "Subscriptions") not in got and ("2026-08", "Subscriptions") in got,
              "Netflix dated 2026-08-01 in the July export counted in August"
              if ("2026-08", "Subscriptions") in got and ("2026-07", "Subscriptions") not in got
              else f"Subscriptions cells: { {k: v for k, v in got.items() if k[1] == 'Subscriptions'} }")
    rep.check("csv-quoting", any(c == "Books, Media" for _, c in got), "'Books, Media' kept as one field")
    ugly = [v for v in got.values() if re.search(r"\.\d{3,}", v)]
    rep.check("money-precision", not ugly, "all amounts have at most 2 decimals" if not ugly
              else f"float noise: {ugly[:3]}")
    inc = {k: v for k, v in got.items() if k[1] in income}
    rep.add("PASS" if not inc else "WARN", "income-not-spending",
            "no Income rows" if not inc else f"income shown as spending: {list(inc.items())[:2]}")
    unc = [k for k in got if k[1] not in known and k[1] not in income]
    rep.add("INFO", "uncategorized-label", ", ".join(sorted({repr(c) for _, c in unc})) or "none")

    # A corrupt export must not be summed silently.
    broken = work / "broken"
    shutil.copytree(FILES / "bank", broken)
    (broken / "2026-11.json").write_text('{"transactions": [ {"date": "2026-11-02", "amount": -5.0, ')
    r2 = run(expand(args.cmd, script=script, dir=broken), env, cwd=work, timeout=60)
    named = "2026-11" in (r2.stderr or "")
    rep.add("PASS" if r2.returncode != 0 and named else "WARN", "corrupt-file",
            f"exit {r2.returncode}, " + ("names the bad file" if named else f"stderr: {tail(r2.stderr, 2) or 'empty'}"))

    text = Path(args.script).read_text(errors="replace")
    lang = ("python" if re.search(r"^#!.*python|import json", text, re.M) else
            "jq" if re.search(r"\bjq\b", text) else "shell text tools")
    rep.add("INFO", "json-parser", lang)
    response_checks(rep, args.response)
    raise SystemExit(rep.finish(args.json))


if __name__ == "__main__":
    main()
