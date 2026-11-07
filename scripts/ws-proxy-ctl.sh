#!/usr/bin/env bash
# VPS Manager Suite - Python Proxy control (original code)
#
# The suite's WebSocket proxy (SSH-WS) is a Python service: systemd unit
# "ws-proxy", script scripts/ws-proxy.py. This helper gives the web panel
# (and the terminal) one streamed, line-by-line control surface for it:
#
#   ws-proxy-ctl.sh start          Start the service, then verify
#   ws-proxy-ctl.sh stop           Stop the service, then verify
#   ws-proxy-ctl.sh restart        Restart the service, then verify
#   ws-proxy-ctl.sh force-start    Force it ON: stop, kill whatever still
#                                  holds the proxy ports, reset the failed
#                                  state, start again, verify
#   ws-proxy-ctl.sh force-stop     Force it OFF: stop, kill whatever still
#                                  holds the proxy ports, verify the ports
#                                  are free and the service stays down
#   ws-proxy-ctl.sh status         Show service state + port state
#   ws-proxy-ctl.sh set-port <n>   Move the proxy to port <n> (the PROXY-
#                                  protocol listener moves to <n>+1):
#                                  config + unit + nginx router + the
#                                  HAProxy CONNECT backend are updated
#                                  together; a port already bound by a
#                                  DIFFERENT process aborts the change
#                                  before anything is touched
#
# Every step prints one [INFO]/[OK]/[WARN]/[ERROR] line so the panel can
# stream the output line by line. The port lives in the suite config
# (config.json key "ws_proxy_port", default 8080).
set -u

UNIT="ws-proxy"
DATA_DIR="${VPSMGR_DATA_DIR:-/var/lib/vps-manager}"
CONFIG_DIR="${VPSMGR_CONFIG_DIR:-/etc/vps-manager}"
CFG="$DATA_DIR/config.json"
UNIT_FILE="${WS_PROXY_UNIT_FILE:-/etc/systemd/system/ws-proxy.service}"
NGINX_CONF="${VPSMGR_NGINX_CONF:-/etc/nginx/nginx.conf}"
HAPROXY_CONF="${VPSMGR_HAPROXY_CONF:-/etc/haproxy/haproxy.cfg}"
PANEL_UNIT="${VPSMGR_PANEL_UNIT:-/etc/systemd/system/vps-panel.service}"
HERE="$(cd "$(dirname "$(readlink -f "${BASH_SOURCE[0]}")")" && pwd)"
SUITE_DIR="$(dirname "$HERE")"
[[ -f "$SUITE_DIR/templates/nginx.conf.tpl" ]] || SUITE_DIR="/opt/vps-manager-suite"

info(){ echo "[INFO] $*"; }
ok(){ echo "[OK] $*"; }
warn(){ echo "[WARN] $*"; }
err(){ echo "[ERROR] $*" >&2; }

cfg_get_port(){
  local p=""
  p=$(python3 - "$CFG" <<'PY' 2>/dev/null || echo ""
import json, sys
try:
    v = json.load(open(sys.argv[1])).get("ws_proxy_port", 8080)
except Exception:
    v = 8080
print(v)
PY
)
  [[ "$p" =~ ^[0-9]+$ ]] || p=8080
  echo "$p"
}

cfg_set_port(){
  python3 - "$CFG" "$CONFIG_DIR/config.json" "$1" <<'PY'
import json, os, sys
data_cfg, etc_cfg, port = sys.argv[1], sys.argv[2], int(sys.argv[3])
for p in (data_cfg, etc_cfg):
    if p == etc_cfg and not os.path.exists(p):
        continue  # mirror into the /etc store only when it already exists
    d = {}
    if os.path.exists(p):
        try: d = json.load(open(p))
        except Exception: d = {}
    d["ws_proxy_port"] = port
    os.makedirs(os.path.dirname(p), exist_ok=True)
    json.dump(d, open(p, "w"), indent=2)
    print("[INFO] Saved ws_proxy_port=%d in %s" % (port, p))
PY
}

svc_active(){ systemctl is-active --quiet "$UNIT" 2>/dev/null; }

port_listening(){
  command -v ss >/dev/null 2>&1 || return 1
  ss -tln 2>/dev/null | grep -q "[:.]$1[[:space:]]"
}

# PIDs holding a TCP listen port (space separated, may be empty)
port_pids(){
  command -v ss >/dev/null 2>&1 || { echo ""; return; }
  ss -tlnp 2>/dev/null | grep "[:.]$1[[:space:]]" | grep -o "pid=[0-9]*" | cut -d= -f2 | sort -u | tr '\n' ' '
}

