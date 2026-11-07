#!/usr/bin/env bash
# Fix SSH-WS / SSL stack - services restart + config test
set -u
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
# shellcheck source=../lib/common.sh
source "$SCRIPT_DIR/lib/common.sh" 2>/dev/null || true
info "Stack fix shuru... (Nginx, HAProxy, Xray, Dropbear, ws-proxy)"
for cfg in "nginx -t" "haproxy -c -f /etc/haproxy/haproxy.cfg"; do
  echo "Check: $cfg"; eval "$cfg" 2>&1 | head -n 20 || warn "$cfg check fail / binary nahi hai"
done
for svc in nginx haproxy xray dropbear ws-proxy vps-panel; do
  if command -v systemctl >/dev/null 2>&1; then systemctl restart "$svc" 2>/dev/null && ok "$svc restarted" || warn "$svc restart skip/fail"; fi
done
print_ports
ok "Fix attempt complete. Agar port phir bhi band hai to logs dekho: journalctl -u <service> -n 50"
log_activity "fix-stack executed"
