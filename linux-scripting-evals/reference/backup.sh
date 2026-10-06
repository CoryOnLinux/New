#!/usr/bin/env bash
# backup - nightly snapshot of ~/Documents and ~/Projects to the external Backup drive.
# Each night is a folder named by date and time; unchanged files are hard links to the
# previous night, so every snapshot is complete but only changes take space. Keeps the
# newest 7. Exits 0 without touching anything when the drive isn't mounted.
# Reference answer for eval 3, used to validate checks/backup.py.
set -Eeuo pipefail

PROG=${0##*/}
readonly PROG
readonly DRIVE=/run/media/$USER/Backup
readonly DEST=$DRIVE/snapshots
readonly KEEP=7
readonly SOURCES=("$HOME/Documents" "$HOME/Projects")
TMP=''

log() { printf '%s: %s\n' "$PROG" "$*" >&2; }
die() { log "error: $*"; exit 1; }
cleanup() { if [[ -n $TMP ]]; then rm -rf -- "$TMP"; fi; }
trap cleanup EXIT
trap 'exit 130' INT
trap 'exit 143' TERM

main() {
  local stamp prev rc=0 old
  local -a snaps=()
  if ! mountpoint -q -- "$DRIVE"; then
    log "backup drive not mounted at $DRIVE, skipping"
    exit 0
  fi
  command -v rsync >/dev/null || die "missing rsync (sudo pacman -S --needed rsync)"
  exec 9>"${XDG_RUNTIME_DIR:-/tmp}/$PROG.lock"
  flock -n 9 || die "another backup is running"

  mkdir -p -- "$DEST"
  mapfile -t snaps < <(find "$DEST" -mindepth 1 -maxdepth 1 -type d -name '20[0-9][0-9]-*' ! -name '*.partial' -printf '%f\n' | sort)
  prev=''
  if ((${#snaps[@]})); then prev=${snaps[-1]}; fi # ${snaps[-1]:-} errors on an empty array
  stamp=$(date +%Y-%m-%d_%H%M%S)
  TMP=$DEST/$stamp.partial

  rsync -a --delete --mkpath ${prev:+--link-dest="$DEST/$prev"} -- "${SOURCES[@]}" "$TMP/" || rc=$?
  if ((rc == 24)); then log "some files vanished during the copy (normal for open files)"; rc=0; fi
  if ((rc != 0)); then die "rsync failed (exit $rc); $stamp not kept, nothing pruned"; fi
  mv -- "$TMP" "$DEST/$stamp"
  TMP=''
  log "snapshot $stamp done"

  # Prune only after a good snapshot, and only our own date-named folders.
  mapfile -t snaps < <(find "$DEST" -mindepth 1 -maxdepth 1 -type d -name '20[0-9][0-9]-*' ! -name '*.partial' -printf '%f\n' | sort)
  if ((${#snaps[@]} > KEEP)); then
    for old in "${snaps[@]:0:${#snaps[@]}-KEEP}"; do
      log "removing old snapshot $old"
      rm -rf -- "${DEST:?}/$old"
    done
  fi
  # A partial from an earlier failed night is no longer needed.
  find "$DEST" -mindepth 1 -maxdepth 1 -type d -name '*.partial' -exec rm -rf -- {} +
}

main "$@"
