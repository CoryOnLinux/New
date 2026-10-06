#!/usr/bin/env python3
"""Eval 1 (mkv -> mp4 on the GPU): run the produced script on generated MKVs and check the MP4s.

VAAPI is swapped for software encoders by shims/media/ffmpeg, so everything except
the GPU itself is real. If the script can't find a GPU in the sandbox, give it its
override through --cmd, e.g.  --cmd 'VAAPI_DEVICE=/dev/null {script} {dir}'  or
--cmd '{script} --device /dev/null {dir}'. {out} is an empty folder for scripts
that want an output directory.
"""
import json
import os
import re
import signal
import subprocess
import time
from pathlib import Path

from common import (FIXTURES, Report, env_with, install_script, kill_all, parser, pids_matching,
                    response_checks, run, snapshot, start, tail, wait_for, workdir, expand)

INPUTS = {
    "rich": "Season:1/ep 01 [x].mkv",
    "hevc10": "-leading dash.mkv",
    "newline": "new\nline.mkv",
    "cover": "cover/with cover.mkv",
}
OPTIONAL = {"upper-ext": "Extras/TRAILER.MKV"}
SIDECAR_EXT = {".sup", ".mks", ".idx", ".sub", ".srt", ".ass", ".vtt", ".mkv"}
LOSSLESS = re.compile(r"^(flac|alac|truehd|mlp|pcm_.*|wavpack|tta)$")


def probe(path):
    r = subprocess.run(["ffprobe", "-v", "error", "-print_format", "json", "-show_format", "-show_streams",
                        "-show_chapters", "file:" + str(path)], capture_output=True, text=True)
    return json.loads(r.stdout) if r.returncode == 0 and r.stdout else None


def duration(info):
    try:
        return float(info["format"]["duration"])
    except (TypeError, KeyError, ValueError):
        return 0.0


def streams(info, kind):
    return [s for s in (info or {}).get("streams", []) if s.get("codec_type") == kind]


def find_output(src_rel, roots, exclude=()):
    stem = Path(src_rel).name.rsplit(".", 1)[0]
    for root in roots:
        for dirpath, _, files in os.walk(root):
            for f in files:
                p = Path(dirpath) / f
                if f.lower() == stem.lower() + ".mp4" and p not in exclude:
                    return p
    return None


def leftovers(root, allowed):
    bad = []
    for dirpath, _, files in os.walk(root):
        for f in files:
            p = Path(dirpath) / f
            if p in allowed or p.suffix.lower() in SIDECAR_EXT:
                continue
            bad.append(str(p.relative_to(root)))
    return bad


