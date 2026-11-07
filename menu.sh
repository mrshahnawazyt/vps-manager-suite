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
DOMAIN="$(get_domain)"; [[ -z "$DOMAIN" ]] && DOMAIN=$(python3 -c "import json;print(json.load(open('$DATA/config.json')).get('domain','example.com'))" 2>/dev/null || echo "example.com")

pause(){ read -rp "Enter dabao wapas menu ke liye..." _; }

add_ssh_user(){
  echo "--- Create SSH Account (tunnel-only) ---"
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
  python3 - "$DATA" "$u" "$pw" "$days" "$gb" "$ip" <<'PY'
import json,os,sys,datetime
data,u,pw,days,gb,ip=sys.argv[1:7]
days=int(days); gb=int(gb); ip=int(ip)
created=datetime.date.today()
rec={"username":u,"password":pw,"expiry_days":days,"gb_limit":gb,"ip_limit":ip,"used_gb":0,"created":created.isoformat(),"expires":(created+datetime.timedelta(days=days)).isoformat()}
p=os.path.join(data,"ssh_users.json"); arr=json.load(open(p)) if os.path.exists(p) else []
arr.append(rec); json.dump(arr,open(p,"w"),indent=2)
print(f"Created: {u} | Expiry: {rec['expires']} | Data: {'Unlimited' if gb==0 else str(gb)+' GB'} | IP Limit: {'Unlimited' if ip==0 else ip}")
PY
  echo "Payload: GET /ws HTTP/1.1[crlf]Host: $DOMAIN[crlf]Upgrade: websocket[crlf][crlf]"
  log_activity "menu ssh create $u days=$days gb=$gb ip=$ip"; pause
}

add_xray_user(){
  echo "--- Create Xray Account ---"
  read -rp "Protocol (VMess/VLESS/Trojan) [VLESS]: " proto; proto="${proto:-VLESS}"
  read -rp "Remark/Naam: " remark; [[ -z "$remark" ]] && return
  read -rp "Days [30]: " days; days="${days:-30}"
  read -rp "GB limit (0 = unlimited) [0]: " gb; gb="${gb:-0}"
  read -rp "IP limit (0 = unlimited) [0]: " ip; ip="${ip:-0}"
  python3 - "$DATA" "$proto" "$remark" "$days" "$gb" "$ip" "$DOMAIN" <<'PY'
import json,os,sys,uuid,base64,datetime
data,proto,remark,days,gb,ip,domain=sys.argv[1:8]
days=int(days); gb=int(gb); ip=int(ip); uid=str(uuid.uuid4()); proto_l=proto.lower()
if proto_l=="vless": link=f"vless://{uid}@{domain}:443?encryption=none&security=tls&type=ws&host={domain}&path=%2Fvless#{remark}"
elif proto_l=="trojan": link=f"trojan://{uid}@{domain}:443?security=tls&type=ws&host={domain}&path=%2Ftrojan#{remark}"
else:
    vm={"v":"2","ps":remark,"add":domain,"port":"443","id":uid,"aid":"0","net":"ws","type":"none","host":domain,"path":"/vmess","tls":"tls"}
    link="vmess://"+base64.b64encode(json.dumps(vm).encode()).decode()
created=datetime.date.today()
rec={"id":uid,"protocol":proto_l,"remark":remark,"link":link,"expiry_days":days,"gb_limit":gb,"ip_limit":ip,"used_gb":0,"created":created.isoformat(),"expires":(created+datetime.timedelta(days=days)).isoformat()}
p=os.path.join(data,"xray_users.json"); arr=json.load(open(p)) if os.path.exists(p) else []
arr.append(rec); json.dump(arr,open(p,"w"),indent=2)
print(link); print(f"Expiry: {rec['expires']} | Data: {'Unlimited' if gb==0 else str(gb)+' GB'} | IP Limit: {'Unlimited' if ip==0 else ip}")
PY
  pause
}

list_users(){
  echo "--- SSH Accounts ---"
  python3 - "$DATA/ssh_users.json" <<'PY' 2>/dev/null || echo "koi SSH user nahi"
import json,sys
for u in json.load(open(sys.argv[1])):
    print(f"{u['username']} | Expiry: {u.get('expires','?')} ({u.get('expiry_days',30)} days) | Data: {'Unlimited' if int(u.get('gb_limit') or 0)==0 else u.get('gb_limit')} | IP Limit: {'Unlimited' if int(u.get('ip_limit') or 0)==0 else u.get('ip_limit')}")
PY
  echo "--- Xray Accounts ---"
  python3 - "$DATA/xray_users.json" <<'PY' 2>/dev/null || echo "koi Xray user nahi"
import json,sys
for u in json.load(open(sys.argv[1])):
    print(f"{u['protocol']} {u.get('remark')} | Expiry: {u.get('expires','?')} | Data: {'Unlimited' if int(u.get('gb_limit') or 0)==0 else u.get('gb_limit')} | IP Limit: {'Unlimited' if int(u.get('ip_limit') or 0)==0 else u.get('ip_limit')}")
PY
  pause
}

