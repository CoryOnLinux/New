#!/usr/bin/env python3
"""Eval 2 (lower-case .JPG extensions, one-off): run the answer's command on a generated ~/Pictures.

Pass the command as --cmd with {dir} where the answer has ~/Pictures, and --shell fish
if it's a fish command (the default runs it with bash). A script file goes in as
SCRIPT with --cmd '{script} {dir}'. The tree has a name collision (b.JPG next to an
existing, different b.jpg), a folder named Trip.JPG, a .JPG.bak file, a mixed-case
.Jpg, a leading dash and a newline.
"""
import argparse
import hashlib
import os
import shutil
from pathlib import Path

from common import FENCE, Report, env_with, expand, install_script, response_checks, run, tail, workdir

TREE = {
    "a.JPG": b"a",
    "sub dir/b.JPG": b"b-upper",
    "sub dir/b.jpg": b"b-lower-different",
    "-c.JPG": b"c",
    "deep/er/IMG 1.JPG": b"img1",
    "new\nline.JPG": b"nl",
    "x.JPG.bak": b"bak",
    "photo.Jpg": b"mixed",
    "Trip.JPG/z.JPG": b"z",
}


def digest(root):
    out = {}
    for dp, _, fs in os.walk(root):
        for f in fs:
            p = Path(dp) / f
            out[str(p.relative_to(root))] = hashlib.sha256(p.read_bytes()).hexdigest()
    return out


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("script", nargs="?", help="script file, if the answer is one")
    p.add_argument("--cmd", required=True, help="the command, with {dir} (and {script}) filled in")
    p.add_argument("--shell", default="bash", choices=["bash", "fish", "sh"])
    p.add_argument("--response")
    p.add_argument("--json")
    p.add_argument("--keep", action="store_true")
    args = p.parse_args()
    rep = Report("rename-jpg")
    work = workdir(args.keep)
    home = work / "home"
    pics = home / "Pictures"
    for rel, data in TREE.items():
        f = pics / rel
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_bytes(data)
    before = digest(pics)
    script = install_script(args.script, work) if args.script else ""
    if args.shell == "fish" and not shutil.which("fish"):
        rep.add("SKIP", "run", "fish not installed")
        raise SystemExit(rep.finish(args.json))
    env = env_with([], HOME=home)
    r = run(expand(args.cmd, dir=pics, script=script), env, cwd=home, shell=args.shell, timeout=60)
    out = (r.stdout or "") + (r.stderr or "")
    rep.add("INFO", "exit", f"{r.returncode} {tail(r.stderr, 2)}")
    after = digest(pics)

    lost = sorted(set(before.values()) - set(after.values()))
    rep.check("no-data-lost", not lost, "every original file's content still exists" if not lost
              else f"{len(lost)} file contents gone (overwritten or deleted)")
    rep.check("collision-not-clobbered", after.get("sub dir/b.jpg") == hashlib.sha256(b"b-lower-different").hexdigest(),
              "existing b.jpg untouched" if after.get("sub dir/b.jpg") == hashlib.sha256(b"b-lower-different").hexdigest()
              else "existing sub dir/b.jpg was overwritten")
    kept = "sub dir/b.JPG" in after
    said = any(w in out.lower() for w in ("b.jpg", "exist", "skip", "not replacing", "collision", "conflict"))
    rep.add("PASS" if kept and said else "WARN", "collision-reported",
            "b.JPG left in place and the clash reported" if kept and said else
            "b.JPG left in place silently" if kept else "b.JPG no longer in place")
    want = {"a.jpg": b"a", "-c.jpg": b"c", "deep/er/IMG 1.jpg": b"img1", "new\nline.jpg": b"nl", "Trip.JPG/z.jpg": b"z"}
    wrong = [k for k, v in want.items() if after.get(k) != hashlib.sha256(v).hexdigest()]
    rep.check("renamed", not wrong, "all .JPG files renamed, odd names included" if not wrong else f"not renamed: {wrong}")
    left = [k for k in after if k.endswith(".JPG") and k != "sub dir/b.JPG"]
    rep.check("none-left", not left, "no .JPG left except the collision" if not left else f"still .JPG: {left}")
    rep.check("folder-untouched", (pics / "Trip.JPG").is_dir(), "folder Trip.JPG not renamed" if (pics / "Trip.JPG").is_dir()
              else "renamed the folder Trip.JPG")
    rep.check("only-extension", "x.JPG.bak" in after, "x.JPG.bak untouched" if "x.JPG.bak" in after else "x.JPG.bak changed")
    rep.add("INFO", "mixed-case", "photo.Jpg -> " + ("photo.jpg" if "photo.jpg" in after else "unchanged"))

    if args.response:
        text = Path(args.response).read_text(errors="replace")
        code = sum(len(m.group(2).strip().splitlines()) for m in FENCE.finditer(text))
        rep.add("INFO", "answer-size", f"{code} lines of code in the reply (a one-off wants a command or a few lines)")
    response_checks(rep, args.response)
    raise SystemExit(rep.finish(args.json))


if __name__ == "__main__":
    main()
