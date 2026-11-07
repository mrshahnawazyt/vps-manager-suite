#!/usr/bin/env bash
# VPS Manager Suite v1.0 - Terminal Menu (original code)
# Limit rule (corrected, video-verified):
#   Days = literal count, default 30. Expiry = creation date + Days. (0 = 0 days, NOT unlimited)
#   GB limit (0 = unlimited) | IP limit (0 = unlimited)
set -u
# Symlink (/usr/local/bin/vpsmenu) se chale to asal file ka folder lo
SCRIPT_DIR="$(cd "$(dirname "$(readlink -f "${BASH_SOURCE[0]}")")" && pwd)"
# common.sh dhoondo: script ke paas, warna /opt wale suite folder me
for _lib in "$SCRIPT_DIR/lib/common.sh" "/opt/vps-manager-suite/lib/common.sh"; do
  [[ -f "$_lib" ]] && { source "$_lib"; break; }
done
# Agar phir bhi common.sh na mile (akeli copy chal rahi ho) to basic functions
# yahi bana lo taaki menu "command not found" diye baghair chale
if ! declare -F banner >/dev/null 2>&1; then
  banner(){ echo "=== $* ==="; }
  info(){ echo "[INFO] $*"; }; warn(){ echo "[!] $*"; }; err(){ echo "[X] $*" >&2; }; ok(){ echo "[OK] $*"; }
  gen_password(){ tr -dc 'A-Za-z0-9' </dev/urandom 2>/dev/null | head -c 14; echo; }
  get_domain(){ python3 -c "import json;print(json.load(open('${VPSMGR_DATA_DIR:-/var/lib/vps-manager}/config.json')).get('domain',''))" 2>/dev/null || echo ""; }
  save_config_key(){ :; }; dns_check(){ echo "DNS check skip (common.sh nahi mila)"; return 0; }
  service_status(){ systemctl is-active "$1" 2>/dev/null || echo "unknown"; }
  print_ports(){ ss -tulnp 2>/dev/null || netstat -tulnp 2>/dev/null || echo "ports nahi dikhe"; }
  log_activity(){ :; }
fi
DATA="${VPSMGR_DATA_DIR:-/var/lib/vps-manager}"
[[ -d "$DATA" ]] || DATA="$SCRIPT_DIR/data"
mkdir -p "$DATA" 2>/dev/null || true
DOMAIN="$(get_domain)"; [[ -z "$DOMAIN" ]] && DOMAIN=$(python3 -c "import json;print(json.load(open('$DATA/config.json')).get('domain',''))" 2>/dev/null || echo "")

pause(){ read -rp "Enter dabao wapas menu ke liye..." _; }

# --- Display helpers: single column, har line max 46 chars (phone terminal) ---
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
  # Real VPS par tunnel-only system user:
  if [[ "${EUID:-$(id -u)}" -eq 0 ]] && command -v useradd >/dev/null; then
    id "$u" >/dev/null 2>&1 || useradd -M -s /usr/sbin/nologin -e "$(date -d "+${days} days" +%F)" "$u" 2>/dev/null || warn "useradd skip/fail"
    echo "$u:$pw" | chpasswd 2>/dev/null || true
  fi
  _out=$(python3 - "$DATA" "$u" "$pw" "$days" "$gb" "$ip" <<'PY'
import json,os,sys,datetime
data,u,pw,days,gb,ip=sys.argv[1:7]
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
PY
)
  echo "$BOX_EDGE"; box_center "SSH ACCOUNT READY"; echo "$BOX_EDGE"
  while IFS= read -r _l; do [[ -n "$_l" ]] && box_line "$_l"; done <<< "$_out"
  echo "$BOX_EDGE"
  echo "Payload (copy):"
  echo "GET /sshws HTTP/1.1[crlf]Host: $DOMAIN[crlf]Upgrade: websocket[crlf][crlf]"
  log_activity "menu ssh create $u days=$days gb=$gb ip=$ip"; pause
}

