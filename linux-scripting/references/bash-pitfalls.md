# Bash pitfalls: what passes review and fails in use

Each entry: the trap, what it does, the fix. All of these were reproduced on bash 5.2.

## Loops and input

**A command in the loop reads the loop's stdin.** ffmpeg, ssh, mpv, `read -p`, anything interactive reads stdin, which here is the rest of the list. The loop runs once and ends quietly.
```bash
while IFS= read -r host; do ssh "$host" uptime; done <hosts.txt          # only the first host
while IFS= read -r host; do ssh -n "$host" uptime; done <hosts.txt       # fixed (-n = stdin from /dev/null)
while IFS= read -r f; do ffmpeg -nostdin -i "file:$f" ...; done < <(...) # ffmpeg: -nostdin
while IFS= read -r -u 3 f; do anything "$f"; done 3<list.txt             # or read from another fd
```
Iterating an array (`mapfile -d '' files < <(find ... -print0)`) avoids the problem entirely.

**Pipe into `while` runs the loop in a subshell.** Variables set inside are gone afterwards.
```bash
n=0; find . -name '*.log' | while IFS= read -r f; do n=$((n + 1)); done; echo "$n"   # always 0
n=0; while IFS= read -r -d '' f; do n=$((n + 1)); done < <(find . -name '*.log' -print0); echo "$n"
```

**The last line without a trailing newline is dropped.** `read` returns 1 at EOF even when it filled the variable. Hand-edited lists often lack the final newline.
```bash
while IFS= read -r line || [[ -n $line ]]; do ...; done <list.txt
```
Skip blank and comment lines explicitly: `[[ -z ${line//[[:space:]]/} || $line == \#* ]] && continue`.

**`for f in $(find ...)` and `xargs` without `-0`** split on spaces and newlines and expand globs. Use `find -print0` with `mapfile -d ''`, `while read -d ''`, `xargs -0`, or `find -exec ... {} +`.

**`find -exec sh -c '... {} ...'`** pastes the file name into shell code (injection, breakage on quotes). Pass it as an argument:
```bash
find . -name '*.JPG' -exec sh -c 'for f; do mv -n -- "$f" "${f%.JPG}.jpg"; done' sh {} +
```

## Exit status and `set -e`

**`local`, `export`, `readonly`, `declare` with a command substitution hide its failure.** The status is that of `local`, which is 0.
```bash
local out=$(curl -fsS "$url")        # curl fails, the script carries on with empty $out
local out; out=$(curl -fsS "$url")   # fails as expected
```

**`set -e` is ignored in any command that is part of a condition**, and that includes every command inside a function called as one: `if f`, `f || x`, `f && x`, `! f`, `while f`. A failing step in `f` doesn't stop `f`.
```bash
convert_one "$f" || failed=$((failed + 1))   # inside convert_one, set -e is OFF
```
In such functions, check each step: `cmd || return 1`.

**`((n++))` when `n` is 0 evaluates to 0, which is status 1, and `set -e` exits.** Same for `((n--))` reaching 0 and `let`. Use `n=$((n + 1))`.

**`cmd | grep -q pattern` under `pipefail` can be false when it matched.** `grep -q` exits at the first match, `cmd` then dies of SIGPIPE (141), and the pipeline fails.
```bash
set -o pipefail
if seq 1 1000000 | grep -q 1; then echo found; else echo "not found"; fi   # prints "not found": PIPESTATUS = 141 0
out=$(seq 1 1000000); if grep -q 1 <<<"$out"; then echo found; fi         # fixed: capture first
if seq 1 1000000 | grep -c 1 >/dev/null; then echo found; fi               # fixed: grep -c reads all input
```
`head`, `sed q` and `awk ... exit` behave the same way.

**`${arr[-1]}` on an empty array** prints "bad array subscript" even with a default (`${arr[-1]:-x}`), and under `set -e` that ends the script. Check the length first: `if ((${#arr[@]})); then last=${arr[-1]}; fi`.

**`$?` after `if`, `[[ ]]` or a function call** is often not the status you meant. Capture right away: `cmd; rc=$?` (in a `set -e` script: `rc=0; cmd || rc=$?`).

**Commands in a pipeline or `( )` can't exit the script.** `exit` in `cmd | while ...; do exit 1; done` leaves only the subshell.

## Names and arguments

