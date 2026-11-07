#!/usr/bin/env bash
# VPS Manager Suite v1.0 - Uninstaller (original)
# A complete, clean uninstall: services, configs, accounts, data - remove everything.
# Every step is guarded with "|| true" so nothing stops halfway.
set -u

# Root prefix for testing (stays empty in real use)
ROOT="${TEST_ROOT:-}"

# --- Box helpers (same style as menu.sh, standalone copy) ---
BOX_EDGE="+--------------------------------------------+"
box_line(){ printf '| %-42s |\n' "$1"; }
box_center(){
  local _t="$1" _p=$(( (42 - ${#1}) / 2 ))
  printf '| %*s%s%*s |\n' "$_p" '' "$_t" $((42 - ${#1} - _p)) ''
}
step(){ echo "[..] $*"; }
ok(){ echo "[OK] $*"; }

# --- Confirmation (asked only once) ---
echo "$BOX_EDGE"
box_center "VPS MANAGER SUITE"
box_center "UNINSTALL"
echo "$BOX_EDGE"
box_line "All services will be stopped."
box_line "The panel, Xray config, all accounts"
box_line "and all data will be DELETED."
box_line "This cannot be undone."
echo "$BOX_EDGE"
read -rp "Are you sure you want to uninstall? (y/N): " ans
[[ "$ans" =~ ^[Yy]$ ]] || { echo "Cancelled - nothing was deleted."; exit 0; }

if [[ "${EUID:-$(id -u)}" -ne 0 && -z "$ROOT" ]]; then
  echo "[!] Run as root, otherwise some files will not be removed."
fi

# --- 1) Stop + disable services ---
step "Stopping services..."
if command -v systemctl >/dev/null 2>&1; then
  for svc in vps-panel ws-proxy xray haproxy nginx dropbear vps-monitor vps-bot; do
    systemctl disable --now "$svc" >/dev/null 2>&1 || true
    systemctl stop "$svc" >/dev/null 2>&1 || true
  done
fi
# QoS rules (HTB classes + iptables marks) belong to the suite too
if [[ -z "$ROOT" && -f "$(dirname "$0")/scripts/qos.sh" ]]; then
  bash "$(dirname "$0")/scripts/qos.sh" off >/dev/null 2>&1 || true
fi
ok "Services stopped."

# --- 2) Remove the systemd units ---
rm -f "$ROOT/etc/systemd/system/vps-panel.service" \
      "$ROOT/etc/systemd/system/ws-proxy.service" \
      "$ROOT/etc/systemd/system/vps-monitor.service" \
      "$ROOT/etc/systemd/system/vps-bot.service" 2>/dev/null || true
rm -rf "$ROOT/etc/systemd/system/xray.service.d" 2>/dev/null || true
rm -f "$ROOT/etc/letsencrypt/renewal-hooks/deploy/vps-manager.sh" 2>/dev/null || true
if command -v systemctl >/dev/null 2>&1; then
  systemctl daemon-reload >/dev/null 2>&1 || true
fi
ok "Service files removed."

# --- 3) Remove the cron entry ---
rm -f "$ROOT/etc/cron.d/vps-manager-limits" 2>/dev/null || true
ok "Limit monitor cron removed."

# --- 4) Remove the SSH users created by the panel ---
step "Removing panel-created SSH accounts..."
USERS_FILE="$ROOT/var/lib/vps-manager/ssh_users.json"
_users=""
if [[ -f "$USERS_FILE" ]]; then
  if command -v python3 >/dev/null 2>&1; then
    _users=$(python3 - "$USERS_FILE" <<'PY' 2>/dev/null
import json,sys
try:
    arr=json.load(open(sys.argv[1]))
    for r in arr:
        u=(r.get("username") or "").strip()
        if u: print(u)
except Exception:
    pass
PY
)
  fi
  if [[ -z "$_users" ]]; then
    _users=$(grep -o '"username"[[:space:]]*:[[:space:]]*"[^"]*"' "$USERS_FILE" 2>/dev/null | sed 's/.*"\([^"]*\)"$/\1/' || true)
  fi
fi
for _u in $_users; do
  case "$_u" in
    root|admin|ubuntu|daemon|nobody) continue ;;
  esac
  userdel "$_u" >/dev/null 2>&1 || true
done
ok "SSH accounts removed."

# --- 5) Remove binaries / wrappers ---
rm -f "$ROOT/usr/local/bin/vps-panel.py" \
      "$ROOT/usr/local/bin/vps-ws-proxy.py" \
      "$ROOT/usr/local/bin/vpsmenu" \
      "$ROOT/usr/local/bin/menu" \
      "$ROOT/usr/local/bin/vpsupdate" 2>/dev/null || true
ok "Commands (vpsmenu/vpsupdate) removed."

# --- 6) Remove our Xray config (keep the binary) ---
rm -f "$ROOT/usr/local/etc/xray/config.json" 2>/dev/null || true
ok "Xray config removed."

# --- 7) Nginx / HAProxy: if the config is ours, restore the default ---
NGINX_CONF="$ROOT/etc/nginx/nginx.conf"
if [[ -f "$NGINX_CONF" ]] && grep -Eq "18080|vps-manager" "$NGINX_CONF" 2>/dev/null; then
  cat > "$NGINX_CONF" <<'EOF' 2>/dev/null || true
user www-data;
worker_processes auto;
pid /run/nginx.pid;
error_log /var/log/nginx/error.log;
include /etc/nginx/modules-enabled/*.conf;

events {
	worker_connections 768;
}

http {
	sendfile on;
	tcp_nopush on;
	types_hash_max_size 2048;
	include /etc/nginx/mime.types;
	default_type application/octet-stream;
	ssl_protocols TLSv1 TLSv1.1 TLSv1.2 TLSv1.3;
	ssl_prefer_server_ciphers on;
	access_log /var/log/nginx/access.log;
	error_log /var/log/nginx/error.log;
	gzip on;
	include /etc/nginx/conf.d/*.conf;
	include /etc/nginx/sites-enabled/*;
}
EOF
  ok "Nginx default config restored."
fi

HAPROXY_CFG="$ROOT/etc/haproxy/haproxy.cfg"
if [[ -f "$HAPROXY_CFG" ]] && grep -Eq "18080|8080|vps-manager" "$HAPROXY_CFG" 2>/dev/null; then
  cat > "$HAPROXY_CFG" <<'EOF' 2>/dev/null || true
global
	log /dev/log	local0
	log /dev/log	local1 notice
	chroot /var/lib/haproxy
	stats socket /run/haproxy/admin.sock mode 660 level admin
	stats timeout 30s
	user haproxy
	group haproxy
	daemon

defaults
	log	global
	mode	http
	option	httplog
	option	dontlognull
	timeout connect 5000
	timeout client  50000
	timeout server  50000
	errorfile 400 /etc/haproxy/errors/400.http
	errorfile 403 /etc/haproxy/errors/403.http
	errorfile 408 /etc/haproxy/errors/408.http
	errorfile 500 /etc/haproxy/errors/500.http
	errorfile 502 /etc/haproxy/errors/502.http
	errorfile 503 /etc/haproxy/errors/503.http
	errorfile 504 /etc/haproxy/errors/504.http
EOF
  ok "HAProxy default config restored."
fi

# --- 8) Remove the config / data / suite folders ---
rm -rf "$ROOT/etc/vps-manager" \
       "$ROOT/var/lib/vps-manager" \
       "$ROOT/opt/vps-manager-suite" \
       "$ROOT/var/backups/vps-manager-suite" 2>/dev/null || true
ok "Data and suite folders removed."

# --- 9) Dropbear: if it carries our banner, restore the defaults ---
DROPBEAR_CONF="$ROOT/etc/default/dropbear"
if [[ -f "$DROPBEAR_CONF" ]] && grep -q "vps-manager" "$DROPBEAR_CONF" 2>/dev/null; then
  cat > "$DROPBEAR_CONF" <<'EOF' 2>/dev/null || true
NO_START=0
DROPBEAR_PORT=22
DROPBEAR_EXTRA_ARGS=""
DROPBEAR_BANNER=""
DROPBEAR_RECEIVE_WINDOW=65536
EOF
  ok "Dropbear defaults restored (port 22)."
fi

# --- Done ---
echo "$BOX_EDGE"
box_center "UNINSTALL COMPLETE"
echo "$BOX_EDGE"
box_line "Services, panel and configs removed."
box_line "Accounts and data were deleted too."
box_line "The VPS is clean and ready for"
box_line "a fresh install."
echo "$BOX_EDGE"
