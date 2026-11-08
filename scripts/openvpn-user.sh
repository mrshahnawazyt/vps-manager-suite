#!/usr/bin/env bash
# VPS Manager Suite - OpenVPN account helper (original code)
# Usage:
#   openvpn-user.sh add     <name> <days>   issue a certificate + profile
#   openvpn-user.sh renew   <name> <days>   renew the certificate
#   openvpn-user.sh revoke  <name>          revoke + regenerate the CRL
#   openvpn-user.sh profile <name>          (re)build the .ovpn profile
#   openvpn-user.sh list                    list issued client names
# Days is a literal count (the suite rule) - it becomes the certificate
# validity period.
set -u
CONFIG_DIR="${VPSMGR_CONFIG_DIR:-/etc/vps-manager}"
DATA_DIR="${VPSMGR_DATA_DIR:-/var/lib/vps-manager}"
OVPN_DIR="$CONFIG_DIR/openvpn"
ER="$OVPN_DIR/easy-rsa"
PKI="$OVPN_DIR/pki"
PORT=1194

ovpn_domain() {
  python3 - "$DATA_DIR/config.json" <<'PY' 2>/dev/null || echo ""
import json, sys
try: print(json.load(open(sys.argv[1])).get("domain", ""))
except Exception: print("")
PY
}

er() { ( cd "$ER" && EASYRSA_PKI="$PKI" EASYRSA_BATCH=1 ./easyrsa "$@" ); }

need_pki() {
  [[ -s "$PKI/ca.crt" && -x "$ER/easyrsa" ]] || {
    echo "OpenVPN PKI is not built yet - run install or Fix Stack first." >&2; exit 1; }
}

valid_name() { [[ "$1" =~ ^[A-Za-z0-9_-]{1,32}$ ]]; }

build_profile() {
  local name="$1" dom prof
  dom="$(ovpn_domain)"
  [[ -n "$dom" ]] || dom="$(hostname -f 2>/dev/null || echo "YOUR-SERVER")"
  mkdir -p "$OVPN_DIR/profiles"
  prof="$OVPN_DIR/profiles/$name.ovpn"
  {
    echo "client"
    echo "dev tun"
    echo "proto udp"
    echo "remote $dom $PORT"
    echo "resolv-retry infinite"
    echo "nobind"
    echo "persist-key"
    echo "persist-tun"
    echo "remote-cert-tls server"
    echo "cipher AES-256-GCM"
    echo "auth SHA256"
    echo "key-direction 1"
    echo "verb 3"
    echo "<ca>";        cat "$PKI/ca.crt";                          echo "</ca>"
    echo "<cert>";      cat "$PKI/issued/$name.crt";                echo "</cert>"
    echo "<key>";       cat "$PKI/private/$name.key";               echo "</key>"
    echo "<tls-auth>";  cat "$OVPN_DIR/ta.key";                     echo "</tls-auth>"
  } > "$prof" || return 1
  chmod 600 "$prof"
  echo "$prof"
}

cmd="${1:-}"; name="${2:-}"; days="${3:-30}"
case "$cmd" in
  add)
    need_pki; valid_name "$name" || { echo "Invalid name (letters, digits, - and _ only, max 32)." >&2; exit 1; }
    [[ "$days" =~ ^[0-9]+$ ]] || days=30
    if [[ -s "$PKI/issued/$name.crt" ]]; then
      echo "Account '$name' already has a certificate - use renew or revoke first." >&2; exit 1
    fi
    EASYRSA_CERT_EXPIRE="$days" er gen-req "$name" nopass >/dev/null 2>&1 \
      || { echo "Certificate request failed for '$name'." >&2; exit 1; }
    EASYRSA_CERT_EXPIRE="$days" er sign-req client "$name" >/dev/null 2>&1 \
      || { echo "Certificate signing failed for '$name'." >&2; exit 1; }
    build_profile "$name" >/dev/null || { echo "Profile build failed for '$name'." >&2; exit 1; }
    echo "OpenVPN account '$name' created (certificate valid $days day(s))."
    echo "Profile: $OVPN_DIR/profiles/$name.ovpn"
    ;;
  renew)
    need_pki; valid_name "$name" || { echo "Invalid name." >&2; exit 1; }
    [[ "$days" =~ ^[0-9]+$ ]] || days=30
    [[ -s "$PKI/issued/$name.crt" ]] || { echo "No certificate for '$name'." >&2; exit 1; }
    # easy-rsa renew keeps the same key; fall back to re-issue if needed
    if ! EASYRSA_CERT_EXPIRE="$days" er renew "$name" >/dev/null 2>&1; then
      er revoke "$name" >/dev/null 2>&1 || true
      er gen-crl >/dev/null 2>&1 || true
      cp -f "$PKI/crl.pem" "$OVPN_DIR/crl.pem" 2>/dev/null || true
      rm -f "$PKI/issued/$name.crt" "$PKI/reqs/$name.req"
      EASYRSA_CERT_EXPIRE="$days" er gen-req "$name" nopass >/dev/null 2>&1 \
        && EASYRSA_CERT_EXPIRE="$days" er sign-req client "$name" >/dev/null 2>&1 \
        || { echo "Renew failed for '$name'." >&2; exit 1; }
    fi
    build_profile "$name" >/dev/null || { echo "Profile build failed for '$name'." >&2; exit 1; }
    echo "OpenVPN account '$name' renewed (certificate valid $days day(s) from today)."
    ;;
  revoke)
    need_pki; valid_name "$name" || { echo "Invalid name." >&2; exit 1; }
    er revoke "$name" >/dev/null 2>&1 || { echo "Revoke failed for '$name' (unknown account?)." >&2; exit 1; }
    er gen-crl >/dev/null 2>&1 || true
    cp -f "$PKI/crl.pem" "$OVPN_DIR/crl.pem" 2>/dev/null || true
    echo "OpenVPN account '$name' revoked. Restart the vps-openvpn service so the new revocation list is loaded."
    ;;
  profile)
    need_pki; valid_name "$name" || { echo "Invalid name." >&2; exit 1; }
    build_profile "$name"
    ;;
  list)
    ls "$PKI/issued" 2>/dev/null | sed 's/\.crt$//' | grep -vx server || true
    ;;
  *)
    echo "Usage: openvpn-user.sh add|renew|revoke|profile|list ..." >&2; exit 2 ;;
esac
