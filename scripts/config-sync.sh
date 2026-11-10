#!/usr/bin/env bash
# VPS Manager Suite - Config Manager sync wrapper (original code)
#
# Runs scripts/config-sync.py (renders the HAProxy SNI map + nginx
# profile map from the panel's first-party profiles). When a map
# actually changed and HAProxy + systemd are present, the live config
# is validated with `haproxy -c` FIRST and only then reloaded
# gracefully (a reload keeps every established connection alive).
#
# Every external step is guarded: on a machine without haproxy or
# systemd (or with VPSMGR_NO_RELOAD=1) this is a safe no-op beyond
# rendering the maps.
#
# Env overrides:
#   VPSMGR_DATA_DIR     data dir (default /var/lib/vps-manager)
#   VPSMGR_SNI_MAP      HAProxy map output path
#   VPSMGR_NGINX_MAP    nginx map output path
#   VPSMGR_HAPROXY_CFG  live haproxy config (default /etc/haproxy/haproxy.cfg)
#   VPSMGR_NO_RELOAD=1  never validate/reload haproxy
set -u
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
HAPROXY_CFG="${VPSMGR_HAPROXY_CFG:-/etc/haproxy/haproxy.cfg}"

OUT="$(python3 "$SCRIPT_DIR/config-sync.py" 2>&1)"
RC=$?
printf '%s\n' "$OUT"
if [[ $RC -ne 0 ]]; then
  echo "[ERROR] config-sync.py failed (exit $RC) - maps were not reloaded."
  exit $RC
fi
if ! printf '%s\n' "$OUT" | grep -qx 'changed'; then
  echo "[OK] Maps unchanged - no reload needed."
  exit 0
fi
if [[ "${VPSMGR_NO_RELOAD:-0}" == "1" ]]; then
  echo "[OK] Maps updated - reload skipped (VPSMGR_NO_RELOAD=1)."
  exit 0
fi
if ! command -v haproxy >/dev/null 2>&1; then
  echo "[OK] Maps updated - haproxy is not installed here, nothing to reload."
  exit 0
fi
if ! command -v systemctl >/dev/null 2>&1; then
  echo "[OK] Maps updated - systemd is not available here, nothing to reload."
  exit 0
fi
if [[ -f "$HAPROXY_CFG" ]]; then
  if haproxy -c -f "$HAPROXY_CFG" >/dev/null 2>&1; then
    systemctl reload haproxy 2>/dev/null \
      && echo "[OK] HAProxy reloaded gracefully (existing connections kept)." \
      || echo "[WARN] HAProxy reload failed - the new map applies on the next reload/restart."
  else
    echo "[WARN] The live HAProxy config did not pass 'haproxy -c' - reload skipped for safety."
  fi
else
  echo "[OK] Maps updated - no live HAProxy config at $HAPROXY_CFG, nothing to reload."
fi
exit 0
