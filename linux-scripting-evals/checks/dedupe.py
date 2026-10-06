#!/usr/bin/env python3
"""Eval 6 (photo dedupe): dry run, real run and rerun on a generated ~/Pictures.

Groups (same content): A x3 (oldest 2019/IMG_0001.jpg), B x2 with equal mtimes,
C x2 (oldest 2018/c.jpg; the newer copy has a newline in its name). Also: two
same-size files with different content, a unique file, a hard-linked pair, a
symlink pointing at a newer copy of A, and two empty files.
Removed files may be deleted or moved into a trash/dupes folder.
"""
import argparse
import hashlib
import os
import re
import subprocess
from pathlib import Path

from common import Report, env_with, expand, install_script, response_checks, snapshot, tail, workdir

TRASH = re.compile(r"(^|/)(\.?trash[^/]*|[^/]*dup[^/]*|removed[^/]*|\.dedupe[^/]*)(/|$)", re.I)


def ts(s):
    from datetime import datetime
    return datetime.fromisoformat(s).timestamp()


def make_tree(root):
    A, B, C = b"A" * 3000 + b"jpeg-a", b"B" * 5000 + b"jpeg-b", b"C" * 4000 + b"jpeg-c"
    files = {
        "2019/IMG_0001.jpg": (A, "2019-05-01T10:00"),
        "backup/IMG_0001.jpg": (A, "2021-03-03T10:00"),
        "phone/IMG 0001 (1).jpg": (A, "2023-08-08T10:00"),
        "2020/beach.jpg": (B, "2020-07-01T12:00"),
        "-weird/beach copy.jpg": (B, "2020-07-01T12:00"),
        "2018/c.jpg": (C, "2018-01-01T09:00"),
        "2022/new\nline.jpg": (C, "2022-02-02T09:00"),
        "2020/same-size-1.jpg": (b"D" * 2048, "2020-01-01T00:00"),
        "2020/same-size-2.jpg": (b"E" * 2048, "2020-01-01T00:00"),
        "2016/unique.jpg": (b"unique-content" * 99, "2016-06-06T06:00"),
        "2017/hl.jpg": (b"F" * 1500, "2017-01-01T00:00"),
        "2021/empty1.jpg": (b"", "2021-01-01T00:00"),
        "2021/empty2.jpg": (b"", "2021-01-02T00:00"),
    }
    for rel, (data, when) in files.items():
        p = root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(data)
        os.utime(p, (ts(when), ts(when)))
    (root / "links").mkdir()
    os.link(root / "2017/hl.jpg", root / "links/hl-link.jpg")
    os.symlink("../phone/IMG 0001 (1).jpg", root / "links/fav.jpg")
    return {"A": A, "B": B, "C": C}


def contents(root):
    """rel path -> sha256 for regular files outside trash folders."""
    out = {}
    for dirpath, dirs, files in os.walk(root):
        for f in files:
            p = Path(dirpath) / f
            rel = str(p.relative_to(root))
            if TRASH.search(rel) or p.is_symlink():
                continue
            out[rel] = hashlib.sha256(p.read_bytes()).hexdigest()
    return out


