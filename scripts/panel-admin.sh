#!/usr/bin/env bash
# VPS Manager Suite - Web Panel administration from the terminal (original code)
# Usage: panel-admin.sh <command> [args]
#   port <n>            Change the panel port (validated: range, reserved
#                       ports, and ports already used by another service)
#   login <user> <pass> Change the panel username + password
#   reset-password [p]  Set a new panel password (random one if omitted -
#                       it is printed ONCE here)
#   logs [n]            Show the last n panel log lines (default 40)
#   reinstall           Re-copy the panel files and restart the service,
#                       KEEPING the current login and all data
#   uninstall           Remove ONLY the panel service (accounts, data and
#                       credentials stay on disk)
#   status              Show panel state, port and URLs
set -u
HERE="$(cd "$(dirname "$(readlink -f "${BASH_SOURCE[0]}")")" && pwd)"
SUITE_DIR="$(dirname "$HERE")"
DATA="${VPSMGR_DATA_DIR:-/var/lib/vps-manager}"
UNIT=/etc/systemd/system/vps-panel.service
RESERVED=" 22 80 109 143 447 443 8080 8081 8443 8444 8445 8446 8447 8448 10085 10443 18080 "

panel_port(){
  local p=""
  [[ -f "$UNIT" ]] && p=$(grep -o 'VPSMGR_PORT=[0-9]*' "$UNIT" 2>/dev/null | head -1 | cut -d= -f2)
  echo "${p:-9090}"
}

creds_files(){
  [[ -f /etc/vps-manager/credentials.json ]] && echo /etc/vps-manager/credentials.json
  [[ -f "$DATA/credentials.json" ]] && echo "$DATA/credentials.json"
}

set_creds(){ # $1=user $2=pass (either may be empty = keep current)
  python3 - "$1" "$2" $(creds_files) <<'PY'
import json, sys, os
user, pw = sys.argv[1], sys.argv[2]
files = sys.argv[3:]
if not files:
    os.makedirs("/var/lib/vps-manager", exist_ok=True)
    files = ["/var/lib/vps-manager/credentials.json"]
for p in files:
    d = {}
    if os.path.exists(p):
        try: d = json.load(open(p))
        except Exception: d = {}
    if user: d["username"] = user
    if pw: d["password"] = pw
    d.setdefault("username", "admin")
    json.dump(d, open(p, "w"), indent=2)
    print("credentials updated: %s (user: %s)" % (p, d["username"]))
PY
}

cmd="${1:-status}"
case "$cmd" in
  port)
    n="${2:-}"
    if ! [[ "$n" =~ ^[0-9]+$ ]] || (( n < 1 || n > 65535 )); then
      echo "[X] Invalid port: '$n' (must be 1-65535)"; exit 1
    fi
    if [[ "$RESERVED" == *" $n "* ]]; then
      echo "[X] Port $n is reserved by the suite stack (SSH/Xray/proxy) - pick another port."; exit 1
    fi
    if [[ -f "$UNIT" ]]; then
      cur="$(panel_port)"
      if [[ "$n" != "$cur" ]] && ss -tln 2>/dev/null | grep -q "[:.]$n[[:space:]]"; then
        echo "[X] Port $n is already in use by another service:"; ss -tlnp 2>/dev/null | grep "[:.]$n[[:space:]]" || true; exit 1
      fi
      sed -i "s/VPSMGR_PORT=[0-9]*/VPSMGR_PORT=$n/" "$UNIT" 2>/dev/null || { echo "[X] Could not edit $UNIT"; exit 1; }
      systemctl daemon-reload 2>/dev/null || true
      systemctl restart vps-panel 2>/dev/null || true
      echo "[OK] Panel port changed: $cur -> $n (panel restarting)"
      echo "     New URL: http://$(hostname -I 2>/dev/null | awk '{print $1}'):$n  (or https://YOUR-DOMAIN:$n)"
    else
      echo "[X] Panel service unit not found ($UNIT) - run Install All first."; exit 1
    fi
    ;;
  login)
    u="${2:-}"; p="${3:-}"
    [[ -z "$u" || -z "$p" ]] && { echo "Usage: panel-admin.sh login <user> <pass>"; exit 1; }
    set_creds "$u" "$p"
    echo "[OK] Panel login changed. Restart is not needed - new logins use it right away."
    ;;
  reset-password)
    p="${2:-}"
    if [[ -z "$p" ]]; then
      p=$(tr -dc 'A-Za-z0-9' </dev/urandom | head -c 14; echo)
      echo "New panel password (save it now, it is shown only here): $p"
    fi
    set_creds "" "$p"
    echo "[OK] Panel password updated."
    ;;
  logs)
    journalctl -u vps-panel -n "${2:-40}" --no-pager 2>/dev/null || echo "No panel logs found."
    ;;
  reinstall)
    if [[ -f "$SUITE_DIR/web/panel.py" && -d /opt/vps-manager-suite/web ]]; then
      cp -f "$SUITE_DIR/web/panel.py" /opt/vps-manager-suite/web/panel.py
      echo "[OK] Panel file refreshed from $SUITE_DIR/web/panel.py"
    fi
    systemctl restart vps-panel 2>/dev/null || true
    echo "[OK] Panel reinstalled/restarted - login and data were kept."
    ;;
  uninstall)
    systemctl disable --now vps-panel 2>/dev/null || true
    rm -f "$UNIT"
    systemctl daemon-reload 2>/dev/null || true
    echo "[OK] Panel service removed. Accounts, data and credentials were kept in $DATA."
    echo "     Reinstall any time with menu option 01 (Install All)."
    ;;
  status)
    _st="$(systemctl is-active vps-panel 2>/dev/null || true)"; [[ -z "$_st" ]] && _st="not installed"
    echo "Panel service : $_st"
    echo "Panel port    : $(panel_port)"
    ip=$(curl -s --max-time 5 https://api.ipify.org 2>/dev/null || hostname -I 2>/dev/null | awk '{print $1}')
    echo "Panel (IP)    : http://$ip:$(panel_port)"
    for f in $(creds_files); do echo "Credentials   : $f"; done
    ;;
  *) echo "Usage: panel-admin.sh {port|login|reset-password|logs|reinstall|uninstall|status}"; exit 1 ;;
esac
