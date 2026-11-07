#!/usr/bin/env bash
# VPS Manager Suite - SSL helper CLI (original code)
# Usage: ssl.sh {issue|force|dry-run|reapply|status}
#   issue    Issue or renew the certificate for the current domain
#            (Let's Encrypt, self-signed fallback) + install the
#            auto-renew deploy hook
#   force    Same, but forces renewal even when the cert is still valid
#   dry-run  Test auto-renewal (certbot renew --dry-run)
#   reapply  Re-apply the current Let's Encrypt cert to all services
#   status   Certificate summary + days left
set -u
HERE="$(cd "$(dirname "$(readlink -f "${BASH_SOURCE[0]}")")" && pwd)"
# shellcheck disable=SC1091
source "$HERE/../lib/common.sh"

domain_now(){
  local d=""
  d="$(get_domain)"
  [[ -z "$d" ]] && d="$(python3 -c "import json;print(json.load(open('$DATA_DIR/config.json')).get('domain',''))" 2>/dev/null || echo "")"
  echo "$d"
}

cmd="${1:-status}"
case "$cmd" in
  issue|force)
    d="$(domain_now)"
    [[ -z "$d" ]] && { err "No domain set - set one first (menu option 02)"; exit 1; }
    if [[ "$cmd" == "force" ]]; then issue_ssl "$d" force; else issue_ssl "$d"; fi
    rc=$?
    install_renewal_hook
    echo "--- Certificate status ---"
    ssl_cert_summary
    exit $rc
    ;;
  dry-run) ssl_dry_run ;;
  reapply) reapply_cert ;;
  status)
    echo "Domain  : $(domain_now)"
    ssl_cert_summary
    hook=/etc/letsencrypt/renewal-hooks/deploy/vps-manager.sh
    [[ -x "$hook" ]] && echo "Auto-renew hook: installed" || echo "Auto-renew hook: NOT installed (run: ssl.sh issue)"
    ;;
  *) echo "Usage: ssl.sh {issue|force|dry-run|reapply|status}"; exit 1 ;;
esac
