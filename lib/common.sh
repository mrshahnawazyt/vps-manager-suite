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
  if [[ "$major" =~ ^[0-9]+$ ]] && (( major < 18 )); then
    warn "Ubuntu $VERSION_ID is not supported"
    warn "This suite needs Ubuntu 18.04 or newer."
  elif [[ "$major" =~ ^[0-9]+$ ]] && (( major == 18 )); then
    info "OS: Ubuntu ${VERSION_ID} (supported)"
    info "Note: CAKE QoS on 18.04 needs the HWE"
    info "kernel - the stock 4.15 kernel has"
    info "no sch_cake, so fq_codel is used there."
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

# HAProxy terminates TLS on port 443, and HAProxy loads its certificate
# as ONE bundle file (cert + key). Rebuild that bundle from the suite
# cert pair whenever the pair is (re)issued, so HAProxy never serves a
# stale certificate after an install or a renewal.
build_haproxy_pem() {
  if [[ -s "$CONFIG_DIR/ssl/fullchain.pem" && -s "$CONFIG_DIR/ssl/privkey.pem" ]]; then
    cat "$CONFIG_DIR/ssl/fullchain.pem" "$CONFIG_DIR/ssl/privkey.pem" > "$CONFIG_DIR/ssl/haproxy.pem" 2>/dev/null || return 1
    chmod 600 "$CONFIG_DIR/ssl/haproxy.pem" 2>/dev/null || true
    return 0
  fi
  return 1
}

# --- Front-door (haproxy/nginx) restore safety ------------------------------
# certbot --standalone needs port 80 free, so issue_ssl() and ssl_dry_run()
# STOP haproxy and nginx while it runs. Those two MUST come back on every
# exit path afterwards. An earlier version "restored" them at the end with
# `systemctl try-restart` - but try-restart only restarts a unit that is
# ALREADY running and is a silent no-op (exit 0, no journal entry) on a
# stopped one. So the two services that had just been stopped were exactly
# the two that never started again: every SSL issue through this path left
# HAProxy and nginx down with no error anywhere (live incident 2026-11-07:
# HAProxy `inactive` with a valid config and certificate; the journal
# showed the clean stop and no start attempt after it).
# web_stack_restore() uses a real `restart` (which also starts stopped
# units) and then VERIFIES with is-active, printing a loud [ERROR] with
# the exact diagnose commands when a front door is still down.
web_stack_verify() {
  # web_stack_verify [service ...]   (default: haproxy nginx)
  command -v systemctl >/dev/null 2>&1 || return 0
  local svcs="${*:-haproxy nginx}" s bad=0
  for s in $svcs; do
    if ! systemctl is-active --quiet "$s" 2>/dev/null; then
      err "$s is NOT running - diagnose: systemctl status $s  |  journalctl -u $s -n 30 --no-pager"
      bad=1
    fi
  done
  return $bad
}

web_stack_restore() {
  command -v systemctl >/dev/null 2>&1 || return 0
  local s
  for s in haproxy nginx; do systemctl restart "$s" 2>/dev/null || true; done
  web_stack_verify haproxy nginx
}

