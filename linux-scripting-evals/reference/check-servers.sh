#!/usr/bin/env bash
# check-servers.sh - warn about full root disks on every server in servers.txt
# Reference answer for eval 4, used to validate checks/servers.py.
set -Eeuo pipefail

list=${1:-"$(dirname -- "$(readlink -f -- "${BASH_SOURCE[0]}")")/servers.txt"}
limit=90
problems=0

check_host() {
  local host=$1 usage
  # -n: without it ssh reads the rest of servers.txt and the loop stops after one host
  if ! usage=$(ssh -n -o BatchMode=yes -o ConnectTimeout=5 "$host" "df --output=pcent / | tail -1"); then
    echo "ERROR: $host unreachable" >&2
    return 1
  fi
  usage=${usage//[!0-9]/}
  if [[ -z $usage ]]; then echo "ERROR: $host: no df output" >&2; return 1; fi
  if ((usage > limit)); then echo "WARNING: $host root is ${usage}% full"; return 1; fi
}

# Read from the file directly (no pipe, so $problems survives), and keep a last line
# that has no trailing newline.
while IFS= read -r host || [[ -n $host ]]; do
  host=${host%%#*}
  host=${host//[[:space:]]/}
  [[ -n $host ]] || continue
  check_host "$host" || problems=$((problems + 1))
done <"$list"

echo "$problems problems found"
((problems == 0))
