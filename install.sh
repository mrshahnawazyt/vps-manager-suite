#!/usr/bin/env bash
# VPS Manager Suite v1.0 - Installer (ORIGINAL code, Ubuntu 22.04)
# Features: Dropbear, ws-proxy, Nginx internal router, HAProxy 80/443 mux,
#           Xray (VMess/VLESS/Trojan, WS/TLS/TCP/XHTTP), CAKE QoS, SSL, Web Panel 9090
# Limit rule (corrected, video-verified): Days = literal (default 30, Expiry = creation + Days). Sirf GB limit aur IP limit par 0 = Unlimited.
set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# --- Single-file bootstrap (video wali style) ---
# Agar sirf install.sh download hua hai (lib/ folder paas nahi hai), to baaki
# saari files GitHub repo se download karke wahi installer dobara chalao.
# Usage: export GH_REPO=username/repo-name [GH_TOKEN=xxx private repo ke liye]
#        curl -fsSL https://raw.githubusercontent.com/username/repo-name/main/install.sh -o install.sh && bash install.sh
if [[ ! -f "$SCRIPT_DIR/lib/common.sh" ]]; then
  if [[ -z "${GH_REPO:-}" ]]; then
    echo "Sirf install.sh mila hai. GH_REPO set karo, e.g.:"
    echo "  export GH_REPO=username/repo-name"
    echo "ya poora project folder VPS par upload karke wahan se chalao."
    exit 1
  fi
  echo "Baaki files GitHub repo ($GH_REPO) se download ho rahi hain..."
  _tmp="$(mktemp -d)"; mkdir -p "$_tmp/src"
  _auth=(); [[ -n "${GH_TOKEN:-}" ]] && _auth=(-H "Authorization: token $GH_TOKEN")
  curl -fsSL "${_auth[@]}" "https://api.github.com/repos/$GH_REPO/tarball/${GH_BRANCH:-main}" -o "$_tmp/repo.tgz"
  tar -xzf "$_tmp/repo.tgz" --strip-components=1 -C "$_tmp/src"
  exec bash "$_tmp/src/install.sh" "$@"
fi
source "$SCRIPT_DIR/lib/common.sh"

require_root; check_os; ensure_dirs
banner "VPS Manager Suite v1.0 — Install All Components"

# 1) Domain poochho
DOMAIN="${1:-}"
if [[ -z "$DOMAIN" ]]; then read -rp "Apna domain likho (e.g. vpn.example.com): " DOMAIN; fi
[[ -z "$DOMAIN" ]] && { err "Domain zaroori hai"; exit 1; }
save_config_key domain "$DOMAIN"
info "Domain: $DOMAIN"; dns_check "$DOMAIN" || warn "DNS abhi sahi nahi — SSL step fail ho sakta hai, baad me menu option 03 se dobara try karna."

# 2) Packages
info "Packages install ho rahe hain..."
export DEBIAN_FRONTEND=noninteractive
apt-get update -y
apt-get install -y curl wget unzip jq nginx haproxy dropbear stunnel4 certbot python3 python3-pip iproute2 iptables ufw speedtest-cli uuid-runtime cron || apt-get install -y curl wget unzip jq nginx haproxy dropbear certbot python3 iproute2 cron

# 3) Panel credentials banao (agar pehle nahi hain)
PANEL_USER="admin"; PANEL_PASS=""
if [[ -f "$CREDS_FILE" ]]; then
  PANEL_PASS=$(python3 -c "import json;print(json.load(open('$CREDS_FILE')).get('password',''))" 2>/dev/null || echo "")
fi
if [[ -z "$PANEL_PASS" ]]; then
  PANEL_PASS=$(gen_password)
  python3 - "$CREDS_FILE" "$PANEL_PASS" <<'PY'
import json,sys
json.dump({"username":"admin","password":sys.argv[2]}, open(sys.argv[1],"w"), indent=2)
PY
  chmod 600 "$CREDS_FILE" || true
fi
# Panel data dir me config sync
mkdir -p "$DATA_DIR"
python3 - "$DATA_DIR/config.json" "$DOMAIN" <<'PY'
import json,sys,os
p=sys.argv[1]; d={}
if os.path.exists(p):
    try: d=json.load(open(p))
    except: d={}