**Leading dash**: a file called `-rf` or `-i` is an option. Use `--` (`rm -- "$f"`, `mv -- "$a" "$b"`) or prefix `./`. `find` doesn't accept `--` for its start points; turn `-x` into `./-x` first.

**Colons and URLs**: ffmpeg and ffprobe read `name:rest` as a protocol (`Season:1/ep.mkv` fails with "Protocol not found"). Always pass `file:$path`. `scp` and `rsync` read `host:path` the same way; use `./` or an absolute path.

**Newlines in names** break anything line-based (`ls`, `find` without `-print0`, `sort`, `uniq`). Use NUL-separated pipelines end to end: `find -print0 | sort -z | xargs -0`.

**`echo "$var"`** mangles values starting with `-n`/`-e` and, with `-e`, backslashes. Use `printf '%s\n' "$var"`.

**Unquoted right side of `[[ == ]]`** is a pattern: `[[ $a == $b ]]` is true for `b='*'`. Quote it to compare literally.

## Files and directories

**`rm -rf "$dir/"*` with `dir` empty or unset** removes from `/`. Use `"${dir:?}"`, and prefer deleting a known temp path you created.

**`mv` across filesystems is copy then delete**, not atomic: a crash leaves a half-written target. Create temp files in the destination folder (`mktemp -- "$dest/.name.XXXXXX"`), not `/tmp`.

**Truncation by redirect**: `cmd <file >file` empties the file before `cmd` reads it. Write to a temp file and `mv`, or use `sponge` (moreutils).

**`cd` that fails** leaves the script running in the wrong place: `cd -- "$d" || exit`. Better: work with absolute paths, or `( cd -- "$d" && ... )`.

**`cp -r src dst` / `rsync src dst`**: if `dst` exists, `cp -r` creates `dst/src`. With rsync, `src/` copies the contents and `src` copies the folder itself.

**`sudo cmd >file`**: the redirect runs as you, not root. Use `cmd | sudo tee file >/dev/null`.

## Signals and cleanup

```bash
cleanup() { [[ -z $tmp ]] || rm -rf -- "$tmp"; }
trap cleanup EXIT
trap 'exit 130' INT
trap 'exit 143' TERM
```
- A trap that cleans up but doesn't exit (`trap cleanup INT TERM`) lets the script continue with its temp files gone, and it exits 0. Reproduced: after SIGTERM such a script kept writing into the deleted folder, reported success, and leaked the folder.
- Bash runs a trap only after the current foreground command ends. Ctrl+C reaches the whole process group, so the child stops too. `kill PID` or `timeout` reach only the script, which then waits for the child. To react at once and not leave the child running:
  ```bash
  "${cmd[@]}" & child=$!
  wait "$child" || rc=$?
  # in the INT/TERM trap: kill "$child" 2>/dev/null; wait "$child" 2>/dev/null
  ```
- A trap runs after the function that set it has returned, so it can't use that function's `local` variables: under `set -u`, `trap 'rm -f -- "$list"' EXIT` set inside `main` with `local list` fails at exit with "unbound variable" (and exits 1 after a successful run). Make the trap's variables global.
- `trap ... ERR` with `set -E` reaches into functions. It doesn't fire for guarded commands (`x=$(cmd) || x=0`), so it's safe to keep as a "failed at line N" reporter.
- A child killed by Ctrl+C makes ffmpeg exit 255 and most tools exit 130. Treat those as "stop the batch", not "this file failed, try the next".

## Concurrency

```bash
exec 9>"${XDG_RUNTIME_DIR:-/tmp}/$PROG.lock"
flock -n 9 || die "already running"
```
The lock is released when the script exits, however it exits. Under systemd, `flock` on a file in `/run/user/$UID` or `$RUNTIME_DIRECTORY` works.

## Portability that bites on one machine

- `sort`, `[a-z]`, `tr`, `\w` and case-insensitive matches depend on the locale. Set `LC_ALL=C` when parsing tool output or comparing bytes.
- Tool output meant for humans changes between versions (`df`, `ip`, `free`). Ask for machine output: `df --output=pcent,target`, `ip -j`, `findmnt -no`, `stat -c`, `ffprobe -of json`.
- `date -d`, `sed -i`, `stat -c`, `readlink -f` and `mktemp -t` are GNU behaviour. That's fine on Linux; say so if the script might end up on macOS or busybox.
