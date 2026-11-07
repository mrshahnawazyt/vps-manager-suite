#!/usr/bin/env bash
# VPS Manager Suite v1.0 - common library (original code)
# Shared helpers used by install.sh, menu.sh and scripts/

CONFIG_DIR="${VPSMGR_CONFIG_DIR:-/etc/vps-manager}"
DATA_DIR="${VPSMGR_DATA_DIR:-/var/lib/vps-manager}"
LOG_FILE="${CONFIG_DIR}/activity.log"
CREDS_FILE="${CONFIG_DIR}/credentials.json"
CONFIG_FILE="${CONFIG_DIR}/config.json"

# Colors
RED='\033[0;31m'; GREEN='\033[0;32m'; YELLOW='\033[1;33m'
CYAN='\033[0;36m'; BLUE='\033[0;34m'; NC='\033[0m'

info()  { echo -e "${CYAN}[INFO]${NC} $*"; }
ok()    { echo -e "${GREEN}[OK]${NC} $*"; }
warn()  { echo -e "${YELLOW}[WARN]${NC} $*"; }
err()   { echo -e "${RED}[ERROR]${NC} $*" >&2; }
banner(){ echo -e "${BLUE}==============================================${NC}"; echo -e "  $*"; echo -e "${BLUE}==============================================${NC}"; }

require_root() {
  if [[ "${EUID:-$(id -u)}" -ne 0 ]]; then err "Root is required. Run with sudo: sudo bash $0"; exit 1; fi
}

check_os() {
  if [[ -f /etc/os-release ]]; then . /etc/os-release; else err "Could not detect the OS"; exit 1; fi
  if [[ "${ID:-}" != "ubuntu" ]]; then
    warn "This installer is made for Ubuntu"
    warn "Detected: ${PRETTY_NAME:-unknown}"
    warn "Continuing anyway - some steps"
    warn "may fail."
    return 0
  fi
  local major="${VERSION_ID%%.*}"
  if [[ "$major" =~ ^[0-9]+$ ]] && (( major < 20 )); then
    warn "Ubuntu $VERSION_ID is very old"
    warn "Versions older than 20.04 may be"
    warn "missing some packages."
  else
    info "OS: Ubuntu ${VERSION_ID} (supported)"
  fi
}

gen_password() { head -c 18 /dev/urandom | base64 | tr -dc 'A-Za-z0-9' | head -c 16; echo; }
gen_uuid() {
  if command -v uuidgen >/dev/null 2>&1; then uuidgen | tr 'A-Z' 'a-z';
  elif [[ -f /proc/sys/kernel/random/uuid ]]; then cat /proc/sys/kernel/random/uuid;
  else python3 -c "import uuid;print(uuid.uuid4())"; fi
}

ensure_dirs() { mkdir -p "$CONFIG_DIR" "$DATA_DIR" "$CONFIG_DIR/ssl" 2>/dev/null || true; touch "$LOG_FILE" 2>/dev/null || true; }

log_activity() { ensure_dirs; echo "[$(date -Is)] $*" >> "$LOG_FILE" 2>/dev/null || true; }

get_domain() {
  if [[ -f "$CONFIG_FILE" ]] && command -v python3 >/dev/null; then
    python3 -c "import json,sys; d=json.load(open('$CONFIG_FILE')); print(d.get('domain',''))" 2>/dev/null || echo ""
  fi
}

save_config_key() {
  # save_config_key key value
  ensure_dirs
  python3 - "$CONFIG_FILE" "$1" "$2" <<'PY'
import json,sys,os
path,key,val=sys.argv[1],sys.argv[2],sys.argv[3]
data={}
if os.path.exists(path):
    try: data=json.load(open(path))
    except: data={}
data[key]=val
json.dump(data,open(path,'w'),indent=2)
PY
}

