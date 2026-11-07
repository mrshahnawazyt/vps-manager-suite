#!/usr/bin/env python3
"""VPS Manager Suite - Xray config sync (original code)

Writes Xray accounts created in the panel/menu (xray_users.json) into the
real Xray config (/usr/local/etc/xray/config.json) — previously accounts
only existed as panel records and Xray never knew them.

- Template: the suite's templates/xray.json.tpl (or from /opt/vps-manager-suite)
- __UUID__ is replaced by a bootstrap UUID (saved in bootstrap.json in the data dir)
- Each account's UUID is added to ALL of its protocol's inbounds:
  VLESS  -> vless-ws, vless-xhttp, vless-xhttp-tls, vless-tcp, tls-fallback
  VMess  -> vmess-ws, vmess-xhttp, vmess-xhttp-tls, vmess-tcp
  Trojan -> trojan-ws, trojan-xhttp, trojan-xhttp-tls, trojan-tcp
  (Trojan clients use password = account id.)
- Live sync: when only the client sets changed (the inbound skeleton is
  identical to the one the running service loaded) and the xray CLI offers
  the HandlerService user commands (adu/rmu), accounts are added/removed
  through the API WITHOUT a restart, so connected users stay online.
  In every other case the config is tested and the service restarted.
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

def _dt_from_iso(s):
    """datetime.fromisoformat() equivalent that also works on Python 3.6.

    Parses the ISO strings this suite writes (YYYY-MM-DD, optionally with
    a time and a +/-HH:MM offset). Raises ValueError on bad input, like
    fromisoformat does.
    """
    s = str(s).strip()
    m = re.search(r"([+-])(\d{2}):(\d{2})(?::(\d{2}))?$", s)
    if m:  # strptime %z cannot parse a colon in the offset before 3.7
        s = s[:m.start()] + m.group(1) + m.group(2) + m.group(3) + (m.group(4) or "")
    for fmt in ("%Y-%m-%dT%H:%M:%S.%f%z", "%Y-%m-%dT%H:%M:%S%z",
                "%Y-%m-%dT%H:%M%z", "%Y-%m-%d %H:%M:%S.%f%z",
                "%Y-%m-%d %H:%M:%S%z", "%Y-%m-%d %H:%M%z",
                "%Y-%m-%dT%H:%M:%S.%f", "%Y-%m-%dT%H:%M:%S",
                "%Y-%m-%dT%H:%M", "%Y-%m-%d %H:%M:%S.%f",
                "%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%Y-%m-%d"):
        try:
            return datetime.datetime.strptime(s, fmt)
        except ValueError:
            continue
    raise ValueError("Invalid isoformat string: %r" % (s,))


def is_expired(acct):
    try:
        base = datetime.datetime.strptime(str(acct.get("created", ""))[:10], "%Y-%m-%d").date()  # 3.6-safe (no fromisoformat)
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
        return datetime.datetime.now() < _dt_from_iso(until)
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
                                "-subj", f"/CN={dom}"], stdout=subprocess.PIPE, stderr=subprocess.PIPE, universal_newlines=True)
            if r.returncode == 0 and os.path.exists(cf) and os.path.exists(kf):
                try: os.chmod(kf, 0o600)
                except Exception: pass
                print(f"sync-xray: TLS certificate was missing - created a self-signed pair at {cf}")
            else:
                print("sync-xray: WARNING could not create a self-signed certificate (openssl missing?) - Xray TLS inbounds will not start")
        except Exception as e:
            print(f"sync-xray: WARNING certificate creation failed: {e}")

def skeleton_hash(cfg):
    """Hash of the config with every client list emptied - identifies the
    inbound skeleton (ports/protocols/streams) independent of accounts."""
    import copy, hashlib
    sk = copy.deepcopy(cfg)
    for ib in sk.get("inbounds", []):
        st = ib.get("settings") or {}
        if "clients" in st:
            st["clients"] = []
    return hashlib.sha256(json.dumps(sk, sort_keys=True).encode()).hexdigest()


def desired_clients(cfg):
    """{tag: {email: client}} for all inbounds (seed clients have no email
    and are managed by the config file itself, never through the API)."""
    out = {}
    for ib in cfg.get("inbounds", []):
        m = {}
        for c in (ib.get("settings") or {}).get("clients") or []:
            if c.get("email"):
                m[c["email"]] = c
        if m:
            out[ib.get("tag")] = m
    return out


def api_user_sync(xray, cfg):
    """Reconcile live clients through the HandlerService API (adu/rmu),
    without restarting Xray. Returns True only if every change applied."""
    server = os.environ.get("VPSMGR_XRAY_STATS", "127.0.0.1:10085")
    try:
        h = subprocess.run([xray, "api", "--help"], stdout=subprocess.PIPE, stderr=subprocess.PIPE, universal_newlines=True, timeout=10)
        helptext = (h.stdout or "") + (h.stderr or "")
    except Exception:
        return False
    if "adu" not in helptext or "rmu" not in helptext:
        return False
    state_p = os.path.join(DATA_DIR, "xray_runtime.json")
    state = load_json(state_p, {})
    old = state.get("clients") or {}
    new = desired_clients(cfg)
    changes = []
    for tag in set(old) | set(new):
        o, n = old.get(tag, {}), new.get(tag, {})
        for em in o:
            if em not in n:
                changes.append(("rmu", tag, em, None))
        for em in n:
            if em not in o:
                changes.append(("adu", tag, em, n[em]))
    tmpd = None
    try:
        for op, tag, em, client in changes:
            if op == "rmu":
                r = subprocess.run([xray, "api", "rmu", "-server=" + server, "-tag", tag, em],
                                   stdout=subprocess.PIPE, stderr=subprocess.PIPE, universal_newlines=True, timeout=15)
            else:
                if tmpd is None:
                    import tempfile
                    tmpd = tempfile.mkdtemp(prefix="vpsm-api-")
                fp = os.path.join(tmpd, "user.json")
                with open(fp, "w") as f:
                    json.dump(client, f)
                r = subprocess.run([xray, "api", "adu", "-server=" + server, "-tag", tag, fp],
                                   stdout=subprocess.PIPE, stderr=subprocess.PIPE, universal_newlines=True, timeout=15)
            if r.returncode != 0:
                print("sync-xray: live API %s failed for %s/%s - falling back to restart" % (op, tag, em))
                return False
    except Exception as e:
        print("sync-xray: live API sync error: %s - falling back to restart" % e)
        return False
    state["clients"] = new
    try:
        with open(state_p, "w") as f:
            json.dump(state, f, indent=2)
    except Exception:
        pass
    print("sync-xray: live API sync applied %d client change(s), no restart" % len(changes))
    return True


def save_runtime(skeleton, clients):
    state_p = os.path.join(DATA_DIR, "xray_runtime.json")
    try:
        with open(state_p, "w") as f:
            json.dump({"skeleton": skeleton, "clients": clients}, f, indent=2)
    except Exception:
        pass


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
        if proto == "vless": targets = ["vless-ws", "vless-xhttp", "vless-xhttp-tls", "vless-tcp", "tls-fallback"]
        elif proto == "vmess": targets = ["vmess-ws", "vmess-xhttp", "vmess-xhttp-tls", "vmess-tcp"]
        elif proto == "trojan": targets = ["trojan-ws", "trojan-xhttp", "trojan-xhttp-tls", "trojan-tcp"]
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
        r = subprocess.run([xray, "run", "-test", "-config", OUT], stdout=subprocess.PIPE, stderr=subprocess.PIPE, universal_newlines=True)
        if r.returncode != 0:
            print("sync-xray: WARNING xray config test FAIL:\n" + (r.stderr or r.stdout)[-800:])
            # Fall back to the template-only seed config (no user accounts) so
            # Xray can still start. The accounts are not lost - they re-sync
            # on the next sync run once the cause above is fixed.
            with open(OUT, "w") as f: json.dump(seed_cfg, f, indent=2)
            print("sync-xray: fell back to the seed config (template only, no user accounts)")
            r2 = subprocess.run([xray, "run", "-test", "-config", OUT], stdout=subprocess.PIPE, stderr=subprocess.PIPE, universal_newlines=True)
            if r2.returncode != 0:
                print("sync-xray: ERROR even the seed config fails the test:\n" + (r2.stderr or r2.stdout)[-800:])
            else:
                print("sync-xray: seed config test OK - Xray can start; user accounts will re-sync on the next sync run")
            rc = 1
        else:
            print("sync-xray: xray config test OK")
    if "--restart" in sys.argv and os.path.exists("/run/systemd/system"):
        state = load_json(os.path.join(DATA_DIR, "xray_runtime.json"), {})
        applied = cfg if rc == 0 else seed_cfg
        live = False
        if rc == 0 and xray and state.get("skeleton") == skeleton_hash(cfg):
            act = subprocess.run(["systemctl", "is-active", "--quiet", "xray"], stdout=subprocess.PIPE, stderr=subprocess.PIPE)
            if act.returncode == 0:
                live = api_user_sync(xray, cfg)
        if not live:
            subprocess.run(["systemctl", "restart", "xray"], stdout=subprocess.PIPE, stderr=subprocess.PIPE)
            print("sync-xray: xray restart attempted")
            save_runtime(skeleton_hash(applied), desired_clients(applied) if rc == 0 else {})
    return rc

if __name__ == "__main__":
    sys.exit(main())
