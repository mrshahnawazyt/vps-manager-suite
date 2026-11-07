#!/usr/bin/env bash
# VPS Manager Suite v1.0 - Installer (ORIGINAL code, Ubuntu 18.04 - 25.x)
# Features: Dropbear, ws-proxy, Nginx internal router, HAProxy 80/443 mux,
#           Xray (VMess/VLESS/Trojan, WS/TLS/TCP/XHTTP), CAKE QoS, SSL, Web Panel 9090
# Limit rule (corrected, video-verified): Days = literal (default 30, Expiry = creation + Days). 0 = Unlimited applies only to the GB limit and the IP limit.
set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# --- Single-file bootstrap (same style as the video) ---
# If only install.sh was downloaded (no lib/ folder next to it), download the
# remaining files from the GitHub repo and re-run this installer.
# Usage: curl -fsSL https://raw.githubusercontent.com/mrshahnawazyt/vps-manager-suite/main/install.sh -o install.sh && bash install.sh
# GH_REPO defaults to this suite's own repo (mrshahnawazyt/vps-manager-suite);
# override with: export GH_REPO=username/repo-name [GH_TOKEN=xxx for a private repo]
if [[ ! -f "$SCRIPT_DIR/lib/common.sh" ]]; then
  GH_REPO="${GH_REPO:-mrshahnawazyt/vps-manager-suite}"
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
# Core packages: available on every supported Ubuntu (18.04 - 25.x).
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
d.update({"domain":sys.argv[2],"banner":d.get("banner","DDK VPS Manager"),"qos_interface":d.get("qos_interface","eth0"),"qos_rate":d.get("qos_rate","950mbit"),"per_client_cap":0})
json.dump(d,open(p,"w"),indent=2)
for f in ("ssh_users.json","xray_users.json","activity.json"):
    fp=os.path.join(os.path.dirname(p),f)
    if not os.path.exists(fp): json.dump([],open(fp,"w"))
PY
cp "$CREDS_FILE" "$DATA_DIR/credentials.json" 2>/dev/null || true

# 4) Dropbear (SSH tunnel) - ports 109 / 143 / 447, banner
info "Configuring Dropbear..."
cat > /etc/default/dropbear <<EOF
NO_START=0
DROPBEAR_PORT=109
DROPBEAR_EXTRA_ARGS="-p 143 -p 447"
DROPBEAR_BANNER="/etc/vps-manager/banner.txt"
DROPBEAR_RECEIVE_WINDOW=65536
EOF
# Note: the -s/-g flags are NOT used - they block password logins,
# and our tunnel users log in with passwords.
# Default SSH banner: the styled design (gold box, colored lines) rendered
# by the shared banner renderer - the same design the panel's SSH Banner
# Designer starts from. A stored design (reinstall) is re-rendered as-is.
# Falls back to the plain line if the renderer is unavailable.
if ! python3 "$SCRIPT_DIR/scripts/banner_render.py" write-default --config "$DATA_DIR/config.json" --out "$CONFIG_DIR/banner.txt" >/dev/null 2>&1; then
  echo "DDK VPS Manager" > "$CONFIG_DIR/banner.txt" 2>/dev/null || true
fi
# Tunnel-only: new SSH users are created from the menu/panel/bot with
# shell /bin/false. Dropbear rejects login shells that are not listed in
# /etc/shells, so make sure the tunnel shell is a member right away.
ensure_tunnel_shell || warn "Could not add /bin/false to /etc/shells - SSH password logins would be rejected"

# 5) ws-proxy (Python Proxy service)
# Port from the suite config (ws_proxy_port, default 8080) so a port chosen
# in the panel survives a reinstall; the PROXY-protocol listener is WS_PORT+1.
WS_PORT=$(python3 -c "import json;print(json.load(open('$DATA_DIR/config.json')).get('ws_proxy_port',8080))" 2>/dev/null || echo 8080)
[[ "$WS_PORT" =~ ^[0-9]+$ ]] || WS_PORT=8080
info "Setting up the ws-proxy service (port $WS_PORT -> SSH 109)..."
install -m 755 "$SCRIPT_DIR/scripts/ws-proxy.py" /usr/local/bin/vps-ws-proxy.py
cat > /etc/systemd/system/ws-proxy.service <<EOF
[Unit]
Description=VPS Manager WS Proxy
After=network.target dropbear.service
[Service]
ExecStart=/usr/bin/python3 /usr/local/bin/vps-ws-proxy.py
Restart=always
Environment=WS_PROXY_PORT=$WS_PORT
Environment=WS_PROXY_PROXY_PORT=$((WS_PORT+1))
Environment=SSH_PORT_INTERNAL=109
Environment=VPSMGR_DATA_DIR=/var/lib/vps-manager
[Install]
WantedBy=multi-user.target
EOF