d.update({"domain":sys.argv[2],"banner":d.get("banner","Welcome to VPS Manager Suite"),"qos_interface":d.get("qos_interface","eth0"),"qos_rate":d.get("qos_rate","950mbit"),"per_client_cap":0})
json.dump(d,open(p,"w"),indent=2)
for f in ("ssh_users.json","xray_users.json","activity.json"):
    fp=os.path.join(os.path.dirname(p),f)
    if not os.path.exists(fp): json.dump([],open(fp,"w"))
PY
cp "$CREDS_FILE" "$DATA_DIR/credentials.json" 2>/dev/null || true

# 4) Dropbear (SSH tunnel) - ports 109 / 143, banner
info "Dropbear configure ho raha hai..."
cat > /etc/default/dropbear <<EOF
NO_START=0
DROPBEAR_PORT=109
DROPBEAR_EXTRA_ARGS="-p 143 -w -s -g"
DROPBEAR_BANNER="/etc/vps-manager/banner.txt"
DROPBEAR_RECEIVE_WINDOW=65536
EOF
echo "Welcome to VPS Manager Suite" > "$CONFIG_DIR/banner.txt" 2>/dev/null || true
# Tunnel-only note: naye SSH users menu/panel se bante hain with shell /usr/sbin/nologin

# 5) ws-proxy (Python placeholder service)
info "ws-proxy service lag raha hai (port 8080 -> SSH 109)..."
install -m 755 "$SCRIPT_DIR/scripts/ws-proxy.py" /usr/local/bin/vps-ws-proxy.py
cat > /etc/systemd/system/ws-proxy.service <<'EOF'
[Unit]
Description=VPS Manager WS Proxy
After=network.target dropbear.service
[Service]
ExecStart=/usr/bin/python3 /usr/local/bin/vps-ws-proxy.py
Restart=always
Environment=WS_PROXY_PORT=8080
Environment=SSH_PORT_INTERNAL=109
[Install]
WantedBy=multi-user.target
EOF

# 6) Nginx internal router + HAProxy mux
info "Nginx + HAProxy templates lag rahe hain..."
sed "s/__DOMAIN__/$DOMAIN/g; s/__PANEL_PORT__/9090/g" "$SCRIPT_DIR/templates/nginx.conf.tpl" > /etc/nginx/nginx.conf
sed "s/__DOMAIN__/$DOMAIN/g" "$SCRIPT_DIR/templates/haproxy.cfg.tpl" > /etc/haproxy/haproxy.cfg
nginx -t || warn "nginx config test warning"
haproxy -c -f /etc/haproxy/haproxy.cfg || warn "haproxy config test warning"

# 7) SSL - Let's Encrypt, fallback self-signed
info "SSL certificate ki koshish (Let's Encrypt)..."
mkdir -p "$CONFIG_DIR/ssl"
if certbot certonly --standalone -d "$DOMAIN" --non-interactive --agree-tos -m "admin@$DOMAIN" --http-01-port 8899 2>/dev/null; then
  cp "/etc/letsencrypt/live/$DOMAIN/fullchain.pem" "$CONFIG_DIR/ssl/fullchain.pem" || true
  cp "/etc/letsencrypt/live/$DOMAIN/privkey.pem" "$CONFIG_DIR/ssl/privkey.pem" || true
  ok "Let's Encrypt SSL mil gaya."
else
  warn "Let's Encrypt fail (DNS/port?) — self-signed fallback bana raha hoon. Menu 03 se baad me renew kar lena."
  openssl req -x509 -newkey rsa:2048 -keyout "$CONFIG_DIR/ssl/privkey.pem" -out "$CONFIG_DIR/ssl/fullchain.pem" -days 365 -nodes -subj "/CN=$DOMAIN" 2>/dev/null || true
fi
# HAProxy/Xray ke liye combined pem bhi rakh lo
cat "$CONFIG_DIR/ssl/fullchain.pem" "$CONFIG_DIR/ssl/privkey.pem" > "$CONFIG_DIR/ssl/combined.pem" 2>/dev/null || true

