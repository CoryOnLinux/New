#!/usr/bin/env python3
"""Eval 3 (nightly backup to an external drive): run the backup script over 11 fake nights.

A copy of the script is run with /run/media/ pointing into a temp folder and HOME at
a generated home (Documents, Projects, odd names, a symlink, a .git folder). The drive
counts as mounted only when listed in FAKE_MOUNTS (mountpoint/findmnt shims); a script
that reads /proc/mounts can't be fooled and will always see it unplugged.
Time: libfaketime (`faketime`, package libfaketime) if installed, so date, printf %T,
touch and find agree; otherwise only `date` is faked. After each night, new folders on
the drive get that night's timestamp, so age-based pruning sees realistic mtimes.
"""
import os
import re
import shutil
import subprocess
import time
from datetime import datetime
from pathlib import Path

from common import Report, env_with, expand, parser, response_checks, tail, workdir

INCOMPLETE = re.compile(r"incomplete|partial|tmp|progress|\.new\b|unfinished|inprogress", re.I)


def make_home(home):
    d, p = home / "Documents", home / "Projects"
    (d / "sub dir").mkdir(parents=True)
    (p / "app/.git").mkdir(parents=True)
    (d / "report.odt").write_bytes(b"report" * 2000)
    (d / "sub dir/notes.txt").write_text("notes\n")
    (d / "changed.txt").write_text("v1\n")
    (d / "gone.txt").write_text("delete me later\n")
    (d / "-dash.txt").write_text("dash\n")
    (d / "new\nline.txt").write_text("newline\n")
    os.symlink("report.odt", d / "link-to-report")
    (p / "app/main.py").write_text("print('hi')\n")
    (p / "app/.git/HEAD").write_text("ref: refs/heads/main\n")
    with open(p / "big.bin", "wb") as f:
        f.truncate(2 * 1024 * 1024)


def snapshots(dest):
    """id -> folder holding Documents and Projects, for each snapshot on the drive."""
    found = {}
    if not dest.is_dir():
        return found
    for dirpath, dirs, _ in os.walk(dest):
        depth = len(Path(dirpath).relative_to(dest).parts)
        if depth > 8:
            dirs[:] = []
            continue
        if "Documents" in dirs and "Projects" in dirs:
            rel = Path(dirpath).relative_to(dest)
            parts = rel.parts
            idx = next((i for i, c in enumerate(parts) if re.search(r"\d", c)), len(parts) - 1)
            found[str(Path(*parts[: idx + 1])) if parts else "."] = Path(dirpath)
            dirs[:] = []
    return found


def complete(snaps):
    return {k: v for k, v in snaps.items() if not INCOMPLETE.search(k)}


def newest(snaps):
    return max(snaps.values(), key=lambda p: (p / "Documents/changed.txt").stat().st_mtime_ns
               if (p / "Documents/changed.txt").exists() else 0, default=None)


def stamp_new(dest, since, when):
    ts = datetime.fromisoformat(when).timestamp()
    for dirpath, dirs, files in os.walk(dest):
        if len(Path(dirpath).relative_to(dest).parts) > 3:
            dirs[:] = []
            continue
        for n in dirs:
            q = Path(dirpath) / n
            if q.lstat().st_mtime > since and not q.is_symlink():
                os.utime(q, (ts, ts))


