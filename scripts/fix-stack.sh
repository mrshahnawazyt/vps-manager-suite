#!/usr/bin/env bash
# Fix SSH-WS / SSL stack - services restart + config test
set -u
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
# shellcheck source=../lib/common.sh
source "$SCRIPT_DIR/lib/common.sh" 2>/dev/null || true
# Keep going even if common.sh is missing (no "command not found" errors)
if ! declare -F info >/dev/null 2>&1; then
  info(){ echo "[INFO] $*"; }; warn(){ echo "[!] $*"; }; ok(){ echo "[OK] $*"; }; err(){ echo "[ERROR] $*" >&2; }
  print_ports(){ ss -tulnp 2>/dev/null || echo "could not list ports"; }; log_activity(){ :; }
fi

# --- 46-char box helpers (same style as menu.sh / uninstall.sh) -------------
BOX_EDGE="+--------------------------------------------+"
box_line(){ printf '| %-42s |\n' "$1"; }
box_title(){ local _t="$1"; local _p=$(( (42 - ${#_t}) / 2 )); printf '| %*s%s%*s |\n' "$_p" '' "$_t" $((42 - ${#_t} - _p)) ''; }

# --- Xray TLS certificates ---------------------------------------------------
# The generated Xray config loads its TLS certificate from these exact paths
# (see templates/xray.json.tpl). If they are missing, Xray exits at once.
XRAY_CERT="${CONFIG_DIR:-/etc/vps-manager}/ssl/fullchain.pem"
XRAY_KEY="${CONFIG_DIR:-/etc/vps-manager}/ssl/privkey.pem"
ensure_xray_certs(){
  if [[ -s "$XRAY_CERT" && -s "$XRAY_KEY" ]]; then
    ok "Xray TLS certificate files are present"
    return 0
  fi
  if [[ -s "$XRAY_CERT" || -s "$XRAY_KEY" ]]; then
    # Only one half exists - replacing one half would break the pair,
    # and a real certificate must never be overwritten.
    warn "The SSL certificate pair is incomplete - one of these files is missing:"
    warn "  $XRAY_CERT"
    warn "  $XRAY_KEY"
    warn "Xray cannot start with an incomplete pair. Re-issue SSL from menu option 03."
    return 1
  fi
  local dom="localhost" _d=""
  if declare -F get_domain >/dev/null 2>&1; then
    _d="$(get_domain 2>/dev/null || true)"; [[ -n "$_d" ]] && dom="$_d"
  fi
  warn "Xray TLS certificate files are missing - creating a self-signed pair for $dom"
  mkdir -p "$(dirname "$XRAY_CERT")" 2>/dev/null || true
  if command -v openssl >/dev/null 2>&1 && openssl req -x509 -newkey rsa:2048 \
      -keyout "$XRAY_KEY" -out "$XRAY_CERT" -days 365 -nodes \
      -subj "/CN=$dom" >/dev/null 2>&1 && [[ -s "$XRAY_CERT" && -s "$XRAY_KEY" ]]; then
    chmod 600 "$XRAY_KEY" 2>/dev/null || true
    ok "Self-signed certificate created (replace it any time from menu option 03)"
    return 0
  fi
  warn "Could not create a self-signed certificate (openssl missing?) - Xray TLS inbounds will not start until SSL is issued from menu option 03."
  return 1
}

info "Starting the stack fix... (Nginx, HAProxy, Xray, Dropbear, ws-proxy)"
ensure_xray_certs || true
mkdir -p /var/log/xray 2>/dev/null || true
# The stock Xray unit runs as User=nobody, which cannot read the TLS
# private key (mode 600) or write the access log - install the drop-in
# that runs the service as root BEFORE restarting it below.
if declare -F ensure_xray_service >/dev/null 2>&1; then
  ensure_xray_service || warn "The Xray service drop-in could not be installed - Xray may still fail to start."
fi
for cfg in "nginx -t" "haproxy -c -f /etc/haproxy/haproxy.cfg"; do
  echo "Check: $cfg"; eval "$cfg" 2>&1 | head -n 20 || warn "$cfg check failed / binary not found"
done
XRAY_CFG="/usr/local/etc/xray/config.json"
if command -v xray >/dev/null 2>&1 && [[ -f "$XRAY_CFG" ]]; then
  echo "Check: xray run -test"
  if xray run -test -config "$XRAY_CFG" >/dev/null 2>&1; then
    ok "Xray config test OK"
  else
    warn "Xray config test failed - rebuilding the config with sync-xray (bad accounts fall back to the seed config)"
    if [[ -f "$SCRIPT_DIR/scripts/sync-xray.py" ]]; then
      python3 "$SCRIPT_DIR/scripts/sync-xray.py" 2>&1 | tail -n 6 || true
    fi
    if xray run -test -config "$XRAY_CFG" >/dev/null 2>&1; then
      ok "Xray config test OK after rebuild"
    else
      warn "Xray config test is still failing - the exact error is:"
      xray run -test -config "$XRAY_CFG" 2>&1 | tail -n 5 || true
    fi
  fi
fi
for svc in nginx haproxy xray dropbear ws-proxy vps-panel; do
  if command -v systemctl >/dev/null 2>&1; then systemctl restart "$svc" 2>/dev/null && ok "$svc restarted" || warn "$svc restart skipped/failed"; fi
done
print_ports

# --- Final service states (46-char box) --------------------------------------
svc_state(){
  if command -v systemctl >/dev/null 2>&1; then
    local s; s="$(systemctl is-active "$1" 2>/dev/null || true)"; [[ -n "$s" ]] && { echo "$s"; return; }
  fi
  echo "unknown"
}
echo "$BOX_EDGE"
box_title "SERVICE STATUS"
echo "$BOX_EDGE"
for svc in nginx haproxy xray dropbear ws-proxy vps-panel; do
  box_line "$svc : $(svc_state "$svc")"
done
_xray_state="$(svc_state xray)"
if [[ "$_xray_state" == "failed" || "$_xray_state" == "inactive" ]] && command -v journalctl >/dev/null 2>&1; then
  box_line "Xray is not running - last log lines:"
  journalctl -u xray -n 5 --no-pager -o cat 2>/dev/null | while IFS= read -r _l; do
    [[ -n "$_l" ]] && box_line "${_l:0:42}"
  done
fi
echo "$BOX_EDGE"
# Journal lines inside the box are truncated to fit - when Xray is down,
# repeat the failure with the full log lines outside the box as well.
if [[ "$_xray_state" == "failed" || "$_xray_state" == "inactive" ]] && declare -F verify_xray_running >/dev/null 2>&1; then
  verify_xray_running || true
fi
ok "Fix attempt complete. If a service is still down, its log has the exact reason: journalctl -u <service> -n 50"
log_activity "fix-stack executed"
