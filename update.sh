#!/usr/bin/env bash
# VPS Manager Suite - Updater (original code)
# Downloads the latest version from the GitHub repo and updates
# /opt/vps-manager-suite - SAFELY:
#   1. the new tree is fully VERIFIED first (required files present,
#      every shell script parses with bash -n, every python file
#      compiles, no suspiciously tiny files)
#   2. the CURRENT suite is backed up (timestamped, last 3 kept) so a
#      bad update can always be rolled back by hand
#   3. only then is the new tree applied, service units refreshed,
#      missing pieces completed (monitor/bot units, xray drop-in,
#      certs, legacy cron removed) and services restarted
#
# Usage: sudo bash update.sh [--dry-run]
# Env overrides (for testing):
#   VPSMGR_REPO=owner/repo        (default: mrshahnawazyt/vps-manager-suite)
#   VPSMGR_BRANCH=main
#   VPSMGR_TARBALL=/path/file.tgz (local tarball - the download is skipped)
#   VPSMGR_SUITE_DIR=/opt/vps-manager-suite
set -u
REPO="${VPSMGR_REPO:-mrshahnawazyt/vps-manager-suite}"
BRANCH="${VPSMGR_BRANCH:-main}"
SUITE_DIR="${VPSMGR_SUITE_DIR:-/opt/vps-manager-suite}"
BACKUP_ROOT="${VPSMGR_BACKUP_ROOT:-/var/backups/vps-manager-suite}"
DRY_RUN=0; [[ "${1:-}" == "--dry-run" ]] && DRY_RUN=1

fail(){ echo "[UPDATE-FAIL] $*" >&2; exit 1; }

[[ "${EUID:-$(id -u)}" -eq 0 ]] || fail "Root is required: sudo bash update.sh"

# Self-exec guard: this script lives at $SUITE_DIR/update.sh and the
# apply step below overwrites that very file while bash is still
# reading it (cp -a truncates in place - same inode), so bash can
# resume at a misaligned offset inside the NEW file and execute an
# unpredictable blend of the old and new updater. Re-exec ONCE from a
# private temp copy, so the running script is never the file being
# replaced and a single `vpsupdate` always applies the full new flow.
if [[ -z "${VPSMGR_UPDATE_REEXEC:-}" ]]; then
  _self_tmp="$(mktemp /tmp/vpsmgr-update.XXXXXX.sh)" || exit 1
  cp "$0" "$_self_tmp" 2>/dev/null || { rm -f "$_self_tmp"; exit 1; }
  chmod 700 "$_self_tmp" 2>/dev/null || true
  VPSMGR_UPDATE_REEXEC=1 exec bash "$_self_tmp" "$@"
fi

TMP="$(mktemp -d)"; trap 'rm -rf "$TMP"' EXIT
TGZ="$TMP/suite.tgz"
if [[ -n "${VPSMGR_TARBALL:-}" ]]; then
  [[ -f "$VPSMGR_TARBALL" ]] || fail "VPSMGR_TARBALL file not found: $VPSMGR_TARBALL"
  cp "$VPSMGR_TARBALL" "$TGZ"
  echo "[update] Using local tarball: $VPSMGR_TARBALL"
else
  echo "[update] Downloading the latest version: $REPO@$BRANCH"
  curl -fsSL "https://api.github.com/repos/$REPO/tarball/$BRANCH" -o "$TGZ" \
    || fail "Download failed - check the internet/GitHub connection."
fi

mkdir -p "$TMP/src"
tar -xzf "$TGZ" --strip-components=1 -C "$TMP/src" || fail "Failed to extract the tarball."

# ---- 1) VERIFY the new tree before touching the installed one -------------
for f in install.sh menu.sh update.sh web/panel.py lib/common.sh scripts/vpsm.py; do
  [[ -f "$TMP/src/$f" ]] || fail "Required file missing from the tarball: $f - update stopped, the existing suite is safe."
