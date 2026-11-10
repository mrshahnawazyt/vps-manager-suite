#!/usr/bin/env python3
"""
VPS Manager Suite - SNI Manager sync (original code, stdlib only).

Reads the first-party connection profiles saved by the web panel
(config-profiles.json in the data dir) and renders the routing maps the
front door can consume:

  * an HAProxy SNI map      ("<sni> <backend>" per line)
  * an nginx profile map    (profile name per owned host)

FIRST-PARTY ONLY: a profile is accepted only when its domain is the
suite's own configured domain (config.json "domain"), a subdomain of
it, or the configured SlowDNS / UDP host - which are themselves inside
that domain on a normal install. Every other hostname is rejected
here AND in the panel, so a profile can never point clients at a
third-party host.

Backends: every map value is a backend name that exists in
templates/haproxy.cfg.tpl. The only SNI target that preserves the
live-verified port-443 chain for every client shape is the chain's own
default entry (https_dispatch_in), so all first-party profiles map
there. The map is consumed by frontend https_in in the template via
ssl_fc_sni (the one place the SNI is visible - TLS is terminated
there); a hit routes to the same backend as the default, and an
empty map changes nothing.

Paths (env overrides exist for testing):
  VPSMGR_DATA_DIR   data dir holding config.json + config-profiles.json
  VPSMGR_SNI_MAP    HAProxy map output (default /etc/vps-manager/sni.map)
  VPSMGR_NGINX_MAP  nginx map output (default /etc/vps-manager/nginx-profile-map.conf)

Writes are atomic (temp file + os.replace) and skipped when the
rendered content is unchanged (sha256 comparison). The word "changed"
or "unchanged" is printed on its own line so the shell wrapper knows
whether a graceful HAProxy reload is needed.
"""
import hashlib
import json
import os
import re
import sys
import tempfile

DATA_DIR = os.environ.get("VPSMGR_DATA_DIR", "/var/lib/vps-manager")
CONFIG_DIR = os.environ.get("VPSMGR_CONFIG_DIR", "/etc/vps-manager")
SNI_MAP_PATH = os.environ.get("VPSMGR_SNI_MAP", os.path.join(CONFIG_DIR, "sni.map"))
NGINX_MAP_PATH = os.environ.get("VPSMGR_NGINX_MAP", os.path.join(CONFIG_DIR, "nginx-profile-map.conf"))

PROTOCOLS = ("ssh-tls", "vmess", "vless", "trojan", "openvpn", "slowdns")
DEFAULT_PORTS = {"ssh-tls": 443, "vmess": 443, "vless": 443,
                 "trojan": 443, "openvpn": 1194, "slowdns": 53}
# Ports that must never be handed to a client as a profile endpoint:
# the panel itself (9090) and the loopback/internal stack ports.
# (Mirrors the internal part of the RESERVED set in panel-admin.sh;
# the stack's public service ports - 443, 1194, 53 - are the legitimate
# profile endpoints and are NOT blocked.)
BLOCKED_PORTS = {9090, 10085, 10443, 10444, 10445, 18080}

# The only connection paths that exist on this stack, per protocol
# (mirrors CONFIG_PATHS in web/panel.py). OpenVPN / SlowDNS have none.
PROFILE_PATHS = {"ssh-tls": ("/sshws",), "vmess": ("/vmess",),
                 "vless": ("/vless",), "trojan": ("/trojan",),
                 "openvpn": (), "slowdns": ()}


def profile_path(proto, path=None):
    """Only the stack's real endpoint for the protocol is kept; any
    other stored value (or a profile saved before paths existed) falls
    back to that default. OpenVPN / SlowDNS carry an empty path."""
    valid = PROFILE_PATHS.get(str(proto or ""), ())
    if path and str(path) in valid:
        return str(path)
    return valid[0] if valid else ""

# SNI -> backend. Only names that exist in templates/haproxy.cfg.tpl.
# https_dispatch_in is the entry backend of the verified 443 chain
# (https_in -> https_dispatch -> https_http / ws-proxy): routing a
# first-party SNI there is exactly today's default behaviour.
SNI_BACKEND = "https_dispatch_in"

_HOST_LABEL = re.compile(r"^[A-Za-z0-9]([A-Za-z0-9-]{0,61}[A-Za-z0-9])?$")


def valid_hostname(v):
    v = str(v or "").strip().rstrip(".")
    if not v or len(v) > 253:
        return False
    return all(_HOST_LABEL.match(part) for part in v.split("."))


def norm_host(v):
    return str(v or "").strip().rstrip(".").lower()


def owned_domains(cfg):
    """The hostnames this install may serve profiles for: the configured
    domain plus the configured SlowDNS / UDP hosts."""
    out = []
    dom = norm_host(cfg.get("domain"))
    if dom:
        out.append(dom)
    sd = norm_host(cfg.get("slowdns_domain")) or (("dns." + dom) if dom else "")
    if sd:
        out.append(sd)
    udp = norm_host(cfg.get("udp_domain")) or dom
    if udp:
        out.append(udp)
    seen, res = set(), []
    for d in out:
        if d and d not in seen:
            seen.add(d)
            res.append(d)
    return res


def is_owned(host, cfg):
    """True only for an owned domain itself or a subdomain of one.
    Suffix tricks fail: 'my7.ddkvps.com.evil.com' is not a subdomain of
    'my7.ddkvps.com', and a look-alike parent ('ddkvps.com.evil.com')
    never matches either."""
    h = norm_host(host)
    if not h or not valid_hostname(h):
        return False
    for base in owned_domains(cfg):
        if h == base or h.endswith("." + base):
            return True
    return False