issue_ssl() {
  local domain="$1" force="${2:-}"
  ensure_dirs; mkdir -p "$CONFIG_DIR/ssl"
  # The body stops haproxy+nginx (certbot needs port 80) and must bring
  # them back on EVERY way out - success, failure, or interrupt - so it
  # runs in a subshell whose EXIT trap performs the restore. The trap
  # lives inside the subshell, so it never clobbers the calling script's
  # own traps (install.sh and update.sh have their own EXIT traps).
  # The explicit INT/TERM traps matter: a shell killed by Ctrl-C while
  # it waits for certbot does NOT run its EXIT trap, so the signals get
  # their own handlers (they run as soon as certbot's process group is
  # gone) and the restore still happens.
  (
  trap 'web_stack_restore' EXIT
  trap 'web_stack_restore; exit 130' INT
  trap 'web_stack_restore; exit 143' TERM
  local le_dir="/etc/letsencrypt/live/$domain"
  if command -v systemctl >/dev/null 2>&1; then
    systemctl stop haproxy nginx 2>/dev/null || true
  fi
  local got="" le_log="/var/log/vps-manager-certbot.log" le_reason=0
  local force_flag=""; [[ "$force" == "force" ]] && force_flag="--force-renewal"
  if command -v certbot >/dev/null 2>&1; then
    mkdir -p "$(dirname "$le_log")" 2>/dev/null || true
    if certbot certonly --standalone $force_flag -d "$domain" --non-interactive --agree-tos -m "admin@$domain" >"$le_log" 2>&1 \
       && [[ -s "$le_dir/fullchain.pem" && -s "$le_dir/privkey.pem" ]]; then
      cp "$le_dir/fullchain.pem" "$CONFIG_DIR/ssl/fullchain.pem" || true
      cp "$le_dir/privkey.pem" "$CONFIG_DIR/ssl/privkey.pem" || true
      got="letsencrypt"
    elif [[ -s "$le_log" ]]; then
      # Show the real reason (DNS mismatch, port 80 blocked, rate limit...)
      # instead of only guessing - the full certbot output stays in $le_log.
      warn "Let's Encrypt failed - certbot reported (full log: $le_log):"
      local _le_lines=""
      _le_lines="$(grep -iE "error|unauthorized|timeout|refused|invalid" "$le_log" 2>/dev/null | tail -n 3 || true)"
      if [[ -n "$_le_lines" ]]; then
        while IFS= read -r _l; do echo "  $_l"; done <<< "$_le_lines"
        le_reason=1
      fi
    fi
  fi
  if [[ -z "$got" ]]; then
    [[ "$le_reason" -eq 0 ]] && warn "Let's Encrypt failed (DNS/port 80)"
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
    build_haproxy_pem || true
    ok "SSL ready ($got)"
    echo "Cert: $CONFIG_DIR/ssl/fullchain.pem"
  else
    err "SSL FAILED: certificate files were not created!"
    echo "The ssl folder is empty - Xray/Panel will not start"
    got=""
  fi
  # xray and the panel were never stopped - restart them so they load
  # the fresh certificate (a real restart: try-restart would silently
  # skip them if they happened to be down). haproxy + nginx are brought
  # back by the EXIT trap (web_stack_restore) when this subshell ends,
  # which also verifies they are actually active again.
  if command -v systemctl >/dev/null 2>&1; then
    for s in xray vps-panel; do systemctl restart "$s" 2>/dev/null || true; done
  fi
  [[ -n "$got" ]]
  )
}

# --- SSL: status, re-apply, auto-renew test + renewal hook -------------------
ssl_days_left() {
  # Whole days until the ACTIVE suite certificate expires (empty if unknown)
  local cert="$CONFIG_DIR/ssl/fullchain.pem" end epoch now
  [[ -s "$cert" ]] || return 0
  end=$(openssl x509 -enddate -noout -in "$cert" 2>/dev/null | cut -d= -f2) || return 0
  [[ -n "$end" ]] || return 0
  epoch=$(date -d "$end" +%s 2>/dev/null) || return 0
  now=$(date +%s)
  echo $(( (epoch - now) / 86400 ))
}

ssl_cert_summary() {
  local cert="$CONFIG_DIR/ssl/fullchain.pem"
  if [[ -s "$cert" ]]; then
    echo "Subject : $(openssl x509 -subject -noout -in "$cert" 2>/dev/null | sed 's/^subject=//')"
    echo "Issuer  : $(openssl x509 -issuer -noout -in "$cert" 2>/dev/null | sed 's/^issuer=//')"
    echo "Expires : $(openssl x509 -enddate -noout -in "$cert" 2>/dev/null | cut -d= -f2) ($(ssl_days_left) days left)"
  else
    echo "No certificate installed yet ($cert missing)"
  fi
}

