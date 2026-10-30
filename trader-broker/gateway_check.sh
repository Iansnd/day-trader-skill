#!/bin/sh
# Ten minutes before the autopilot fires: is IB Gateway up, logged in, paper? Speak + dialog if not.
cd "/Volumes/Extreme SSD/Day trader Setup" || exit 1
out=$(/opt/homebrew/bin/python3 day-trader-skill/trader-broker/ibkr_tws.py ping 2>&1 | grep -v "request timed out")
rc=$?
echo "$(date '+%F %T') rc=$rc $out"
case "$out" in *"connected paper"*) exit 0;; esac
msg=$(printf '%s' "$out" | tail -1 | tr -d '"\\')
osascript -e 'say "IB Gateway is down"' \
          -e "display dialog \"IB Gateway check failed. Autopilot cannot place today.\n\n$msg\" with title \"Day trader\" buttons {\"OK\"} with icon stop" >/dev/null 2>&1 &
exit 1