dns_check() {
  local domain="$1" ip=""
  ip=$(curl -s --max-time 5 https://api.ipify.org || curl -s --max-time 5 ifconfig.me || echo "")
  local resolved=""
  resolved=$(getent hosts "$domain" | awk '{print $1}' | head -n1 || true)
  echo "Server IP : ${ip:-unknown}"
  echo "Domain    : $domain -> ${resolved:-not resolved}"
  if [[ -n "$ip" && -n "$resolved" && "$ip" != "$resolved" ]]; then
    warn "DNS mismatch!"
    warn "Point the A record to this IP:"
    echo "  $ip  (otherwise SSL will fail)"
    return 1
  fi
  [[ -n "$resolved" ]] && ok "DNS looks correct." || warn "The domain is not resolving yet."
  return 0
}

# --- SSL: Let's Encrypt (standalone, port 80) or self-signed fallback -------
# HTTP-01 needs port 80 free, so nginx/haproxy are stopped before certbot runs
# and started again afterwards. (The old --http-01-port 8899 approach never
# worked - ACME always arrives on port 80.)
issue_ssl() {
  local domain="$1" le_dir="/etc/letsencrypt/live/$1"
  ensure_dirs; mkdir -p "$CONFIG_DIR/ssl"
  if command -v systemctl >/dev/null 2>&1; then
    systemctl stop haproxy nginx 2>/dev/null || true
  fi
  local got=""
  if command -v certbot >/dev/null 2>&1; then
    if certbot certonly --standalone -d "$domain" --non-interactive --agree-tos -m "admin@$domain" >/dev/null 2>&1 \
       && [[ -s "$le_dir/fullchain.pem" && -s "$le_dir/privkey.pem" ]]; then
      cp "$le_dir/fullchain.pem" "$CONFIG_DIR/ssl/fullchain.pem" || true
      cp "$le_dir/privkey.pem" "$CONFIG_DIR/ssl/privkey.pem" || true
      got="letsencrypt"
    fi
  fi
  if [[ -z "$got" ]]; then
    warn "Let's Encrypt failed (DNS/port 80)"
    warn "Creating a self-signed fallback"
    if command -v openssl >/dev/null 2>&1; then
      openssl req -x509 -newkey rsa:2048 -keyout "$CONFIG_DIR/ssl/privkey.pem" \
        -out "$CONFIG_DIR/ssl/fullchain.pem" -days 365 -nodes -subj "/CN=$domain" >/dev/null 2>&1 || true
    fi
    got="self-signed"
  fi
  # Verify: both files must exist and be non-empty, otherwise Xray/Panel will not start
  if [[ -s "$CONFIG_DIR/ssl/fullchain.pem" && -s "$CONFIG_DIR/ssl/privkey.pem" ]]; then
    chmod 600 "$CONFIG_DIR/ssl/privkey.pem" 2>/dev/null || true
    cat "$CONFIG_DIR/ssl/fullchain.pem" "$CONFIG_DIR/ssl/privkey.pem" > "$CONFIG_DIR/ssl/combined.pem" 2>/dev/null || true
    ok "SSL ready ($got)"
    echo "Cert: $CONFIG_DIR/ssl/fullchain.pem"
  else
    err "SSL FAILED: certificate files were not created!"
    echo "The ssl folder is empty - Xray/Panel will not start"
    got=""
  fi
  if command -v systemctl >/dev/null 2>&1; then
    for s in haproxy nginx xray vps-panel; do systemctl try-restart "$s" 2>/dev/null || true; done
  fi
  [[ -n "$got" ]]
}

service_status() {
  local svc="$1"
  if command -v systemctl >/dev/null 2>&1 && systemctl is-active --quiet "$svc" 2>/dev/null; then echo "active"; else
    if pgrep -x "$svc" >/dev/null 2>&1; then echo "active (process)"; else echo "inactive"; fi
  fi
}

print_ports() {
  echo "--- Listening Ports ---"
  if command -v ss >/dev/null 2>&1; then ss -tulnp 2>/dev/null | head -n 60; else netstat -tulnp 2>/dev/null | head -n 60 || echo "ss/netstat not found"; fi
}

is_port_listening() { ss -tuln 2>/dev/null | grep -q ":$1 " || netstat -tuln 2>/dev/null | grep -q ":$1 "; }

# --- LIMIT SEMANTICS (user-confirmed, EXACT) --------------------------------
# Days / Expiry:   LITERAL count only. Default 30. Expiry = creation + Days. 0 = 0 days (NOT unlimited).
# GB limit:        0 = Unlimited. UI label: "GB limit (0 = unlimited)". >0 = enforce GB quota.
# IP limit:        0 = Unlimited. UI label: "IP limit (0 = unlimited)". >0 = enforce max IPs/logins.
# Enforcement MUST skip GB/IP checks when 0; expiry always literal.
fmt_expiry()  { local d="${1:-30}"; echo "${d} days"; }
fmt_data()    { local v="${1:-0}"; [[ "$v" == "0" ]] && echo "Unlimited" || echo "${v} GB"; }
fmt_logins()  { local v="${1:-0}"; [[ "$v" == "0" ]] && echo "Unlimited" || echo "${v}"; }
fmt_limit()   { local v="${1:-0}"; [[ "$v" == "0" ]] && echo "Unlimited" || echo "${v}"; }
expires_on() {
  # expires_on <created YYYY-MM-DD> <expiry_days>
  local created="${1:-}" days="${2:-30}"
  date -d "${created} +${days} days" +%F 2>/dev/null || echo "+${days} days"
}
is_expired() {
  # is_expired <created> <expiry_days> -> literal check. Days=0 => expiry = creation date.
  local days="${2:-30}"
  local exp; exp=$(date -d "${1} +${days} days" +%s 2>/dev/null) || return 1
  [[ "$(date +%s)" -gt "$exp" ]]
}
quota_exceeded() {
  # quota_exceeded <used_gb> <limit_gb> -> skip when limit 0
  local lim="${2:-0}"; [[ "$lim" == "0" ]] && return 1
  awk -v u="${1:-0}" -v l="$lim" 'BEGIN{exit !(u>=l)}'
}
ip_limit_exceeded() {
  # ip_limit_exceeded <active_count> <limit> -> skip when limit 0
  local lim="${2:-0}"; [[ "$lim" == "0" ]] && return 1
  [[ "${1:-0}" -gt "$lim" ]]
}
# Summary: Days literal, GB/IP 0=Unlimited. e.g. Days 30, GB 0, IP 0 -> Expiry date + Data: Unlimited | IP: Unlimited
limit_summary() {
  echo "Days: $(fmt_expiry "${1:-30}") | Data: $(fmt_data "${2:-0}") | IP Limit: $(fmt_logins "${3:-0}")"
}