while true; do
  clear 2>/dev/null || true
  banner "VPS Manager Suite v1.0 — Menu"
  echo " Domain: $DOMAIN   Data: $DATA"
  echo " 01) Install All Components        07) Ping Stability (QoS / CAKE)"
  echo " 02) Add / Change Domain           08) VPS Stats / Speedtest"
  echo " 03) SSL Certificate (issue/renew) 09) Telegram Bot"
  echo " 04) Manage Users / Limits / Banner 10) Web Panel (link/login/port)"
  echo " 05) Status & Ports                11) Uninstall"
  echo " 06) Fix SSH-WS / SSL Stack        12) Exit"
  echo " Note: Days = literal (default 30) | GB limit (0 = unlimited) | IP limit (0 = unlimited)"
  read -rp "Option chuno [01-12]: " opt
  case "$opt" in
    01|1) bash "$SCRIPT_DIR/install.sh" "$DOMAIN"; pause ;;
    02|2) read -rp "Naya domain: " nd; [[ -n "$nd" ]] && { DOMAIN="$nd"; save_config_key domain "$nd" 2>/dev/null || true; python3 -c "import json,os; p='$DATA/config.json'; d=json.load(open(p)) if os.path.exists(p) else {}; d['domain']='$nd'; json.dump(d,open(p,'w'),indent=2)"; dns_check "$nd" || true; }; pause ;;
    03|3) echo "SSL: certbot try kar raha hoon (fallback self-signed install me hai)"; command -v certbot >/dev/null && certbot certonly --standalone -d "$DOMAIN" --non-interactive --agree-tos -m "admin@$DOMAIN" || echo "certbot nahi / fail — install.sh wala self-signed use hoga"; pause ;;
    04|4) echo "a) Add SSH  b) Add Xray  c) List Users  d) Banner change"; read -rp "Chuno: " s; case "$s" in a) add_ssh_user;; b) add_xray_user;; c) list_users;; d) read -rp "Naya SSH banner: " b; echo "$b" > "${CONFIG_DIR:-/etc/vps-manager}/banner.txt" 2>/dev/null || echo "$b" > "$DATA/banner.txt"; echo "Banner saved";; *) pause;; esac ;;
    05|5) for s in nginx haproxy xray dropbear ws-proxy vps-panel; do echo "$s: $(service_status $s)"; done; print_ports; pause ;;
    06|6) bash "$SCRIPT_DIR/scripts/fix-stack.sh"; pause ;;
    07|7) read -rp "Interface [eth0]: " iface; iface="${iface:-eth0}"; read -rp "Rate [950mbit]: " rate; rate="${rate:-950mbit}"; bash "$SCRIPT_DIR/scripts/qos.sh" "$iface" "$rate" || echo "QoS ke liye root chahiye"; pause ;;
    08|8) bash "$SCRIPT_DIR/scripts/speedtest.sh"; pause ;;
    09|9) echo "Telegram Bot: Panel > Telegram Bot me token + admin ID save karo, phir: python3 scripts/telegram-bot.py"; pause ;;
    10) _ip=$(curl -s --max-time 5 https://api.ipify.org 2>/dev/null || hostname -I 2>/dev/null | awk '{print $1}'); _cred=""
      for _f in "/etc/vps-manager/credentials.json" "$DATA/credentials.json"; do [[ -f "$_f" ]] && { _cred="$_f"; break; }; done
      echo " --- WEB PANEL ---"
      echo " Panel : https://$DOMAIN:9090  |  http://$_ip:9090"
      if [[ -n "$_cred" ]]; then
        python3 - "$_cred" <<'PY'
import json,sys
try:
    d=json.load(open(sys.argv[1]))
    print(f" User  : {d.get('username','admin')}")
    print(f" Pass  : {d.get('password','(nahi mila)')}")
except Exception:
    print(" User  : admin"); print(" Pass  : (credentials file parh nahi paya)")
PY
      else
        echo " User  : admin"; echo " Pass  : (credentials file nahi mili — pehle option 01 Install chalao)"
      fi
      echo " Port  : 9090"; pause ;;
    11) bash "$SCRIPT_DIR/uninstall.sh"; pause ;;
    12) echo "Allah Hafiz! Menu band."; exit 0 ;;
    *) echo "Galat option"; pause ;;
  esac
done
