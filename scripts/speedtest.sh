#!/usr/bin/env bash
# VPS Stats + Speedtest wrapper
set -u
echo "=== VPS Stats ==="
echo "Hostname: $(hostname)"; echo "Uptime: $(uptime -p 2>/dev/null || uptime)"
echo "CPU cores: $(nproc 2>/dev/null || echo '?')"
free -h 2>/dev/null || true; df -h / 2>/dev/null || true
echo; echo "=== Speedtest ==="
if command -v speedtest >/dev/null 2>&1; then speedtest --accept-license --accept-gdpr 2>&1 | tail -n 20;
elif command -v speedtest-cli >/dev/null 2>&1; then speedtest-cli --simple;
else echo "speedtest-cli is not installed. Install it: apt install -y speedtest-cli  or  pip install speedtest-cli"; echo "Fallback ping: $(ping -c 3 -W 2 1.1.1.1 2>&1 | tail -n 2)"; fi