reapply_cert() {
  # Copy the Let's Encrypt cert for the current domain into the suite ssl
  # dir and restart every service that loads it (HAProxy TLS bundle,
  # Xray direct-TLS inbounds, the panel). Used after a renewal that
  # happened outside the suite (e.g. a manual certbot run).
  local domain le_dir
  domain="$(get_domain)"
  [[ -n "$domain" ]] || domain="$(python3 -c "import json;print(json.load(open('$DATA_DIR/config.json')).get('domain',''))" 2>/dev/null || echo "")"
  le_dir="/etc/letsencrypt/live/$domain"
  ensure_dirs; mkdir -p "$CONFIG_DIR/ssl"
  if [[ -n "$domain" && -s "$le_dir/fullchain.pem" && -s "$le_dir/privkey.pem" ]]; then
    cp "$le_dir/fullchain.pem" "$CONFIG_DIR/ssl/fullchain.pem" || true
    cp "$le_dir/privkey.pem" "$CONFIG_DIR/ssl/privkey.pem" || true
    chmod 600 "$CONFIG_DIR/ssl/privkey.pem" 2>/dev/null || true
    cat "$CONFIG_DIR/ssl/fullchain.pem" "$CONFIG_DIR/ssl/privkey.pem" > "$CONFIG_DIR/ssl/combined.pem" 2>/dev/null || true
    build_haproxy_pem || true
    ok "Certificate for $domain re-applied from Let's Encrypt"
  elif [[ -s "$CONFIG_DIR/ssl/fullchain.pem" ]]; then
    cat "$CONFIG_DIR/ssl/fullchain.pem" "$CONFIG_DIR/ssl/privkey.pem" > "$CONFIG_DIR/ssl/combined.pem" 2>/dev/null || true
    build_haproxy_pem || true
    warn "No Let's Encrypt cert found for '$domain' - kept the existing suite certificate"
  else
    err "No certificate found to re-apply (domain: '$domain')"
    return 1
  fi
  # Real restarts, not try-restart: if one of these services is down,
  # re-applying the cert must bring it back, not silently skip it.
  # HAProxy is verified afterwards so a front door that stays down is
  # reported loudly instead of looking "done".
  if command -v systemctl >/dev/null 2>&1; then
    for s in haproxy xray vps-panel; do systemctl restart "$s" 2>/dev/null || true; done
    web_stack_verify haproxy || true
  fi
  ssl_cert_summary
}

ssl_dry_run() {
  # Test the auto-renewal path end to end WITHOUT touching the live cert.
  # certbot's standalone authenticator needs port 80, so the web stack is
  # paused for the test and started again right after.
  command -v certbot >/dev/null 2>&1 || { err "certbot is not installed"; return 1; }
  # Same stop/restore contract as issue_ssl: the body runs in a subshell
  # whose EXIT trap (web_stack_restore) brings haproxy + nginx back on
  # every exit path - the old try-restart loop below silently skipped
  # them because they had just been stopped.
  (
  trap 'web_stack_restore' EXIT
  trap 'web_stack_restore; exit 130' INT
  trap 'web_stack_restore; exit 143' TERM
  if command -v systemctl >/dev/null 2>&1; then systemctl stop haproxy nginx 2>/dev/null || true; fi
  certbot renew --dry-run 2>&1 | tail -n 15
  local rc=${PIPESTATUS[0]}
  if command -v systemctl >/dev/null 2>&1; then
    for s in xray vps-panel; do systemctl restart "$s" 2>/dev/null || true; done
  fi
  exit $rc
  )
}

install_renewal_hook() {
  # certbot deploy hook: after ANY successful renewal, push the fresh cert
  # into the suite ssl dir and restart the services that use it, so the
  # panel/Xray/HAProxy never serve an old cert after auto-renewal.
  local hook=/etc/letsencrypt/renewal-hooks/deploy/vps-manager.sh
  mkdir -p "$(dirname "$hook")" 2>/dev/null || return 0
  cat > "$hook" <<'HOOK'
#!/usr/bin/env bash
# VPS Manager Suite - certbot deploy hook (installed by lib/common.sh)
# RENEWED_LINEAGE points at the renewed cert's live directory.
set -u
SSL_DIR="/etc/vps-manager/ssl"
[[ -n "${RENEWED_LINEAGE:-}" && -s "$RENEWED_LINEAGE/fullchain.pem" ]] || exit 0
mkdir -p "$SSL_DIR"
cp "$RENEWED_LINEAGE/fullchain.pem" "$SSL_DIR/fullchain.pem" || true
cp "$RENEWED_LINEAGE/privkey.pem" "$SSL_DIR/privkey.pem" || true
chmod 600 "$SSL_DIR/privkey.pem" 2>/dev/null || true
cat "$SSL_DIR/fullchain.pem" "$SSL_DIR/privkey.pem" > "$SSL_DIR/combined.pem" 2>/dev/null || true
# HAProxy terminates TLS on 443 from a single bundle file - rebuild it too,
# then the restart below makes HAProxy serve the fresh certificate.
cat "$SSL_DIR/fullchain.pem" "$SSL_DIR/privkey.pem" > "$SSL_DIR/haproxy.pem" 2>/dev/null || true
chmod 600 "$SSL_DIR/haproxy.pem" 2>/dev/null || true
for s in haproxy xray vps-panel; do systemctl restart "$s" 2>/dev/null || true; done
exit 0
HOOK
  chmod +x "$hook" 2>/dev/null || true
}