def main():
    args = parser(__doc__).parse_args()
    rep = Report("mkv2mp4")
    work = workdir(args.keep)
    script = install_script(args.script, work)
    lib, out = work / "library", work / "out"
    out.mkdir()
    subprocess.run([str(FIXTURES / "make-media.sh"), "full", str(lib)], check=True)
    (work / "home").mkdir()
    (work / "run").mkdir(mode=0o700)
    log = work / "ffmpeg.log"
    base_env = dict(SHIM_LOG=log, HOME=work / "home", XDG_RUNTIME_DIR=work / "run", VAAPI_DEVICE="/dev/null")
    env = env_with(["media"], **base_env)
    srcs = {k: lib / v for k, v in {**INPUTS, **OPTIONAL}.items()}
    done_mp4 = lib / "already/done.mp4"
    done_before = os.stat(done_mp4)

    # ---- run 0: a relative folder argument with a colon, as typed from inside the library
    r = run(expand(args.cmd, script=script, dir="Season:1", out=out), env, cwd=lib)
    o = find_output(INPUTS["rich"], [lib / "Season:1", out])
    rep.check("relative-colon-arg", r.returncode == 0 and o is not None,
              f"'Season:1' from inside the library: exit {r.returncode}"
              + ("" if o else f", no MP4: {tail(r.stderr, 3)}"))

    # ---- run 1 ------------------------------------------------------------------------
    r = run(expand(args.cmd, script=script, dir=lib, out=out), env)
    rep.check("run1-exit", r.returncode == 0, f"exit {r.returncode}: {tail(r.stderr)}")

    outs = {}
    for key, src in srcs.items():
        o = find_output(src.relative_to(lib), [lib, out], exclude={done_mp4})
        outs[key] = o
        info = probe(o) if o else None
        ok = info is not None and duration(info) >= 0.95 * duration(probe(src))
        if key in OPTIONAL:
            rep.add("PASS" if ok else "WARN", f"output-{key}", f"{src.name!r} -> {o.name if o else 'nothing'}")
        else:
            rep.check(f"output-{key}", ok, f"{str(src.relative_to(lib))!r} -> "
                      + (f"{str(o.relative_to(work))!r} ({duration(info):.2f}s)" if info else "no valid MP4"))

    lines = log.read_text(errors="replace").splitlines() if log.exists() else []
    rich_cmds = [l for l in lines if "ep\\ 01" in l or "ep 01" in l]
    rep.check("vaapi-encoder", any("_vaapi" in l for l in rich_cmds),
              "H.264 source encoded with a *_vaapi encoder" if any("_vaapi" in l for l in rich_cmds)
              else "no *_vaapi encoder in the ffmpeg calls for the H.264 source")

    rich = probe(outs["rich"]) if outs["rich"] else None
    if rich:
        src_audio = streams(probe(srcs["rich"]), "audio")
        aud = streams(rich, "audio")
        rep.check("audio-all-tracks", len(aud) == len(src_audio), f"{len(aud)} of {len(src_audio)} audio tracks")
        pairs = list(zip(src_audio, aud)) if len(aud) == len(src_audio) else []
        detail = ", ".join(f"{a['codec_name']}->{b['codec_name']}" for a, b in pairs)
        lossless_in = [(a, b) for a, b in pairs if LOSSLESS.match(a["codec_name"])]
        lost = [f"{a['codec_name']}->{b['codec_name']}" for a, b in lossless_in if not LOSSLESS.match(b["codec_name"])]
        if pairs:
            rep.check("lossless-audio", not lost, "FLAC, PCM and TrueHD stay lossless" if not lost
                      else "lossless made lossy: " + ", ".join(lost))
            dts = [(a, b) for a, b in pairs if a["codec_name"] == "dts"]
            for a, b in dts:
                kept = b["codec_name"] == "dts" or LOSSLESS.match(b["codec_name"])
                rep.add("PASS" if kept else "WARN", "dts-audio",
                        f"dts->{b['codec_name']}" + ("" if kept else " (MP4 can carry DTS untouched)"))
            reenc = [f"{a['codec_name']}->{b['codec_name']}" for a, b in pairs
                     if a["codec_name"] in ("aac", "opus") and b["codec_name"] != a["codec_name"]]
            rep.add("WARN" if reenc else "PASS", "compatible-audio-copied",
                    "re-encoded: " + ", ".join(reenc) if reenc else "AAC and Opus copied")
            rep.add("INFO", "audio-map", detail)
        else:
            rep.add("FAIL", "lossless-audio", "track count changed, can't pair tracks")
        langs = [s.get("tags", {}).get("language") for s in aud]
        rep.check("audio-language", "jpn" in langs, f"audio languages: {langs}")

        subs = streams(rich, "subtitle")
        text = [s for s in subs if s.get("codec_name") == "mov_text"]
        tl = sorted(s.get("tags", {}).get("language", "und") for s in text)
        rep.check("text-subs", len(text) == 2 and tl == ["eng", "jpn"], f"mov_text tracks: {tl}")
        forced = [s for s in text if s.get("tags", {}).get("language") == "jpn" and s.get("disposition", {}).get("forced")]
        rep.add("PASS" if forced else "WARN", "forced-flag", "jpn forced flag kept" if forced else "forced flag lost")
        rep.check("chapters", len(rich.get("chapters", [])) == 2, f"{len(rich.get('chapters', []))} of 2 chapters")

        # Picture subs: MP4 can't hold them, so look for sidecars next to the output.
        stem = outs["rich"].name[:-4]
        side = [p for p in outs["rich"].parent.iterdir()
                if p.name.startswith(stem) and p.suffix.lower() in {".sup", ".mks", ".idx", ".sub", ".mkv"}
                and p != srcs["rich"]]
        pic = []
        for p in side:
            info = probe(p)
            pic += [s["codec_name"] for s in streams(info, "subtitle")
                    if s.get("codec_name") in ("hdmv_pgs_subtitle", "dvd_subtitle")]
        rep.check("picture-subs", {"hdmv_pgs_subtitle", "dvd_subtitle"} <= set(pic),
                  f"sidecars: {[p.name for p in side]} holding {sorted(set(pic))}" if side
                  else "PGS and VobSub tracks dropped, no sidecar files")

        v = streams(rich, "video")
        if v and v[0]["codec_name"] == "hevc":
            tag = v[0].get("codec_tag_string")
            rep.add("PASS" if tag == "hvc1" else "WARN", "hevc-tag", f"codec tag {tag} (Apple/TVs need hvc1)")

    cover = probe(outs["cover"]) if outs["cover"] else None
    if cover:
        vids = [s for s in streams(cover, "video") if not s.get("disposition", {}).get("attached_pic")]
        rep.check("cover-not-encoded", len(vids) == 1, f"{len(vids)} real video stream(s) in output")

    hevc = probe(outs["hevc10"]) if outs["hevc10"] else None
    if hevc:
        v = streams(hevc, "video")
        pix = v[0].get("pix_fmt", "") if v else ""
        rep.check("ten-bit-kept", "10" in pix, f"pix_fmt {pix}")
        enc = [l for l in lines if "leading\\ dash" in l and ("_vaapi" in l)]
        rep.add("INFO", "hevc-source", "re-encoded" if enc else "copied (no encoder call)")
    text = Path(args.script).read_text(errors="replace")
    rep.add("INFO", "offers-video-copy",
            "yes" if re.search(r"(-c:v|-vcodec|-codec:v)\s+copy|c:v copy|copy[-_ ]?video|remux", text, re.I) else "no")
    rep.add("INFO", "render-node",
            ("hard-codes renderD128; " if "renderD128" in text else "")
            + ("detects via sysfs" if re.search(r"/sys/class/drm|0x1002|mem_info_vram", text) else "no sysfs detection"))

    after = os.stat(done_mp4)
    rep.check("existing-mp4-untouched", (after.st_ino, after.st_mtime_ns) == (done_before.st_ino, done_before.st_mtime_ns),
              "already/done.mp4 left alone")
    allowed = set(srcs.values()) | {done_mp4, lib / "already/done.mkv"} | {o for o in outs.values() if o}
    extra = leftovers(lib, allowed) + leftovers(out, allowed)
    rep.check("no-leftovers", not extra, "clean" if not extra else f"left behind: {extra}")

    # ---- run 2: rerun changes nothing ---------------------------------------------------
    before = snapshot(work / "library"), snapshot(out)
    r2 = run(expand(args.cmd, script=script, dir=lib, out=out), env)
    changed = [k for k in set(before[0]) | set(snapshot(lib)) if before[0].get(k) != snapshot(lib).get(k)]
    changed += [k for k in set(before[1]) | set(snapshot(out)) if before[1].get(k) != snapshot(out).get(k)]
    rep.check("rerun-skips", r2.returncode == 0 and not changed,
              f"exit {r2.returncode}, " + ("nothing rewritten" if not changed else f"changed: {changed}"))

    # ---- truncated encode that exits 0 must not be kept ----------------------------------
    tdir, tout = work / "trunc", work / "trunc-out"
    tout.mkdir()
    subprocess.run([str(FIXTURES / "make-media.sh"), "single", str(tdir), "3"], check=True)
    tenv = env_with(["media"], SHIM_TRUNCATE_MATCH="single.mkv", **base_env)
    r3 = run(expand(args.cmd, script=script, dir=tdir, out=tout), tenv)
    o = find_output("single.mkv", [tdir, tout])
    rep.check("truncated-rejected", o is None,
              "1 s output of a 3 s file was not kept" if o is None
              else f"kept {o.name} at {duration(probe(o)):.2f}s of 3s; reruns would skip it forever")
    rep.add("PASS" if r3.returncode != 0 else "WARN", "truncated-exit", f"exit {r3.returncode}")
    kept = {o} if o else set()  # already failed above; don't count it twice
    extra = leftovers(tdir, {tdir / "single.mkv"} | kept) + leftovers(tout, kept)
    rep.check("truncated-no-leftovers", not extra, "clean" if not extra else f"left behind: {extra}")

    # ---- SIGTERM to the script mid-encode (kill PID / systemctl stop) ------------------
    sdir, sout = work / "term", work / "term-out"
    sout.mkdir()
    subprocess.run([str(FIXTURES / "make-media.sh"), "single", str(sdir), "5"], check=True)
    senv = env_with(["media"], SHIM_SLOW=1, **base_env)
    p = start(expand(args.cmd, script=script, dir=sdir, out=sout), senv)
    pat = f"^ffmpeg .*{re.escape(str(sdir))}/single.mkv"
    if not wait_for(lambda: pids_matching(pat), 30):
        p.kill()
        rep.add("SKIP", "sigterm", "never saw ffmpeg start on the file")
    else:
        time.sleep(1.0)
        t0 = time.monotonic()
        p.send_signal(signal.SIGTERM)
        try:
            p.wait(timeout=30)
        except subprocess.TimeoutExpired:
            p.kill()
            p.wait()
        took = time.monotonic() - t0
        rep.check("sigterm-exit", p.returncode != 0, f"exit {p.returncode} after {took:.1f}s")
        rep.add("PASS" if took < 3 else "WARN", "sigterm-prompt", f"stopped {took:.1f}s after SIGTERM")
        time.sleep(1.0)
        orphans = pids_matching(pat)
        rep.check("sigterm-no-orphan", not orphans,
                  "ffmpeg stopped with the script" if not orphans else "ffmpeg kept running after the script exited")
        kill_all(orphans)
        time.sleep(0.5)
        o = find_output("single.mkv", [sdir, sout])
        partial = o is not None and duration(probe(o)) < 4.75
        extra = leftovers(sdir, {sdir / "single.mkv"} | ({o} if o and not partial else set())) + leftovers(sout, {o} if o else set())
        rep.check("sigterm-no-partial", not partial and not extra,
                  "no partial or temp files" if not partial and not extra
                  else f"partial MP4: {o.name if partial else '-'}; left behind: {extra}")
        r4 = run(expand(args.cmd, script=script, dir=sdir, out=sout), env)
        o = find_output("single.mkv", [sdir, sout])
        rep.check("sigterm-then-rerun", r4.returncode == 0 and o is not None and duration(probe(o)) >= 4.75,
                  f"rerun exit {r4.returncode}, output {'complete' if o else 'missing'}")

    response_checks(rep, args.response)
    raise SystemExit(rep.finish(args.json))


if __name__ == "__main__":
    main()
