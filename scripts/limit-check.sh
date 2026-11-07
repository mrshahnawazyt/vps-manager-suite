#!/usr/bin/env bash
# VPS Manager Suite - one limit/enforcement pass (original code)
# Kept for manual runs and older cron entries: it runs a single pass of
# the monitor (scripts/monitor.py). The vps-monitor service runs the
# same pass continuously every few seconds.
set -u
HERE="$(cd "$(dirname "$0")" && pwd)"
MON="$HERE/monitor.py"
if [[ -f "$MON" ]]; then
  exec python3 "$MON" --once
fi
echo "[limit-check] ERROR: monitor.py not found at $MON" >&2
exit 1