# 6) Nginx internal router + HAProxy mux
info "Applying the Nginx + HAProxy templates..."
sed "s/__DOMAIN__/$DOMAIN/g; s/__PANEL_PORT__/9090/g; s/__WSPROXY_PORT__/$WS_PORT/g" "$SCRIPT_DIR/templates/nginx.conf.tpl" > /etc/nginx/nginx.conf
sed "s/__DOMAIN__/$DOMAIN/g; s/__WSPROXY_PORT__/$WS_PORT/g; s/__WSPROXY_PROXY_PORT__/$((WS_PORT+1))/g" "$SCRIPT_DIR/templates/haproxy.cfg.tpl" > /etc/haproxy/haproxy.cfg
nginx -t || warn "nginx config test warning"
# HAProxy terminates TLS on 443 from the bundle $CONFIG_DIR/ssl/haproxy.pem.
# On a reinstall the cert pair already exists, so the bundle can be built
# and the config fully checked right away. On a first install the pair is
# only created in the SSL step below - the check is deferred until then
# (issue_ssl builds the bundle, and the services start after it).
build_haproxy_pem || true
if [[ -s "$CONFIG_DIR/ssl/haproxy.pem" ]]; then
  haproxy -c -f /etc/haproxy/haproxy.cfg || warn "haproxy config test warning"
else
  info "HAProxy config check deferred - the TLS bundle is built in the SSL step below"
fi

# 7) SSL - Let's Encrypt (standalone, port 80) or self-signed fallback.
# Use issue_ssl() from common.sh - it stops nginx/haproxy to free port 80,
# verifies the certificate, and builds combined.pem + haproxy.pem (the
# single-file TLS bundle HAProxy terminates port 443 with).
info "Requesting an SSL certificate (Let's Encrypt)..."
issue_ssl "$DOMAIN" || warn "The SSL step failed - the panel will run on HTTP. Issue SSL again from menu option 03 for the Xray TLS inbounds."
install_renewal_hook || true

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
# The stock Xray unit runs as User=nobody, which cannot read the private
# key above (mode 600) or write /var/log/xray/access.log - the config test
# passes as root but the service would die at startup. Install the drop-in
# that runs the service as root BEFORE the service is started in step 12.
ensure_xray_service || warn "The Xray service drop-in could not be installed - Xray may fail to start; run menu option 06 (Fix Stack)."
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

# 11) Limit monitor service - a fast loop (every few seconds) that enforces
# expiry/GB/IP limits, tracks connected IPs and auto-deletes dead accounts.
# (Replaces the old */10 cron - the legacy cron file is removed if present.)
rm -f /etc/cron.d/vps-manager-limits
cat > /etc/systemd/system/vps-monitor.service <<EOF
[Unit]
Description=VPS Manager Suite - Limit Monitor
After=network.target
[Service]
Type=simple
WorkingDirectory=$SCRIPT_DIR
Environment=VPSMGR_DATA_DIR=$DATA_DIR
ExecStart=/usr/bin/python3 $SCRIPT_DIR/scripts/monitor.py
Restart=always
RestartSec=3
[Install]
WantedBy=multi-user.target
EOF
# Telegram bot service (started from menu option 09 once a token is set -
# it is intentionally NOT auto-enabled here: without a token it would just
# exit, and the menu manages its full lifecycle)
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
for svc in nginx haproxy dropbear ws-proxy vps-panel xray vps-monitor; do systemctl enable --now "$svc" 2>/dev/null && ok "$svc up" || warn "$svc failed to start - check: systemctl status $svc"; done
# 'enable --now' does NOT restart an already-running service - so restart the
# panel explicitly, so the new panel.py and the SSL certificate issued in this
# install are loaded immediately (otherwise the panel stays on old code / HTTP).
systemctl restart vps-panel 2>/dev/null && ok "vps-panel restarted (fresh code + SSL cert loaded)" || warn "vps-panel restart failed - check: systemctl status vps-panel"
# Same for Xray: make sure the config and certificate from this install are
# the ones actually loaded, and say so plainly if it did not come up.
systemctl restart xray 2>/dev/null && ok "xray restarted (fresh config + certificate loaded)" || warn "xray restart command failed - check: systemctl status xray"
# Final health check: a successful restart command does not prove the
# service stayed up (it can die a second later on a bad config or an
# unreadable key) - verify the real state and show the log if it is down.
verify_xray_running || true

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
  SSH Dropbear : 109, 143, 447
  SSH WS       : 80 (HTTP) | 443 (WS+SSL/TLS)
  Python Proxy : $WS_PORT (ws-proxy -> 109)
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
# Final welcome box - printed LAST so it is the screen the user is left
# looking at when the install ends: panel URL + login + how to open the menu.
_WELCOME_EDGE="+--------------------------------------------+"
_wline(){ printf '| %-42s |\n' "$1"; }
echo "$_WELCOME_EDGE"
_wline "WELCOME - INSTALL COMPLETE"
echo "$_WELCOME_EDGE"
_wline "Panel : $PANEL_LINE"
_wline "User  : admin"
_wline "Pass  : ${PANEL_PASS:-(see menu option 10)}"
echo "$_WELCOME_EDGE"
_wline "Type menu to open the main menu"
echo "$_WELCOME_EDGE"
ok "Done! Open the panel and log in."
echo "Go to Settings to confirm or change your password."
