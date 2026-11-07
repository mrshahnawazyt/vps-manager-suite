#!/usr/bin/env bash
# VPS Manager Suite v1.0 - Uninstaller (original)
set -u
echo "VPS Manager Suite uninstall — services band hongi, config backup reh sakta hai."
read -rp "Pakka uninstall karna hai? (y/N): " ans; [[ "$ans" =~ ^[Yy]$ ]] || { echo "Cancel."; exit 0; }
if [[ "${EUID:-$(id -u)}" -eq 0 ]] && command -v systemctl >/dev/null; then
  for svc in vps-panel ws-proxy xray haproxy; do systemctl disable --now "$svc" 2>/dev/null || true; done
  rm -f /etc/systemd/system/vps-panel.service /etc/systemd/system/ws-proxy.service /etc/cron.d/vps-manager-limits
  systemctl daemon-reload || true
fi
rm -f /usr/local/bin/vps-panel.py /usr/local/bin/vps-ws-proxy.py /usr/local/bin/vpsmenu
echo "Services/files hata diye. Data backup ke liye /etc/vps-manager aur /var/lib/vps-manager manually check karo:"
echo "  sudo rm -rf /etc/vps-manager /var/lib/vps-manager   # sirf tab jab backup na chahiye ho"
echo "Uninstall done."