def ssh_payload(domain):
    return ("GET /sshws HTTP/1.1[crlf]Host: %s[crlf]Upgrade: websocket[crlf][crlf]"
            % domain)


def validate_profile(raw, cfg, seen_ids=None):
    """-> (clean_profile, None) or (None, error_string).
    The SNI is never taken from input: it is always the profile domain."""
    if not isinstance(raw, dict):
        return None, "profile is not an object"
    name = str(raw.get("name") or "").strip()
    if not (1 <= len(name) <= 60):
        return None, "profile name must be 1-60 characters"
    proto = str(raw.get("protocol") or "").strip().lower()
    if proto not in PROTOCOLS:
        return None, "unknown protocol: %s" % (proto or "(blank)")
    domain = norm_host(raw.get("domain"))
    if not is_owned(domain, cfg):
        return None, ("domain '%s' is not one of this server's own domains - "
                      "only the configured domain and its subdomains are allowed"
                      % (domain or "(blank)"))
    try:
        port = int(str(raw.get("port", DEFAULT_PORTS[proto])).strip())
    except Exception:
        return None, "port is not a number"
    if not (1 <= port <= 65535):
        return None, "port out of range (1-65535)"
    if port in BLOCKED_PORTS:
        return None, "port %d is an internal/panel port and cannot be a client endpoint" % port
    pid = str(raw.get("id") or "").strip()
    if seen_ids is not None:
        if not pid:
            return None, "profile has no id"
        if pid in seen_ids:
            return None, "duplicate profile id: %s" % pid
        seen_ids.add(pid)
    prof = {
        "id": pid,
        "name": name,
        "protocol": proto,
        "domain": domain,
        "port": port,
        "sni": domain,
        "path": profile_path(proto, raw.get("path")),
        "active": bool(raw.get("active", True)),
    }
    if proto == "ssh-tls":
        prof["payload"] = ssh_payload(domain)
    return prof, None


def load_json_file(path, default):
    if os.path.exists(path):
        try:
            with open(path) as f:
                return json.load(f)
        except Exception:
            return default
    return default


def load_inputs(data_dir=DATA_DIR):
    cfg = load_json_file(os.path.join(data_dir, "config.json"), {})
    raw = load_json_file(os.path.join(data_dir, "config-profiles.json"), [])
    if not isinstance(raw, list):
        raw = []
    return cfg, raw


def collect_profiles(cfg, raw):
    """Validate every stored profile. Returns (valid, errors)."""
    valid, errors, seen = [], [], set()
    for i, item in enumerate(raw):
        prof, err = validate_profile(item, cfg, seen_ids=seen)
        if err:
            errors.append("profile #%d: %s" % (i + 1, err))
        else:
            valid.append(prof)
    return valid, errors


def render_sni_map(profiles):
    lines = ["# VPS Manager Suite - SNI Manager SNI map",
             "# Generated by scripts/config-sync.py - do not edit by hand.",
             "# Format: <sni> <backend>   (backends exist in templates/haproxy.cfg.tpl)"]
    for p in sorted((x for x in profiles if x.get("active")),
                    key=lambda x: x["sni"]):
        lines.append("%s %s" % (p["sni"], SNI_BACKEND))
    return "\n".join(lines) + "\n"


def render_nginx_map(profiles):
    lines = ["# VPS Manager Suite - SNI Manager profile map (nginx)",
             "# Generated by scripts/config-sync.py - do not edit by hand.",
             "# Maps each first-party host to its active profile name.",
             "map $host $vpsmgr_config_profile {"]
    for p in sorted((x for x in profiles if x.get("active")),
                    key=lambda x: x["domain"]):
        lines.append('    "%s" "%s";' % (p["domain"], p["name"].replace('"', "'")))
    lines.append('    default "";')
    lines.append("}")
    return "\n".join(lines) + "\n"


def sha256_text(text):
    return hashlib.sha256(text.encode()).hexdigest()


def file_sha256(path):
    if not os.path.exists(path):
        return ""
    with open(path, "rb") as f:
        return hashlib.sha256(f.read()).hexdigest()


def atomic_write(path, content):
    d = os.path.dirname(os.path.abspath(path))
    os.makedirs(d, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=d, prefix=".cm-", suffix=".tmp")
    try:
        with os.fdopen(fd, "w") as f:
            f.write(content)
        os.replace(tmp, path)
    except Exception:
        try:
            os.unlink(tmp)
        except Exception:
            pass
        raise


def sync(data_dir=DATA_DIR, sni_path=SNI_MAP_PATH, nginx_path=NGINX_MAP_PATH, out=None):
    """Run one sync pass. Returns (changed, profiles, errors)."""
    out = out or sys.stdout
    cfg, raw = load_inputs(data_dir)
    profiles, errors = collect_profiles(cfg, raw)
    for e in errors:
        print("[WARN] Skipped - %s" % e, file=out)
    changed = False
    for path_, content in ((sni_path, render_sni_map(profiles)),
                           (nginx_path, render_nginx_map(profiles))):
        if file_sha256(path_) != sha256_text(content):
            atomic_write(path_, content)
            changed = True
            print("[OK] Wrote %s" % path_, file=out)
    active = sum(1 for p in profiles if p.get("active"))
    print("[OK] Config sync: %d profile(s), %d active, %d skipped"
          % (len(profiles), active, len(errors)), file=out)
    print("changed" if changed else "unchanged", file=out)
    return changed, profiles, errors


def main():
    sync()
    return 0


if __name__ == "__main__":
    sys.exit(main())
