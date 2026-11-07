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

# --- HAProxy TLS bundle -------------------------------------------------------
# HAProxy terminates TLS on 443 and loads its certificate as ONE bundle
# file (cert + key): $CONFIG_DIR/ssl/haproxy.pem. If the bundle is missing
# or older than the cert pair (e.g. an install from before the bundle
# existed, or a cert replaced by hand), HAProxy refuses to start or keeps
# serving the old certificate - rebuild it from the pair. Mirrors
# build_haproxy_pem() in lib/common.sh, kept standalone so this script
# still works when common.sh cannot be sourced.
SSL_DIR_FIX="${CONFIG_DIR:-/etc/vps-manager}/ssl"
ensure_haproxy_pem(){
  local bundle="$SSL_DIR_FIX/haproxy.pem"
  if [[ -s "$SSL_DIR_FIX/fullchain.pem" && -s "$SSL_DIR_FIX/privkey.pem" ]]; then
    if [[ ! -s "$bundle" || "$SSL_DIR_FIX/fullchain.pem" -nt "$bundle" || "$SSL_DIR_FIX/privkey.pem" -nt "$bundle" ]]; then
      if cat "$SSL_DIR_FIX/fullchain.pem" "$SSL_DIR_FIX/privkey.pem" > "$bundle" 2>/dev/null; then
        chmod 600 "$bundle" 2>/dev/null || true
        ok "HAProxy TLS bundle rebuilt ($bundle)"
        return 0
      fi
      warn "Could not write the HAProxy TLS bundle: $bundle"
      return 1
    fi
    ok "HAProxy TLS bundle is present and current"
    return 0
  fi
  warn "No SSL certificate pair in $SSL_DIR_FIX - HAProxy cannot terminate TLS on 443 until SSL is issued (menu option 03)."
  return 1
}

info "Starting the stack fix... (Nginx, HAProxy, Xray, Dropbear, ws-proxy)"
ensure_xray_certs || true
ensure_haproxy_pem || true
# SSH logins: Dropbear rejects accounts whose shell is not in /etc/shells.
# Guarantee the tunnel shell is listed and repair any stored account whose
# system user still carries the old nologin shell.
if declare -F ensure_tunnel_shell >/dev/null 2>&1; then
  ensure_tunnel_shell || warn "Could not add the tunnel shell to /etc/shells - SSH password logins will be rejected."
  repair_tunnel_shells || true
fi
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
# --- Component repair (beyond restarts) --------------------------------------
# 1) Missing base packages are reinstalled (xray has its own installer -
#    a missing xray binary is reported, not apt-installed)
for pair in "nginx:nginx" "haproxy:haproxy" "dropbear:dropbear" "certbot:certbot"; do
  _bin="${pair%%:*}"; _pkg="${pair##*:}"
  if ! command -v "$_bin" >/dev/null 2>&1 && command -v apt-get >/dev/null 2>&1; then
    warn "Package missing: $_pkg - installing..."
    apt-get install -y "$_pkg" >/dev/null 2>&1 && ok "$_pkg installed" || warn "$_pkg install failed - check the network/apt sources"
  fi
done
command -v xray >/dev/null 2>&1 || warn "The Xray binary is missing - run menu option 01 (Install All) to install it."
# 2) Router configs are re-rendered from the templates when they drifted
DOM_NOW=""
if declare -F get_domain >/dev/null 2>&1; then DOM_NOW="$(get_domain 2>/dev/null || true)"; fi
[[ -z "$DOM_NOW" && -f "${DATA_DIR:-/var/lib/vps-manager}/config.json" ]] && DOM_NOW=$(python3 -c "import json;print(json.load(open('${DATA_DIR:-/var/lib/vps-manager}/config.json')).get('domain',''))" 2>/dev/null || echo "")
if [[ -n "$DOM_NOW" ]]; then
  WS_PORT=$(python3 -c "import json;print(json.load(open('${DATA_DIR:-/var/lib/vps-manager}/config.json')).get('ws_proxy_port',8080))" 2>/dev/null || echo 8080)
  [[ "$WS_PORT" =~ ^[0-9]+$ ]] || WS_PORT=8080
  if [[ -f "$SCRIPT_DIR/templates/nginx.conf.tpl" ]]; then
    _want=$(sed "s/__DOMAIN__/$DOM_NOW/g; s/__PANEL_PORT__/9090/g; s/__WSPROXY_PORT__/$WS_PORT/g" "$SCRIPT_DIR/templates/nginx.conf.tpl")
    if [[ "$_want" != "$(cat /etc/nginx/nginx.conf 2>/dev/null)" ]]; then
      echo "$_want" > /etc/nginx/nginx.conf 2>/dev/null && ok "nginx router re-rendered from the template" || warn "Could not rewrite the nginx config"
    fi
  fi
  if [[ -f "$SCRIPT_DIR/templates/haproxy.cfg.tpl" && -d /etc/haproxy ]]; then
    _want=$(sed "s/__DOMAIN__/$DOM_NOW/g; s/__WSPROXY_PORT__/$WS_PORT/g" "$SCRIPT_DIR/templates/haproxy.cfg.tpl")
    if [[ "$_want" != "$(cat /etc/haproxy/haproxy.cfg 2>/dev/null)" ]]; then
      echo "$_want" > /etc/haproxy/haproxy.cfg 2>/dev/null && ok "haproxy config re-rendered from the template" || warn "Could not rewrite the haproxy config"
    fi
  fi
