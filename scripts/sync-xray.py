#!/usr/bin/env python3
"""VPS Manager Suite - Xray config sync (original code)

Writes Xray accounts created in the panel/menu (xray_users.json) into the
real Xray config (/usr/local/etc/xray/config.json) — previously accounts
only existed as panel records and Xray never knew them.

- Template: the suite's templates/xray.json.tpl (or from /opt/vps-manager-suite)
- __UUID__ is replaced by a bootstrap UUID (saved in bootstrap.json in the data dir)
- Each account's UUID is added to its protocol's inbounds:
  VLESS -> vless-ws, vless-xhttp, vless-tcp, tls-fallback
  VMess -> vmess-ws ; Trojan -> trojan-ws (password = account id)
- Every client gets "email" = remark (sanitized) so Xray stats and the
  access log attribute traffic to that account (used by limit-check)
- Expired, disabled (by limit-check) and IP-blocked accounts are skipped
- TLS cert/key pairs referenced by the config: if a pair is completely
  missing, a self-signed pair is created (a real certificate is never
  overwritten) - otherwise Xray exits at once on the missing files
- After writing, the config is tested with `xray run -test` (if the binary
  exists) and the service is restarted on --restart. If the test FAILS, the
  template-only seed config is written instead (accounts re-sync next run),
  so a known-bad config is never left in place

Usage: python3 sync-xray.py [--restart]
Env: VPSMGR_DATA_DIR, VPSMGR_XRAY_CONFIG, VPSMGR_XRAY_TEMPLATE
"""
import json, os, re, sys, uuid, datetime, subprocess

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA_DIR = os.environ.get("VPSMGR_DATA_DIR", "/var/lib/vps-manager")
OUT = os.environ.get("VPSMGR_XRAY_CONFIG", "/usr/local/etc/xray/config.json")
TPL_CANDIDATES = [
    os.environ.get("VPSMGR_XRAY_TEMPLATE", ""),
    os.path.join(BASE, "templates", "xray.json.tpl"),
    "/opt/vps-manager-suite/templates/xray.json.tpl",
]

def load_json(p, default):
    if os.path.exists(p):
        try:
            with open(p) as f: return json.load(f)
        except Exception: pass
    return default

def bootstrap_uuid():
    p = os.path.join(DATA_DIR, "bootstrap.json")
    d = load_json(p, {})
    if not d.get("uuid"):
        d["uuid"] = str(uuid.uuid4())
        try:
            os.makedirs(DATA_DIR, exist_ok=True)
            with open(p, "w") as f: json.dump(d, f, indent=2)
        except Exception: pass
    return d["uuid"]

def is_expired(acct):
    try:
        base = datetime.date.fromisoformat(str(acct.get("created", ""))[:10])
        return datetime.date.today() > base + datetime.timedelta(days=int(acct.get("expiry_days", 30)))
    except Exception:
        return False

def stats_email(acct):
    """Per-user email for Xray stats/log attribution.
    limit-check.sh uses this exact same function — keep both in sync."""
    e = re.sub(r"[^A-Za-z0-9@._\-]", "_", str(acct.get("remark") or acct.get("id") or ""))
    return e or str(acct.get("id") or "")

def ip_block_active(acct):
    """limit-check sets ip_blocked_until (ISO datetime) when the IP limit is exceeded."""
    until = acct.get("ip_blocked_until")
    if not until:
        return False
    try:
        return datetime.datetime.now() < datetime.datetime.fromisoformat(str(until))
    except Exception:
        return False

def ensure_certs(cfg):
    """Make sure every TLS cert/key pair referenced by the config exists.

    A completely missing pair is created self-signed (the panel domain is
    used as CN when known). A real certificate is NEVER overwritten; a
    half-missing pair is only reported, because replacing one half would
    break the pair - re-issue SSL from menu option 03 in that case.
    """
    pairs = set()
    for ib in cfg.get("inbounds", []):
        tls = (ib.get("streamSettings") or {}).get("tlsSettings") or {}
        for c in tls.get("certificates", []) or []:
            cf, kf = c.get("certificateFile"), c.get("keyFile")
            if cf and kf:
                pairs.add((cf, kf))
    for cf, kf in sorted(pairs):
        have_c = os.path.exists(cf) and os.path.getsize(cf) > 0
        have_k = os.path.exists(kf) and os.path.getsize(kf) > 0
        if have_c and have_k:
            continue
        if have_c != have_k:
            print(f"sync-xray: WARNING certificate pair is incomplete ({cf} / {kf}) - Xray cannot start; re-issue SSL from menu option 03")
            continue
        dom = "localhost"
        conf = load_json(os.path.join(DATA_DIR, "config.json"), {})
        if conf.get("domain"):
            dom = str(conf["domain"])
        try:
            os.makedirs(os.path.dirname(cf) or ".", exist_ok=True)
            r = subprocess.run(["openssl", "req", "-x509", "-newkey", "rsa:2048",
                                "-keyout", kf, "-out", cf, "-days", "365", "-nodes",
                                "-subj", f"/CN={dom}"], capture_output=True, text=True)
            if r.returncode == 0 and os.path.exists(cf) and os.path.exists(kf):
                try: os.chmod(kf, 0o600)
                except Exception: pass
                print(f"sync-xray: TLS certificate was missing - created a self-signed pair at {cf}")
            else:
                print("sync-xray: WARNING could not create a self-signed certificate (openssl missing?) - Xray TLS inbounds will not start")
        except Exception as e:
            print(f"sync-xray: WARNING certificate creation failed: {e}")

