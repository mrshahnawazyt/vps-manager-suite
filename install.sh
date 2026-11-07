#!/usr/bin/env bash
# VPS Manager Suite v1.0 - Installer (ORIGINAL code, Ubuntu 20.04 - 25.x)
# Features: Dropbear, ws-proxy, Nginx internal router, HAProxy 80/443 mux,
#           Xray (VMess/VLESS/Trojan, WS/TLS/TCP/XHTTP), CAKE QoS, SSL, Web Panel 9090
# Limit rule (corrected, video-verified): Days = literal (default 30, Expiry = creation + Days). 0 = Unlimited applies only to the GB limit and the IP limit.
set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# --- Single-file bootstrap (same style as the video) ---
# If only install.sh was downloaded (no lib/ folder next to it), download the
# remaining files from the GitHub repo and re-run this installer.
# Usage: export GH_REPO=username/repo-name [GH_TOKEN=xxx for a private repo]
#        curl -fsSL https://raw.githubusercontent.com/username/repo-name/main/install.sh -o install.sh && bash install.sh
if [[ ! -f "$SCRIPT_DIR/lib/common.sh" ]]; then
  if [[ -z "${GH_REPO:-}" ]]; then
    echo "Only install.sh was found. Set GH_REPO, e.g.:"
    echo "  export GH_REPO=username/repo-name"
    echo "or upload the full project folder to the VPS and run it from there."
    exit 1
  fi
  echo "Downloading the remaining files from the GitHub repo ($GH_REPO)..."
  _tmp="$(mktemp -d)"; mkdir -p "$_tmp/src"
  _auth=(); [[ -n "${GH_TOKEN:-}" ]] && _auth=(-H "Authorization: token $GH_TOKEN")
  curl -fsSL "${_auth[@]}" "https://api.github.com/repos/$GH_REPO/tarball/${GH_BRANCH:-main}" -o "$_tmp/repo.tgz"
  tar -xzf "$_tmp/repo.tgz" --strip-components=1 -C "$_tmp/src"
  exec bash "$_tmp/src/install.sh" "$@"
fi
# --- Keep the suite in a permanent location (/opt/vps-manager-suite) ---
# So vpsmenu, cron and the fix scripts always run from the right path -
# even when the install started from a temp folder, the files do not vanish.
SUITE_DIR="/opt/vps-manager-suite"
if [[ "$SCRIPT_DIR" != "$SUITE_DIR" && -f "$SCRIPT_DIR/lib/common.sh" ]]; then
  mkdir -p "$SUITE_DIR"
  cp -a "$SCRIPT_DIR/." "$SUITE_DIR/"
  SCRIPT_DIR="$SUITE_DIR"
fi
source "$SCRIPT_DIR/lib/common.sh"

require_root; check_os; ensure_dirs
banner "VPS Manager Suite v1.0 — Install All Components"

# 1) Ask for the domain
DOMAIN="${1:-}"
if [[ -z "$DOMAIN" ]]; then read -rp "Enter your domain (e.g. vpn.example.com): " DOMAIN; fi
[[ -z "$DOMAIN" ]] && { err "A domain is required"; exit 1; }
save_config_key domain "$DOMAIN"
info "Domain: $DOMAIN"; dns_check "$DOMAIN" || warn "DNS does not look right yet - the SSL step may fail; retry later from menu option 03."

# 2) Packages
info "Installing packages..."
export DEBIAN_FRONTEND=noninteractive
apt-get update -y
# Note: ufw is intentionally NOT installed - the installer does not enable a
# firewall, which avoids any risk of ports like 9090/443 being blocked.
# openssl is required (self-signed fallback and certs for Xray/Panel).
# Core packages: available on every supported Ubuntu (20.04 - 25.x).
apt-get install -y curl wget unzip jq nginx haproxy dropbear certbot python3 python3-pip iproute2 openssl uuid-runtime cron
# Optional packages: names/availability differ on some versions -
# if one is not found, skip it; the rest of the install must not stop.
for _pkg in stunnel4 speedtest-cli; do
  apt-get install -y "$_pkg" 2>/dev/null || warn "$_pkg was not found on this version - skipping."
done

# 3) Create the panel credentials (if they do not exist yet)
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
# Sync the config into the panel data dir
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
info "Configuring Dropbear..."
cat > /etc/default/dropbear <<EOF
NO_START=0
DROPBEAR_PORT=109
DROPBEAR_EXTRA_ARGS="-p 143"
DROPBEAR_BANNER="/etc/vps-manager/banner.txt"
DROPBEAR_RECEIVE_WINDOW=65536
EOF
# Note: the -s/-g flags are NOT used - they block password logins,
# and our tunnel users log in with passwords.
echo "Welcome to VPS Manager Suite" > "$CONFIG_DIR/banner.txt" 2>/dev/null || true
# Tunnel-only: new SSH users are created from the menu/panel with shell /usr/sbin/nologin

