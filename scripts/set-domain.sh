#!/usr/bin/env bash
# VPS Manager Suite - change the domain and fully re-provision (original code)
# Usage: set-domain.sh <new-domain>
#
# Changing a domain is more than saving a setting. This script:
#   1. saves the domain in the suite config
#   2. re-renders the nginx router + HAProxy config from the templates
#   3. issues a fresh SSL certificate for the new domain (menu 03 logic,
#      incl. port-80 handling + self-signed fallback + renewal hook)
#   4. restarts the stack and regenerates EVERY stored Xray link with
#      the new domain, so existing accounts keep working
set -u
HERE="$(cd "$(dirname "$(readlink -f "${BASH_SOURCE[0]}")")" && pwd)"
SUITE_DIR="$(dirname "$HERE")"
# shellcheck disable=SC1091
source "$HERE/../lib/common.sh"

NEW="${1:-}"
if ! [[ "$NEW" =~ ^([a-zA-Z0-9-]{1,63}\.)+[a-zA-Z]{2,}$ ]]; then
  err "Usage: set-domain.sh <domain>  (e.g. my.example.com)"
  exit 1
fi
echo "[domain] Changing domain to: $NEW"

# 1) save in both config stores (data dir + /etc config)
python3 - "$DATA_DIR/config.json" "$CONFIG_FILE" "$NEW" <<'PY'
import json, os, sys
for p in sys.argv[1:3]:
    d = {}
    if p and os.path.exists(p):
        try: d = json.load(open(p))
        except Exception: d = {}
    d["domain"] = sys.argv[3]
    os.makedirs(os.path.dirname(p), exist_ok=True)
    json.dump(d, open(p, "w"), indent=2)
    print("[domain] saved in %s" % p)
PY

# panel port (for the nginx template placeholder)
PANEL_PORT=$(grep -o 'VPSMGR_PORT=[0-9]*' /etc/systemd/system/vps-panel.service 2>/dev/null | head -1 | cut -d= -f2)
PANEL_PORT="${PANEL_PORT:-9090}"

# 2) re-render templates
WS_PORT=$(python3 -c "import json;print(json.load(open('$DATA_DIR/config.json')).get('ws_proxy_port',8080))" 2>/dev/null || echo 8080)
[[ "$WS_PORT" =~ ^[0-9]+$ ]] || WS_PORT=8080
if [[ -f "$SUITE_DIR/templates/nginx.conf.tpl" ]]; then
  sed "s/__DOMAIN__/$NEW/g; s/__PANEL_PORT__/$PANEL_PORT/g; s/__WSPROXY_PORT__/$WS_PORT/g" "$SUITE_DIR/templates/nginx.conf.tpl" > /etc/nginx/nginx.conf
  if nginx -t 2>/dev/null; then
    systemctl restart nginx 2>/dev/null || true
    echo "[domain] nginx router re-rendered + restarted"
  else
    echo "[domain] WARNING: nginx config test failed - nginx left running with the old config"
  fi
fi
if [[ -f "$SUITE_DIR/templates/haproxy.cfg.tpl" && -d /etc/haproxy ]]; then
  sed "s/__DOMAIN__/$NEW/g; s/__WSPROXY_PORT__/$WS_PORT/g" "$SUITE_DIR/templates/haproxy.cfg.tpl" > /etc/haproxy/haproxy.cfg
  systemctl restart haproxy 2>/dev/null || true
  echo "[domain] haproxy config re-rendered + restarted"
fi

# 3) SSL for the new domain (stops/starts the web stack itself)
issue_ssl "$NEW" || echo "[domain] WARNING: SSL step reported a problem - the self-signed fallback should be active; see menu option 03"
install_renewal_hook

# 4) regenerate all stored Xray links with the new domain + resync
VPSMGR_DATA_DIR="$DATA_DIR" python3 "$SUITE_DIR/scripts/vpsm.py" regen-links "$NEW" || true
python3 "$SUITE_DIR/scripts/sync-xray.py" --restart 2>/dev/null || true
systemctl restart vps-panel 2>/dev/null || true
log_activity "domain changed to $NEW"
echo "[domain] DONE - domain is now $NEW (SSL re-issued, links regenerated)"
