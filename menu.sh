#!/usr/bin/env bash
# VPS Manager Suite v1.0 - Terminal Menu (original code)
# Limit rule (corrected, video-verified):
#   Days = literal count, default 30. Expiry = creation date + Days. (0 = 0 days, NOT unlimited)
#   GB limit (0 = unlimited) | IP limit (0 = unlimited)
set -u
# If launched via a symlink (/usr/local/bin/vpsmenu), resolve the real file's folder
SCRIPT_DIR="$(cd "$(dirname "$(readlink -f "${BASH_SOURCE[0]}")")" && pwd)"
# Find common.sh: next to this script, otherwise in the /opt suite folder
for _lib in "$SCRIPT_DIR/lib/common.sh" "/opt/vps-manager-suite/lib/common.sh"; do
  [[ -f "$_lib" ]] && { source "$_lib"; break; }
done
# If common.sh is still not found (running as a single copied file), define the
# basic functions here so the menu runs without "command not found" errors
if ! declare -F banner >/dev/null 2>&1; then
  banner(){ echo "=== $* ==="; }
  info(){ echo "[INFO] $*"; }; warn(){ echo "[!] $*"; }; err(){ echo "[X] $*" >&2; }; ok(){ echo "[OK] $*"; }
  gen_password(){ tr -dc 'A-Za-z0-9' </dev/urandom 2>/dev/null | head -c 14; echo; }
  get_domain(){ python3 -c "import json;print(json.load(open('${VPSMGR_DATA_DIR:-/var/lib/vps-manager}/config.json')).get('domain',''))" 2>/dev/null || echo ""; }
  save_config_key(){ :; }; dns_check(){ echo "DNS check skipped (common.sh not found)"; return 0; }
  service_status(){ systemctl is-active "$1" 2>/dev/null || echo "unknown"; }
  print_ports(){ ss -tulnp 2>/dev/null || netstat -tulnp 2>/dev/null || echo "could not list ports"; }
  log_activity(){ :; }
fi
DATA="${VPSMGR_DATA_DIR:-/var/lib/vps-manager}"
[[ -d "$DATA" ]] || DATA="$SCRIPT_DIR/data"
mkdir -p "$DATA" 2>/dev/null || true
DOMAIN="$(get_domain)"; [[ -z "$DOMAIN" ]] && DOMAIN=$(python3 -c "import json;print(json.load(open('$DATA/config.json')).get('domain',''))" 2>/dev/null || echo "")

pause(){ read -rp "Press Enter to return to the menu..." _; }