add_xray_user(){
  echo "$BOX_EDGE"; box_center "Create Xray Account"; echo "$BOX_EDGE"
  read -rp "Protocol (VMess/VLESS/Trojan) [VLESS]: " proto; proto="${proto:-VLESS}"
  read -rp "Remark/Naam: " remark; [[ -z "$remark" ]] && return
  read -rp "Days [30]: " days; days="${days:-30}"
  read -rp "GB limit (0 = unlimited) [0]: " gb; gb="${gb:-0}"
  read -rp "IP limit (0 = unlimited) [0]: " ip; ip="${ip:-0}"
  _out=$(python3 - "$DATA" "$proto" "$remark" "$days" "$gb" "$ip" "$DOMAIN" <<'PY'
import json,os,sys,uuid,base64,datetime
data,proto,remark,days,gb,ip,domain=sys.argv[1:8]
days=int(days); gb=int(gb); ip=int(ip); uid=str(uuid.uuid4()); proto_l=proto.lower()
if proto_l=="vless":
    links={
      "Link TLS": f"vless://{uid}@{domain}:443?encryption=none&security=tls&type=ws&host={domain}&path=%2Fvless&sni={domain}#{remark}",
      "Link NTLS": f"vless://{uid}@{domain}:80?encryption=none&security=none&type=ws&host={domain}&path=%2Fvless#{remark}",
      "Link TCP": f"vless://{uid}@{domain}:8443?encryption=none&security=tls&type=tcp&headerType=none&sni={domain}#{remark}-tcp",
      "Link XHTTP TLS": f"vless://{uid}@{domain}:443?encryption=none&security=tls&type=xhttp&path=%2Fxhttp&host={domain}&mode=auto&sni={domain}#{remark}-xhttp-tls",
      "Link XHTTP NTLS": f"vless://{uid}@{domain}:80?encryption=none&security=none&type=xhttp&path=%2Fxhttp&host={domain}&mode=auto#{remark}-xhttp-ntls",
    }
elif proto_l=="trojan":
    links={"Link TLS": f"trojan://{uid}@{domain}:443?security=tls&type=ws&host={domain}&path=%2Ftrojan&sni={domain}#{remark}",
           "Link NTLS": f"trojan://{uid}@{domain}:80?security=none&type=ws&host={domain}&path=%2Ftrojan#{remark}"}
else:
    def _vm(port,tls_on):
        vm={"v":"2","ps":remark,"add":domain,"port":str(port),"id":uid,"aid":"0","net":"ws","type":"none","host":domain,"path":"/vmess","tls":"tls" if tls_on else ""}
        return "vmess://"+base64.b64encode(json.dumps(vm).encode()).decode()
    links={"Link TLS": _vm(443,True), "Link NTLS": _vm(80,False)}
link=links.get("Link TLS","")
created=datetime.date.today()
rec={"id":uid,"protocol":proto_l,"remark":remark,"link":link,"links":links,"expiry_days":days,"gb_limit":gb,"ip_limit":ip,"used_gb":0,"created":created.isoformat(),"expires":(created+datetime.timedelta(days=days)).isoformat()}
p=os.path.join(data,"xray_users.json"); arr=json.load(open(p)) if os.path.exists(p) else []
arr.append(rec); json.dump(arr,open(p,"w"),indent=2)
print(f"Remark : {remark}")
print(f"Proto  : {proto_l}")
print(f"Expiry : {rec['expires']}")
print(f"Data   : {'Unlimited' if gb==0 else str(gb)+' GB'}")
print(f"IP Lim : {'Unlimited' if ip==0 else ip}")
print("==LINKS==")
for k,v in links.items(): print(f"{k}: {v}")
PY
)
  echo "$BOX_EDGE"; box_center "XRAY ACCOUNT READY"; echo "$BOX_EDGE"
  _inlinks=0
  while IFS= read -r _l; do
    if [[ "$_l" == "==LINKS==" ]]; then _inlinks=1; echo "$BOX_EDGE"; echo "Links (copy):"; continue; fi
    if [[ $_inlinks -eq 0 ]]; then [[ -n "$_l" ]] && box_line "$_l"; else echo "$_l"; fi
  done <<< "$_out"
  # Account ko asal Xray config me bhi daalo (sirf record se kaam nahi chalta)
  python3 "$SCRIPT_DIR/scripts/sync-xray.py" --restart 2>/dev/null || echo "(xray sync baad me hoga — VPS par /opt se chalao)"
  pause
}

