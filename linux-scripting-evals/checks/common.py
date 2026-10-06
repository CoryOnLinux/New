"""Shared helpers for the execution checks (stdlib only)."""
import argparse
import json
import os
import re
import shlex
import shutil
import signal
import subprocess
import sys
import tempfile
import time
from pathlib import Path

EVALS = Path(__file__).resolve().parent.parent
SHIMS = EVALS / "shims"
FIXTURES = EVALS / "fixtures"
FILES = EVALS / "files"


class Report:
    """Collects PASS / FAIL / WARN / INFO rows, prints them as they come, writes JSON."""

    def __init__(self, name):
        self.name = name
        self.rows = []

    def add(self, status, check, detail=""):
        self.rows.append({"check": check, "status": status, "detail": detail})
        print(f"{status:<5} {check:<26} {detail}".rstrip(), flush=True)

    def check(self, check, ok, detail=""):
        self.add("PASS" if ok else "FAIL", check, detail)
        return ok

    def finish(self, json_path=None):
        counts = {s: sum(r["status"] == s for r in self.rows) for s in ("PASS", "FAIL", "WARN", "SKIP")}
        print(f"\n{self.name}: " + ", ".join(f"{n} {s.lower()}" for s, n in counts.items() if n))
        if json_path:
            Path(json_path).write_text(json.dumps({"eval": self.name, "results": self.rows}, indent=2))
        return 1 if counts["FAIL"] else 0


def parser(description, default_cmd="{script} {dir}", needs_script=True):
    p = argparse.ArgumentParser(description=description)
    if needs_script:
        p.add_argument("script", help="the script the run produced (copied before running; never edited in place)")
    p.add_argument("--cmd", default=default_cmd,
                   help=f"command template; {{script}} and {{dir}} are filled in (default: {default_cmd!r})")
    p.add_argument("--response", help="the run's reply (markdown) for the reply-format checks")
    p.add_argument("--json", help="also write results to this JSON file")
    p.add_argument("--keep", action="store_true", help="keep the temp directory for inspection")
    return p


def workdir(keep):
    d = Path(tempfile.mkdtemp(prefix="lsx-eval."))
    if not keep:
        import atexit
        atexit.register(shutil.rmtree, d, True)
    else:
        print(f"INFO  workdir                    {d}")
    return d


def install_script(src, work, name=None):
    dst = work / "bin" / (name or Path(src).name)
    dst.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy(src, dst)
    dst.chmod(0o755)
    return dst


def expand(template, **values):
    out = template
    for k, v in values.items():
        out = out.replace("{" + k + "}", shlex.quote(str(v)))
    return out


def env_with(shim_dirs=(), **extra):
    env = dict(os.environ)
    env["PATH"] = os.pathsep.join([str(SHIMS / d) for d in shim_dirs] + [env.get("PATH", "")])
    env.update({k: str(v) for k, v in extra.items()})
    return env


def run(cmd, env, cwd=None, timeout=600, shell="bash", stdin=subprocess.DEVNULL):
    """Run a command string through bash (or fish); returns CompletedProcess with text output."""
    try:
        return subprocess.run([shell, "-c", cmd], env=env, cwd=cwd, stdin=stdin, capture_output=True,
                              text=True, errors="replace", timeout=timeout)
    except subprocess.TimeoutExpired as e:
        return subprocess.CompletedProcess(e.cmd, 124, e.stdout or "", (e.stderr or "") + "\n[timed out]")


def start(cmd, env, cwd=None):
    """Start `exec CMD` so the returned PID is the script itself (for signal tests)."""
    return subprocess.Popen(["bash", "-c", "exec " + cmd], env=env, cwd=cwd, stdin=subprocess.DEVNULL,
                            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, errors="replace",
                            start_new_session=True)


def pids_matching(text):
    out = subprocess.run(["pgrep", "-f", text], capture_output=True, text=True).stdout.split()
    return [int(p) for p in out if int(p) != os.getpid()]


def wait_for(pred, timeout, step=0.1):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if pred():
            return True
        time.sleep(step)
    return pred()


def kill_all(pids):
    for p in pids:
        try:
            os.kill(p, signal.SIGKILL)
        except ProcessLookupError:
            pass


def snapshot(root):
    """path -> (inode, size, mtime_ns, is_link) for everything under root."""
    snap = {}
    for dirpath, dirnames, filenames in os.walk(root):
        for n in filenames + [d for d in dirnames if os.path.islink(os.path.join(dirpath, d))]:
            p = os.path.join(dirpath, n)
            st = os.lstat(p)
            snap[os.path.relpath(p, root)] = (st.st_ino, st.st_size, st.st_mtime_ns, os.path.islink(p))
    return snap


def tail(text, n=6):
    lines = [l for l in (text or "").splitlines() if l.strip()]
    return " | ".join(lines[-n:])


# ---- reply-format checks shared by every eval ------------------------------------------

FENCE = re.compile(r"^```([^\n`]*)\n(.*?)^```", re.S | re.M)
FISH_BAD = [
    (re.compile(r"<<-?\s*['\"]?\w+"), "heredoc"),
    (re.compile(r"<\("), "process substitution <(...) (fish uses (cmd | psub))"),
    (re.compile(r"\[\["), "[[ ]]"),
    (re.compile(r"\$\?"), "$? (fish uses $status)"),
    (re.compile(r"^\s*(for|while)\b.*;\s*do\b|^\s*done\b", re.M), "for/while ... do/done"),
    (re.compile(r"^\s*[A-Za-z_][A-Za-z0-9_]*=\S*\s*$", re.M), "bare VAR=value (fish uses set -gx)"),
    (re.compile(r"^\s*if\s.*;\s*then\b|^\s*fi\s*$", re.M), "if/then/fi"),
    (re.compile(r"\$\{"), "${var} expansion"),
]


def fish_problems(code):
    return [why for rx, why in FISH_BAD if rx.search(code)]


def response_checks(report, path, max_notes=3):
    """Mechanical facts about the reply; the grader decides how strict to be."""
    if not path:
        report.add("SKIP", "reply-format", "no --response given")
        return
    text = Path(path).read_text(errors="replace")
    blocks = [(m.group(1).strip().lower(), m.group(2)) for m in FENCE.finditer(text)]
    prose = FENCE.sub("", text)
    prose_lines = [l for l in prose.splitlines() if l.strip()]
    bullets = [l for l in prose_lines if re.match(r"\s*([-*+]|\d+\.)\s", l)]
    headers = [l for l in prose_lines if l.lstrip().startswith("#")]
    report.add("INFO", "reply-shape",
               f"{len(blocks)} code blocks, {len(prose_lines)} prose lines, {len(bullets)} bullets, {len(headers)} headers")
    verified = [l for l in prose_lines if re.match(r"\s*\**verified\**\s*:", l, re.I)]
    report.add("INFO", "reply-verified-line", verified[0].strip()[:200] if verified else "none")

    run_blocks = [code for lang, code in blocks if lang in ("fish", "")] or \
                 [code for lang, code in blocks if lang in ("sh", "bash", "shell", "console") and len(code.splitlines()) <= 12]
    if not run_blocks:
        report.add("INFO", "reply-fish-commands", "no command block found")
        return
    issues = []
    for code in run_blocks:
        issues += fish_problems(code)
        if shutil.which("fish"):
            r = subprocess.run(["fish", "--no-execute", "-c", code], capture_output=True, text=True)
            if r.returncode != 0:
                issues.append("fish --no-execute: " + tail(r.stderr, 2))
    report.check("reply-fish-commands", not issues,
                 "valid fish" if not issues else "; ".join(sorted(set(issues))))