# --- Display helpers: single column, every line max 46 chars (phone terminal) ---
BOX_EDGE="+--------------------------------------------+"
box_line(){ printf '| %-42s |\n' "$1"; }
box_center(){
  local _t="$1" _p=$(( (42 - ${#1}) / 2 ))
  printf '| %*s%s%*s |\n' "$_p" '' "$_t" $((42 - ${#1} - _p)) ''
}

add_ssh_user(){
  echo "$BOX_EDGE"; box_center "Create SSH (tunnel-only)"; echo "$BOX_EDGE"
  read -rp "Username: " u; [[ -z "$u" ]] && return
  read -rp "Password (blank = auto): " pw; [[ -z "$pw" ]] && pw=$(gen_password)
  read -rp "Days [30]: " days; days="${days:-30}"
  read -rp "GB limit (0 = unlimited) [0]: " gb; gb="${gb:-0}"
  read -rp "IP limit (0 = unlimited) [0]: " ip; ip="${ip:-0}"
  # On a real VPS, create a tunnel-only system user:
  if [[ "${EUID:-$(id -u)}" -eq 0 ]] && command -v useradd >/dev/null; then
    # Dropbear rejects login shells that are not listed in /etc/shells,
    # so the tunnel shell must be a member before accounts can log in.
    grep -qx "/bin/false" /etc/shells 2>/dev/null || echo "/bin/false" >> /etc/shells 2>/dev/null || true
    # Accounts created before the shell fix may still have a nologin shell.
    [[ "$(getent passwd "$u" 2>/dev/null | cut -d: -f7)" == */nologin ]] && usermod -s /bin/false "$u" 2>/dev/null || true
    id "$u" >/dev/null 2>&1 || useradd -M -s /bin/false -e "$(date -d "+${days} days" +%F)" "$u" 2>/dev/null || warn "useradd skipped/failed"
    echo "$u:$pw" | chpasswd 2>/dev/null || true
  fi
  PROXY_PORT=$(python3 -c "import json;print(json.load(open('$DATA/config.json')).get('ws_proxy_port',8080))" 2>/dev/null || echo 8080)
  [[ "$PROXY_PORT" =~ ^[0-9]+$ ]] || PROXY_PORT=8080
  _out=$(python3 - "$DATA" "$u" "$pw" "$days" "$gb" "$ip" "$PROXY_PORT" <<'PY'
import json,os,sys,datetime
data,u,pw,days,gb,ip,proxy=sys.argv[1:8]
days=int(days); gb=int(gb); ip=int(ip)
created=datetime.date.today()
rec={"username":u,"password":pw,"expiry_days":days,"gb_limit":gb,"ip_limit":ip,"used_gb":0,"created":created.isoformat(),"expires":(created+datetime.timedelta(days=days)).isoformat()}
p=os.path.join(data,"ssh_users.json"); arr=json.load(open(p)) if os.path.exists(p) else []
arr.append(rec); json.dump(arr,open(p,"w"),indent=2)
print(f"User   : {u}")
print(f"Pass   : {pw}")
print(f"Expiry : {rec['expires']}")
print(f"Data   : {'Unlimited' if gb==0 else str(gb)+' GB'}")
print(f"IP Lim : {'Unlimited' if ip==0 else ip}")
print(f"Proxy  : {proxy} (Python Proxy)")
PY
)
  echo "$BOX_EDGE"; box_center "SSH ACCOUNT READY"; echo "$BOX_EDGE"
  while IFS= read -r _l; do [[ -n "$_l" ]] && box_line "$_l"; done <<< "$_out"
  echo "$BOX_EDGE"
  echo "VPN Details (copy):"
  echo "$DOMAIN:443@$u:$pw"
  echo "Payload (copy):"
  echo "GET /sshws HTTP/1.1[crlf]Host: $DOMAIN[crlf]Upgrade: websocket[crlf][crlf]"
  log_activity "menu ssh create $u days=$days gb=$gb ip=$ip"; pause
}

add_xray_user(){
  local proto="${1:-}"
  echo "$BOX_EDGE"; box_center "Create Xray Account"; echo "$BOX_EDGE"
  if [[ -z "$proto" ]]; then
    read -rp "Protocol (VMess/VLESS/Trojan) [VLESS]: " proto; proto="${proto:-VLESS}"
  fi
  read -rp "Remark/Name: " remark; [[ -z "$remark" ]] && return
  read -rp "Days [30]: " days; days="${days:-30}"
  read -rp "GB limit (0 = unlimited) [0]: " gb; gb="${gb:-0}"
  read -rp "IP limit (0 = unlimited) [0]: " ip; ip="${ip:-0}"
  _out=$(VPSMGR_DATA_DIR="$DATA" python3 - "$SCRIPT_DIR/scripts" "$proto" "$remark" "$days" "$gb" "$ip" "$DOMAIN" <<'PY'
import os,sys,uuid
sys.path.insert(0, sys.argv[1])
import vpsm
proto,remark,days,gb,ip,domain=sys.argv[2:8]
uid=str(uuid.uuid4())
try:
    rec=vpsm.xray_create(proto, remark, uid, days, gb, ip, domain)
except Exception as e:
    print(f"Error  : {e}"); sys.exit(0)
print(f"Remark : {remark}")
print(f"Proto  : {rec['protocol']}")
print(f"Expiry : {rec['expires']}")
print(f"Data   : {'Unlimited' if rec['gb_limit']==0 else str(rec['gb_limit'])+' GB'}")
print(f"IP Lim : {'Unlimited' if rec['ip_limit']==0 else rec['ip_limit']}")
print("==LINKS==")
for k,v in rec["links"].items(): print(f"{k}: {v}")
PY
)
  echo "$BOX_EDGE"; box_center "XRAY ACCOUNT READY"; echo "$BOX_EDGE"
  _inlinks=0
  while IFS= read -r _l; do
    if [[ "$_l" == "==LINKS==" ]]; then _inlinks=1; echo "$BOX_EDGE"; echo "Links (copy):"; continue; fi
    if [[ $_inlinks -eq 0 ]]; then [[ -n "$_l" ]] && box_line "$_l"; else echo "$_l"; fi
  done <<< "$_out"
  # Also add the account to the live Xray config (a record alone is not enough)
  python3 "$SCRIPT_DIR/scripts/sync-xray.py" --restart 2>/dev/null || echo "(xray sync will run later - run it from /opt on the VPS)"
  pause
}

list_users(){
  echo "$BOX_EDGE"; box_center "SSH ACCOUNTS"; echo "$BOX_EDGE"
  _out=$(VPSMGR_DATA_DIR="$DATA" python3 "$SCRIPT_DIR/scripts/vpsm.py" ssh-list 2>/dev/null | python3 -c "
import json,sys
try: users=json.load(sys.stdin)
except Exception: users=[]
for i,u in enumerate(users):
    if i: print('')
    print(u['name'])
    print(f\"  Status : {u['status']}\")
    print(f\"  Expiry : {u.get('expires','?')} ({u.get('expiry_days',30)} days)\")
    print(f\"  Data   : {'Unlimited' if int(u.get('gb_limit') or 0)==0 else u.get('gb_limit')} (used {u.get('used_gb',0)})\")
    print(f\"  IP Lim : {'Unlimited' if int(u.get('ip_limit') or 0)==0 else u.get('ip_limit')}\")
    if u.get('dead_note'): print(f\"  Note   : {u['dead_note']}\")
" 2>/dev/null)
  [[ -z "$_out" ]] && _out="No SSH users yet"
  while IFS= read -r _l; do box_line "$_l"; done <<< "$_out"
  echo "$BOX_EDGE"; box_center "XRAY ACCOUNTS"; echo "$BOX_EDGE"
  _out=$(VPSMGR_DATA_DIR="$DATA" python3 "$SCRIPT_DIR/scripts/vpsm.py" xray-list 2>/dev/null | python3 -c "
import json,sys
try: users=json.load(sys.stdin)
except Exception: users=[]
for i,u in enumerate(users):
    if i: print('')
    print(u['name'])
    print(f\"  Status : {u['status']}\")
    print(f\"  Expiry : {u.get('expires','?')} ({u.get('expiry_days',30)} days)\")
    print(f\"  Data   : {'Unlimited' if int(u.get('gb_limit') or 0)==0 else u.get('gb_limit')} (used {u.get('used_gb',0)})\")
    print(f\"  IP Lim : {'Unlimited' if int(u.get('ip_limit') or 0)==0 else u.get('ip_limit')}\")
    if u.get('dead_note'): print(f\"  Note   : {u['dead_note']}\")
" 2>/dev/null)
  [[ -z "$_out" ]] && _out="No Xray users yet"
  while IFS= read -r _l; do box_line "$_l"; done <<< "$_out"
  echo "$BOX_EDGE"
  pause
}

# --- account pickers: list goes to stderr, chosen key comes back on stdout ---
pick_ssh_user(){
  python3 - "$DATA/ssh_users.json" <<'PY'
import json,sys,os
try: users=json.load(open(sys.argv[1]))
except Exception: users=[]
if not users:
    print("No SSH users yet", file=sys.stderr); sys.exit(1)
for i,u in enumerate(users,1):
    print(f"{i}) {u.get('username')}", file=sys.stderr)
try: n=int(input("Number (0 = back): ").strip())
except Exception: sys.exit(1)
if n<1 or n>len(users): sys.exit(1)
print(users[n-1].get("username"))
PY
}

pick_xray_user(){
  # pick_xray_user [protocol] - lists matching accounts, prints the id
  python3 - "$DATA/xray_users.json" "${1:-}" <<'PY'
import json,sys
try: users=json.load(open(sys.argv[1]))
except Exception: users=[]
proto=sys.argv[2].lower() if len(sys.argv)>2 else ""
if proto: users=[u for u in users if str(u.get("protocol","")).lower()==proto]
if not users:
    print("No matching Xray accounts", file=sys.stderr); sys.exit(1)
for i,u in enumerate(users,1):
    print(f"{i}) {u.get('protocol')} {u.get('remark')} ({u.get('id','')[:8]}...)", file=sys.stderr)
try: n=int(input("Number (0 = back): ").strip())
except Exception: sys.exit(1)
if n<1 or n>len(users): sys.exit(1)
print(users[n-1].get("id"))
PY
}

_vpsm(){ VPSMGR_DATA_DIR="$DATA" python3 "$SCRIPT_DIR/scripts/vpsm.py" "$@" 2>/dev/null; }
_xray_sync(){ python3 "$SCRIPT_DIR/scripts/sync-xray.py" --restart >/dev/null 2>&1 || true; }

sys_ssh_lock(){ # lock + kill sessions (root only, best effort)
  [[ "${EUID:-$(id -u)}" -eq 0 ]] || return 0
  usermod -L "$1" 2>/dev/null || true
  local _uid; _uid=$(id -u "$1" 2>/dev/null || echo ""); [[ -n "$_uid" ]] && pkill -u "$_uid" 2>/dev/null || true
}
sys_ssh_unlock(){ [[ "${EUID:-$(id -u)}" -eq 0 ]] || return 0; usermod -U "$1" 2>/dev/null || true; }
sys_ssh_kick(){ local _uid; _uid=$(id -u "$1" 2>/dev/null || echo ""); [[ -n "$_uid" ]] && pkill -u "$_uid" 2>/dev/null || true; }

manage_ssh(){
  local u; u=$(pick_ssh_user) || { pause; return; }
  echo "$BOX_EDGE"; box_center "SSH: $u"; echo "$BOX_EDGE"
  box_line "1) Delete account"
  box_line "2) Renew (add days)"
  box_line "3) Edit limits (GB / IP)"
  box_line "4) Reset usage"
  box_line "5) Lock account"
  box_line "6) Unlock account"
  box_line "7) Kick live sessions"
  box_line "0) Back"
  echo "$BOX_EDGE"
  read -rp "Choose: " a
  case "$a" in
    1) read -rp "Delete $u? Type y to confirm: " c; [[ "$c" == "y" ]] && { _vpsm ssh-delete "$u"; [[ "${EUID:-$(id -u)}" -eq 0 ]] && userdel "$u" 2>/dev/null || true; ok "SSH account deleted: $u"; log_activity "menu ssh delete $u"; } ;;
    2) read -rp "Add how many days? [30]: " d; d="${d:-30}"; _o=$(_vpsm ssh-renew "$u" "$d"); box_line "Renewed: $_o"
       _exp=$(python3 -c "import json;print([x['expires'] for x in json.load(open('$DATA/ssh_users.json')) if x['username']=='$u'][0])" 2>/dev/null || echo "")
       [[ -n "$_exp" && "${EUID:-$(id -u)}" -eq 0 ]] && usermod -e "$_exp" "$u" 2>/dev/null || true
       log_activity "menu ssh renew $u +$d days" ;;
    3) read -rp "GB limit (0 = unlimited) [0]: " gb; gb="${gb:-0}"; read -rp "IP limit (0 = unlimited) [0]: " ip; ip="${ip:-0}"
       _vpsm ssh-limits "$u" "$gb" "$ip" && ok "Limits saved for $u"; log_activity "menu ssh limits $u gb=$gb ip=$ip" ;;
    4) _vpsm ssh-reset "$u" && ok "Usage reset for $u"; log_activity "menu ssh reset $u" ;;
    5) _vpsm ssh-lock "$u"; sys_ssh_lock "$u"; ok "Locked: $u"; log_activity "menu ssh lock $u" ;;
    6) _vpsm ssh-unlock "$u"; sys_ssh_unlock "$u"; ok "Unlocked: $u"; log_activity "menu ssh unlock $u" ;;
    7) sys_ssh_kick "$u"; ok "Sessions kicked for $u" ;;
    *) ;;
  esac
  pause
}

manage_xray_one(){
  # manage_xray_one <id> <label>
  local id="$1" label="$2"
  echo "$BOX_EDGE"; box_center "XRAY: $label"; echo "$BOX_EDGE"
  box_line "1) Show links"
  box_line "2) Delete account"
  box_line "3) Renew (add days)"
  box_line "4) Edit limits (GB / IP)"
  box_line "5) Reset usage"
  box_line "6) Lock account"
  box_line "7) Unlock account"
  box_line "0) Back"
  echo "$BOX_EDGE"
  read -rp "Choose: " a
  case "$a" in
    1) echo "Links (copy):"; _vpsm links "$id" ;;
    2) read -rp "Delete this account? Type y to confirm: " c; [[ "$c" == "y" ]] && { _vpsm xray-delete "$id"; _xray_sync; ok "Xray account deleted"; log_activity "menu xray delete $id"; } ;;
    3) read -rp "Add how many days? [30]: " d; d="${d:-30}"; _o=$(_vpsm xray-renew "$id" "$d"); box_line "Renewed: $_o"; _xray_sync; log_activity "menu xray renew $id +$d" ;;
    4) read -rp "GB limit (0 = unlimited) [0]: " gb; gb="${gb:-0}"; read -rp "IP limit (0 = unlimited) [0]: " ip; ip="${ip:-0}"
       _vpsm xray-limits "$id" "$gb" "$ip" && ok "Limits saved"; _xray_sync; log_activity "menu xray limits $id gb=$gb ip=$ip" ;;
    5) _vpsm xray-reset "$id" && ok "Usage reset"; _xray_sync; log_activity "menu xray reset $id" ;;
    6) _vpsm xray-lock "$id"; _xray_sync; ok "Locked (removed from Xray)"; log_activity "menu xray lock $id" ;;
    7) _vpsm xray-unlock "$id"; _xray_sync; ok "Unlocked (back in Xray)"; log_activity "menu xray unlock $id" ;;
    *) ;;
  esac
  pause
}

xray_proto_menu(){
  local proto="$1" plabel="$2"
  while true; do
    echo "$BOX_EDGE"; box_center "$plabel MENU"; echo "$BOX_EDGE"
    box_line "1) Create account"
    box_line "2) List accounts"
    box_line "3) Manage one account"
    box_line "   (links/delete/renew/limits/"
    box_line "    reset/lock/unlock)"
    box_line "4) Connected IPs"
    box_line "0) Back"
    echo "$BOX_EDGE"
    read -rp "Choose: " a
    case "$a" in
      1) add_xray_user "$proto" ;;
      2) _out=$(VPSMGR_DATA_DIR="$DATA" python3 "$SCRIPT_DIR/scripts/vpsm.py" xray-list 2>/dev/null | VPSMGR_PROTO="$proto" python3 -c "
import json,sys,os
proto=os.environ['VPSMGR_PROTO'].lower()
try: users=json.load(sys.stdin)
except Exception: users=[]
users=[u for u in users if u['name'].lower().startswith(proto+'/')]
for i,u in enumerate(users):
    if i: print('')
    print(u['name'])
    print(f\"  Status : {u['status']}\")
    print(f\"  Expiry : {u.get('expires','?')} ({u.get('expiry_days',30)} days)\")
    print(f\"  Data   : {'Unlimited' if int(u.get('gb_limit') or 0)==0 else u.get('gb_limit')} (used {u.get('used_gb',0)})\")
    print(f\"  IP Lim : {'Unlimited' if int(u.get('ip_limit') or 0)==0 else u.get('ip_limit')}\")
    if u.get('dead_note'): print(f\"  Note   : {u['dead_note']}\")
" 2>/dev/null)
         [[ -z "$_out" ]] && _out="No $plabel accounts yet"
         while IFS= read -r _l; do box_line "$_l"; done <<< "$_out"
         echo "$BOX_EDGE"; pause ;;
      3) local _id _lbl
         _id=$(pick_xray_user "$proto") || { pause; continue; }
         _lbl=$(python3 -c "import json;print([x.get('remark','') for x in json.load(open('$DATA/xray_users.json')) if x['id']=='$_id'][0])" 2>/dev/null || echo "$_id")
         manage_xray_one "$_id" "$plabel $_lbl" ;;
      4) show_connected xray "$proto" ;;
      0) return ;;
      *) pause ;;
    esac
  done
}

show_connected(){
  # show_connected [ssh|xray] [protocol] - reads the monitor's connected.json
  echo "$BOX_EDGE"; box_center "CONNECTED IPS"; echo "$BOX_EDGE"
  _out=$(python3 - "$DATA/connected.json" "${1:-all}" "${2:-}" <<'PY' 2>/dev/null
import json,sys,os
p=sys.argv[1]; kind=sys.argv[2]; proto=sys.argv[3] if len(sys.argv)>3 else ""
try: d=json.load(open(p))
except Exception: d={}
def emit(section, title):
    rows=d.get(section) or {}
    if proto: rows={k:v for k,v in rows.items() if k.lower().startswith(proto.lower()+"/")}
    if not rows: return
    print(title)
    for name,ips in rows.items():
        for ip,meta in (ips or {}).items():
            geo=(meta or {}).get("country","") or "-"
            print(f"  {name} : {ip} ({geo})")
if kind in ("all","ssh"): emit("ssh","SSH:")
if kind in ("all","xray"): emit("xray","XRAY:")
PY
)
  [[ -z "$_out" ]] && _out="No live connections recorded yet"
  while IFS= read -r _l; do box_line "$_l"; done <<< "$_out"
  echo "$BOX_EDGE"
  box_line "Kick: SSH manage menu, option 7"
  echo "$BOX_EDGE"
  pause
}

# ---------------- Telegram bot service management (option 09) ----------------
bot_cfg_set(){ python3 - "$DATA/config.json" "$1" "$2" <<'PY'
import json, sys, os
p, k, v = sys.argv[1], sys.argv[2], sys.argv[3]
c = {}
if os.path.exists(p):
    try: c = json.load(open(p))
    except Exception: c = {}
if v.startswith("["):
    try: v = json.loads(v)
    except Exception: pass
c[k] = v
json.dump(c, open(p, "w"), indent=2)
PY
}

bot_ensure_unit(){
  [[ -f /etc/systemd/system/vps-bot.service ]] && return 0
  cat > /etc/systemd/system/vps-bot.service <<'EOF'
[Unit]
Description=VPS Manager Suite - Telegram Bot
After=network.target
[Service]
Type=simple
WorkingDirectory=/opt/vps-manager-suite
ExecStart=/usr/bin/python3 /opt/vps-manager-suite/scripts/telegram-bot.py
Restart=on-failure
RestartSec=5
[Install]
WantedBy=multi-user.target
EOF
  systemctl daemon-reload 2>/dev/null || true
}

bot_admins_menu(){
  local c id
  while true; do
    echo "$BOX_EDGE"; box_center "BOT ADMINS"; echo "$BOX_EDGE"
    python3 - "$DATA/config.json" <<'PY' | while IFS= read -r _l; do box_line "$_l"; done
import json, sys
try: c = json.load(open(sys.argv[1]))
except Exception: c = {}
print("Super: %s" % (c.get("telegram_admin_id") or "-"))
for a in c.get("telegram_bot_admins") or []:
    print("Admin: %s" % a)
PY
    box_line "1) Add admin (chat ID)"
    box_line "2) Remove admin (chat ID)"
    box_line "0) Back"
    echo "$BOX_EDGE"
    read -rp "Select: " c
    case "$c" in
      1) read -rp "Chat ID to add: " id
         if [[ -n "$id" ]]; then python3 - "$DATA/config.json" "$id" <<'PY'
import json, sys, os
p, i = sys.argv[1], sys.argv[2]
c = json.load(open(p)) if os.path.exists(p) else {}
ids = [str(x) for x in c.get("telegram_bot_admins") or []]
if i not in ids: ids.append(i)
c["telegram_bot_admins"] = ids
json.dump(c, open(p, "w"), indent=2)
print("Admin added.")
PY
         fi; pause ;;
      2) read -rp "Chat ID to remove: " id
         if [[ -n "$id" ]]; then python3 - "$DATA/config.json" "$id" <<'PY'
import json, sys, os
p, i = sys.argv[1], sys.argv[2]
c = json.load(open(p)) if os.path.exists(p) else {}
c["telegram_bot_admins"] = [str(x) for x in c.get("telegram_bot_admins") or [] if str(x) != i]
json.dump(c, open(p, "w"), indent=2)
print("Admin removed.")
PY
         fi; pause ;;
      0) return ;;
      *) echo "Invalid choice"; pause ;;
    esac
  done
}