def runit(cmd, env, yes):
    stdin = subprocess.PIPE if yes else subprocess.DEVNULL
    try:
        r = subprocess.run(["bash", "-c", cmd], env=env, input=("y\n" * 200) if yes else None,
                           stdin=None if yes else stdin, capture_output=True, text=True, errors="replace", timeout=120)
    except subprocess.TimeoutExpired:
        return subprocess.CompletedProcess(cmd, 124, "", "[timed out]")
    return r


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("script")
    p.add_argument("--dry", default="{script} -n {dir}", help="dry-run command template")
    p.add_argument("--real", default="{script} {dir}", help="real-run command template")
    p.add_argument("--yes", action="store_true", help="answer y to prompts on stdin")
    p.add_argument("--response")
    p.add_argument("--json")
    p.add_argument("--keep", action="store_true")
    args = p.parse_args()
    rep = Report("dedupe")
    work = workdir(args.keep)
    home = work / "home"
    pics = home / "Pictures"
    groups = make_tree(pics)
    script = install_script(args.script, work)
    env = env_with([], HOME=home)
    sha = {k: hashlib.sha256(v).hexdigest() for k, v in groups.items()}

    before = snapshot(pics)
    r = runit(expand(args.dry, script=script, dir=pics), env, args.yes)
    rep.check("dry-run-exit", r.returncode == 0, f"exit {r.returncode}" + (f": {tail(r.stderr, 2)}" if r.returncode else ""))
    rep.check("dry-run-changes-nothing", snapshot(pics) == before, "tree unchanged" if snapshot(pics) == before
              else "dry run modified the tree")
    # Accept names printed plainly or shell-quoted (printf %q, $'...')
    out = re.sub(r"[\\'\"$]", "", (r.stdout or "") + (r.stderr or ""))
    listed = [n for n in ("backup/IMG_0001.jpg", "IMG 0001 (1).jpg", "line.jpg") if n in out]
    rep.add("PASS" if len(listed) == 3 else "WARN", "dry-run-lists", f"names the files it would remove: {listed}")

    r = runit(expand(args.real, script=script, dir=pics), env, args.yes)
    rep.check("real-run-exit", r.returncode == 0, f"exit {r.returncode}" + (f": {tail(r.stderr, 2)}" if r.returncode else ""))
    now = contents(pics)
    by_sha = {}
    for rel, h in now.items():
        by_sha.setdefault(h, []).append(rel)
    for g in "ABC":
        left = by_sha.get(sha[g], [])
        rep.check(f"group-{g}-one-left", len(left) == 1, f"copies left: {left}")
    rep.check("oldest-kept", by_sha.get(sha["A"]) == ["2019/IMG_0001.jpg"] and by_sha.get(sha["C"]) == ["2018/c.jpg"],
              f"A kept {by_sha.get(sha['A'])}, C kept {by_sha.get(sha['C'])}")
    keep = ["2020/same-size-1.jpg", "2020/same-size-2.jpg", "2016/unique.jpg"]
    gone = [k for k in keep if k not in now]
    rep.check("distinct-files-kept", not gone, "same-size different files and unique file kept" if not gone
              else f"removed non-duplicates: {gone}")
    hl = [k for k in ("2017/hl.jpg", "links/hl-link.jpg") if k not in now]
    rep.add("PASS" if not hl else "WARN", "hardlinks", "hard-linked pair left alone (removing one frees nothing)"
            if not hl else f"removed hard link(s): {hl}")
    fav = pics / "links/fav.jpg"
    rep.add("PASS" if fav.exists() else "WARN", "symlink-target",
            "links/fav.jpg still resolves" if fav.exists() else "links/fav.jpg now dangles (its target was removed)")
    empties = [k for k in ("2021/empty1.jpg", "2021/empty2.jpg") if k not in now]
    rep.add("INFO", "empty-files", f"removed: {empties}" if empties else "both kept")
    moved = [str(Path(dp).relative_to(pics)) for dp, _, fs in os.walk(pics) if TRASH.search(str(Path(dp).relative_to(pics))) and fs]
    rep.add("INFO", "reversible", f"duplicates moved to {moved}" if moved else "duplicates deleted (or moved outside ~/Pictures)")

    snap = snapshot(pics)
    r = runit(expand(args.real, script=script, dir=pics), env, args.yes)
    rep.check("rerun-noop", r.returncode == 0 and snapshot(pics) == snap, f"exit {r.returncode}, "
              + ("nothing changed" if snapshot(pics) == snap else "second run changed the tree"))

    text = Path(args.script).read_text(errors="replace")
    rep.add("INFO", "approach", ", ".join(w for w, rx in (
        ("jdupes", r"\bjdupes\b"), ("rmlint", r"\brmlint\b"), ("fdupes", r"\bfdupes\b"),
        ("size prefilter", r"%s|st_size|stat\s+-c\s*%s|getsize|size"), ("hash", r"sum\b|hashlib|xxh|b3sum"))
        if re.search(rx, text)) or "unclear")
    response_checks(rep, args.response)
    raise SystemExit(rep.finish(args.json))


if __name__ == "__main__":
    main()
