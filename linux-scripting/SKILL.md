---
name: linux-scripting
description: Write, fix, review or explain Linux shell scripts and command-line one-offs - Bash or POSIX sh, batch jobs over files (ffmpeg, rsync, find, imagemagick), renames, cleanups, backups, cron and systemd jobs, and "why does my script do X". Use whenever the deliverable is a script or a command the user will run on Linux, even if they never say "bash". Not for application code or Python projects beyond a helper script.
---

# Linux scripting

Generic shell hygiene (quoting, strict mode, not parsing `ls`) you already do. This skill is about what still goes wrong: scripts that are tidy but lose what the user wanted kept, break on the environment they actually run in, or were never run.

## Before writing

1. **List the requirements.** Every "keep", "skip", "don't", "rerun", "only" in the request becomes a line of code or a stated limitation. "Keep" means at the same quality: no lossless-to-lossy conversion, no dropped tracks, metadata, timestamps or permissions. When the target can't hold something, use the closest lossless route (copy, a lossless codec, a sidecar file, a different container) and name whatever is still lost in the reply.
2. **Learn the tool's limits before the shell.** What the format, filesystem or tool can't do decides the design: MP4 can't hold picture subtitles, FAT/exFAT can't hold `:` or files over 4 GiB (FAT32), `rsync src` vs `src/`, a `/bin/sh` that is dash or busybox. For these, read the matching reference before writing:
   - ffmpeg, media conversion, GPU encoding: `references/media-ffmpeg.md`
   - cron, systemd timers, anything that runs unattended: `references/scheduled-jobs.md`
   - `#!/bin/sh`, dash, busybox, OpenWrt, initramfs: `references/posix-sh.md`
3. **Do the cheapest correct thing.** Copy before re-encode, `rsync --link-dest` before full copies, an existing tool (`jdupes`, `rmlint`, `perl-rename`) before a homemade one. If the user asked for the expensive route, do it, and offer the cheap one as a flag or a note.
4. **Size it to the job.** One-off: a command or a script under ~20 lines, no flags, no template. Installed, scheduled or rerun: start from `template.sh` in this skill's folder.
5. **Pick the language.** Shell glues programs and files together. Use Python (stdlib) for structured data (JSON beyond a `jq` one-liner, CSV with quoting, money or date arithmetic) or for logic that has grown past ~150 lines.

## The user's machine

- CachyOS (Arch-based). Packages: `sudo pacman -S --needed pkg`. Say so when something is AUR-only.
- The interactive shell is **fish**. Scripts are bash or sh files. Every command the user is told to paste into their own terminal must be valid fish (commands run on another machine, such as a router over ssh, use that machine's shell): no heredocs, `<(...)` (use `(cmd | psub)`), `[[ ]]`, `$?` (use `$status`), `for ...; do ... done`, `if ...; then ... fi`, bare `VAR=value` (use `set -gx VAR value`) or `${var}`. If it doesn't fit in plain commands, put it in a script file.
- Install personal scripts with `install -Dm755 script.sh ~/.local/bin/name` (run `fish_add_path ~/.local/bin` once).
- systemd is the scheduler; cron usually isn't installed. Use user units (`systemctl --user`) unless root is needed.
- Don't hard-code device numbers (`renderD128`, `card0`, `sda`): detect, and allow an override.

If a script is meant for other machines, drop the pacman hints and check for tools generically.

## Pitfalls that pass review and fail in use

Details and fixes for each: `references/bash-pitfalls.md`.

- A command inside a `while read` loop that reads stdin (ffmpeg, ssh, mpv, `read`) eats the rest of the list. Use `-nostdin`, `ssh -n`, `</dev/null`, or loop over an array.
- `local v=$(cmd)` and `export v=$(cmd)` report success when `cmd` fails. Declare first, then assign.
- `set -e` is off inside anything called from `if`, `while`, `&&`, `||` or `!`, including whole functions. Check each step there yourself.
- `((n++))` with `n=0` returns 1 and exits the script under `set -e`. Use `n=$((n + 1))`.
- `cmd | while read ...` runs the loop in a subshell, so counters reset. Use `done < <(cmd)`.
- `read` drops a last line that has no trailing newline: `while IFS= read -r l || [[ -n $l ]]`.
- `cmd | grep -q x` under `pipefail` can report "no match" when `grep` exits early and `cmd` dies of SIGPIPE. Capture the output first, or use `grep -c`.
- Arguments that start with `-` or contain `:` get read as options or URL protocols: use `--`, `./name`, and `file:` for ffmpeg and ffprobe.
- `rm -rf "$dir/"*` with an empty `$dir` deletes from `/`. Use `"${dir:?}"`.
- Traps: `trap cleanup EXIT` plus `trap 'exit 130' INT` and `trap 'exit 143' TERM`. A trap that only cleans up lets the script carry on after `kill` and exit 0. Variables a trap uses must be global: a function's `local` is gone by the time the EXIT trap runs.
- A signal sent to the script alone (`kill PID`, `timeout`) isn't handled until the current child finishes. For long children run `cmd & pid=$!; wait "$pid"` so the trap fires at once, and kill `$pid` in the trap, or the child keeps running after the script exits.
- `mv` is atomic only within one filesystem. Temp files for finished outputs go in the destination folder, not `/tmp`.

## Rerunnable and destructive work

- Never modify or delete inputs unless asked. Write next to them or into an output folder.
- Done means verified: write to a temp name in the destination folder, check the result (duration, size, checksum, exit status of every step), then `mv` it into place. A skip-if-exists rerun makes a truncated output permanent.
- Delete, overwrite or rename only behind `-n`/`--dry-run`, which prints exactly what would happen. Detect collisions before acting (two inputs mapping to one output, a target that already exists) and refuse to overwrite with `mv -n` or an explicit check.
- Prefer reversible: move to a trash folder or print a delete list over `rm`.
- Use `flock` when overlapping runs would collide (timers, long batches).

## Testing

- `bash -n` (or `dash -n` for sh), plus `shellcheck` if it's installed. Don't install it just for this.
- **Run the script for real** on small generated inputs in a temp folder: odd names (spaces, leading `-`, `:`, a newline), an empty input, one failing item, a rerun, and an interrupt. Generate media with `ffmpeg -f lavfi`, trees with `mktemp -d`, `touch -d`, and `truncate`.
- Stub only what this machine lacks (a GPU, a remote host, root). A small PATH shim that swaps the missing piece (`hevc_vaapi` to `libx265`, an `ssh` that runs the command locally) keeps everything else real. Don't mock a whole tool. Check results with the tool itself (`ffprobe`, `diff`, `stat`), not by reading your own logs. More in `references/testing.md`.
- Keep it proportional: a one-off gets one real run, an installed batch script gets the edge cases.
- Never present an untested path as working. Name it.

## Reply

- The script in one code block, or the command. No intro, no walkthrough of the code.
- A `Run:` block of fish commands: install, dry run, real run.
- At most 3 short notes, only for what the user would want to change or must know: defaults chosen, anything lost or not carried over, anything untested.
- One `Verified:` line saying what you actually ran.
- Debugging or review: lead with the cause of the reported symptom, then the other bugs in order of impact, then the fixed script.