bot_menu(){
  local c st tk ai
  while true; do
    clear 2>/dev/null || true
    st=$(systemctl is-active vps-bot 2>/dev/null || echo "not installed")
    echo "$BOX_EDGE"; box_center "TELEGRAM BOT"; echo "$BOX_EDGE"
    box_line "Status: $st"
    box_line "1) Setup token + admin ID"
    box_line "2) Start bot"
    box_line "3) Stop bot"
    box_line "4) Restart bot"
    box_line "5) Bot logs (last 20 lines)"
    box_line "6) Bot admins"
    box_line "7) Update bot files"
    box_line "8) Remove bot"
    box_line "0) Back"
    echo "$BOX_EDGE"
    read -rp "Select: " c
    case "$c" in
      1) read -rp "Bot token: " tk; read -rp "Your Telegram chat ID: " ai
         [[ -n "$tk" ]] && bot_cfg_set telegram_bot_token "$tk"
         [[ -n "$ai" ]] && bot_cfg_set telegram_admin_id "$ai"
         echo "Saved. Use option 2 to start the bot."; pause ;;
      2) bot_ensure_unit; systemctl enable --now vps-bot 2>/dev/null || true; systemctl restart vps-bot 2>/dev/null || true
         echo "Bot status: $(systemctl is-active vps-bot 2>/dev/null || echo unknown)"; pause ;;
      3) systemctl stop vps-bot 2>/dev/null || true; echo "Bot stopped."; pause ;;
      4) bot_ensure_unit; systemctl restart vps-bot 2>/dev/null || true
         echo "Bot status: $(systemctl is-active vps-bot 2>/dev/null || echo unknown)"; pause ;;
      5) journalctl -u vps-bot -n 20 --no-pager 2>/dev/null || echo "No logs yet."; pause ;;
      6) bot_admins_menu ;;
      7) cp -f "$SCRIPT_DIR/scripts/telegram-bot.py" /opt/vps-manager-suite/scripts/ 2>/dev/null || true
         cp -f "$SCRIPT_DIR/scripts/vpsm.py" /opt/vps-manager-suite/scripts/ 2>/dev/null || true
         systemctl restart vps-bot 2>/dev/null || true; echo "Bot files updated + restarted."; pause ;;
      8) systemctl disable --now vps-bot 2>/dev/null || true; rm -f /etc/systemd/system/vps-bot.service
         systemctl daemon-reload 2>/dev/null || true
         bot_cfg_set telegram_bot_token ""; bot_cfg_set telegram_bot_admins "[]"
         echo "Bot removed (service stopped, token cleared)."; pause ;;
      0) return ;;
      *) echo "Invalid choice"; pause ;;
    esac
  done
}

