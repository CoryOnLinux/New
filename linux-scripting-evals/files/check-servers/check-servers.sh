#!/bin/bash
# check-servers.sh - warn about full root disks on every server in servers.txt
set -euo pipefail

problems=0

check_host() {
  local usage=$(ssh "$1" "df --output=pcent / | tail -1" | tr -dc '0-9')
  if [ "$usage" -gt 90 ]; then
    echo "WARNING: $1 root is ${usage}% full"
    problems=$((problems + 1))
  fi
}

cat servers.txt | while read host; do
  check_host $host
done

echo "$problems problems found"
[ "$problems" -eq 0 ]
