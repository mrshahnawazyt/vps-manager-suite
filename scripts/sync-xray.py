#!/usr/bin/env python3
"""VPS Manager Suite - Xray config sync (original code)

Panel/menu me bane hue Xray accounts (xray_users.json) ko asal Xray config
(/usr/local/etc/xray/config.json) me daalta hai — pehle accounts sirf panel
ke record me rehte thay, Xray unko pehchanta hi nahi tha.

- Template: suite ke templates/xray.json.tpl se (ya /opt/vps-manager-suite se)
- __UUID__ ki jagah ek bootstrap UUID (data dir me bootstrap.json me save)
- Har account ka UUID apne protocol wale inbounds me add hota hai:
  VLESS -> vless-ws, vless-xhttp, vless-tcp, tls-fallback
  VMess -> vmess-ws ; Trojan -> trojan-ws (password = account id)
- Expired accounts skip hote hain
- Config likhne ke baad `xray run -test` (binary ho to) aur --restart par service restart

Usage: python3 sync-xray.py [--restart]
Env: VPSMGR_DATA_DIR, VPSMGR_XRAY_CONFIG, VPSMGR_XRAY_TEMPLATE
"""
import json, os, sys, uuid, datetime, subprocess

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

def main():
    tpl_path = next((p for p in TPL_CANDIDATES if p and os.path.exists(p)), "")
    if not tpl_path:
        print("sync-xray: template nahi mili, skip"); return 0
    with open(tpl_path) as f: raw = f.read()
    seed = bootstrap_uuid()
    cfg = json.loads(raw.replace("__UUID__", seed))
    accounts = load_json(os.path.join(DATA_DIR, "xray_users.json"), [])
    by_tag = {ib.get("tag"): ib for ib in cfg.get("inbounds", [])}
    added = 0
    for a in accounts:
        uid = a.get("id")
        if not uid or is_expired(a): continue
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
                if not any(c.get("password") == uid for c in clients):
                    clients.append({"password": uid}); added += 1
            elif proto == "vmess":
                if not any(c.get("id") == uid for c in clients):
                    clients.append({"alterId": 0, "id": uid}); added += 1
            else:
                if not any(c.get("id") == uid for c in clients):
                    clients.append({"id": uid}); added += 1
    os.makedirs(os.path.dirname(OUT) or ".", exist_ok=True)
    with open(OUT, "w") as f: json.dump(cfg, f, indent=2)
    print(f"sync-xray: config likh di -> {OUT} (seed + {added} account-clients, kul accounts: {len(accounts)})")
    # Config test (binary ho to) — kharab config par purani service na giraye is liye pehle test
    xray = subprocess.getoutput("command -v xray").strip()
    if xray:
        r = subprocess.run([xray, "run", "-test", "-c", OUT], capture_output=True, text=True)
        if r.returncode != 0:
            print("sync-xray: WARNING xray config test FAIL:\n" + (r.stderr or r.stdout)[-800:]); return 1
        print("sync-xray: xray config test OK")
    if "--restart" in sys.argv and os.path.exists("/run/systemd/system"):
        subprocess.run(["systemctl", "restart", "xray"], capture_output=True)
        print("sync-xray: xray restart ki koshish ho gayi")
    return 0

if __name__ == "__main__":
    sys.exit(main())