def main():
    tpl_path = next((p for p in TPL_CANDIDATES if p and os.path.exists(p)), "")
    if not tpl_path:
        print("sync-xray: template not found, skip"); return 0
    with open(tpl_path) as f: raw = f.read()
    seed = bootstrap_uuid()
    cfg = json.loads(raw.replace("__UUID__", seed))
    seed_cfg = json.loads(json.dumps(cfg))  # template-only config, kept as the fallback
    ensure_certs(cfg)
    accounts = load_json(os.path.join(DATA_DIR, "xray_users.json"), [])
    by_tag = {ib.get("tag"): ib for ib in cfg.get("inbounds", [])}
    added = 0
    skipped = 0
    for a in accounts:
        uid = a.get("id")
        # Disabled (expiry/quota — limit-check) or IP-block active: NOT added to the config
        if not uid or is_expired(a) or a.get("disabled") is True or ip_block_active(a):
            skipped += 1
            continue
        email = stats_email(a)
        proto = (a.get("protocol") or "").lower()
        targets = []
        if proto == "vless": targets = ["vless-ws", "vless-xhttp", "vless-tcp", "tls-fallback"]
        elif proto == "vmess": targets = ["vmess-ws"]
        elif proto == "trojan": targets = ["trojan-ws"]
        for tag in targets:
            ib = by_tag.get(tag)
            if not ib: continue
            clients = ib.setdefault("settings", {}).setdefault("clients", [])
            if proto == "trojan":
                ex = next((c for c in clients if c.get("password") == uid), None)
                if ex is None:
                    clients.append({"password": uid, "email": email}); added += 1
                else:
                    ex.setdefault("email", email)
            elif proto == "vmess":
                ex = next((c for c in clients if c.get("id") == uid), None)
                if ex is None:
                    clients.append({"alterId": 0, "id": uid, "email": email}); added += 1
                else:
                    ex.setdefault("email", email)
            else:
                ex = next((c for c in clients if c.get("id") == uid), None)
                if ex is None:
                    clients.append({"id": uid, "email": email}); added += 1
                else:
                    ex.setdefault("email", email)
    os.makedirs(os.path.dirname(OUT) or ".", exist_ok=True)
    with open(OUT, "w") as f: json.dump(cfg, f, indent=2)
    print(f"sync-xray: config written -> {OUT} (seed + {added} account-clients, total accounts: {len(accounts)}, skipped: {skipped})")
    # Config test (if binary exists) — test first so a bad config never takes the running service down
    xray = subprocess.getoutput("command -v xray").strip()
    rc = 0
    if xray:
        r = subprocess.run([xray, "run", "-test", "-config", OUT], capture_output=True, text=True)
        if r.returncode != 0:
            print("sync-xray: WARNING xray config test FAIL:\n" + (r.stderr or r.stdout)[-800:])
            # Fall back to the template-only seed config (no user accounts) so
            # Xray can still start. The accounts are not lost - they re-sync
            # on the next sync run once the cause above is fixed.
            with open(OUT, "w") as f: json.dump(seed_cfg, f, indent=2)
            print("sync-xray: fell back to the seed config (template only, no user accounts)")
            r2 = subprocess.run([xray, "run", "-test", "-config", OUT], capture_output=True, text=True)
            if r2.returncode != 0:
                print("sync-xray: ERROR even the seed config fails the test:\n" + (r2.stderr or r2.stdout)[-800:])
            else:
                print("sync-xray: seed config test OK - Xray can start; user accounts will re-sync on the next sync run")
            rc = 1
        else:
            print("sync-xray: xray config test OK")
    if "--restart" in sys.argv and os.path.exists("/run/systemd/system"):
        subprocess.run(["systemctl", "restart", "xray"], capture_output=True)
        print("sync-xray: xray restart attempted")
    return rc

if __name__ == "__main__":
    sys.exit(main())
