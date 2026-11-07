#!/usr/bin/env bash
# VPS Manager Suite - Updater (original code)
# Downloads the latest version from the GitHub repo and updates
# /opt/vps-manager-suite, refreshes the panel/ws-proxy files and the
# vpsmenu/menu/vpsupdate wrappers, and restarts vps-panel (+ ws-proxy).
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

# Safety: a partial/corrupt download must NOT replace the suite - verify first
for f in install.sh menu.sh update.sh web/panel.py lib/common.sh; do
  [[ -f "$TMP/src/$f" ]] || fail "Required file missing from the tarball: $f - update stopped, the existing suite is safe."
done
echo "[update] Tarball verified OK"

if [[ "$DRY_RUN" -eq 1 ]]; then
  echo "UPDATE DRY-RUN OK: all files are present; run without --dry-run to apply the update."
  exit 0
fi

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

# Restart the services (where present)
if command -v systemctl >/dev/null 2>&1; then
  systemctl restart vps-panel 2>/dev/null || true
  systemctl restart ws-proxy 2>/dev/null || true
  systemctl restart xray 2>/dev/null || true
fi

echo "UPDATE OK - the latest version of VPS Manager Suite is installed. Panel: systemctl status vps-panel"