# 5) ws-proxy (Python service)
info "Setting up the ws-proxy service (port 8080 -> SSH 109)..."
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
info "Applying the Nginx + HAProxy templates..."
sed "s/__DOMAIN__/$DOMAIN/g; s/__PANEL_PORT__/9090/g" "$SCRIPT_DIR/templates/nginx.conf.tpl" > /etc/nginx/nginx.conf
sed "s/__DOMAIN__/$DOMAIN/g" "$SCRIPT_DIR/templates/haproxy.cfg.tpl" > /etc/haproxy/haproxy.cfg
nginx -t || warn "nginx config test warning"
haproxy -c -f /etc/haproxy/haproxy.cfg || warn "haproxy config test warning"

# 7) SSL - Let's Encrypt (standalone, port 80) or self-signed fallback.
# Use issue_ssl() from common.sh - it stops nginx/haproxy to free port 80,
# verifies the certificate, and builds combined.pem.
info "Requesting an SSL certificate (Let's Encrypt)..."
issue_ssl "$DOMAIN" || warn "The SSL step failed - the panel will run on HTTP. Issue SSL again from menu option 03 for the Xray TLS inbounds."

# 8) Xray (official script) + config
info "Installing Xray (official script)..."
if ! command -v xray >/dev/null 2>&1; then
  _xsh="$(mktemp)"
  if curl -fsSL https://github.com/XTLS/Xray-install/raw/main/install-release.sh -o "$_xsh" && [[ -s "$_xsh" ]]; then
    bash "$_xsh" install || warn "The Xray install script failed."
  else
    warn "Failed to download the Xray install script - check the internet/GitHub connection, then run menu option 01 again."
  fi
  rm -f "$_xsh"
fi
mkdir -p /usr/local/etc/xray /var/log/xray
# The Xray TLS inbounds load the panel certificate files - if the SSL step
# above did not produce them, Xray exits at once. Create a self-signed pair,
# but only when both files are absent (a real certificate is never overwritten).
XRAY_CERT="$CONFIG_DIR/ssl/fullchain.pem"; XRAY_KEY="$CONFIG_DIR/ssl/privkey.pem"
if [[ ! -s "$XRAY_CERT" && ! -s "$XRAY_KEY" ]]; then
  warn "SSL certificate files are missing - creating a self-signed pair so Xray can start"
  mkdir -p "$CONFIG_DIR/ssl" || true
  if openssl req -x509 -newkey rsa:2048 -keyout "$XRAY_KEY" -out "$XRAY_CERT" -days 365 -nodes -subj "/CN=$DOMAIN" >/dev/null 2>&1; then
    chmod 600 "$XRAY_KEY" 2>/dev/null || true
    ok "Self-signed certificate created (replace it any time from menu option 03)"
  else
    warn "Could not create a self-signed certificate - Xray TLS inbounds will not start until SSL is issued from menu option 03."
  fi
elif [[ ! -s "$XRAY_CERT" || ! -s "$XRAY_KEY" ]]; then
  warn "The SSL certificate pair is incomplete (one file is missing) - Xray cannot start until SSL is re-issued from menu option 03."
fi
# Seed client: keep at least one client in every inbound (bootstrap UUID).
# Real user accounts are added later by scripts/sync-xray.py.
XRAY_SEED_UUID="$(gen_uuid)"
sed "s/__DOMAIN__/$DOMAIN/g; s/__UUID__/$XRAY_SEED_UUID/g" "$SCRIPT_DIR/templates/xray.json.tpl" > /usr/local/etc/xray/config.json
if command -v xray >/dev/null 2>&1; then
  ok "Xray binary: $(xray version 2>/dev/null | head -n1)"
  if xray run -test -config /usr/local/etc/xray/config.json >/dev/null 2>&1; then
    ok "Xray config test OK"
  else
    err "Xray config test FAILED - the exact error is:"
    xray run -test -config /usr/local/etc/xray/config.json 2>&1 | tail -n 5 || true
    warn "The seed config is in place. Fix the cause shown above, then run menu option 06 (Fix Stack) - the xray service will not start until this passes."
  fi
else
  warn "The Xray binary was not installed - the xray service will not start. Check the internet connection and run menu option 01 again."
fi
# Xray paths: VMess /vmess (10001), VLESS /vless (10002), Trojan /trojan (10003), XHTTP /xhttp (10004), TLS fallback 10443, VLESS TCP 8443