# PIDs that belong to the proxy itself (its systemd main pid + script pids)
own_pids(){
  local out="" mp=""
  mp=$(systemctl show -p MainPID --value "$UNIT" 2>/dev/null || echo "")
  [[ "$mp" =~ ^[0-9]+$ && "$mp" != "0" ]] && out="$out $mp"
  if command -v pgrep >/dev/null 2>&1; then
    out="$out $(pgrep -f 'vps-ws-proxy\.py' 2>/dev/null | tr '\n' ' ')"
  fi
  echo "$out"
}

wait_active(){
  local i
  for i in 1 2 3 4 5; do svc_active && return 0; sleep 1; done
  svc_active
}

wait_port(){
  # wait_port <port> <listening|free>
  local i
  for i in 1 2 3 4 5; do
    if [[ "$2" == "listening" ]]; then port_listening "$1" && return 0
    else ! port_listening "$1" && return 0; fi
    sleep 1
  done
  if [[ "$2" == "listening" ]]; then port_listening "$1"; else ! port_listening "$1"; fi
}

verify_on(){
  # verify_on <port> - service active + both listeners up
  local p="$1" rc=0
  if svc_active; then ok "Service $UNIT is active"; else err "Service $UNIT is NOT active"; rc=1; fi
  if ! command -v ss >/dev/null 2>&1; then
    warn "ss not found - port check skipped"
  else
    if port_listening "$p"; then ok "Port $p is listening (WebSocket)"; else err "Port $p is NOT listening"; rc=1; fi
    if port_listening "$((p+1))"; then ok "Port $((p+1)) is listening (PROXY protocol)"; else err "Port $((p+1)) is NOT listening"; rc=1; fi
  fi
  return $rc
}

verify_off(){
  # verify_off <port> - service down + both ports free
  local p="$1" rc=0
  if svc_active; then err "Service $UNIT is still active"; rc=1; else ok "Service $UNIT is stopped"; fi
  if ! command -v ss >/dev/null 2>&1; then
    warn "ss not found - port check skipped"
  else
    if port_listening "$p"; then err "Port $p is still bound"; rc=1; else ok "Port $p is free"; fi
    if port_listening "$((p+1))"; then err "Port $((p+1)) is still bound"; rc=1; else ok "Port $((p+1)) is free"; fi
  fi
  return $rc
}

kill_holders(){
  # kill_holders <port> - kill whatever still holds the port (TERM, then KILL)
  local p="$1" pids left pid
  pids=$(port_pids "$p")
  if [[ -z "${pids// }" ]]; then
    info "Port $p has no process to kill"
    return 0
  fi
  warn "Port $p is still held by PID(s): $pids - killing them"
  for pid in $pids; do kill "$pid" 2>/dev/null || true; done
  sleep 1
  left=$(port_pids "$p")
  if [[ -n "${left// }" ]]; then
    warn "PID(s) still alive: $left - sending SIGKILL"
    for pid in $left; do kill -9 "$pid" 2>/dev/null || true; done
    sleep 1
  fi
  if port_listening "$p"; then
    err "Port $p is STILL bound after the kill attempts"
    return 1
  fi
  ok "Port $p is free now"
  return 0
}

do_start(){
  local p; p=$(cfg_get_port)
  info "Starting the Python Proxy (service $UNIT, port $p)..."
  systemctl start "$UNIT" 2>/dev/null || warn "systemctl start reported a problem"
  if wait_active && verify_on "$p"; then
    ok "Python Proxy final state: RUNNING on port $p (PROXY listener $((p+1)))"
    return 0
  fi
  err "Python Proxy final state: NOT RUNNING - check: journalctl -u $UNIT -n 30"
  return 1
}

do_stop(){
  local p; p=$(cfg_get_port)
  info "Stopping the Python Proxy (service $UNIT, port $p)..."
  systemctl stop "$UNIT" 2>/dev/null || warn "systemctl stop reported a problem"
  sleep 1
  if verify_off "$p"; then
    ok "Python Proxy final state: STOPPED (ports $p and $((p+1)) free)"
    return 0
  fi
  err "Python Proxy final state: STILL UP - try Force Stop"
  return 1
}

do_restart(){
  local p; p=$(cfg_get_port)
  info "Restarting the Python Proxy (service $UNIT, port $p)..."
  systemctl restart "$UNIT" 2>/dev/null || warn "systemctl restart reported a problem"
  if wait_active && verify_on "$p"; then
    ok "Python Proxy final state: RUNNING on port $p (PROXY listener $((p+1)))"
    return 0
  fi
  err "Python Proxy final state: NOT RUNNING - check: journalctl -u $UNIT -n 30"
  return 1
}