done
echo "[update] Verifying shell syntax (bash -n)..."
while IFS= read -r f; do
  bash -n "$f" || fail "Shell syntax error in the new version: ${f#$TMP/src/} - update stopped, the existing suite is safe."
done < <(find "$TMP/src" -name '*.sh' -type f)
echo "[update] Verifying python files compile..."
while IFS= read -r f; do
  python3 -m py_compile "$f" 2>/dev/null || fail "Python compile error in the new version: ${f#$TMP/src/} - update stopped, the existing suite is safe."
done < <(find "$TMP/src" -name '*.py' -type f)
while IFS= read -r f; do
  sz=$(stat -c%s "$f" 2>/dev/null || echo 0)
  (( sz >= 100 )) || fail "File looks truncated (${sz} bytes): ${f#$TMP/src/} - update stopped, the existing suite is safe."
done < <(find "$TMP/src" -type f \( -name '*.sh' -o -name '*.py' \))
echo "[update] New version verified OK (files + syntax + sizes)"

if [[ "$DRY_RUN" -eq 1 ]]; then
  echo "UPDATE DRY-RUN OK: the new version passed all checks; run without --dry-run to apply it."
  exit 0
fi

# ---- 2) BACK UP the current suite (keep the last 3) -----------------------
if [[ -d "$SUITE_DIR" && -n "$(ls -A "$SUITE_DIR" 2>/dev/null)" ]]; then
  mkdir -p "$BACKUP_ROOT"
  TS="$(date +%Y%m%d-%H%M%S)"
  cp -a "$SUITE_DIR" "$BACKUP_ROOT/suite-$TS" || fail "Backup failed - update stopped before changing anything."
  echo "[update] Backup of the current version: $BACKUP_ROOT/suite-$TS"
  ls -1dt "$BACKUP_ROOT"/suite-* 2>/dev/null | tail -n +4 | while read -r old; do rm -rf "$old"; done
fi