# 8) Xray (official script) + config
info "Xray install ho raha hai (official script)..."
if ! command -v xray >/dev/null 2>&1; then
  bash -c "$(curl -L https://github.com/XTLS/Xray-install/raw/main/install-release.sh)" @ install || warn "Xray official install fail — internet/GitHub check karo, phir menu 01 dobara chalao."
fi
mkdir -p /usr/local/etc/xray
sed "s/__DOMAIN__/$DOMAIN/g" "$SCRIPT_DIR/templates/xray.json.tpl" > /usr/local/etc/xray/config.json
# Xray paths: VMess /vmess (10001), VLESS /vless (10002), Trojan /trojan (10003), XHTTP /xhttp (10004), TLS fallback 10443

# 9) CAKE QoS
info "CAKE QoS (Ping Stability)..."
bash "$SCRIPT_DIR/scripts/qos.sh" || warn "QoS abhi nahi laga — menu option 07 se interface/rate set karke dobara try karo."

# 10) Web Panel service (9090)
info "Web Panel service (port 9090)..."
install -m 755 "$SCRIPT_DIR/web/panel.py" /usr/local/bin/vps-panel.py
cat > /etc/systemd/system/vps-panel.service <<EOF
[Unit]
Description=VPS Manager Web Panel
After=network.target
[Service]
ExecStart=/usr/bin/python3 /usr/local/bin/vps-panel.py
Restart=always
Environment=VPSMGR_PORT=9090
Environment=VPSMGR_DATA_DIR=$DATA_DIR
[Install]
WantedBy=multi-user.target
EOF

# 11) Limit monitor cron (GB/IP = 0 wale checks skip honge; Days literal expiry check hoga)
cat > /etc/cron.d/vps-manager-limits <<EOF
*/10 * * * * root bash $SCRIPT_DIR/scripts/limit-check.sh >> /var/log/vps-manager-limits.log 2>&1
EOF
install -m 755 "$SCRIPT_DIR/menu.sh" /usr/local/bin/vpsmenu 2>/dev/null || cp "$SCRIPT_DIR/menu.sh" /usr/local/bin/menu || true

# 12) Services enable + health check
systemctl daemon-reload || true
for svc in nginx haproxy dropbear ws-proxy vps-panel xray; do systemctl enable --now "$svc" 2>/dev/null && ok "$svc up" || warn "$svc start check karo: systemctl status $svc"; done

IP=$(curl -s --max-time 5 https://api.ipify.org || echo "YOUR_SERVER_IP")
EXPIRY_DATE=$(date -d "+365 days" +%F 2>/dev/null || echo "-")
log_activity "install completed for $DOMAIN"
banner "INSTALL COMPLETE — Summary (video style)"
cat <<EOF
  Domain          : $DOMAIN
  Server IP       : $IP
  Panel URL       : https://$DOMAIN:9090  (agar SSL hai)  |  http://$IP:9090
  Panel Username  : admin
  Panel Password  : $PANEL_PASS
  Credentials file: $CREDS_FILE  (aur $DATA_DIR/credentials.json)
  SSH Dropbear    : 109, 143
  SSH WS (HTTP)   : 80   |  SSH WS+SSL / Xray TLS: 443
  ws-proxy        : 8080 (internal -> 109)
  Nginx internal  : 127.0.0.1:18080
  Xray            : VMess /vmess · VLESS /vless · Trojan /trojan · XHTTP /xhttp
  Services        : nginx, haproxy, xray, dropbear, ws-proxy, vps-panel (check: menu option 05)
  Account Limits  : Days = literal (default 30) -> Expiry = creation + Days
                    GB limit (0 = unlimited) | IP limit (0 = unlimited)
                    Example: Days 30 / GB 0 / IP 0 -> Expiry creation+30d, Data: Unlimited, IP: Unlimited
  Suite Expiry    : $EXPIRY_DATE (reminder only)
  Menu chalao     : bash menu.sh   ya   vpsmenu
EOF
print_ports
ok "Ho gaya! Panel kholo, login karo, aur Settings me jaake password confirm/badlo."
