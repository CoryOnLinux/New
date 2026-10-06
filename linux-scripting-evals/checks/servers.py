#!/usr/bin/env python3
"""Eval 4 (debug check-servers.sh): run the fixed script against fake hosts.

The ssh shim reads stdin like real ssh does (unless -n or a redirect), and runs the
remote command locally with a fake df. Hosts: web1 93 %, web2 45 %, db1 unreachable,
backup 95 % (last line of servers.txt, no trailing newline). The script is copied
next to servers.txt and run from that folder; --cmd can change that.
"""
import re
import shutil
from pathlib import Path

from common import FILES, Report, env_with, expand, parser, response_checks, run, tail, workdir

SCENARIOS = {
    "problems": {"web1": "93", "web2": "45", "db1": "down", "backup": "95"},
    "healthy": {"web1": "40", "web2": "45", "db1": "30", "backup": "50"},
}


def scenario(work, script, cmd, name, cwd=None):
    d = work / name
    d.mkdir()
    shutil.copy(FILES / "check-servers/servers.txt", d / "servers.txt")
    s = d / "check-servers.sh"  # next to servers.txt, where the user keeps it
    shutil.copy(script, s)
    s.chmod(0o755)
    state = d / "hosts.state"
    state.write_text("".join(f"{h} {v}\n" for h, v in SCENARIOS[name].items()))
    log = d / "ssh.log"
    log.touch()
    env = env_with(["ssh"], SSH_FAKE_HOSTS=state, SHIM_LOG=log)
    r = run(expand(cmd, script=s, dir=d), env, cwd=cwd or d, timeout=60)
    hosts = [l.split("\t")[0] for l in log.read_text().splitlines()]
    return r, hosts, (r.stdout or "") + (r.stderr or "")


def main():
    args = parser(__doc__, default_cmd="{script}").parse_args()
    rep = Report("check-servers")
    work = workdir(args.keep)

    r, hosts, out = scenario(work, Path(args.script), args.cmd, "problems")
    want = ["web1", "web2", "db1", "backup"]
    missing = [h for h in want if h not in hosts]
    rep.check("all-hosts-checked", not missing,
              f"ssh calls: {hosts}" + (f"; never checked {missing}" if missing else ""))
    rep.check("last-line-host", "backup" in hosts, "host on the unterminated last line checked" if "backup" in hosts
              else "'backup' (last line, no newline) skipped")
    junk = [h for h in hosts if h not in want]
    rep.check("no-junk-hosts", not junk, f"unexpected ssh targets: {junk}" if junk else "only real hosts")

    lines = out.splitlines()
    full = [h for h in ("web1", "backup") if any(h in l and re.search(r"9[35]\s*%|full|warn", l, re.I) for l in lines)]
    rep.check("full-disks-reported", full == ["web1", "backup"], f"reported: {full}")
    ok_flagged = [l for l in lines if "web2" in l and re.search(r"warn|full|problem", l, re.I)]
    rep.check("healthy-not-flagged", not ok_flagged, "web2 (45 %) not flagged" if not ok_flagged else ok_flagged[0])
    # ssh's own "connect to host db1" message doesn't count: the script has to say it.
    down = [l for l in lines if "db1" in l and not l.startswith("ssh:")
            and re.search(r"unreach|fail|error|down|timed out|could not|connect|offline", l, re.I)]
    rep.check("unreachable-reported", bool(down),
              down[0].strip() if down else "db1 is down, but the output never says so (treated as fine)")
    # The summary is the last line with a number, on stdout if there is one, else stderr.
    summary = next((l for stream in (r.stdout, r.stderr) for l in reversed((stream or "").splitlines())
                    if re.search(r"\d", l) and not l.startswith("ssh:")), "")
    nums = [int(n) for n in re.findall(r"\b\d+\b", summary)]
    rep.check("summary-count", any(n in (2, 3) for n in nums), f"summary line: {summary.strip()!r}")
    rep.check("exit-nonzero-on-problems", r.returncode != 0, f"exit {r.returncode}")

    r2, hosts2, out2 = scenario(work, Path(args.script), args.cmd, "healthy")
    summary2 = next((l for l in reversed((r2.stdout or "").splitlines()) if re.search(r"\d", l)), "")
    rep.check("healthy-run", r2.returncode == 0 and len(set(hosts2)) == 4,
              f"exit {r2.returncode}, hosts {hosts2}, summary {summary2.strip()!r}")

    # Run from another directory, the way cron or a systemd timer would.
    elsewhere = work / "elsewhere"
    elsewhere.mkdir()
    d = work / "healthy"
    env = env_with(["ssh"], SSH_FAKE_HOSTS=d / "hosts.state", SHIM_LOG=d / "ssh-cwd.log")
    (d / "ssh-cwd.log").touch()
    r3 = run(expand(args.cmd, script=d / "check-servers.sh", dir=d), env, cwd=elsewhere, timeout=60)
    n3 = len((d / "ssh-cwd.log").read_text().splitlines())
    rep.add("PASS" if r3.returncode == 0 and n3 == 4 else "WARN", "runs-from-any-cwd",
            f"from another folder: exit {r3.returncode}, {n3} hosts checked" + ("" if n3 else f" ({tail(r3.stderr, 1)})"))

    text = Path(args.script).read_text(errors="replace")
    how = [w for w, rx in (("ssh -n", r"ssh\s+(-\w*\s+)*-\w*n"), ("</dev/null", r"</dev/null"),
                           ("read -u / fd 3", r"read\s+.*-u\s*\d|\d<"), ("mapfile/array", r"mapfile|readarray"))
           if re.search(rx, text)]
    rep.add("INFO", "stdin-fix", ", ".join(how) or "none spotted")
    rep.add("INFO", "local-masking", "still `local x=$(...)`" if re.search(r"local\s+\w+=\$\(", text) else "fixed or absent")

    response_checks(rep, args.response)
    raise SystemExit(rep.finish(args.json))


if __name__ == "__main__":
    main()