service_status() {
  # systemctl is the ONLY source of truth, and its exact state word is
  # what gets printed. There is deliberately NO process-name fallback:
  # a leftover/orphaned process with the same name does not mean the
  # service is up (an orphaned HAProxy process once made a stopped unit
  # display as "active (process)" while port 443 was dead).
  local svc="$1" st=""
  if command -v systemctl >/dev/null 2>&1; then
    st="$(systemctl is-active "$svc" 2>/dev/null || true)"
  fi
  echo "${st:-unknown}"
}

# --- Xray service identity ---------------------------------------------------
# The official Xray install script creates a systemd unit that runs the
# service as User=nobody. That user cannot read this suite's TLS private
# key ($CONFIG_DIR/ssl/privkey.pem, mode 600) which the TLS inbounds load,
# and cannot write the access log under /var/log/xray - so the service
# dies at startup even though a config test run as root passes.
# A systemd drop-in switches the service to root, which owns both paths.
# Idempotent: the drop-in is only rewritten when its content differs.
# --- Tunnel-only SSH accounts: shell + /etc/shells ---------------------------
# Dropbear refuses password logins for any account whose shell is not
# listed in /etc/shells (its log shows: User '<name>' has invalid shell,
# rejected) - the client only sees "incorrect user name or password".
# Every SSH account the suite creates is tunnel-only with this shell, so
# the shell must always be a member of /etc/shells. All creation paths
# (panel, menu, Telegram bot) use this one shell.
TUNNEL_SHELL="/bin/false"
SHELLS_FILE="${VPSMGR_SHELLS_FILE:-/etc/shells}"

ensure_tunnel_shell() {
  if [[ -f "$SHELLS_FILE" ]] && grep -qx "$TUNNEL_SHELL" "$SHELLS_FILE" 2>/dev/null; then
    return 0
  fi
  if echo "$TUNNEL_SHELL" >> "$SHELLS_FILE" 2>/dev/null; then
    ok "Added $TUNNEL_SHELL to $SHELLS_FILE (Dropbear rejects login shells that are not listed there)"
    log_activity "ensure_tunnel_shell: added $TUNNEL_SHELL to $SHELLS_FILE"
    return 0
  fi
  warn "Could not add $TUNNEL_SHELL to $SHELLS_FILE - Dropbear will reject SSH password logins"
  return 1
}

# Repair accounts created before the /etc/shells fix: the panel and the
# terminal menu used to create the system user with shell
# /usr/sbin/nologin, which Dropbear rejects when it is not listed in
# /etc/shells. Point every stored SSH account whose system user still has
# a nologin shell at the tunnel shell instead. Idempotent; users that do
# not exist on this box are skipped.
repair_tunnel_shells() {
  ensure_tunnel_shell || true
  local store="$DATA_DIR/ssh_users.json" u cur fixed=0
  [[ -f "$store" ]] || return 0
  command -v python3 >/dev/null 2>&1 || return 0
  while IFS= read -r u; do
    [[ -n "$u" ]] || continue
    cur="$(getent passwd "$u" 2>/dev/null | cut -d: -f7)"
    [[ -n "$cur" ]] || cur="$(awk -F: -v n="$u" '$1==n{print $7}' /etc/passwd 2>/dev/null)"
    case "$cur" in
      /usr/sbin/nologin|/sbin/nologin)
        if usermod -s "$TUNNEL_SHELL" "$u" 2>/dev/null; then
          ok "SSH account '$u': system shell $cur -> $TUNNEL_SHELL (Dropbear login fix)"
          log_activity "repair_tunnel_shells: $u shell $cur -> $TUNNEL_SHELL"
          fixed=$((fixed+1))
        else
          warn "SSH account '$u': could not change the system shell (still $cur)"
        fi
        ;;
    esac
  done < <(python3 -c "import json,sys
try:
    for r in json.load(open(sys.argv[1])):
        print(r.get('username',''))
except Exception:
    pass" "$store" 2>/dev/null)
  if [[ "$fixed" -gt 0 ]]; then
    ok "Repaired the system shell for $fixed SSH account(s)"
  fi
  return 0
}