do_force_start(){
  local p; p=$(cfg_get_port)
  info "FORCE START requested for the Python Proxy (port $p)"
  info "Step 1/4: stopping the service (errors ignored)"
  systemctl stop "$UNIT" 2>/dev/null || true
  sleep 1
  info "Step 2/4: clearing anything that still holds the proxy ports"
  kill_holders "$p" || true
  kill_holders "$((p+1))" || true
  info "Step 3/4: clearing the failed state and starting the service"
  systemctl reset-failed "$UNIT" 2>/dev/null || true
  systemctl start "$UNIT" 2>/dev/null || warn "systemctl start reported a problem"
  info "Step 4/4: verifying the final state"
  if wait_active && verify_on "$p"; then
    ok "Python Proxy final state: RUNNING on port $p (PROXY listener $((p+1)))"
    return 0
  fi
  err "Python Proxy final state: NOT RUNNING even after Force Start - check: journalctl -u $UNIT -n 30"
  return 1
}

do_force_stop(){
  local p; p=$(cfg_get_port)
  info "FORCE STOP requested for the Python Proxy (port $p)"
  info "Step 1/3: stopping the service (errors ignored)"
  systemctl stop "$UNIT" 2>/dev/null || true
  sleep 1
  info "Step 2/3: clearing anything that still holds the proxy ports"
  kill_holders "$p" || true
  kill_holders "$((p+1))" || true
  systemctl reset-failed "$UNIT" 2>/dev/null || true
  info "Step 3/3: verifying the final state"
  if verify_off "$p"; then
    ok "Python Proxy final state: STOPPED (ports $p and $((p+1)) free)"
    return 0
  fi
  err "Python Proxy final state: STILL UP even after Force Stop"
  return 1
}

do_status(){
  local p; p=$(cfg_get_port)
  info "Python Proxy (service $UNIT)"
  if svc_active; then ok "Service state: RUNNING"; else warn "Service state: STOPPED"; fi
  info "Configured WebSocket port: $p"
  info "Configured PROXY-protocol port: $((p+1))"
  if command -v ss >/dev/null 2>&1; then
    if port_listening "$p"; then ok "Port $p: listening"; else warn "Port $p: not listening"; fi
    if port_listening "$((p+1))"; then ok "Port $((p+1)): listening"; else warn "Port $((p+1)): not listening"; fi
  else
    warn "ss not found - port state unknown"
  fi
  if svc_active; then return 0; fi
  return 1
}

# Is <port> bound by a process that is NOT the proxy itself?
port_busy_by_other(){
  local p="$1" holders own pid
  port_listening "$p" || return 1
  holders=$(port_pids "$p")
  if [[ -z "${holders// }" ]]; then
    # Listening but no PID visible (not root?) - treat as busy to stay safe
    return 0
  fi
  own=" $(own_pids) $(port_pids "$(cfg_get_port)") $(port_pids "$(( $(cfg_get_port) + 1 ))") "
  for pid in $holders; do
    case "$own" in *" $pid "*) ;; *) return 0 ;; esac
  done
  return 1
}

