# POSIX sh, dash and busybox

Read when the script runs under `#!/bin/sh`, on OpenWrt or another busybox system, Alpine, an initramfs, or Debian/Ubuntu's `/bin/sh` (dash).

## Test with the shells it will meet

- On Arch and CachyOS, `/bin/sh` is bash in POSIX mode and accepts most bashisms. Running `sh script` there proves nothing.
- busybox ash accepts several bashisms that dash rejects, so passing on the router doesn't mean POSIX. Tested (dash 0.5.12, busybox 1.36):

| Feature | dash | busybox ash |
|---|---|---|
| `local` | yes | yes |
| `[[ ]]`, `[ a == b ]`, `${v//x/y}`, `$'\t'`, `source`, `echo -e`, `read -d`, `$RANDOM` | no | yes |
| `set -o pipefail` | **no, and the unknown option aborts the script** | yes |
| arrays, `<<<`, `{a,b}`, `((...))`, `<(...)` | no | no |
| `trap ... EXIT INT TERM`, `mktemp -d`, `date +%s` | yes | yes |

- Check with `shellcheck -s sh` (if installed), `dash -n script` for syntax, and actually run it under `dash` and `busybox sh` (`pacman -S dash busybox` on Arch). `dash -n` won't flag `[[`, because to the parser that's just a command name.

## Writing it

- `#!/bin/sh`, then `set -eu`. No `pipefail` unless the target shell is known to have it. Where a pipeline's status matters, split it: `out=$(cmd) || exit 1; printf '%s\n' "$out" | filter`.
- The only array is `"$@"`: build lists with `set -- "$@" "$item"`. For lists of file names, use `find ... -exec cmd {} +` rather than word-splitting.
- `[ ]` with every variable quoted, `=` not `==`, and `case` for pattern matching:
  ```sh
  case $f in *.log) ... ;; esac
  ```
- `printf` instead of `echo` for anything other than a fixed string.
- `.` instead of `source`; `$(( ))` for arithmetic, and `n=$((n + 1))` as a statement.
- `local` works in dash and busybox but isn't POSIX. Fine for those targets; say so if the script must run on any sh.
- Traps take names without `SIG`: `trap cleanup EXIT` and `trap 'exit 143' TERM`, `trap 'exit 130' INT`.
- Locking without `flock` (some busybox builds lack it): `mkdir "$lock" 2>/dev/null || exit 0`, plus `trap 'rmdir "$lock"' EXIT`. To survive a crash, also store the PID and check it with `kill -0`.

## OpenWrt specifics

- cron is busybox crond. The root crontab is `/etc/crontabs/root`, edited with `crontab -e` or directly, and needs `/etc/init.d/cron restart` afterwards. Cron's PATH is minimal: use full paths, or set PATH at the top of the script.
- `/tmp` is RAM (tmpfs): good for state that may be lost on reboot (counters, timestamps, locks), bad for growing logs. `/etc` and `/root` live on flash: avoid writing there every minute, because it wears the flash.
- Log with `logger -t name "message"`, read with `logread -e name`.
- busybox `ping`: `ping -c 1 -W 2 host` (`-W` is the timeout in seconds). Check the exit status; don't parse the output.
- No bash by default (`opkg install bash` exists, but scripts for routers should not need it).
- State that has to survive between cron runs goes in small files: `echo "$n" >"$state.tmp" && mv "$state.tmp" "$state"`. Treat a missing or garbage file as zero, not as an error.