list_users(){
  echo "$BOX_EDGE"; box_center "SSH ACCOUNTS"; echo "$BOX_EDGE"
  _out=$(python3 - "$DATA/ssh_users.json" <<'PY' 2>/dev/null
import json,sys
try: users=json.load(open(sys.argv[1]))
except Exception: users=[]
for i,u in enumerate(users):
    if i: print("")
    print(u['username'])
    print(f"  Expiry : {u.get('expires','?')} ({u.get('expiry_days',30)} days)")
    print(f"  Data   : {'Unlimited' if int(u.get('gb_limit') or 0)==0 else u.get('gb_limit')}")
    print(f"  IP Lim : {'Unlimited' if int(u.get('ip_limit') or 0)==0 else u.get('ip_limit')}")
PY
)
  [[ -z "$_out" ]] && _out="koi SSH user nahi"
  while IFS= read -r _l; do box_line "$_l"; done <<< "$_out"
  echo "$BOX_EDGE"; box_center "XRAY ACCOUNTS"; echo "$BOX_EDGE"
  _out=$(python3 - "$DATA/xray_users.json" <<'PY' 2>/dev/null
import json,sys
try: users=json.load(open(sys.argv[1]))
except Exception: users=[]
for i,u in enumerate(users):
    if i: print("")
    print(f"{u['protocol']} {u.get('remark')}")
    print(f"  Expiry : {u.get('expires','?')} ({u.get('expiry_days',30)} days)")
    print(f"  Data   : {'Unlimited' if int(u.get('gb_limit') or 0)==0 else u.get('gb_limit')}")
    print(f"  IP Lim : {'Unlimited' if int(u.get('ip_limit') or 0)==0 else u.get('ip_limit')}")
PY
)
  [[ -z "$_out" ]] && _out="koi Xray user nahi"
  while IFS= read -r _l; do box_line "$_l"; done <<< "$_out"
  echo "$BOX_EDGE"
  pause
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
  read -rp "Option chuno [01-12]: " opt
  case "$opt" in
    01|1) bash "$SCRIPT_DIR/install.sh" "$DOMAIN"; pause ;;
    02|2) read -rp "Naya domain: " nd; [[ -n "$nd" ]] && { DOMAIN="$nd"; save_config_key domain "$nd" 2>/dev/null || true; python3 -c "import json,os; p='$DATA/config.json'; d=json.load(open(p)) if os.path.exists(p) else {}; d['domain']='$nd'; json.dump(d,open(p,'w'),indent=2)"; echo "$BOX_EDGE"; box_line "Domain set: $nd"; echo "$BOX_EDGE"; dns_check "$nd" || true; }; pause ;;
    03|3) echo "$BOX_EDGE"; box_center "SSL CERTIFICATE"; echo "$BOX_EDGE"
      if declare -F issue_ssl >/dev/null 2>&1; then
        if issue_ssl "$DOMAIN"; then box_line "SSL ho gaya"; box_line "Panel/Xray restart ho gaye"; else box_line "SSL fail - error upar dekho"; box_line "DNS A record is VPS par hai?"; fi
      else box_line "issue_ssl nahi mila"; box_line "Pehle vpsupdate chalao"; fi
      echo "$BOX_EDGE"; pause ;;
    04|4) echo "$BOX_EDGE"; box_center "MANAGE USERS"; echo "$BOX_EDGE"; box_line "a) Add SSH Account"; box_line "b) Add Xray Account"; box_line "c) List Users"; box_line "d) Banner Change"; echo "$BOX_EDGE"; read -rp "Chuno: " s; case "$s" in a) add_ssh_user;; b) add_xray_user;; c) list_users;; d) read -rp "Naya SSH banner: " b; echo "$b" > "${CONFIG_DIR:-/etc/vps-manager}/banner.txt" 2>/dev/null || echo "$b" > "$DATA/banner.txt"; echo "Banner saved";; *) pause;; esac ;;
    05|5) echo "$BOX_EDGE"; box_center "STATUS & PORTS"; echo "$BOX_EDGE"; for s in nginx haproxy xray dropbear ws-proxy vps-panel; do printf -v _sl '%-10s: %s' "$s" "$(service_status "$s")"; box_line "$_sl"; done; echo "$BOX_EDGE"; print_ports; pause ;;
    06|6) bash "$SCRIPT_DIR/scripts/fix-stack.sh"; pause ;;
    07|7) read -rp "Interface [eth0]: " iface; iface="${iface:-eth0}"; read -rp "Rate [950mbit]: " rate; rate="${rate:-950mbit}"; bash "$SCRIPT_DIR/scripts/qos.sh" "$iface" "$rate" || echo "QoS ke liye root chahiye"; pause ;;
    08|8) bash "$SCRIPT_DIR/scripts/speedtest.sh"; pause ;;
    09|9) echo "$BOX_EDGE"; box_center "TELEGRAM BOT"; echo "$BOX_EDGE"; box_line "Panel > Telegram Bot me token"; box_line "+ admin ID save karo, phir:"; box_line "python3 scripts/telegram-bot.py"; echo "$BOX_EDGE"; pause ;;
    10) _ip=$(curl -s --max-time 5 https://api.ipify.org 2>/dev/null || hostname -I 2>/dev/null | awk '{print $1}'); _cred=""
      for _f in "/etc/vps-manager/credentials.json" "$DATA/credentials.json"; do [[ -f "$_f" ]] && { _cred="$_f"; break; }; done
      _user="admin"; _pass="(nahi mila)"
      if [[ -n "$_cred" ]]; then
        mapfile -t _cu < <(python3 - "$_cred" <<'PY'
import json,sys
try:
    d=json.load(open(sys.argv[1]))
    print(d.get('username','admin'))
    print(d.get('password','(nahi mila)'))
except Exception:
    print("admin"); print("(credentials file parh nahi paya)")
PY
)
        _user="${_cu[0]:-admin}"; _pass="${_cu[1]:-(nahi mila)}"
      else
        _pass="(pehle option 01 Install chalao)"
      fi
      echo "$BOX_EDGE"
      box_center "WEB PANEL"
      echo "$BOX_EDGE"
      box_line "Panel (HTTPS):"
      box_line "https://$DOMAIN:9090"
      box_line "Panel (IP):"
      box_line "http://$_ip:9090"
      echo "$BOX_EDGE"
      box_line "User : $_user"
      box_line "Pass : $_pass"
      box_line "Port : 9090"
      echo "$BOX_EDGE"; pause ;;
    11) bash "$SCRIPT_DIR/uninstall.sh"; pause ;;
    12) echo "Allah Hafiz! Menu band."; exit 0 ;;
    *) echo "Galat option"; pause ;;
  esac
done
