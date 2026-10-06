#!/usr/bin/env python3
"""Eval 7 (OpenWrt connectivity watchdog): run the script once per simulated minute.

A copy of the script gets its fixed paths pointed into a temp folder: /usr/sbin/modem-reset
(records the minute), /bin/ping and friends (shims), /tmp, /var/run, /var/lock, /var/state
and /proc/uptime. The internet is up or down per minute:

  minute  0-1 up | 2-3 down | 4 up | 5-7 down | 8-24 down | 25-28 up | 29-46 down | 47-49 up

Expected resets: minute 7 (third failure in a row), then about 22 (still down, 15 min
cool-down over), none at 31 (three failures, but only 9 min after the last reset), then
15 min after the second. The 50-minute run uses dash (shimmable sleep); busybox ash
runs minutes 0-8 to confirm it works on the router's shell.
"""
import re
import shutil
import subprocess
from pathlib import Path

from common import SHIMS, Report, env_with, parser, response_checks, tail, workdir

PLAN = "uu" + "dd" + "u" + "ddd" + "d" * 17 + "uuuu" + "d" * 18 + "uuu"
T0 = 1791244800  # 2026-10-06 00:00 UTC


def patch(text, work):
    sh = SHIMS / "router"
    subs = [
        (r"/usr/sbin/modem-reset", str(sh / "modem-reset")),
        (r"(?<![\w./-])/(?:usr/)?s?bin/(ping|date|sleep|logger)\b", r"\1"),
        (r"(?<![\w./-])/proc/uptime\b", str(work / "uptime")),
        (r"(?<![\w./-])/var/(run|lock|state)\b", str(work / r"var/\1")),
        (r"(?<![\w./-])/tmp\b", str(work / "tmp")),
    ]
    for rx, rep in subs:
        text = re.sub(rx, rep.replace("\\", "\\\\") if "\\1" not in rep else rep, text)
    # A script that sets its own PATH (right for cron) would bypass the shims.
    text = re.sub(r"^(\s*(?:export\s+)?PATH=['\"]?)", lambda m: m.group(1) + str(sh) + ":", text, flags=re.M)
    return text


def simulate(script, work, shell, minutes, rep_prefix=""):
    state = work / f"state-{shell}"
    shutil.rmtree(state, ignore_errors=True)
    for d in ("tmp", "var/run", "var/lock", "var/state"):
        (state / d).mkdir(parents=True)
    body = patch(script.read_text(errors="replace"), state)
    s = state / "watchdog"
    s.write_text(body)
    s.chmod(0o755)
    resets, log = state / "resets.log", state / "shim.log"
    resets.touch()
    results = []
    for m in range(minutes):
        (state / "uptime").write_text(f"{3600 + m * 60}.00 {7000 + m * 50}.00\n")
        env = env_with(["router"], PING_PLAN=PLAN, FAKE_MINUTE=m, FAKE_EPOCH=T0 + 60 * m,
                       RESET_LOG=resets, SHIM_LOG=log, PATH_EXTRA="")
        env["PATH"] = f"{SHIMS / 'router'}:/usr/sbin:/usr/bin:/sbin:/bin"   # cron-like PATH
        try:
            argv = ["busybox", "sh", str(s)] if shell == "busybox" else [shell, str(s)]  # `busybox FILE` runs an applet
            r = subprocess.run(argv, env=env, cwd="/", stdin=subprocess.DEVNULL,
                               capture_output=True, text=True, errors="replace", timeout=25)
            results.append((m, r.returncode, r.stderr))
        except subprocess.TimeoutExpired:
            results.append((m, 124, "timed out: still running after 25 s"))
            break
    got = [int(x) for x in resets.read_text().split()]
    return got, results, state


def main():
    args = parser(__doc__, default_cmd="{script}").parse_args()
    rep = Report("router-watchdog")
    work = workdir(args.keep)
    script = Path(args.script)
    text = script.read_text(errors="replace")
    first = text.splitlines()[0] if text else ""
    rep.check("shebang-sh", first.strip() in ("#!/bin/sh", "#!/usr/bin/env sh"), f"first line: {first!r}")
    r = subprocess.run(["dash", "-n", str(script)], capture_output=True, text=True)
    rep.check("dash-syntax", r.returncode == 0, "dash -n ok" if r.returncode == 0 else tail(r.stderr, 2))
    if shutil.which("shellcheck"):
        r = subprocess.run(["shellcheck", "-s", "sh", "-f", "gcc", str(script)], capture_output=True, text=True)
        issues = [l.split(": ", 2)[-1] for l in r.stdout.splitlines()]
        bashisms = [i for i in issues if "POSIX sh" in i or "undefined" in i]
        rep.check("posix-only", not bashisms, "no bashisms per shellcheck -s sh" if not bashisms else bashisms[0])
        rep.add("INFO", "shellcheck", f"{len(issues)} findings" + (f": {issues[0]}" if issues else ""))

    got, results, state = simulate(script, work, "dash", len(PLAN))
    timeouts = [m for m, rc, _ in results if rc == 124]
    rep.check("exits-each-minute", not timeouts, "every run finished (cron style)" if not timeouts
              else f"still running at minute {timeouts[0]}: a loop or long sleep instead of one check per run")
    errs = [(m, e) for m, rc, e in results if rc not in (0, 124) and e.strip()]
    rep.add("PASS" if not errs else "WARN", "clean-runs", "no errors" if not errs else
            f"minute {errs[0][0]}: {tail(errs[0][1], 1)}")
    rep.add("INFO", "resets", f"modem-reset at minutes {got}")
    rep.check("first-reset-at-3rd-failure", bool(got) and got[0] == 7,
              "reset on the 3rd failure in a row (a success in between restarts the count)" if got and got[0] == 7
              else f"first reset at {got[0] if got else 'never'}; expected 7")
    second = [g for g in got if 8 <= g <= 30]
    rep.check("cooldown-then-second", len(second) == 1 and 22 <= second[0] <= 24,
              f"while still down: resets at {second or 'none'} (expected one at 22-24)")
    early = [g for g in got if 29 <= g <= 36]
    third = [g for g in got if g > 30]
    rep.check("cooldown-blocks-31", not early, "no reset at 31 (only 9 min after the last)" if not early
              else f"reset at {early} inside the 15-minute cool-down")
    ok3 = len(third) == 1 and second and 15 <= third[0] - second[0] <= 17
    rep.check("third-after-cooldown", bool(ok3), f"after the cool-down: resets at {third or 'none'}"
              + (f" (expected {second[0] + 15})" if second else ""))
    rep.check("total-resets", len(got) == 3, f"{len(got)} resets in 50 minutes (expected 3)")
    leftovers = [str(p.relative_to(state)) for p in (state / "tmp").rglob("*")] + \
                [str(p.relative_to(state)) for p in (state / "var").rglob("*") if p.is_file()]
    rep.add("INFO", "state-files", ", ".join(leftovers) or "none")

    if shutil.which("busybox"):
        bb, bres, _ = simulate(script, work, "busybox", 9)
        to = [m for m, rc, _ in bres if rc == 124]
        rep.check("busybox-ash", bb == [7] and not to,
                  f"busybox sh, minutes 0-8: resets at {bb}" + (f", timed out at {to[0]}" if to else ""))
    else:
        rep.add("SKIP", "busybox-ash", "busybox not installed")

    response_checks(rep, args.response)
    raise SystemExit(rep.finish(args.json))


if __name__ == "__main__":
    main()