do_set_port(){
  local new="${1:-}" cur dom panel_port tmp rc=0
  # 1) validation
  if ! [[ "$new" =~ ^[0-9]+$ ]] || (( new < 1 || new > 65535 )); then
    err "Invalid port '$new' - enter an integer between 1 and 65535"
    return 1
  fi
  if (( new == 65535 )); then
    err "Port 65535 cannot be used - the PROXY-protocol listener needs port $((new+1))"
    return 1
  fi
  if [[ ! -f "$UNIT_FILE" ]]; then
    err "Unit file not found: $UNIT_FILE - run the installer first"
    return 1
  fi
  cur=$(cfg_get_port)
  info "Python Proxy port change requested: $cur -> $new (PROXY listener $((cur+1)) -> $((new+1)))"
  if (( new == cur )); then
    info "Port $new is already the configured port - verifying the running state only"
    verify_on "$cur" && ok "Python Proxy final state: RUNNING on port $cur" && return 0
    err "Python Proxy is configured for port $cur but is not fully up - try Restart or Force Start"
    return 1
  fi
  # 2) busy check - a DIFFERENT process on the target ports aborts everything
  if port_busy_by_other "$new"; then
    err "Port $new is already in use by a different process - change aborted, nothing was modified"
    err "The Python Proxy keeps running on its current port $cur"
    return 1
  fi
  ok "Port $new is free"
  if port_busy_by_other "$((new+1))"; then
    err "Port $((new+1)) (needed for the PROXY-protocol listener) is already in use by a different process - change aborted, nothing was modified"
    err "The Python Proxy keeps running on its current port $cur"
    return 1
  fi
  ok "Port $((new+1)) is free"
  # 3) render + validate the new nginx router BEFORE touching the live stack
  dom=$(python3 -c "import json;print(json.load(open('$CFG')).get('domain',''))" 2>/dev/null || echo "")
  panel_port=$(grep -o 'VPSMGR_PORT=[0-9]*' "$PANEL_UNIT" 2>/dev/null | head -1 | cut -d= -f2)
  panel_port="${panel_port:-9090}"
  tmp="$(mktemp)"
  if [[ -f "$SUITE_DIR/templates/nginx.conf.tpl" && -n "$dom" ]]; then
    sed "s/__DOMAIN__/$dom/g; s/__PANEL_PORT__/$panel_port/g; s/__WSPROXY_PORT__/$new/g" \
      "$SUITE_DIR/templates/nginx.conf.tpl" > "$tmp"
    info "Nginx router rendered from the template (SSH-WS now points to port $new)"
  elif [[ -f "$NGINX_CONF" ]]; then
    sed -E "s|(location /sshws \{ proxy_pass http://127\.0\.0\.1:)[0-9]+|\1$new|" "$NGINX_CONF" > "$tmp"
    warn "Template or domain missing - updated only the /sshws line of the live nginx config"
  else
    err "No nginx template and no live nginx config found - change aborted, nothing was modified"
    rm -f "$tmp"
    return 1
  fi
  if command -v nginx >/dev/null 2>&1; then
    if nginx -t -c "$tmp" >/dev/null 2>&1; then
      ok "Nginx config test passed"
    else
      err "Nginx config test FAILED for the rendered config - change aborted, nothing was modified"
      rm -f "$tmp"
      return 1
    fi
  else
    warn "nginx binary not found - config test skipped"
  fi
  # 3b) render + validate the new HAProxy config the same way - on 443
  # HAProxy terminates TLS and sends CONNECT requests straight to this
  # proxy, so its ws_proxy_direct backend must move with the port too.
  tmp2="$(mktemp)"
  if [[ -f "$SUITE_DIR/templates/haproxy.cfg.tpl" && -n "$dom" ]]; then
    sed "s/__DOMAIN__/$dom/g; s/__WSPROXY_PORT__/$new/g; s/__WSPROXY_PROXY_PORT__/$((new+1))/g" \
      "$SUITE_DIR/templates/haproxy.cfg.tpl" > "$tmp2"
    info "HAProxy config rendered from the template (CONNECT backend now points to port $new)"
  elif [[ -f "$HAPROXY_CONF" ]]; then
    sed -E "s|(server wsproxy 127\.0\.0\.1:)[0-9]+$|\1$new|; s|(server wsproxy 127\.0\.0\.1:)[0-9]+ send-proxy|\1$((new+1)) send-proxy|" "$HAPROXY_CONF" > "$tmp2"
    warn "Template or domain missing - updated only the wsproxy line of the live HAProxy config"
  else
    err "No HAProxy template and no live HAProxy config found - change aborted, nothing was modified"
    rm -f "$tmp" "$tmp2"
    return 1
  fi
  if command -v haproxy >/dev/null 2>&1; then
    # haproxy -c also loads the TLS bundle referenced by the config -
    # when it is not built yet (pre-SSL state), only warn: the port move
    # itself is still valid, HAProxy picks the bundle up once SSL exists.
    if haproxy -c -f "$tmp2" >/dev/null 2>&1; then
      ok "HAProxy config test passed"
    elif grep -q "haproxy.pem" "$tmp2" && [[ ! -s /etc/vps-manager/ssl/haproxy.pem ]]; then
      warn "HAProxy config test skipped - the TLS bundle /etc/vps-manager/ssl/haproxy.pem does not exist yet (issue SSL first)"
    else
      err "HAProxy config test FAILED for the rendered config - change aborted, nothing was modified"
      rm -f "$tmp" "$tmp2"
      return 1
    fi
  else
    warn "haproxy binary not found - config test skipped"
  fi
  # 4) persist the choice (suite config)
  cfg_set_port "$new" || { err "Could not save the config - change aborted"; rm -f "$tmp" "$tmp2"; return 1; }
  ok "Config updated: ws_proxy_port=$new"
  # 5) update the systemd unit environment
  python3 - "$UNIT_FILE" "$new" <<'PY'
import sys
path, port = sys.argv[1], int(sys.argv[2])
lines = open(path).read().splitlines()
out, seen_main, seen_proxy = [], False, False
for ln in lines:
    if ln.startswith("Environment=WS_PROXY_PORT="):
        out.append("Environment=WS_PROXY_PORT=%d" % port); seen_main = True
    elif ln.startswith("Environment=WS_PROXY_PROXY_PORT="):
        out.append("Environment=WS_PROXY_PROXY_PORT=%d" % (port + 1)); seen_proxy = True
    else:
        out.append(ln)
    if ln.startswith("ExecStart=") and not seen_main:
        out.append("Environment=WS_PROXY_PORT=%d" % port); seen_main = True
# place a missing PROXY line right after the main one
final = []
for ln in out:
    final.append(ln)
    if ln.startswith("Environment=WS_PROXY_PORT=") and not seen_proxy:
        final.append("Environment=WS_PROXY_PROXY_PORT=%d" % (port + 1)); seen_proxy = True
open(path, "w").write("\n".join(final) + "\n")
print("[INFO] Unit updated: WS_PROXY_PORT=%d, WS_PROXY_PROXY_PORT=%d" % (port, port + 1))
PY
  [[ $? -eq 0 ]] || { err "Could not update the unit file - change aborted"; rm -f "$tmp" "$tmp2"; return 1; }
  # 6) install the rendered nginx router + HAProxy config (backups kept)
  if [[ -f "$NGINX_CONF" ]]; then cp -a "$NGINX_CONF" "$NGINX_CONF.bak-pyproxy" 2>/dev/null || true; fi
  mkdir -p "$(dirname "$NGINX_CONF")" 2>/dev/null || true
  cat "$tmp" > "$NGINX_CONF" && info "Nginx router installed (backup: $NGINX_CONF.bak-pyproxy)" || warn "Could not write $NGINX_CONF"
  rm -f "$tmp"
  if [[ -f "$HAPROXY_CONF" ]]; then cp -a "$HAPROXY_CONF" "$HAPROXY_CONF.bak-pyproxy" 2>/dev/null || true; fi
  mkdir -p "$(dirname "$HAPROXY_CONF")" 2>/dev/null || true
  cat "$tmp2" > "$HAPROXY_CONF" && info "HAProxy config installed (backup: $HAPROXY_CONF.bak-pyproxy)" || warn "Could not write $HAPROXY_CONF"
  rm -f "$tmp2"
  # 7) apply: reload systemd, restart the proxy, reload nginx, restart HAProxy
  systemctl daemon-reload 2>/dev/null || warn "systemctl daemon-reload reported a problem"
  info "Restarting the Python Proxy on the new port..."
  systemctl restart "$UNIT" 2>/dev/null || warn "systemctl restart reported a problem"
  if systemctl reload nginx 2>/dev/null; then
    info "Nginx reloaded"
  elif systemctl restart nginx 2>/dev/null; then
    info "Nginx restarted"
  else
    warn "Nginx reload/restart reported a problem - check: systemctl status nginx"
  fi
  if systemctl restart haproxy 2>/dev/null; then
    info "HAProxy restarted (CONNECT backend now on port $new)"
  else
    warn "HAProxy restart reported a problem - check: systemctl status haproxy"
  fi
  # 8) verify
  if wait_active && verify_on "$new"; then
    if port_listening "$cur"; then
      warn "Port $cur is still bound by something - check with: ss -tlnp | grep $cur"
    else
      info "Old port $cur is released"
    fi
    ok "Python Proxy final state: RUNNING on port $new (PROXY listener $((new+1)))"
    return 0
  fi
  err "Python Proxy final state: NOT RUNNING on port $new - check: journalctl -u $UNIT -n 30"
  return 1
}

case "${1:-}" in
  start) do_start ;;
  stop) do_stop ;;
  restart) do_restart ;;
  force-start) do_force_start ;;
  force-stop) do_force_stop ;;
  status) do_status ;;
  set-port) do_set_port "${2:-}" ;;
  force)
    err "'force' needs a target state - use: ws-proxy-ctl.sh force-start | force-stop"
    exit 1 ;;
  *)
    err "Usage: ws-proxy-ctl.sh {start|stop|restart|force-start|force-stop|status|set-port <n>}"
    exit 1 ;;
esac
