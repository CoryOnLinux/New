# Testing a script before handing it over

The goal is evidence that the real thing works, at a cost proportional to the script. Run the script; don't simulate it.

## How much

| Script | Test |
|---|---|
| One-off command | Run it once on a copy or a generated sample, or show a dry-run form first. |
| Small script | `bash -n`, `shellcheck`, one real run on generated input, one run with an awkward name. |
| Installed, scheduled or batch | Everything below. |

## Inputs

Generate them in a temp folder. Never test on the user's real data.
```bash
t=$(mktemp -d); trap 'rm -rf -- "$t"' EXIT
mkdir -p -- "$t/sub dir" "$t/-dash dir" "$t/Season:1"
touch -- "$t/a b.txt" "$t/-n.txt" "$t/new"$'\n'"line.txt" "$t/colon:name.txt" "$t/ünïcode.txt" "$t/sub dir/x.txt"
touch -d '2020-01-01 12:00' -- "$t/a b.txt"           # controlled mtimes
truncate -s 5M -- "$t/big.bin"                         # sized files without writing data
```
- Media: `ffmpeg -f lavfi -i testsrc2=s=320x240:d=3 -f lavfi -i sine=d=3 ...`, one file per codec or stream case that matters (see media-ffmpeg.md).
- Structured data: write a small JSON or CSV by hand that has the awkward cases: nested items, a missing field, a comma or quote inside a value, a record on a boundary (a date in the next month).
- Duplicates and collisions: the same content under two names, different content with the same size, an existing target with the name you'd produce.

## Stub only what's missing

When the machine lacks a piece (a GPU, a remote host, root, a mounted drive, a specific date), put a small shim earlier on PATH that replaces that one piece. Keep everything else real:

| Missing | Shim |
|---|---|
| VAAPI GPU | `ffmpeg` wrapper: `*_vaapi` to `libx265`/`libx264`, drop the hw options, exec the real ffmpeg |
| remote host | `ssh` that drops options, takes the host, and runs the remote command locally with a fake `df`/`uptime` for that host |
| mounted drive | `mountpoint` that returns 0 or 1 depending on a marker file |
| a date | `date` that adds `-d "$FAKE_NOW"` when no `-d` was given |
| network | `ping`/`curl` that succeed or fail according to a script of results |

Don't write a mock that pretends to be the whole tool. If the script calls tools by absolute path (`/usr/bin/ssh`), test on a copy with the path replaced, and suggest dropping the absolute path.

## What to run

1. **Happy path**: run the script, then check the results with the tools themselves: `ffprobe -of json`, `diff -r`, `stat -c '%i %s %Y'`, `sha256sum`, `find -links +1`. Don't just read the script's own log.
2. **Rerun**: a second run changes nothing (compare `stat` output before and after) and exits 0.
3. **Failure**: one item fails (corrupt file, host down, disk full via a tiny `tmpfs` or a shim). The batch continues, the summary counts it, and the exit status is non-zero.
4. **Interrupt**: `timeout --preserve-status -s TERM 2 ./script ...`, then check that the status is 143 (or 130 for INT), no temp or partial files remain, no child process is still running (`pgrep -f`), and a rerun finishes the job.
5. **Dry run**: `-n` changes nothing (compare `stat` output before and after) and prints what the real run then does.
6. **Odd names**: the fixtures above, through the whole pipeline.

## Reporting

End the reply with one line saying what actually ran, and what didn't:

> Verified: shellcheck clean; ran on 6 generated MKVs (every audio and subtitle type, odd names) with VAAPI swapped for libx265: outputs checked with ffprobe, rerun skipped all, SIGTERM left no partial files. Not tested: the real VAAPI encode.

Never write "tested" for something that ran only against your own stubs, and never omit the piece you couldn't run.