def main():
    args = parser(__doc__, default_cmd="{script}").parse_args()
    rep = Report("backup")
    work = workdir(args.keep)
    home = work / "home"
    make_home(home)
    media = work / "media"
    dest = media / "cory/Backup"
    text = Path(args.script).read_text(errors="replace")
    patched = text.replace("/run/media/", f"{media}/").replace("/home/cory", str(home))
    script = work / "bin/backup"
    script.parent.mkdir()
    script.write_text(patched)
    script.chmod(0o755)
    use_faketime = shutil.which("faketime") is not None and not os.environ.get("LSX_NO_FAKETIME")
    rep.add("INFO", "time-faking", "libfaketime" if use_faketime else "date shim only (install libfaketime for find/printf)")
    since = time.time() - 1

    def night(when, mounted=True, fail=False):
        env = env_with(["backup"], HOME=home, USER="cory", LOGNAME="cory",
                       FAKE_MOUNTS=str(dest) if mounted else "", FAKE_NOW=when, XDG_RUNTIME_DIR=work / "run")
        # Fake the clock, not file timestamps: with stat() faked too, every source file
        # looks modified each night and --link-dest never links anything.
        env["NO_FAKE_STAT"] = "1"
        if fail:
            env["FAIL_RSYNC"] = "1"
        (work / "run").mkdir(exist_ok=True, mode=0o700)
        cmd = expand(args.cmd, script=script, dir=dest)
        argv = (["faketime", "-f", "@" + when.replace("T", " ") + ":00"] if use_faketime else []) + ["bash", "-c", cmd]
        try:
            r = subprocess.run(argv, env=env, stdin=subprocess.DEVNULL, capture_output=True, text=True,
                               errors="replace", timeout=120)
        except subprocess.TimeoutExpired:
            r = subprocess.CompletedProcess(argv, 124, "", "[timed out]")
        if dest.is_dir():
            stamp_new(dest, since, when)
        return r

    # ---- drive unplugged -------------------------------------------------------------
    r = night("2026-09-30T02:30", mounted=False)
    rep.check("unplugged-writes-nothing", not media.exists() or not any(media.rglob("*")),
              "nothing created under /run/media" if not media.exists() or not any(media.rglob("*"))
              else f"created {[str(p.relative_to(media)) for p in media.rglob('*')][:4]} on the root filesystem")
    rep.add("PASS" if r.returncode == 0 else "WARN", "unplugged-quiet-exit", f"exit {r.returncode} {tail(r.stderr, 1)}")
    shutil.rmtree(media, ignore_errors=True)   # start clean even if the first run wrote there
    dest.mkdir(parents=True)
    r = night("2026-09-30T03:30", mounted=False)
    leaked = [str(p.relative_to(dest)) for p in dest.rglob("*")]
    rep.check("unmounted-dir-untouched", not leaked,
              "empty mount point left alone" if not leaked else f"wrote into the unmounted folder: {leaked[:4]}")
    for p in sorted(dest.rglob("*"), reverse=True):
        p.unlink() if p.is_file() or p.is_symlink() else p.rmdir()

    # ---- night 1 and 2 -----------------------------------------------------------------
    r = night("2026-10-01T02:30")
    s1 = complete(snapshots(dest))
    rep.check("night1-snapshot", r.returncode == 0 and len(s1) == 1,
              f"exit {r.returncode}, snapshots {sorted(s1)}" + ("" if r.returncode == 0 else f": {tail(r.stderr, 2)}"))
    if len(s1) != 1:
        rep.add("SKIP", "remaining-nights", "no snapshot to build on; " + ("uses borg/restic?" if re.search(
            r"\b(borg|restic|kopia)\b", text) else "see stderr above"))
        response_checks(rep, args.response)
        raise SystemExit(rep.finish(args.json))
    snap1 = next(iter(s1.values()))
    want = ["Documents/report.odt", "Documents/sub dir/notes.txt", "Documents/-dash.txt", "Documents/new\nline.txt",
            "Projects/app/.git/HEAD", "Projects/big.bin"]
    miss = [w for w in want if not (snap1 / w).exists()]
    rep.check("all-files-copied", not miss, "odd names, hidden folders, big file" if not miss else f"missing {miss}")
    rep.add("PASS" if (snap1 / "Documents/link-to-report").is_symlink() else "WARN", "symlink-kept",
            "symlink copied as a symlink" if (snap1 / "Documents/link-to-report").is_symlink() else "symlink followed or lost")

    # Edited during the day: new content and a new mtime (an edit within the same second
    # and size would be skipped by rsync's size+mtime check, which isn't the script's fault).
    (home / "Documents/changed.txt").write_text("v2 edited\n")
    t = datetime.fromisoformat("2026-10-01T12:00").timestamp()
    os.utime(home / "Documents/changed.txt", (t, t))
    (home / "Documents/gone.txt").unlink()
    (home / "Documents/new.txt").write_text("new\n")
    r = night("2026-10-02T02:30")
    s2 = complete(snapshots(dest))
    snap2 = next((v for k, v in s2.items() if v != snap1), None)
    rep.check("night2-snapshot", r.returncode == 0 and len(s2) == 2 and snap2 is not None,
              f"exit {r.returncode}, snapshots {sorted(s2)}")
    if snap2:
        same = (snap1 / "Documents/report.odt").stat().st_ino == (snap2 / "Documents/report.odt").stat().st_ino
        rep.check("unchanged-files-shared", same, "unchanged files are hard links between nights" if same
                  else "every night is a full copy (7x the space; use --link-dest)")
        ok = ((snap1 / "Documents/changed.txt").read_text(), (snap2 / "Documents/changed.txt").read_text()) == ("v1\n", "v2 edited\n")
        rep.check("snapshots-independent", ok, "night 1 still has v1, night 2 has v2" if ok
                  else "changing a file altered an older snapshot")
        rep.add("PASS" if not (snap2 / "Documents/gone.txt").exists() else "WARN", "deletions-mirrored",
                "deleted file absent from night 2" if not (snap2 / "Documents/gone.txt").exists() else "deleted file still in night 2")
        rep.check("new-file-copied", (snap2 / "Documents/new.txt").exists(), "new.txt in night 2")

    # ---- nights 3..9: retention -------------------------------------------------------
    codes = [night(f"2026-10-{d:02d}T02:30").returncode for d in range(3, 10)]
    s9 = complete(snapshots(dest))
    n = len(s9)
    rep.add("PASS" if n == 7 else "WARN" if n == 8 else "FAIL", "keeps-a-week",
            f"{n} snapshots after 9 nights: {sorted(s9)}; exits {sorted(set(codes))}")
    r = night("2026-10-09T14:00")
    s9b = complete(snapshots(dest))
    nw = newest(s9b)
    ok = r.returncode == 0 and nw is not None and (nw / "Documents/report.odt").exists() and len(s9b) in (7, 8)
    rep.check("same-day-rerun", ok, f"second run on one day: exit {r.returncode}, {len(s9b)} snapshots")

    # ---- night 10 fails mid-transfer ----------------------------------------------------
    before = set(complete(snapshots(dest)))
    r = night("2026-10-10T02:30", fail=True)
    after = set(complete(snapshots(dest)))
    rep.check("failure-exit", r.returncode != 0, f"rsync exit 23 -> script exit {r.returncode}")
    rep.check("failure-not-kept", after <= before, "failed night not presented as a snapshot" if after <= before
              else f"failed run kept as complete: {sorted(after - before)}")
    rep.check("failure-no-prune", before <= after, "nothing pruned after a failed run" if before <= after
              else f"pruned after failure: {sorted(before - after)}")
    r = night("2026-10-11T02:30")
    s11 = snapshots(dest)
    junk = [k for k in s11 if INCOMPLETE.search(k)]
    rep.check("recovers", r.returncode == 0 and len(complete(s11)) in (7, 8),
              f"next night: exit {r.returncode}, {len(complete(s11))} snapshots")
    rep.add("PASS" if not junk else "WARN", "no-stale-partials", "no leftover partial snapshots" if not junk
            else f"left behind: {junk}")

    rep.add("INFO", "method", ", ".join(w for w, rx in (
        ("rsync --link-dest", r"--link-dest"), ("cp -al", r"cp\s+-a?l"), ("btrfs", r"\bbtrfs\b"),
        ("borg", r"\bborg\b"), ("restic", r"\brestic\b"), ("flock", r"\bflock\b"),
        ("mountpoint check", r"mountpoint|findmnt"), ("/proc/mounts", r"/proc/mounts")) if re.search(rx, text)) or "unclear")
    response_checks(rep, args.response)
    raise SystemExit(rep.finish(args.json))


if __name__ == "__main__":
    main()
