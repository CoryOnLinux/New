# linux-scripting evals

Seven evals for the `linux-scripting` skill. Each is graded mostly by **running** the script the model wrote on generated inputs and checking the results with real tools. Reading the code alone missed that the 12/12 run of the first benchmark turned lossless audio into AAC and dropped the picture subtitles.

This folder lives outside the skill on purpose: the skill run must not be able to read the checks, the reference answers or the expectations.

## The evals

| # | Prompt (short) | What separates good from tidy-but-wrong |
|---|---|---|
| 1 | mkv to mp4 on the RX 9070 XT, keep audio/subs, rerunnable | lossless audio stays lossless, picture subs to sidecars, `Season:1` paths, truncated encodes rejected, SIGTERM cleanup |
| 2 | lowercase every .JPG (one-off) | sized to the job, no overwriting an existing .jpg, a folder called `Trip.JPG` left alone |
| 3 | nightly backup to a removable drive, keep a week | no writes when unplugged, `--link-dest`, nothing kept or pruned after a failed rsync, systemd timer |
| 4 | debug: "only the first server, always 0 problems" | ssh eating stdin, pipe subshell, last line without newline, unreachable host |
| 5 | bank JSON to CSV per category and month | splits, month from the date not the file, CSV quoting, no float noise |
| 6 | dedupe 40k photos, keep the oldest | size prefilter, dry run, oldest kept, same-size different files kept, hard links |
| 7 | OpenWrt watchdog with a 15-minute cool-down | POSIX sh under busybox and dash, reset logic over 50 simulated minutes |

`evals.json` has the prompts and expectations in the skill-creator format. Expectations tagged `[check: name]` are decided by the named rows of the check script's output; the rest are judged from the reply.

## Requirements

Python 3 (stdlib only), bash, ffmpeg/ffprobe, rsync, dash, busybox, shellcheck, fish, libfaketime.

```fish
sudo pacman -S --needed python ffmpeg rsync dash busybox shellcheck fish libfaketime
```
A missing fish, busybox or libfaketime turns the rows that need it into SKIP or a less exact mode; nothing else is optional.

## Running a check

Save the script from the run's reply to a file, then:

```fish
python3 checks/mkv2mp4.py out/mkv2mp4.sh --response out/response.md --json out/grading-checks.json
python3 checks/rename.py --cmd "find {dir} -type f -name '*.JPG' -exec ..." --shell fish --response out/response.md
python3 checks/backup.py out/backup.sh --response out/response.md
python3 checks/servers.py out/check-servers.sh --response out/response.md
python3 checks/bank.py out/spending.py --response out/response.md
python3 checks/dedupe.py out/dedupe.sh --dry '{script} -n {dir}' --real '{script} {dir}' --response out/response.md
python3 checks/router.py out/netwatch.sh --response out/response.md
```

- `--cmd` is the command template: `{script}` is a copy of the script and `{dir}` the generated input folder (`{out}` too, for eval 1). Change it to match the script's interface, for example `--cmd '{script} --device /dev/null {dir}'` when the script can't find a GPU in the sandbox.
- Each row is PASS, FAIL, WARN, INFO or SKIP. FAIL means a broken expectation. WARN is a judgment call for the grader (DTS converted, a symlink left dangling). INFO is evidence (approach, audio map, reset minutes).
- `--keep` leaves the temp folder for inspection. Checks never touch anything outside their temp folder: HOME, XDG_RUNTIME_DIR and the target paths all point inside it.

## How the missing pieces are faked

Only what the sandbox lacks is stubbed (`shims/`). Everything else is real:

| Eval | Shim | Does |
|---|---|---|
| 1 | `shims/media/ffmpeg` | `*_vaapi` to libx265/libx264/libsvtav1, drops hw options and filters, execs the real ffmpeg; `SHIM_SLOW` for interrupt tests, `SHIM_TRUNCATE_MATCH` for a "successful" short encode |
| 4 | `shims/ssh/ssh` + `remote/df` | reads stdin like real ssh unless `-n`, runs the remote command locally with a fake `df` per host |
| 3 | `shims/backup/*` | `mountpoint`/`findmnt` driven by `FAKE_MOUNTS`, `date` driven by `FAKE_NOW`, `rsync` that can fail with exit 23; libfaketime for everything else |
| 7 | `shims/router/*` | `ping` up or down per simulated minute, `date` from `FAKE_EPOCH`, no-op `sleep`, `modem-reset` that logs the minute |

Fixed paths in the script under test (`/run/media/`, `/usr/sbin/modem-reset`, `/tmp`, `/proc/uptime`, and a `PATH=` line) are rewritten in a copy, never in the original.

Known limits: a backup script that reads `/proc/mounts` always sees the drive unplugged. busybox ash runs its own `sleep`, so eval 7 runs the full 50 minutes under dash and only minutes 0–8 under busybox. The real VAAPI encode is never exercised.

## Validating the checks

Every check was run against a good answer (`reference/`) and a bad one before use:

| Check | Good answer | Bad answer |
|---|---|---|
| mkv2mp4 | `reference/mkv2mp4.sh` (the benchmark's skill-run script with its two gaps fixed): 31/31 | benchmark skill run: 2 fail (lossless audio, picture subs); benchmark baseline: 3 fail (colon path, truncated file kept) |
| rename | `find -exec sh -c 'mv -n ...'`: 7/7 | `for f in $(find)`: 3 fail; `while read` + `mv` without `-n`: 2 fail |
| backup | `reference/backup.sh`: 18/18 | `mkdir -p` + `cp -a` + `ls \| head`: 6 fail |
| servers | `reference/check-servers.sh`: 10/10 | the original script: 7 fail |
| bank | `reference/bank-spending.py`: 9/9 | jq, month from file name: 5 fail |
| dedupe | `reference/dedupe-photos.sh`: 11 pass, 1 warn (symlink) | md5 loop with `rm`: 7 fail |
| router | `reference/netwatch.sh`: 11/11 | bash, no cool-down: 7 fail |

Writing the references caught bugs the skill now warns about: a trap using a function's `local` variable, and `${arr[-1]}` on an empty array, which both fail at runtime under `set -euo pipefail`.

## Running the benchmark

- Run each eval 3 times per configuration (with skill, without): one run per configuration gives no variance.
- Track tokens and time next to the pass rate. The first benchmark's skill run spent 33% more tokens on a 29-check stubbed test suite that a single real ffmpeg run would have replaced.
- Give both configurations the same user context (CachyOS, fish, hardware); otherwise the comparison measures the context, not the skill.
