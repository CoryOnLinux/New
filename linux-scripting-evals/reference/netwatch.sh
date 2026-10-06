#!/bin/sh
# netwatch - run from cron every minute: ping 1.1.1.1 once, and after 3 failures in a
# row run /usr/sbin/modem-reset, at most once every 15 minutes.
# State lives in /tmp (RAM), so a reboot starts fresh and the flash isn't worn.
# Reference answer for eval 7, used to validate checks/router.py.
set -eu

PATH=/usr/sbin:/usr/bin:/sbin:/bin
TARGET=1.1.1.1
LIMIT=3
COOLDOWN=900
STATE=/tmp/netwatch
LOCK=/tmp/netwatch.lock

# A run that overlaps the previous one (slow ping, slow reset) just skips.
mkdir "$LOCK" 2>/dev/null || exit 0
trap 'rmdir "$LOCK"' EXIT
trap 'exit 143' TERM
trap 'exit 130' INT

fails=0 last=0
if [ -r "$STATE" ]; then read -r fails last <"$STATE" || true; fi
case $fails in '' | *[!0-9]*) fails=0 ;; esac
case $last in '' | *[!0-9]*) last=0 ;; esac
now=$(date +%s)

if ping -c 1 -W 2 "$TARGET" >/dev/null 2>&1; then
  fails=0
else
  fails=$((fails + 1))
  logger -t netwatch "ping $TARGET failed ($fails in a row)"
  if [ "$fails" -ge "$LIMIT" ]; then
    if [ $((now - last)) -ge "$COOLDOWN" ]; then
      logger -t netwatch "resetting modem"
      /usr/sbin/modem-reset || logger -t netwatch "modem-reset failed"
      last=$now
    else
      logger -t netwatch "still down, last reset $(((now - last) / 60)) min ago; waiting"
    fi
  fi
fi

printf '%s %s\n' "$fails" "$last" >"$STATE.new" && mv "$STATE.new" "$STATE"
