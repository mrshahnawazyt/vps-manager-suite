#!/usr/bin/env python3
"""VPS Manager Suite - ZiVPN password sync (original code).

The ZiVPN UDP server authenticates clients with a plain password list.
This script rebuilds that list from the suite's SSH account store:
every active SSH account's password becomes a ZiVPN password, so ONE
account works for SSH, SlowDNS and ZiVPN UDP. Locked/disabled accounts
are excluded. Called at install, Fix Stack, and after panel SSH
account create/delete/lock operations (best-effort there).

Env: VPSMGR_DATA_DIR (panel store), VPSMGR_CONFIG_DIR (suite config).
"""
import json
import os
import sys

DATA_DIR = os.environ.get("VPSMGR_DATA_DIR", "/var/lib/vps-manager")
CONFIG_DIR = os.environ.get("VPSMGR_CONFIG_DIR", "/etc/vps-manager")
ZIVPN_DIR = os.path.join(CONFIG_DIR, "zivpn")
OUT = os.path.join(ZIVPN_DIR, "config.json")


def load(path, default):
    try:
        with open(path) as f:
            return json.load(f)
    except Exception:
        return default


def main():
    cfg = load(os.path.join(DATA_DIR, "config.json"), {})
    ssh_users = load(os.path.join(DATA_DIR, "ssh_users.json"), [])
    passwords = []
    for u in ssh_users:
        if not isinstance(u, dict):
            continue
        if u.get("disabled"):
            continue
        pw = str(u.get("password") or "").strip()
        if pw and pw not in passwords:
            passwords.append(pw)
    if not passwords:
        # The server refuses an empty list; keep one starter entry.
        passwords = ["zi"]
    obfs = str(cfg.get("zivpn_obfs") or "vpsmgr")
    out = {
        "listen": ":5667",
        "cert": os.path.join(ZIVPN_DIR, "zivpn.crt"),
        "key": os.path.join(ZIVPN_DIR, "zivpn.key"),
        "auth": {"mode": "passwords", "config": passwords},
        "obfs": obfs,
    }
    os.makedirs(ZIVPN_DIR, exist_ok=True)
    with open(OUT, "w") as f:
        json.dump(out, f, indent=2)
    print("ZiVPN: password list synced (%d active SSH account(s))" % len(passwords))
    return 0


if __name__ == "__main__":
    sys.exit(main())
