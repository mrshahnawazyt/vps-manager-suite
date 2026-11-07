#!/usr/bin/env bash
# VPS Manager Suite - Updater (original code)
# GitHub repo se latest version la kar /opt/vps-manager-suite update karta hai,
# panel/ws-proxy files aur vpsmenu/menu/vpsupdate wrappers refresh karta hai,
# aur vps-panel (+ ws-proxy) restart karta hai.
#
# Usage: sudo bash update.sh [--dry-run]
# Env overrides (testing ke liye):
#   VPSMGR_REPO=owner/repo        (default: mrshahnawazyt/vps-manager-suite)
#   VPSMGR_BRANCH=main
#   VPSMGR_TARBALL=/path/file.tgz (local tarball — download skip ho jata hai)
#   VPSMGR_SUITE_DIR=/opt/vps-manager-suite
set -u
REPO="${VPSMGR_REPO:-mrshahnawazyt/vps-manager-suite}"
BRANCH="${VPSMGR_BRANCH:-main}"
SUITE_DIR="${VPSMGR_SUITE_DIR:-/opt/vps-manager-suite}"
DRY_RUN=0; [[ "${1:-}" == "--dry-run" ]] && DRY_RUN=1

fail(){ echo "[UPDATE-FAIL] $*" >&2; exit 1; }

[[ "${EUID:-$(id -u)}" -eq 0 ]] || fail "Root chahiye: sudo bash update.sh"

TMP="$(mktemp -d)"; trap 'rm -rf "$TMP"' EXIT
TGZ="$TMP/suite.tgz"
if [[ -n "${VPSMGR_TARBALL:-}" ]]; then
  [[ -f "$VPSMGR_TARBALL" ]] || fail "VPSMGR_TARBALL wali file nahi mili: $VPSMGR_TARBALL"
  cp "$VPSMGR_TARBALL" "$TGZ"
  echo "[update] Local tarball use ho raha hai: $VPSMGR_TARBALL"
else
  echo "[update] Latest version download ho raha hai: $REPO@$BRANCH"
  curl -fsSL "https://api.github.com/repos/$REPO/tarball/$BRANCH" -o "$TGZ" \
    || fail "Download fail — internet/GitHub check karo."
fi

mkdir -p "$TMP/src"
tar -xzf "$TGZ" --strip-components=1 -C "$TMP/src" || fail "Tarball extract fail ho gaya."

# Safety: aadha/kharab download suite ko replace NA kare — pehle verify karo
for f in install.sh menu.sh update.sh web/panel.py lib/common.sh; do
  [[ -f "$TMP/src/$f" ]] || fail "Tarball me zaroori file nahi mili: $f — update rok diya, purana suite safe hai."
done
echo "[update] Tarball verify OK"

if [[ "$DRY_RUN" -eq 1 ]]; then
  echo "UPDATE DRY-RUN OK: sab files maujood hain, asal update ke liye baghair --dry-run chalao."
  exit 0
fi

mkdir -p "$SUITE_DIR"
cp -a "$TMP/src/." "$SUITE_DIR/"
chmod +x "$SUITE_DIR"/*.sh "$SUITE_DIR"/scripts/*.sh 2>/dev/null || true

# Panel / ws-proxy ki installed copies refresh karo
install -m 755 "$SUITE_DIR/web/panel.py" /usr/local/bin/vps-panel.py
install -m 755 "$SUITE_DIR/scripts/ws-proxy.py" /usr/local/bin/vps-ws-proxy.py

# Command wrappers refresh karo
for _cmd in vpsmenu menu; do
  printf '#!/usr/bin/env bash\nexec bash %s/menu.sh "$@"\n' "$SUITE_DIR" > "/usr/local/bin/$_cmd"
  chmod +x "/usr/local/bin/$_cmd"
done
printf '#!/usr/bin/env bash\nexec bash %s/update.sh "$@"\n' "$SUITE_DIR" > /usr/local/bin/vpsupdate
chmod +x /usr/local/bin/vpsupdate

# Services restart (jo maujood hon)
if command -v systemctl >/dev/null 2>&1; then
  systemctl restart vps-panel 2>/dev/null || true
  systemctl restart ws-proxy 2>/dev/null || true
fi

echo "UPDATE OK — VPS Manager Suite ka latest version lag gaya hai. Panel: systemctl status vps-panel"