# ---- 3) APPLY --------------------------------------------------------------
mkdir -p "$SUITE_DIR"
cp -a "$TMP/src/." "$SUITE_DIR/"
chmod +x "$SUITE_DIR"/*.sh "$SUITE_DIR"/scripts/*.sh 2>/dev/null || true

# Refresh the installed copies of the panel / ws-proxy
install -m 755 "$SUITE_DIR/web/panel.py" /usr/local/bin/vps-panel.py
install -m 755 "$SUITE_DIR/scripts/ws-proxy.py" /usr/local/bin/vps-ws-proxy.py

# Refresh the command wrappers
for _cmd in vpsmenu menu; do
  printf '#!/usr/bin/env bash\nexec bash %s/menu.sh "$@"\n' "$SUITE_DIR" > "/usr/local/bin/$_cmd"
  chmod +x "/usr/local/bin/$_cmd"
done
printf '#!/usr/bin/env bash\nexec bash %s/update.sh "$@"\n' "$SUITE_DIR" > /usr/local/bin/vpsupdate
chmod +x /usr/local/bin/vpsupdate

# A stale shell alias can shadow the wrappers (another tool may have added
# `alias menu=...` to root's .bashrc) - remove ours/other menu aliases so
# `vpsmenu` and `menu` always reach THIS suite.
if [[ -f /root/.bashrc ]] && grep -qE '^[[:space:]]*alias (menu|vpsmenu)=' /root/.bashrc; then
  sed -i.bak -E '/^[[:space:]]*alias (menu|vpsmenu)=/d' /root/.bashrc
  echo "[update] Removed shadowing menu/vpsmenu aliases from /root/.bashrc (backup: /root/.bashrc.bak)"
fi

# Apply service-level fixes from the NEW code before restarting anything:
# the stock Xray unit runs as User=nobody, which cannot read the suite's
# TLS private key (mode 600) or write its access log, so Xray dies at
# startup. ensure_xray_service (from the updated lib/common.sh) installs
# the systemd drop-in that runs Xray as root and reloads systemd.
if [[ -f "$SUITE_DIR/lib/common.sh" ]]; then
  # shellcheck disable=SC1091
  source "$SUITE_DIR/lib/common.sh" 2>/dev/null || true
fi
if declare -F ensure_xray_service >/dev/null 2>&1; then
  ensure_xray_service || echo "[update] Warning: the Xray service drop-in could not be installed."
fi
# SSH logins: Dropbear rejects accounts whose shell is not listed in
# /etc/shells. Older panel/menu versions created the system user with a
# nologin shell, which made every such account fail password login with
# "incorrect user name or password". Guarantee the tunnel shell is listed
# and repair the stored accounts in place.
if declare -F ensure_tunnel_shell >/dev/null 2>&1; then
  ensure_tunnel_shell || echo "[update] Warning: could not add the tunnel shell to /etc/shells."
  repair_tunnel_shells || echo "[update] Warning: the SSH account shell repair did not complete."
fi

# ---- 4) COMPLETE any missing pieces ---------------------------------------
# Monitor service (fast limit enforcement loop) - create if missing
if [[ ! -f /etc/systemd/system/vps-monitor.service ]]; then
  cat > /etc/systemd/system/vps-monitor.service <<EOF
[Unit]
Description=VPS Manager Suite - Limit Monitor
After=network.target
[Service]
Type=simple
WorkingDirectory=$SUITE_DIR
Environment=VPSMGR_DATA_DIR=/var/lib/vps-manager
ExecStart=/usr/bin/python3 $SUITE_DIR/scripts/monitor.py
Restart=always
RestartSec=3
[Install]
WantedBy=multi-user.target
EOF
  echo "[update] Created the missing vps-monitor.service"
fi
# Telegram bot service - create if missing (started from menu option 09)
if [[ ! -f /etc/systemd/system/vps-bot.service ]]; then
  cat > /etc/systemd/system/vps-bot.service <<EOF
[Unit]
Description=VPS Manager Suite - Telegram Bot
After=network.target
[Service]
Type=simple
WorkingDirectory=$SUITE_DIR
ExecStart=/usr/bin/python3 $SUITE_DIR/scripts/telegram-bot.py
Restart=on-failure
RestartSec=5
[Install]
WantedBy=multi-user.target
EOF
  echo "[update] Created the missing vps-bot.service"
fi
# The old 10-minute limit cron is replaced by the monitor service
if [[ -f /etc/cron.d/vps-manager-limits ]]; then
  rm -f /etc/cron.d/vps-manager-limits
  echo "[update] Removed the legacy limit cron (the monitor service replaces it)"
fi
# Missing Xray binary while the rest of the stack exists -> point at repair
if ! command -v xray >/dev/null 2>&1 && command -v dropbear >/dev/null 2>&1; then
  echo "[update] NOTE: the Xray binary is missing while Dropbear exists - run menu option 01 (Install All) or 06 (Fix Stack) to complete the stack."
fi
# Certs + xray config self-heal (creates the self-signed pair if absent)
python3 "$SUITE_DIR/scripts/sync-xray.py" --restart >/dev/null 2>&1 || true

# Router configs re-render (nginx + HAProxy) from the NEW templates.
# This matters most for port 443: HAProxy now TERMINATES TLS and routes
# CONNECT to ws-proxy, while an existing install still carries the old
# SNI-passthrough config in /etc/haproxy/haproxy.cfg - and nothing else
# in the update flow rewrites it. Also rebuild the HAProxy TLS bundle
# (haproxy.pem) from the current cert pair; HAProxy will not start on
# 443 without it. The restarts at the end of this script (nginx and
# haproxy are in the loop below) put the new frontend live, so no manual
# fix-stack run is needed after updating.
_upd_dom="$(python3 -c "import json;print(json.load(open('/var/lib/vps-manager/config.json')).get('domain',''))" 2>/dev/null || echo "")"
if [[ -z "$_upd_dom" ]] && declare -F get_domain >/dev/null 2>&1; then _upd_dom="$(get_domain)"; fi
if [[ -n "$_upd_dom" ]]; then
  _upd_wsp="$(python3 -c "import json;print(json.load(open('/var/lib/vps-manager/config.json')).get('ws_proxy_port',8080))" 2>/dev/null || echo 8080)"
  [[ "$_upd_wsp" =~ ^[0-9]+$ ]] || _upd_wsp=8080
  if [[ -d /etc/nginx ]]; then
    sed "s/__DOMAIN__/$_upd_dom/g; s/__PANEL_PORT__/9090/g; s/__WSPROXY_PORT__/$_upd_wsp/g" "$SUITE_DIR/templates/nginx.conf.tpl" > /etc/nginx/nginx.conf 2>/dev/null \
      && echo "[update] nginx router re-rendered from the new template" || echo "[update] Warning: could not re-render /etc/nginx/nginx.conf"
  fi
  if [[ -d /etc/haproxy ]]; then
    sed "s/__DOMAIN__/$_upd_dom/g; s/__WSPROXY_PORT__/$_upd_wsp/g; s/__WSPROXY_PROXY_PORT__/$((_upd_wsp+1))/g" "$SUITE_DIR/templates/haproxy.cfg.tpl" > /etc/haproxy/haproxy.cfg 2>/dev/null \
      && echo "[update] haproxy config re-rendered (port 443 = TLS termination + CONNECT routing)" || echo "[update] Warning: could not re-render /etc/haproxy/haproxy.cfg"
  fi
else
  echo "[update] Warning: no domain found in the config - router configs not re-rendered (run menu option 06, Fix Stack, after setting a domain)."
fi
if declare -F build_haproxy_pem >/dev/null 2>&1; then
  build_haproxy_pem && echo "[update] HAProxy TLS bundle rebuilt (/etc/vps-manager/ssl/haproxy.pem)" \
    || echo "[update] Warning: no SSL certificate pair found - HAProxy 443 stays down until SSL is issued (menu option 03)."
fi

# Restart the services (where present)
if command -v systemctl >/dev/null 2>&1; then
  systemctl daemon-reload 2>/dev/null || true
  systemctl enable vps-monitor 2>/dev/null || true
  for s in nginx haproxy vps-panel ws-proxy xray vps-monitor; do systemctl restart "$s" 2>/dev/null || true; done
  # restart the bot only if it was running (never start it unconfigured)
  systemctl is-active --quiet vps-bot 2>/dev/null && systemctl restart vps-bot 2>/dev/null || true
fi

# Verification pass: prove the core services are actually up before
# claiming success. A restart above can fail (bad config, missing
# binary) and the old script printed "UPDATE OK" anyway - a stopped
# front door like HAProxy then looked like a finished update. Now any
# core service that is down gets ONE start attempt, and whatever is
# still down is named in a loud [WARNING] instead of a silent success.
_upd_down=""
if command -v systemctl >/dev/null 2>&1; then
  for s in nginx haproxy vps-panel ws-proxy xray; do
    if ! systemctl is-active --quiet "$s" 2>/dev/null; then
      echo "[update] $s is not running after the restart - trying one start..."
      systemctl start "$s" 2>/dev/null || true
      sleep 1
      systemctl is-active --quiet "$s" 2>/dev/null || _upd_down="$_upd_down $s"
    fi
  done
fi
if [[ -n "$_upd_down" ]]; then
  echo "[WARNING] UPDATE APPLIED, but these services are NOT running:$_upd_down"
  echo "[WARNING] The new version is in place; a service that will not start usually has a config or certificate problem."
  echo "[WARNING] Diagnose: systemctl status <service>  |  journalctl -u <service> -n 30 --no-pager"
  echo "[WARNING] Or run the repair pass: menu option 06 (Fix Stack)."
else
  echo "UPDATE OK - the latest version of VPS Manager Suite is installed, and all core services (nginx, haproxy, vps-panel, ws-proxy, xray) are verified running. Panel: systemctl status vps-panel"
fi