while true; do
  clear 2>/dev/null || true
  echo "$BOX_EDGE"
  box_center "VPS Manager Suite v1.0"
  box_center "Main Menu"
  echo "$BOX_EDGE"
  if [[ -n "$DOMAIN" ]]; then box_line "Domain: $DOMAIN"; else box_line "Domain: (not set - use 02)"; fi
  echo "$BOX_EDGE"
  box_line "01) Install All Components"
  box_line "02) Add / Change Domain"
  box_line "03) SSL Certificate (issue/renew)"
  box_line "04) Manage Users / Limits / Banner"
  box_line "05) Status & Ports"
  box_line "06) Fix SSH-WS / SSL Stack"
  box_line "07) Ping Stability (QoS / CAKE)"
  box_line "08) VPS Stats / Speedtest"
  box_line "09) Telegram Bot"
  box_line "10) Web Panel (link/login/port)"
  box_line "11) Uninstall"
  box_line "12) Exit"
  echo "$BOX_EDGE"
  box_line "Note: Days = literal (default 30)"
  box_line "GB/IP limit (0 = unlimited)"
  echo "$BOX_EDGE"
  read -rp "Choose an option [01-12]: " opt
  case "$opt" in
    01|1) bash "$SCRIPT_DIR/install.sh" "$DOMAIN"; pause ;;
    02|2) read -rp "New domain: " nd
      if [[ -n "$nd" ]]; then
        dns_check "$nd" || true
        echo "Re-provisioning for $nd (SSL + links)..."
        if bash "$SCRIPT_DIR/scripts/set-domain.sh" "$nd"; then DOMAIN="$nd"; box_line "Domain is now: $nd"; else box_line "Domain change had problems"; box_line "See the output above"; fi
      fi
      pause ;;
    03|3) while true; do
      echo "$BOX_EDGE"; box_center "SSL CERTIFICATE"; echo "$BOX_EDGE"
      _days=""; declare -F ssl_days_left >/dev/null 2>&1 && _days=$(ssl_days_left)
      box_line "Domain: $DOMAIN"
      box_line "Certificate days left: ${_days:-unknown}"
      box_line "1) Issue / Renew certificate"
      box_line "2) Force renew"
      box_line "3) Test auto-renew (dry run)"
      box_line "4) Re-apply cert to services"
      box_line "5) Certificate status"
      box_line "0) Back"
      echo "$BOX_EDGE"
      read -rp "Choose: " _sc
      case "$_sc" in
        1) bash "$SCRIPT_DIR/scripts/ssl.sh" issue; pause ;;
        2) bash "$SCRIPT_DIR/scripts/ssl.sh" force; pause ;;
        3) bash "$SCRIPT_DIR/scripts/ssl.sh" dry-run; pause ;;
        4) bash "$SCRIPT_DIR/scripts/ssl.sh" reapply; pause ;;
        5) bash "$SCRIPT_DIR/scripts/ssl.sh" status; pause ;;
        0) break ;;
        *) pause ;;
      esac
    done ;;
    04|4) while true; do
      echo "$BOX_EDGE"; box_center "MANAGE USERS"; echo "$BOX_EDGE"
      box_line "1) Add SSH Account"
      box_line "2) SSH: Manage (delete/renew/"
      box_line "   limits/reset/lock/kick)"
      box_line "3) VMess menu"
      box_line "4) VLESS menu"
      box_line "5) Trojan menu"
      box_line "6) List all users"
      box_line "7) Connected IPs"
      box_line "8) Banner change"
      box_line "0) Back"
      echo "$BOX_EDGE"
      read -rp "Choose: " s
      case "$s" in
        1) add_ssh_user ;;
        2) manage_ssh ;;
        3) xray_proto_menu vmess VMess ;;
        4) xray_proto_menu vless VLESS ;;
        5) xray_proto_menu trojan Trojan ;;
        6) list_users ;;
        7) show_connected all ;;
        8) read -rp "New SSH banner: " b; echo "$b" > "${CONFIG_DIR:-/etc/vps-manager}/banner.txt" 2>/dev/null || echo "$b" > "$DATA/banner.txt"; echo "Banner saved"; pause ;;
        0) break ;;
        *) pause ;;
      esac
    done ;;
    05|5) echo "$BOX_EDGE"; box_center "STATUS & PORTS"; echo "$BOX_EDGE"
      _ip=$(curl -s --max-time 5 https://api.ipify.org 2>/dev/null || hostname -I 2>/dev/null | awk '{print $1}')
      _loc=$(_vpsm server-location 2>/dev/null || echo "")
      box_line "Server IP : $_ip"
      [[ -n "$_loc" ]] && box_line "Location  : $_loc"
      box_line "Domain    : $DOMAIN"
      _days=""; declare -F ssl_days_left >/dev/null 2>&1 && _days=$(ssl_days_left)
      box_line "SSL days left: ${_days:-unknown}"
      _counts=$(python3 - "$DATA" <<'PY' 2>/dev/null || echo "? / ?"
import json, os, sys
def n(f):
    try: return len(json.load(open(os.path.join(sys.argv[1], f))))
    except Exception: return 0
print("%d / %d" % (n("ssh_users.json"), n("xray_users.json")))
PY
)
      box_line "Accounts SSH/Xray: $_counts"
      echo "$BOX_EDGE"
      for s in nginx haproxy xray dropbear ws-proxy vps-panel vps-monitor vps-bot vps-slowdns vps-zivpn vps-openvpn; do printf -v _sl '%-13s: %s' "$s" "$(service_status "$s")"; box_line "$_sl"; done
      _sd=$(python3 -c "import json;d=json.load(open('$DATA/config.json'));print(d.get('slowdns_domain',''))" 2>/dev/null || echo "")
      [[ -n "$_sd" ]] && box_line "SlowDNS NS: $_sd"
      box_line "ZiVPN UDP : 5667 (apps 6000-19999)"
      box_line "OpenVPN   : UDP 1194"
      echo "$BOX_EDGE"
      _up=""; for p in 22 80 443 109 143 8080 8443 8446 8447 9090; do ss -tln 2>/dev/null | grep -q "[:.]$p[[:space:]]" && _up="$_up $p"; done
      box_line "Key ports up:${_up:- none}"
      echo "$BOX_EDGE"; print_ports; pause ;;
    06|6) bash "$SCRIPT_DIR/scripts/fix-stack.sh"; pause ;;
    07|7) while true; do
      echo "$BOX_EDGE"; box_center "QOS / PING STABILITY"; echo "$BOX_EDGE"
      box_line "1) Apply QoS"
      box_line "2) QoS status"
      box_line "3) Per-user cap (Mbps)"
      box_line "4) Set interface / total rate"
      box_line "5) Remove QoS"
      box_line "0) Back"
      echo "$BOX_EDGE"
      read -rp "Choose: " _qc
      case "$_qc" in
        1) bash "$SCRIPT_DIR/scripts/qos.sh" apply || echo "QoS requires root"; pause ;;
        2) bash "$SCRIPT_DIR/scripts/qos.sh" status; pause ;;
        3) read -rp "Per-user cap in Mbps (0 = none): " _cap; [[ "$_cap" =~ ^[0-9]+$ ]] && bash "$SCRIPT_DIR/scripts/qos.sh" cap "$_cap" || echo "Invalid number"; pause ;;
        4) read -rp "Interface [eth0]: " iface; iface="${iface:-eth0}"; read -rp "Rate [950mbit]: " rate; rate="${rate:-950mbit}"; bash "$SCRIPT_DIR/scripts/qos.sh" "$iface" "$rate" || echo "QoS requires root"; pause ;;
        5) bash "$SCRIPT_DIR/scripts/qos.sh" off || echo "QoS requires root"; pause ;;
        0) break ;;
        *) pause ;;
      esac
    done ;;
    08|8) bash "$SCRIPT_DIR/scripts/speedtest.sh"; pause ;;
    09|9) bot_menu ;;
    10) while true; do
      _port=$(grep -o 'VPSMGR_PORT=[0-9]*' /etc/systemd/system/vps-panel.service 2>/dev/null | head -1 | cut -d= -f2); _port="${_port:-9090}"
      _ip=$(curl -s --max-time 5 https://api.ipify.org 2>/dev/null || hostname -I 2>/dev/null | awk '{print $1}')
      _cred=""; for _f in "/etc/vps-manager/credentials.json" "$DATA/credentials.json"; do [[ -f "$_f" ]] && { _cred="$_f"; break; }; done
      _user="admin"; _pass="(not found)"
      if [[ -n "$_cred" ]]; then mapfile -t _cu < <(python3 -c "import json,sys; d=json.load(open(sys.argv[1])); print(d.get('username','admin')); print(d.get('password','(not found)'))" "$_cred" 2>/dev/null); _user="${_cu[0]:-admin}"; _pass="${_cu[1]:-(not found)}"; fi
      echo "$BOX_EDGE"; box_center "WEB PANEL"; echo "$BOX_EDGE"
      box_line "Panel (HTTPS): https://$DOMAIN:$_port"
      box_line "Panel (IP): http://$_ip:$_port"
      box_line "User : $_user"
      box_line "Pass : $_pass"
      echo "$BOX_EDGE"
      box_line "1) Change panel port"
      box_line "2) Change panel login"
      box_line "3) Reset panel password"
      box_line "4) Panel logs"
      box_line "5) Reinstall panel (keep login)"
      box_line "6) Uninstall panel only"
      box_line "0) Back"
      echo "$BOX_EDGE"
      read -rp "Choose: " _pc
      case "$_pc" in
        1) read -rp "New panel port: " _np; [[ -n "$_np" ]] && bash "$SCRIPT_DIR/scripts/panel-admin.sh" port "$_np"; pause ;;
        2) read -rp "New panel username: " _nu; read -rp "New panel password: " _npw; [[ -n "$_nu" && -n "$_npw" ]] && bash "$SCRIPT_DIR/scripts/panel-admin.sh" login "$_nu" "$_npw"; pause ;;
        3) bash "$SCRIPT_DIR/scripts/panel-admin.sh" reset-password; pause ;;
        4) bash "$SCRIPT_DIR/scripts/panel-admin.sh" logs 30; pause ;;
        5) bash "$SCRIPT_DIR/scripts/panel-admin.sh" reinstall; pause ;;
        6) read -rp "Remove ONLY the panel service? [y/N]: " _cf; [[ "$_cf" == "y" || "$_cf" == "Y" ]] && bash "$SCRIPT_DIR/scripts/panel-admin.sh" uninstall; pause ;;
        0) break ;;
        *) pause ;;
      esac
    done ;;
    11) bash "$SCRIPT_DIR/uninstall.sh"; pause ;;
    12) echo "Goodbye!"; exit 0 ;;
    *) echo "Invalid option"; pause ;;
  esac
done