# 9) CAKE QoS
info "CAKE QoS (Ping Stability)..."
bash "$SCRIPT_DIR/scripts/qos.sh" || warn "QoS was not applied yet - set the interface/rate from menu option 07 and try again."

# 10) Web Panel service (9090)
info "Setting up the Web Panel service (port 9090)..."
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

# 11) Limit monitor cron (checks are skipped when GB/IP = 0; Days are checked as a literal expiry)
cat > /etc/cron.d/vps-manager-limits <<EOF
*/10 * * * * root bash $SCRIPT_DIR/scripts/limit-check.sh >> /var/log/vps-manager-limits.log 2>&1
EOF
# vpsmenu / menu / vpsupdate are small wrappers that run from the /opt suite folder
# (copying a single file on its own lost lib/common.sh - hence the wrappers)
for _cmd in vpsmenu menu; do
  printf '#!/usr/bin/env bash\nexec bash %s/menu.sh "$@"\n' "$SCRIPT_DIR" > "/usr/local/bin/$_cmd"
  chmod +x "/usr/local/bin/$_cmd"
done
printf '#!/usr/bin/env bash\nexec bash %s/update.sh "$@"\n' "$SCRIPT_DIR" > /usr/local/bin/vpsupdate
chmod +x /usr/local/bin/vpsupdate

# 12) Services enable + health check
systemctl daemon-reload || true
for svc in nginx haproxy dropbear ws-proxy vps-panel xray; do systemctl enable --now "$svc" 2>/dev/null && ok "$svc up" || warn "$svc failed to start - check: systemctl status $svc"; done
# 'enable --now' does NOT restart an already-running service - so restart the
# panel explicitly, so the new panel.py and the SSL certificate issued in this
# install are loaded immediately (otherwise the panel stays on old code / HTTP).
systemctl restart vps-panel 2>/dev/null && ok "vps-panel restarted (fresh code + SSL cert loaded)" || warn "vps-panel restart failed - check: systemctl status vps-panel"
# Same for Xray: make sure the config and certificate from this install are
# the ones actually loaded, and say so plainly if it did not come up.
systemctl restart xray 2>/dev/null && ok "xray restarted (fresh config + certificate loaded)" || warn "xray failed to start - the exact reason is in the log: journalctl -u xray -n 20"

IP=$(curl -s --max-time 5 https://api.ipify.org || ip route get 1.1.1.1 2>/dev/null | awk '{for(i=1;i<=NF;i++) if($i=="src") print $(i+1)}' | head -n1 || hostname -I 2>/dev/null | awk '{print $1}')
EXPIRY_DATE=$(date -d "+365 days" +%F 2>/dev/null || echo "-")
log_activity "install completed for $DOMAIN"
banner "INSTALL COMPLETE — Summary"
# The correct panel URL: when a certificate exists, the panel is HTTPS ONLY (the http link will not work)
if [[ -s "$CONFIG_DIR/ssl/fullchain.pem" && -s "$CONFIG_DIR/ssl/privkey.pem" ]]; then
  PANEL_LINE="https://$DOMAIN:9090"
  PANEL_NOTE="HTTPS only - the http link will not work"
else
  PANEL_LINE="http://${IP:-127.0.0.1}:9090"
  PANEL_NOTE="No SSL certificate - the panel is on HTTP"
fi
cat <<EOF
  Domain       : $DOMAIN
  Server IP    : ${IP:-not detected}
  Panel URL    : $PANEL_LINE
                 $PANEL_NOTE
  Username     : admin
  Password     : $PANEL_PASS
  Creds file   : $CREDS_FILE
                 (and $DATA_DIR/credentials.json)
  SSH Dropbear : 109, 143
  SSH WS       : 80 (HTTP) | 443 (WS+SSL/TLS)
  ws-proxy     : 8080 (internal -> 109)
  Nginx        : 127.0.0.1:18080 (internal)
  Xray         : VMess /vmess, VLESS /vless
                 Trojan /trojan, XHTTP /xhttp
  Services     : nginx, haproxy, xray,
                 dropbear, ws-proxy, vps-panel
                 (check: menu option 05)
  Limits       : Days = literal (default 30)
                 Expiry = creation + Days
                 GB limit (0 = unlimited)
                 IP limit (0 = unlimited)
                 Ex: Days 30 / GB 0 / IP 0 ->
                 Expiry +30d, Data: Unlimited,
                 IP Limit: Unlimited
  Suite Expiry : $EXPIRY_DATE (reminder only)
  Run the menu : vpsmenu  or  bash menu.sh
EOF
print_ports
ok "Done! Open the panel and log in."
echo "Go to Settings to confirm or change your password."