XRAY_DROPIN_DIR="${VPSMGR_XRAY_DROPIN_DIR:-/etc/systemd/system/xray.service.d}"
XRAY_DROPIN_FILE="$XRAY_DROPIN_DIR/10-vpsmanager.conf"
XRAY_LOG_DIR_PATH="${VPSMGR_XRAY_LOG_DIR:-/var/log/xray}"
XRAY_CFG_DIR_PATH="${VPSMGR_XRAY_CFG_DIR:-/usr/local/etc/xray}"

ensure_xray_service() {
  mkdir -p "$XRAY_LOG_DIR_PATH" "$XRAY_CFG_DIR_PATH" "$XRAY_DROPIN_DIR" 2>/dev/null || true
  # The log files must exist AND be writable by the service user before the
  # first start: Xray opens access.log at startup and exits at once with
  # "permission denied" when it cannot (seen live 2026-11-07 - the service
  # ran as User=nobody against a root-owned log dir). Create both logs and
  # hand them to nobody when that user exists; also open the permissions so
  # either service identity (root or nobody) can always write.
  touch "$XRAY_LOG_DIR_PATH/access.log" "$XRAY_LOG_DIR_PATH/error.log" 2>/dev/null || true
  if id nobody >/dev/null 2>&1; then
    chown -R nobody:nogroup "$XRAY_LOG_DIR_PATH" 2>/dev/null || chown -R nobody "$XRAY_LOG_DIR_PATH" 2>/dev/null || true
  fi
  chmod 777 "$XRAY_LOG_DIR_PATH" 2>/dev/null || true
  chmod 666 "$XRAY_LOG_DIR_PATH/access.log" "$XRAY_LOG_DIR_PATH/error.log" 2>/dev/null || true
  local want=""
  want="$(cat <<'EOF'
# VPS Manager Suite: run the Xray service as root.
# The stock Xray unit runs as User=nobody, which cannot read this suite's
# TLS private key (mode 600) loaded by the TLS inbounds, and cannot write
# the access log under /var/log/xray - the service dies at startup.
# Root owns both paths, so the service starts reliably.
[Service]
User=root
EOF
)"
  local cur=""
  if [[ -f "$XRAY_DROPIN_FILE" ]]; then cur="$(cat "$XRAY_DROPIN_FILE" 2>/dev/null || true)"; fi
  if [[ "$cur" != "$want" ]]; then
    if printf '%s\n' "$want" > "$XRAY_DROPIN_FILE" 2>/dev/null; then
      info "Xray service drop-in installed: the service will run as root ($XRAY_DROPIN_FILE)"
    else
      warn "Could not write the Xray service drop-in: $XRAY_DROPIN_FILE"
      return 1
    fi
  fi
  if command -v systemctl >/dev/null 2>&1; then
    systemctl daemon-reload 2>/dev/null || true
  fi
  return 0
}

# verify_xray_running - call right after a restart of the xray service.
# Prints an [OK] line when the service is active; otherwise a clear
# failure line plus the last journal lines verbatim, so the real reason
# is visible on screen instead of a silent "stopped" in the panel.
verify_xray_running() {
  if ! command -v systemctl >/dev/null 2>&1; then return 0; fi
  if systemctl is-active --quiet xray 2>/dev/null; then
    ok "xray is running"
    return 0
  fi
  err "xray FAILED to start - the last log lines are:"
  if command -v journalctl >/dev/null 2>&1; then
    journalctl -u xray -n 5 --no-pager -o cat 2>/dev/null || true
  fi
  warn "Full Xray log: journalctl -u xray -n 50"
  return 1
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
