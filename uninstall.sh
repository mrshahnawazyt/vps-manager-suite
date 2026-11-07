#!/usr/bin/env bash
# VPS Manager Suite v1.0 - Uninstaller (original)
# Poora saaf uninstall: services, configs, accounts, data — sab hata do.
# Har step "|| true" ke saath hai taaki beech mein rukawat na aaye.
set -u

# Test ke liye root prefix (asli istemal mein khaali rehta hai)
ROOT="${TEST_ROOT:-}"

# --- Box helpers (menu.sh wala style, standalone copy) ---
BOX_EDGE="+--------------------------------------------+"
box_line(){ printf '| %-42s |\n' "$1"; }
box_center(){
  local _t="$1" _p=$(( (42 - ${#1}) / 2 ))
  printf '| %*s%s%*s |\n' "$_p" '' "$_t" $((42 - ${#1} - _p)) ''
}
step(){ echo "[..] $*"; }
ok(){ echo "[OK] $*"; }

# --- Confirmation (ek hi dafa) ---
echo "$BOX_EDGE"
box_center "VPS MANAGER SUITE"
box_center "UNINSTALL"
echo "$BOX_EDGE"
box_line "Saari services band ho jayengi."
box_line "Panel, Xray config, saare accounts"
box_line "aur poora data DELETE ho jayega."
box_line "Ye wapas nahi aayega."
echo "$BOX_EDGE"
read -rp "Pakka uninstall karna hai? (y/N): " ans
[[ "$ans" =~ ^[Yy]$ ]] || { echo "Cancel - kuch delete nahi hua."; exit 0; }

if [[ "${EUID:-$(id -u)}" -ne 0 && -z "$ROOT" ]]; then
  echo "[!] Root se chalao, warna kuch files nahi hatengi."
fi

# --- 1) Services stop + disable ---
step "Services band ho rahi hain..."
if command -v systemctl >/dev/null 2>&1; then
  for svc in vps-panel ws-proxy xray haproxy nginx dropbear; do
    systemctl disable --now "$svc" >/dev/null 2>&1 || true
    systemctl stop "$svc" >/dev/null 2>&1 || true
  done
fi
ok "Services band."

# --- 2) Systemd units hatao ---
rm -f "$ROOT/etc/systemd/system/vps-panel.service" \
      "$ROOT/etc/systemd/system/ws-proxy.service" 2>/dev/null || true
if command -v systemctl >/dev/null 2>&1; then
  systemctl daemon-reload >/dev/null 2>&1 || true
fi
ok "Panel services ki files hata dein."

# --- 3) Cron hatao ---
rm -f "$ROOT/etc/cron.d/vps-manager-limits" 2>/dev/null || true
ok "Limit monitor cron hata diya."

# --- 4) Panel ke banaye SSH users hatao ---
step "Panel wale SSH accounts hata raha hoon..."
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
ok "SSH accounts hata diye."

# --- 5) Binaries / wrappers hatao ---
rm -f "$ROOT/usr/local/bin/vps-panel.py" \
      "$ROOT/usr/local/bin/vps-ws-proxy.py" \
      "$ROOT/usr/local/bin/vpsmenu" \
      "$ROOT/usr/local/bin/menu" \
      "$ROOT/usr/local/bin/vpsupdate" 2>/dev/null || true
ok "Commands (vpsmenu/vpsupdate) hata diye."

# --- 6) Xray ka hamara config hatao (binary rehne do) ---
rm -f "$ROOT/usr/local/etc/xray/config.json" 2>/dev/null || true
ok "Xray config hata diya."

# --- 7) Nginx / HAProxy: hamara ho to default wapas likho ---
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
  ok "Nginx default config wapas laga diya."
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
  ok "HAProxy default config wapas laga diya."
fi

# --- 8) Config / data / suite folders hatao ---
rm -rf "$ROOT/etc/vps-manager" \
       "$ROOT/var/lib/vps-manager" \
       "$ROOT/opt/vps-manager-suite" 2>/dev/null || true
ok "Data aur suite folders hata diye."

# --- 9) Dropbear: hamara banner ho to defaults wapas ---
DROPBEAR_CONF="$ROOT/etc/default/dropbear"
if [[ -f "$DROPBEAR_CONF" ]] && grep -q "vps-manager" "$DROPBEAR_CONF" 2>/dev/null; then
  cat > "$DROPBEAR_CONF" <<'EOF' 2>/dev/null || true
NO_START=0
DROPBEAR_PORT=22
DROPBEAR_EXTRA_ARGS=""
DROPBEAR_BANNER=""
DROPBEAR_RECEIVE_WINDOW=65536
EOF
  ok "Dropbear default (port 22) wapas laga diya."
fi

# --- Done ---
echo "$BOX_EDGE"
box_center "UNINSTALL COMPLETE"
echo "$BOX_EDGE"
box_line "Services, panel, configs hata diye."
box_line "Accounts aur data bhi delete ho gaya."
box_line "VPS ab saaf hai - dobara install"
box_line "ke liye ready hai."
echo "$BOX_EDGE"
