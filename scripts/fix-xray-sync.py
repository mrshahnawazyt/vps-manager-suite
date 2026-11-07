#!/usr/bin/env python3
"""VPS Manager Suite - Xray repair (original code)

Repairs the Xray side of the stack WITHOUT disturbing account data:

  1. Timestamped backups of the live Xray config and the nginx config
     (kept under <data>/backups/, the newest 5 repair sets are retained)
  2. The nginx router is re-rendered from the suite template with the
     current domain, so any missing WS/XHTTP locations come back
  3. The Xray config is rebuilt from the template by sync-xray.py -
     every existing account is preserved and re-added to its inbounds
  4. The rebuilt config must pass `xray run -test` before Xray is
     restarted; nginx is reloaded only when its config changed and
     passes `nginx -t`

Usage: python3 fix-xray-sync.py [--no-restart]
Env:   VPSMGR_DATA_DIR, VPSMGR_XRAY_CONFIG, VPSMGR_NGINX_CONF,
       VPSMGR_SUITE_DIR
"""
import datetime, json, os, shutil, subprocess, sys

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SUITE_DIR = os.environ.get("VPSMGR_SUITE_DIR", BASE)
DATA_DIR = os.environ.get("VPSMGR_DATA_DIR", "/var/lib/vps-manager")
XRAY_CFG = os.environ.get("VPSMGR_XRAY_CONFIG", "/usr/local/etc/xray/config.json")
NGINX_CFG = os.environ.get("VPSMGR_NGINX_CONF", "/etc/nginx/nginx.conf")
NO_RESTART = "--no-restart" in sys.argv


def run(cmd, timeout=60):
    try:
        return subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, universal_newlines=True, timeout=timeout)
    except Exception as e:
        class R:  # minimal failed-result stand-in
            returncode = 1
            stdout = ""
            stderr = str(e)
        return R()


def load_json(p, default):
    if os.path.exists(p):
        try:
            with open(p) as f:
                return json.load(f)
        except Exception:
            pass
    return default


def step(msg):
    print("[repair] %s" % msg)


def main():
    ts = datetime.datetime.now().strftime("%Y%m%d-%H%M%S")
    bdir = os.path.join(DATA_DIR, "backups", "xray-repair-" + ts)
    os.makedirs(bdir, exist_ok=True)

    # 1) backups
    for src, name in ((XRAY_CFG, "xray-config.json"), (NGINX_CFG, "nginx.conf")):
        if os.path.exists(src):
            shutil.copy2(src, os.path.join(bdir, name))
            step("backup: %s -> %s" % (src, os.path.join(bdir, name)))
    # keep only the newest 5 repair sets
    root = os.path.join(DATA_DIR, "backups")
    try:
        sets = sorted(d for d in os.listdir(root) if d.startswith("xray-repair-"))
        for old in sets[:-5]:
            shutil.rmtree(os.path.join(root, old), ignore_errors=True)
    except Exception:
        pass

    conf = load_json(os.path.join(DATA_DIR, "config.json"), {})
    domain = str(conf.get("domain") or "")

    # 2) nginx router re-render (repairs missing locations)
    tpl_p = os.path.join(SUITE_DIR, "templates", "nginx.conf.tpl")
    nginx_changed = False
    if os.path.exists(tpl_p) and domain:
        ws_port = str(conf.get("ws_proxy_port") or 8080)
        if not ws_port.isdigit():
            ws_port = "8080"
        with open(tpl_p) as f:
            want = f.read().replace("__DOMAIN__", domain).replace("__PANEL_PORT__", "9090").replace("__WSPROXY_PORT__", ws_port)
        have = ""
        if os.path.exists(NGINX_CFG):
            with open(NGINX_CFG) as f:
                have = f.read()
        if want != have:
            os.makedirs(os.path.dirname(NGINX_CFG), exist_ok=True)
            with open(NGINX_CFG, "w") as f:
                f.write(want)
            nginx_changed = True
            step("nginx router re-rendered from the template (locations repaired)")
        else:
            step("nginx router already matches the template")
    else:
        step("nginx re-render skipped (template or domain missing)")

    # 3) rebuild the Xray config through sync-xray (accounts preserved)
    sync = os.path.join(SUITE_DIR, "scripts", "sync-xray.py")
    if not os.path.exists(sync):
        step("ERROR: sync-xray.py not found at %s" % sync)
        return 1
    r = run([sys.executable, sync], timeout=120)
    for line in (r.stdout or "").strip().splitlines():
        step("sync: " + line)
    if r.returncode != 0:
        step("WARNING: sync reported a problem (see the lines above)")

    # 4) verify + restart
    xray = shutil.which("xray")
    if xray and os.path.exists(XRAY_CFG):
        t = run([xray, "run", "-test", "-config", XRAY_CFG], timeout=30)
        if t.returncode != 0:
            step("ERROR: the rebuilt Xray config FAILS the test - Xray was NOT restarted:")
            for line in ((t.stderr or "") + (t.stdout or "")).strip().splitlines()[-5:]:
                step("  " + line)
            step("the previous config is backed up at %s" % bdir)
            return 1
        step("Xray config test OK")
    if NO_RESTART or not os.path.exists("/run/systemd/system"):
        step("restart skipped")
        return 0
    if nginx_changed:
        nt = run(["nginx", "-t"], timeout=30)
        if nt.returncode == 0:
            run(["systemctl", "reload", "nginx"], timeout=30)
            step("nginx reloaded")
        else:
            step("WARNING: nginx -t failed - nginx was NOT reloaded")
    run(["systemctl", "restart", "xray"], timeout=60)
    act = run(["systemctl", "is-active", "xray"], timeout=15)
    if (act.stdout or "").strip() == "active":
        step("xray is running with the repaired config")
        return 0
    step("WARNING: xray is not active after the restart - check: journalctl -u xray -n 30")
    return 1


if __name__ == "__main__":
    sys.exit(main())
