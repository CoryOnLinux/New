#!/usr/bin/env bash
# NAME - one line on what it does and to what.
# Skeleton for scripts that get installed, scheduled or rerun. One-offs don't need it:
# delete what the script doesn't use.
set -Eeuo pipefail

PROG=${0##*/}
readonly PROG
DRY_RUN=0
TMP=''

log() { printf '%s: %s\n' "$PROG" "$*" >&2; }
die() { log "error: $*"; exit 1; }
# Prints the command instead of running it under -n. Use it for every change to disk.
run() { if ((DRY_RUN)); then log "would run: ${*@Q}"; else "$@"; fi; }

usage() {
  cat <<EOF
Usage: $PROG [-n] FILE...
What it does, in a sentence or two, including what it never touches.
  -n  dry run: print what would be done, change nothing
  -h  show this help
EOF
}

cleanup() { if [[ -n $TMP ]]; then rm -rf -- "$TMP"; fi; }
trap cleanup EXIT
# Exit on signals, which runs the EXIT trap; cleaning up without exiting would
# let the script carry on and report success.
trap 'exit 130' INT
trap 'exit 143' TERM
trap 'log "failed at line $LINENO: $BASH_COMMAND"' ERR

main() {
  local opt f
  while getopts ':nh' opt; do # getopts also stops at --
    case $opt in
      n) DRY_RUN=1 ;;
      h) usage; exit 0 ;;
      :) die "-$OPTARG needs a value" ;;
      *) usage >&2; exit 2 ;;
    esac
  done
  shift $((OPTIND - 1))
  if (($# == 0)); then usage >&2; exit 2; fi

  command -v jq >/dev/null || die "missing jq (sudo pacman -S --needed jq)"

  # Scratch space next to the outputs, so finished files can be mv'd into place atomically:
  # TMP=$(mktemp -d -- "$dest/.$PROG.XXXXXX")

  for f in "$@"; do
    [[ -e $f ]] || die "no such file: $f"
    run touch -- "$f" # the real work goes here
  done
}

main "$@"