fi
# 2b) Dropbear listener config restored when it drifted - a package
#     reinstall can reset /etc/default/dropbear to a single port, which
#     would silently drop the extra SSH ports (143, 447). The restart
#     loop below picks the restored config up.
_want_db=$(cat <<'DBEOF'
NO_START=0
DROPBEAR_PORT=109
DROPBEAR_EXTRA_ARGS="-p 143 -p 447"
DROPBEAR_BANNER="/etc/vps-manager/banner.txt"
DROPBEAR_RECEIVE_WINDOW=65536
DBEOF
)
if [[ "$(cat /etc/default/dropbear 2>/dev/null)" != "$_want_db" ]]; then
  echo "$_want_db" > /etc/default/dropbear 2>/dev/null && ok "dropbear config restored (ports 109, 143, 447)" || warn "Could not rewrite the dropbear config"
fi
# 3) Xray deep repair (backups + rebuild preserving accounts + verify)
if [[ -f "$SCRIPT_DIR/scripts/fix-xray-sync.py" ]]; then
  info "Running the Xray repair (fix-xray-sync)..."
  python3 "$SCRIPT_DIR/scripts/fix-xray-sync.py" --no-restart 2>&1 | tail -n 8 || true
fi
# 4) Installed service files + units are refreshed from this suite
install -m 755 "$SCRIPT_DIR/web/panel.py" /usr/local/bin/vps-panel.py 2>/dev/null || true
install -m 755 "$SCRIPT_DIR/scripts/ws-proxy.py" /usr/local/bin/vps-ws-proxy.py 2>/dev/null || true
if [[ ! -f /etc/systemd/system/vps-monitor.service ]]; then
  cat > /etc/systemd/system/vps-monitor.service <<EOF
[Unit]
Description=VPS Manager Suite - Limit Monitor
After=network.target
[Service]
Type=simple
WorkingDirectory=$SCRIPT_DIR
Environment=VPSMGR_DATA_DIR=${DATA_DIR:-/var/lib/vps-manager}
ExecStart=/usr/bin/python3 $SCRIPT_DIR/scripts/monitor.py
Restart=always
RestartSec=3
[Install]
WantedBy=multi-user.target
EOF
  ok "vps-monitor.service created (was missing)"
fi
if [[ ! -f /etc/systemd/system/vps-bot.service ]]; then
  cat > /etc/systemd/system/vps-bot.service <<EOF
[Unit]
Description=VPS Manager Suite - Telegram Bot
After=network.target
[Service]
Type=simple
WorkingDirectory=$SCRIPT_DIR
ExecStart=/usr/bin/python3 $SCRIPT_DIR/scripts/telegram-bot.py
Restart=on-failure
RestartSec=5
[Install]
WantedBy=multi-user.target
EOF
  ok "vps-bot.service created (was missing)"
fi
systemctl daemon-reload 2>/dev/null || true
for svc in nginx haproxy xray dropbear ws-proxy vps-panel vps-monitor; do
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
for svc in nginx haproxy xray dropbear ws-proxy vps-panel vps-monitor vps-bot; do
  box_line "$svc : $(svc_state "$svc")"
done
_xray_state="$(svc_state xray)"
# Self-heal retry: if Xray is still down, rebuild + restart it once more
if [[ "$_xray_state" == "failed" || "$_xray_state" == "inactive" ]] && command -v systemctl >/dev/null 2>&1; then
  warn "Xray is still down - one self-heal retry (cert check + config rebuild + restart)..."
  ensure_xray_certs || true
  [[ -f "$SCRIPT_DIR/scripts/sync-xray.py" ]] && python3 "$SCRIPT_DIR/scripts/sync-xray.py" >/dev/null 2>&1 || true
  systemctl restart xray 2>/dev/null || true
  sleep 1
  _xray_state="$(svc_state xray)"
  echo "$BOX_EDGE"
  box_line "xray after retry : $_xray_state"
  echo "$BOX_EDGE"
fi
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
