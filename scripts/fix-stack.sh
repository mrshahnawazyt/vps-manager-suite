#!/usr/bin/env bash
# Fix SSH-WS / SSL stack - services restart + config test
set -u
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
# shellcheck source=../lib/common.sh
source "$SCRIPT_DIR/lib/common.sh" 2>/dev/null || true
# Keep going even if common.sh is missing (no "command not found" errors)
if ! declare -F info >/dev/null 2>&1; then
  info(){ echo "[INFO] $*"; }; warn(){ echo "[!] $*"; }; ok(){ echo "[OK] $*"; }
  print_ports(){ ss -tulnp 2>/dev/null || echo "could not list ports"; }; log_activity(){ :; }
fi
info "Starting the stack fix... (Nginx, HAProxy, Xray, Dropbear, ws-proxy)"
for cfg in "nginx -t" "haproxy -c -f /etc/haproxy/haproxy.cfg"; do
  echo "Check: $cfg"; eval "$cfg" 2>&1 | head -n 20 || warn "$cfg check failed / binary not found"
done
if command -v xray >/dev/null 2>&1 && [[ -f /usr/local/etc/xray/config.json ]]; then
  echo "Check: xray run -test"; xray run -test -c /usr/local/etc/xray/config.json 2>&1 | tail -n 3 || warn "xray config test failed - common cause: missing SSL certificate files (issue SSL from menu option 03)"
fi
for svc in nginx haproxy xray dropbear ws-proxy vps-panel; do
  if command -v systemctl >/dev/null 2>&1; then systemctl restart "$svc" 2>/dev/null && ok "$svc restarted" || warn "$svc restart skipped/failed"; fi
done
print_ports
ok "Fix attempt complete. If a port is still closed, check the logs: journalctl -u <service> -n 50"
log_activity "fix-stack executed"
