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

# Restart the services (where present)
if command -v systemctl >/dev/null 2>&1; then
  systemctl daemon-reload 2>/dev/null || true
  systemctl enable vps-monitor 2>/dev/null || true
  for s in vps-panel ws-proxy xray vps-monitor; do systemctl restart "$s" 2>/dev/null || true; done
  # restart the bot only if it was running (never start it unconfigured)
  systemctl is-active --quiet vps-bot 2>/dev/null && systemctl restart vps-bot 2>/dev/null || true
fi

echo "UPDATE OK - the latest version of VPS Manager Suite is installed. Panel: systemctl status vps-panel"
