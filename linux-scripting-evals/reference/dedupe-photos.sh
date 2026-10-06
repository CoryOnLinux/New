#!/usr/bin/env bash
# dedupe-photos.sh - find files with identical content under DIR and move every copy
# except the oldest into DIR/.dupes-DATE/, keeping their relative paths. Dry run by default.
# Reference answer for eval 6, used to validate checks/dedupe.py.
set -Eeuo pipefail

PROG=${0##*/}
readonly PROG
log() { printf '%s: %s\n' "$PROG" "$*" >&2; }
die() { log "error: $*"; exit 1; }

usage() {
  cat <<EOF
Usage: $PROG [-y] DIR
Moves duplicate files (same content) into DIR/.dupes-DATE/, keeping the oldest copy.
Hard links, symlinks and empty files are left alone. Without -y it only prints what
it would do.
  -y  really move the duplicates
EOF
}

main() {
  local opt apply=0 dir trash rec size rest ino path hash t loser n=0 bytes=0
  local -a recs=()
  local -A count=() seen_ino=() keep=() keep_t=()
  while getopts ':nyh' opt; do
    case $opt in
      n) apply=0 ;;
      y) apply=1 ;;
      h) usage; exit 0 ;;
      *) usage >&2; exit 2 ;;
    esac
  done
  shift $((OPTIND - 1))
  (($# == 1)) || { usage >&2; exit 2; }
  dir=$(readlink -f -- "$1")
  [[ -d $dir ]] || die "not a folder: $1"
  trash=$dir/.dupes-$(date +%F)

  # Only files that share a size can be duplicates, so only those get hashed.
  while IFS= read -r -d '' rec; do
    recs+=("$rec")
    size=${rec%% *}
    count[$size]=$((${count[$size]:-0} + 1))
  done < <(find "$dir" -path "$dir/.dupes-*" -prune -o -type f -size +0 -printf '%s %i %T@ %p\0' | sort -z -k4)

  for rec in "${recs[@]}"; do
    size=${rec%% *} rest=${rec#* }
    ino=${rest%% *} rest=${rest#* }
    t=${rest%% *} path=${rest#* }
    ((count[$size] > 1)) || continue
    [[ -z ${seen_ino[$ino]:-} ]] || continue # another name for the same file
    seen_ino[$ino]=1
    hash=$(b2sum <"$path") hash=${hash%% *}
    if [[ -z ${keep[$hash]:-} ]]; then keep[$hash]=$path keep_t[$hash]=$t; continue; fi
    loser=$path
    if [[ $(printf '%s\n' "$t" "${keep_t[$hash]}" | sort -g | head -1) == "$t" && $t != "${keep_t[$hash]}" ]]; then
      loser=${keep[$hash]} keep[$hash]=$path keep_t[$hash]=$t
    fi
    n=$((n + 1)) bytes=$((bytes + size))
    if ((apply)); then
      mkdir -p -- "$trash/$(dirname -- "${loser#"$dir"/}")"
      mv -n -- "$loser" "$trash/${loser#"$dir"/}"
      printf 'moved %q (kept %q)\n' "${loser#"$dir"/}" "${keep[$hash]#"$dir"/}"
    else
      printf 'would move %q (keeping %q)\n' "${loser#"$dir"/}" "${keep[$hash]#"$dir"/}"
    fi
  done
  if ((apply)); then
    log "$n duplicates ($((bytes / 1024)) KiB) moved to $trash"
  else
    log "$n duplicates ($((bytes / 1024)) KiB); run with -y to move them"
  fi
}

main "$@"
