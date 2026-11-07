#!/usr/bin/env python3
"""
VPS Manager Suite v1.0 - Web Panel (original code, stdlib only)
Listen: HTTPS if cert exists else HTTP, default port 9090 (env VPSMGR_PORT)
Data dir: env VPSMGR_DATA_DIR (default /var/lib/vps-manager, dev fallback: ./data)
Credentials: /etc/vps-manager/credentials.json if it exists, else $DATA_DIR/credentials.json
             If no credentials file is found, a RANDOM password is generated (there is
             NO default password) and printed once in the panel log/stdout.
"""
import os, json, time, uuid, hashlib, secrets, subprocess, shutil, urllib.parse, html
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from http.cookies import SimpleCookie
import ssl

BASE = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.environ.get("VPSMGR_DATA_DIR", "/var/lib/vps-manager")
# Local dev: if /var/lib is not writable, fall back to ./data
try:
    os.makedirs(DATA_DIR, exist_ok=True)
except Exception:
    DATA_DIR = os.path.join(os.getcwd(), "data"); os.makedirs(DATA_DIR, exist_ok=True)

PORT = int(os.environ.get("VPSMGR_PORT", "9090"))
HOST = os.environ.get("VPSMGR_HOST", "0.0.0.0")
CERT_FILE = os.environ.get("VPSMGR_CERT", "/etc/vps-manager/ssl/fullchain.pem")
KEY_FILE = os.environ.get("VPSMGR_KEY", "/etc/vps-manager/ssl/privkey.pem")
SYSTEM_CREDS = "/etc/vps-manager/credentials.json"

def path(name): return os.path.join(DATA_DIR, name)

def load_json(name, default):
    p = path(name)
    if os.path.exists(p):
        try:
            with open(p) as f: return json.load(f)
        except Exception: return default
    return default

def save_json(name, data):
    with open(path(name), "w") as f: json.dump(data, f, indent=2)

def load_creds():
    # System credentials first
    for cand in (SYSTEM_CREDS, path("credentials.json")):
        if os.path.exists(cand):
            try:
                with open(cand) as f: return json.load(f)
            except Exception: pass
    # No credentials file - generate a random password (a default password would be a security bug)
    creds = {"username": "admin", "password": secrets.token_urlsafe(12)}
    save_json("credentials.json", creds)
    print(f"WARNING: no credentials file found - generated a new admin password: {creds['password']}  (saved in credentials.json in the data dir; log in and change it)", flush=True)
    return creds

def load_config(): return load_json("config.json", {"domain": "", "banner": "Welcome to VPS Manager Suite", "qos_interface": "eth0", "qos_rate": "950mbit", "per_client_cap": "0"})

def sync_xray():
    """Send Xray accounts to the real xray config (best-effort)."""
    for cand in ("/opt/vps-manager-suite/scripts/sync-xray.py",
                 os.path.join(BASE, "..", "scripts", "sync-xray.py"),
                 os.path.join(os.getcwd(), "scripts", "sync-xray.py")):
        if os.path.exists(cand):
            try: subprocess.run(["python3", cand, "--restart"], timeout=30, capture_output=True)
            except Exception: pass
            return
def save_config(c): save_json("config.json", c)

def log_activity(action):
    logs = load_json("activity.json", [])
    logs.insert(0, {"time": time.strftime("%Y-%m-%d %H:%M:%S"), "action": action})
    save_json("activity.json", logs[:300])

# --- LIMIT SEMANTICS (user-confirmed, EXACT) ---------------------------------
# Days / Expiry:      LITERAL count only. Default 30. Expiry = creation date + Days.
#                     0 means 0 days (NOT unlimited for expiry).
# GB limit:           0 = Unlimited.  Label in UI: "GB limit (0 = unlimited)". >0 enforces quota.
# IP limit:           0 = Unlimited.  Label in UI: "IP limit (0 = unlimited)". >0 enforces max IPs/logins.
# Enforcement MUST skip GB/IP checks when those limits are 0; expiry is always checked literally.
def to_int(v, default=0):
    try: return int(str(v).strip())
    except Exception: return default

def fmt_expiry(days):
    """Display helper: literal days only, e.g. '30 days'."""
    d = to_int(days, 30)
    return f"{d} days"

def fmt_limit(val, suffix=""):
    """Display helper: 0 -> 'Unlimited', else the number (+suffix)."""
    n = to_int(val, 0)
    return "Unlimited" if n == 0 else f"{n}{suffix}"

def fmt_data(val): return fmt_limit(val, " GB")
def fmt_logins(val): return fmt_limit(val)

def expires_on(created_ymd, days):
    """Expiry date string: ALWAYS created + Days (literal). 0 = same day as creation."""
    d = to_int(days, 30)
    try:
        import datetime as _dt
        base = _dt.date.fromisoformat(str(created_ymd)[:10])
        return (base + _dt.timedelta(days=d)).isoformat()
    except Exception: return f"+{d} days"

def is_expired(created_ymd, days):
    """Enforcement: literal expiry check. Days=0 means expiry date = creation date."""
    d = to_int(days, 30)
    try:
        import datetime as _dt
        base = _dt.date.fromisoformat(str(created_ymd)[:10])
        return _dt.date.today() > base + _dt.timedelta(days=d)
    except Exception: return False

def quota_exceeded(used_gb, limit_gb):
    """Enforcement: skip when 0 (Unlimited)."""
    lim = to_int(limit_gb, 0)
    if lim == 0: return False
    try: return float(used_gb) >= float(lim)
    except Exception: return False

def ip_limit_exceeded(active_ips, limit):
    """Enforcement: skip when 0 (Unlimited). active_ips = count or iterable."""
    lim = to_int(limit, 0)
    if lim == 0: return False
    count = active_ips if isinstance(active_ips, (int, float)) else len(list(active_ips))
    return count > lim

def account_status(acct):
    """Combined check - 0 limits are never enforced."""
    if is_expired(acct.get("created", ""), acct.get("expiry_days", 30)): return "Expired"
    if quota_exceeded(acct.get("used_gb", 0), acct.get("gb_limit", 0)): return "Quota Full"
    return "Active"

SESSIONS = {}

def _cpu_times():
    with open("/proc/stat") as f:
        vals = [int(x) for x in f.readline().split()[1:]]
    idle = vals[3] + (vals[4] if len(vals) > 4 else 0)
    return sum(vals), idle

def sys_stats():
    stats = {"cpu": 0, "ram": 0, "disk": 0, "uptime": ""}
    try:
        t1, i1 = _cpu_times(); time.sleep(0.1); t2, i2 = _cpu_times()
        dt, di = t2 - t1, i2 - i1
        stats["cpu"] = round(100 * (1 - di / dt), 1) if dt > 0 else 0
    except Exception: pass
    try:
        # Load average from /proc/loadavg
        with open("/proc/loadavg") as f: stats["load"] = f.read().split()[:3]
    except Exception: stats["load"] = []
    try:
        mem = {}
        with open("/proc/meminfo") as f:
            for line in f:
                k, v = line.split(":"); mem[k] = int(v.split()[0])
        total = mem.get("MemTotal", 1); avail = mem.get("MemAvailable", 0)
        stats["ram"] = round((total - avail) * 100 / total, 1); stats["ram_total_gb"] = round(total/1024/1024, 2)
    except Exception: pass
    try:
        st = shutil.disk_usage("/"); stats["disk"] = round(st.used*100/st.total, 1); stats["disk_total_gb"] = round(st.total/1024**3, 1)
    except Exception: pass
    try: stats["uptime"] = subprocess.getoutput("uptime -p").strip()
    except Exception: pass
    # Network totals
    try:
        with open("/proc/net/dev") as f: lines = f.readlines()[2:]
        rx = tx = 0
        for l in lines:
            parts = l.split()
            if parts[0].startswith("lo:"): continue
            rx += int(parts[1]); tx += int(parts[9])
        stats["net_rx_gb"] = round(rx/1024**3, 2); stats["net_tx_gb"] = round(tx/1024**3, 2)
    except Exception: stats["net_rx_gb"] = stats["net_tx_gb"] = 0
    return stats

def service_list():
    names = ["nginx", "haproxy", "xray", "dropbear", "ssh", "ws-proxy", "vps-panel", "cron", "systemd-resolved"]
    out = []
    for n in names:
        status = "unknown"
        try:
            r = subprocess.run(["systemctl", "is-active", n], capture_output=True, text=True, timeout=3)
            status = r.stdout.strip() or "inactive"
        except Exception:
            status = "n/a (no systemd)"
        out.append({"name": n, "status": status})
    return out

CSS = """<style>
*{box-sizing:border-box}
:root{--bg:#07090d;--panel:#0b0f14;--card:#10151d;--card2:#0d1117;--line:#1e2833;--txt:#dbe4ee;--mut:#8494a7;--acc:#45e3c0;--acc2:#38bdf8;--ok:#3ddc84;--bad:#f87171;--warn:#fbbf24}
html,body{margin:0;padding:0}
body{background:radial-gradient(1100px 550px at 85% -10%,rgba(56,189,248,.08),transparent 60%),radial-gradient(900px 500px at -10% 110%,rgba(69,227,192,.05),transparent 60%),var(--bg);color:var(--txt);font-family:system-ui,'Segoe UI',Arial,sans-serif;font-size:15px}
a{color:var(--acc2);text-decoration:none}
.layout{display:flex;min-height:100vh}
.sidebar{width:232px;flex:0 0 232px;background:linear-gradient(180deg,rgba(69,227,192,.05),transparent 30%),var(--panel);border-right:1px solid var(--line);padding:20px 14px;position:sticky;top:0;height:100vh;display:flex;flex-direction:column;gap:4px;transition:transform .28s ease}
.brand{display:flex;align-items:center;gap:11px;padding:2px 10px 4px}
.brand .mark{width:38px;height:38px;flex:0 0 38px;border-radius:11px;background:linear-gradient(135deg,var(--acc),var(--acc2));display:flex;align-items:center;justify-content:center;font-size:19px;font-weight:800;color:#06251e;box-shadow:0 4px 16px rgba(69,227,192,.35)}
.brand .bt{font-size:17px;font-weight:700;color:#fff;line-height:1.2}
.brand small{display:block;font-size:10px;color:var(--acc);letter-spacing:2.5px;font-weight:600;margin-top:1px}
.sidehead{display:flex;align-items:flex-start;justify-content:space-between;gap:8px}
.closebtn{display:none;background:var(--card);border:1px solid var(--line);color:var(--txt);border-radius:9px;width:32px;height:32px;padding:0;font-size:14px;align-items:center;justify-content:center;flex:0 0 32px;cursor:pointer}
.burger{display:none;background:var(--card);border:1px solid var(--line);color:var(--txt);border-radius:10px;padding:7px 12px;font-size:16px;line-height:1;cursor:pointer}
#nav-overlay{position:fixed;inset:0;background:rgba(2,6,10,.6);opacity:0;pointer-events:none;transition:opacity .25s ease;z-index:40}
.logincard .brand{padding:2px 0 12px}
::-webkit-scrollbar{width:10px;height:10px}
::-webkit-scrollbar-thumb{background:#22303f;border-radius:99px;border:2px solid var(--bg)}
::-webkit-scrollbar-track{background:transparent}
.navlabel{font-size:10px;letter-spacing:2px;color:var(--mut);margin:16px 10px 6px;text-transform:uppercase}
.snav a{display:flex;align-items:center;gap:10px;padding:10px 12px;border-radius:10px;color:var(--txt);font-size:14px;border:1px solid transparent;transition:background .2s,border-color .2s,color .2s}
.snav a:hover{background:var(--card);border-color:var(--line)}
.snav a.active{background:linear-gradient(90deg,rgba(69,227,192,.18),rgba(56,189,248,.08));border-color:#17453a;color:var(--acc);font-weight:600;box-shadow:inset 3px 0 0 var(--acc)}
.snav .ic{width:20px;text-align:center;flex:0 0 20px}
.sidefoot{margin-top:auto;padding:10px;color:var(--mut);font-size:12px;border-top:1px solid var(--line)}
.main{flex:1;min-width:0;display:flex;flex-direction:column}
.topbar{display:flex;align-items:center;gap:12px;padding:14px 22px;border-bottom:1px solid var(--line);background:var(--panel);position:sticky;top:0;z-index:5}
.topbar .title{font-size:13px;font-weight:700;letter-spacing:2px;color:var(--acc);text-transform:uppercase}
.topbar .sp{flex:1}
.chip{display:inline-flex;align-items:center;gap:6px;background:var(--card);border:1px solid var(--line);border-radius:999px;padding:5px 12px;font-size:12.5px;color:var(--txt)}
.chip .dot{width:8px;height:8px;border-radius:50%;background:var(--ok)}
.chip.off .dot{background:var(--bad)}
.content{padding:22px;max-width:1180px;width:100%;margin:0 auto}
h2{margin:4px 0 14px;font-size:22px;background:linear-gradient(90deg,#ffffff,#9be8d8);-webkit-background-clip:text;background-clip:text;-webkit-text-fill-color:transparent;color:transparent}
h3{margin:0 0 12px;font-size:15px;letter-spacing:.4px}
.card{background:linear-gradient(165deg,rgba(255,255,255,.035),rgba(255,255,255,0) 55%),var(--card);border:1px solid var(--line);border-radius:16px;padding:18px;margin:0 0 16px;box-shadow:0 8px 24px rgba(0,0,0,.25)}
.grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(230px,1fr));gap:14px;margin-bottom:16px}
.stat .big{font-size:30px;font-weight:700;color:#fff}
.stat .lbl{color:var(--acc);font-size:11px;letter-spacing:2px;text-transform:uppercase;margin-bottom:6px}
.stat .sub{color:var(--mut);font-size:13px;margin-top:4px}
.bar{height:8px;background:#0a0e13;border:1px solid var(--line);border-radius:99px;overflow:hidden;margin-top:10px}
.bar i{display:block;height:100%;background:linear-gradient(90deg,#1fbfa0,var(--acc));border-radius:99px}
.pill{display:inline-block;padding:3px 12px;border-radius:999px;font-size:12px;font-weight:600}
.pill.on{background:#0d2b1d;color:var(--ok);border:1px solid #14532d}
.pill.off{background:#2b1215;color:var(--bad);border:1px solid #5b1d22}
.pill.warn{background:#2b2110;color:var(--warn);border:1px solid #57450f}
table{width:100%;border-collapse:collapse;font-size:14px}
td,th{padding:9px 8px;text-align:left;border-bottom:1px solid var(--line);vertical-align:top}
th{color:var(--mut);font-size:11.5px;letter-spacing:1.2px;text-transform:uppercase}
.tblwrap{overflow-x:auto}
input,select,textarea{background:#0a0e13;color:#fff;border:1px solid #2a3644;border-radius:10px;padding:10px 12px;margin:4px 0 10px;width:100%;font-size:14px}
input:focus,select:focus,textarea:focus{outline:none;border-color:var(--acc)}
label{font-size:13px;color:var(--mut);display:block;margin-top:6px}
button,.btn{background:linear-gradient(135deg,var(--acc),#2bc4a8);color:#06251e;border:0;padding:10px 20px;border-radius:10px;cursor:pointer;font-weight:700;font-size:14px;display:inline-block;transition:filter .2s,transform .15s}
button:hover,.btn:hover{filter:brightness(1.08)}
button.ghost,.btn.ghost{background:var(--card);color:var(--txt);border:1px solid var(--line);font-weight:600}
button.danger{background:#3a1418;color:#fda4a4;border:1px solid #5b1d22;font-weight:600;padding:7px 14px}
button.small{padding:6px 14px;font-size:13px}
code{font-family:ui-monospace,Menlo,Consolas,monospace;font-size:12.5px;word-break:break-all}
.kv{display:grid;grid-template-columns:repeat(auto-fit,minmax(200px,1fr));gap:10px 26px}
.kv .k{color:var(--mut);font-size:11.5px;letter-spacing:1.2px;text-transform:uppercase}
.kv .v{font-size:15px;margin-top:2px;word-break:break-all}
.codebox{position:relative;background:#05070a;border:1px solid var(--line);border-radius:12px;padding:14px;margin-top:8px}
.codebox pre{margin:0;white-space:pre-wrap;word-break:break-all;font-family:ui-monospace,Menlo,Consolas,monospace;font-size:12.5px;color:#c8f5e8}
.copybtn{position:absolute;top:10px;right:10px;background:var(--card);color:var(--txt);border:1px solid var(--line);padding:5px 12px;font-size:12px;border-radius:8px;font-weight:600}
.linkrow{background:var(--card2);border:1px solid var(--line);border-radius:12px;padding:12px 14px;margin:0 0 10px}
.linkrow .lh{display:flex;justify-content:space-between;align-items:center;margin-bottom:6px}
.linkrow .lt{color:var(--acc);font-size:12px;letter-spacing:1.5px;text-transform:uppercase;font-weight:700}
.linkrow code{color:#bfe9dc;display:block}
.result{border-color:#17453a;background:#0c1712}
.result h3{color:var(--acc)}
.mut{color:var(--mut)} .ok{color:var(--ok)} .bad{color:var(--bad)}
.loginwrap{min-height:100vh;display:flex;align-items:center;justify-content:center;padding:20px}
.logincard{width:100%;max-width:380px;background:var(--card);border:1px solid var(--line);border-radius:18px;padding:28px}
.logincard h2{margin:0 0 4px}
.logincard .sub{color:var(--mut);margin:0 0 18px;font-size:13.5px}
pre.raw{background:#05070a;border:1px solid var(--line);border-radius:12px;padding:14px;overflow-x:auto;font-size:12.5px}
.svcrow{display:flex;justify-content:space-between;align-items:center;padding:9px 2px;border-bottom:1px solid var(--line)}
.svcrow:last-child{border-bottom:0}
@media (max-width:860px){
  .sidebar{position:fixed;left:0;top:0;width:264px;flex:none;height:100vh;height:100dvh;z-index:50;transform:translateX(-105%);box-shadow:0 0 44px rgba(0,0,0,.55);overflow-y:auto;padding:18px 14px}
  body.nav-open .sidebar{transform:translateX(0)}
  body.nav-open #nav-overlay{opacity:1;pointer-events:auto}
  .burger{display:inline-block}
  .closebtn{display:inline-flex}
  .content{padding:14px}
  .topbar{padding:12px 14px;flex-wrap:wrap}
}
</style>"""

JS = """<script>
function copyText(id, btn){
  var el = document.getElementById(id); if(!el) return;
  var txt = el.innerText || el.textContent;
  function done(){ if(btn){ var o = btn.textContent; btn.textContent = 'Copied'; setTimeout(function(){ btn.textContent = o; }, 1200); } }
  if(navigator.clipboard && navigator.clipboard.writeText){ navigator.clipboard.writeText(txt).then(done, done); }
  else { var ta = document.createElement('textarea'); ta.value = txt; document.body.appendChild(ta); ta.select(); try{ document.execCommand('copy'); }catch(e){} document.body.removeChild(ta); done(); }
}
function toggleNav(force){
  if(typeof force === 'boolean'){ document.body.classList.toggle('nav-open', force); }
  else { document.body.classList.toggle('nav-open'); }
}
</script>"""

NAV_ITEMS = [
    ("/", "Dashboard", "▦"),
    ("/ssh", "SSH Accounts", "🔑"),
    ("/xray", "Xray Accounts", "🌐"),
    ("/services", "Running Services", "⚙"),
    ("/stats", "VPS Stats", "📊"),
    ("/telegram", "Telegram Bot", "✈"),
    ("/settings", "Settings", "🛠"),
    ("/admins", "Panel Admins", "👤"),
    ("/activity", "Activity", "🕘"),
    ("/logout", "Logout", "⏻"),
]

BRAND = ("<div class='brand'><span class='mark'>V</span>"
         "<span class='bt'>VPS Manager<small>SUITE V1.0</small></span></div>")

def page(title, body, nav=True, active="", user=""):
    if not nav:
        return ("<!doctype html><html><head><meta charset='utf-8'>"
                "<meta name='viewport' content='width=device-width,initial-scale=1'>"
                f"<title>{html.escape(title)} - VPS Manager</title>{CSS}</head>"
                f"<body><div class='loginwrap'>{body}</div>{JS}</body></html>")
    links = "".join(
        f"<a href='{href}' class='{'active' if href == active else ''}' onclick='toggleNav(false)'><span class='ic'>{ic}</span>{label}</a>"
        for href, label, ic in NAV_ITEMS)
    cfg = load_config(); domain = cfg.get("domain") or ""
    dom_chip = f"<span class='chip'><span class='dot'></span>{html.escape(domain)}</span>" if domain else ""
    user_chip = f"<span class='chip'>👤 {html.escape(user or 'admin')}</span>" if user else ""
    return ("<!doctype html><html><head><meta charset='utf-8'>"
            "<meta name='viewport' content='width=device-width,initial-scale=1'>"
            f"<title>{html.escape(title)} - VPS Manager</title>{CSS}</head><body>"
            "<div id='nav-overlay' onclick='toggleNav(false)'></div>"
            "<div class='layout'>"
            f"<aside class='sidebar' id='sidebar'><div class='sidehead'>{BRAND}"
            "<button type='button' class='closebtn' onclick='toggleNav(false)' aria-label='Close menu'>✕</button></div>"
            f"<div class='navlabel'>Menu</div><nav class='snav'>{links}</nav>"
            "<div class='sidefoot'>Web Panel · Port " + str(PORT) + "</div></aside>"
            "<div class='main'>"
            f"<div class='topbar'><button type='button' class='burger' onclick='toggleNav()' aria-label='Open menu'>☰</button><span class='title'>{html.escape(title)}</span><span class='sp'></span>{dom_chip}{user_chip}"
            "<a class='btn ghost small' href='javascript:location.reload()'>Refresh</a></div>"
            f"<div class='content'>{body}</div>"
            "</div></div>" + JS + "</body></html>")

def chip(status):
    if status == "active": return "<span class='pill on'>● running</span>"
    if status in ("inactive", "failed"): return "<span class='pill off'>● stopped</span>"
    return f"<span class='pill warn'>● {html.escape(str(status))}</span>"

def links_html(links, prefix):
    out = []
    for i, (k, v) in enumerate(links.items()):
        cid = f"{prefix}-{i}"
        out.append(f"<div class='linkrow'><div class='lh'><span class='lt'>{html.escape(k)}</span>"
                   f"<button type='button' class='small ghost' onclick=\"copyText('{cid}', this)\">Copy</button></div>"
                   f"<code id='{cid}'>{html.escape(v)}</code></div>")
    return "".join(out)

RULE_NOTE = ("<p class='mut'>Rule: <b>Days is a literal count</b> (default 30) - Expiry = creation date + Days. "
             "Only the <b>GB limit</b> and <b>IP limit</b> treat <b>0 as unlimited</b>. "
             "Example: Days 30 / GB 0 / IP 0 -&gt; Expires: creation + 30 days, Data: Unlimited, IP Limit: Unlimited.</p>")
LIMIT_HINT = "<p class='mut' style='margin-top:-4px'>0 = Unlimited. If you set a value, the account stops automatically when the limit is reached.</p>"

def display_status(acct):
    """Status for lists: a disabled record shows its stored reason."""
    if acct.get("disabled"):
        return str(acct.get("disabled_reason") or "Disabled")
    return account_status(acct)

def status_pill(st):
    if st == "Active": return "<span class='pill on'>Active</span>"
    if st.startswith("Disabled"): return "<span class='pill off'>" + html.escape(st) + "</span>"
    return "<span class='pill warn'>" + html.escape(st) + "</span>"

def system_user_create(username, password, expiry_ymd):
    """Create a real tunnel-only system user (root only). Best-effort: never raises."""
    if os.geteuid() != 0: return
    try:
        exists = subprocess.run(["id", username], capture_output=True).returncode == 0
        if not exists:
            r = subprocess.run(["useradd", "-M", "-s", "/usr/sbin/nologin", "-e", expiry_ymd, username],
                               capture_output=True, text=True, timeout=10)
            if r.returncode != 0:
                log_activity(f"ssh useradd failed for {username}: {(r.stderr or '').strip()[:120]}")
                return
        r = subprocess.run(["chpasswd"], input=f"{username}:{password}\n",
                           capture_output=True, text=True, timeout=10)
        if r.returncode != 0:
            log_activity(f"ssh chpasswd failed for {username}: {(r.stderr or '').strip()[:120]}")
    except Exception as e:
        try: log_activity(f"ssh system user create error for {username}: {e}")
        except Exception: pass

def system_user_delete(username):
    """Remove the real system user (root only). Best-effort: never raises."""
    if os.geteuid() != 0: return
    try:
        r = subprocess.run(["userdel", username], capture_output=True, text=True, timeout=10)
        if r.returncode != 0:
            log_activity(f"ssh userdel failed for {username}: {(r.stderr or '').strip()[:120]}")
    except Exception as e:
        try: log_activity(f"ssh system user delete error for {username}: {e}")
        except Exception: pass

class Handler(BaseHTTPRequestHandler):
    def log_message(self, *a): pass
    def session_user(self):
        c = SimpleCookie(self.headers.get("Cookie", ""))
        if "session" in c and c["session"].value in SESSIONS: return SESSIONS[c["session"].value]
        return None
    def send_html(self, content, code=200, headers=None):
        b = content.encode(); self.send_response(code); self.send_header("Content-Type", "text/html; charset=utf-8"); self.send_header("Content-Length", str(len(b)))
        if headers:
            for k, v in headers: self.send_header(k, v)
        self.end_headers(); self.wfile.write(b)
    def redirect(self, loc, code=302, headers=None):
        self.send_response(code); self.send_header("Location", loc)
        if headers:
            for k, v in headers: self.send_header(k, v)
        self.end_headers()
    def form(self):
        length = int(self.headers.get("Content-Length", 0)); raw = self.rfile.read(length).decode()
        return {k: v[0] for k, v in urllib.parse.parse_qs(raw).items()}

    def do_GET(self):
        p = urllib.parse.urlparse(self.path).path
        if p == "/login":
            body = ("<div class='logincard'>" + BRAND +
                    "<h2>Panel Login</h2><p class='sub'>Sign in to manage your VPS.</p>"
                    "<form method='post' action='/login'><label>Username</label><input name='username' required autofocus>"
                    "<label>Password</label><input name='password' type='password' required>"
                    "<button style='width:100%'>Login</button></form></div>")
            return self.send_html(page("Login", body, nav=False))
        user = self.session_user()
        if not user: return self.redirect("/login")
        cfg = load_config(); domain = cfg.get("domain") or ""
        if p == "/logout":
            c = SimpleCookie(self.headers.get("Cookie", ""))
            if "session" in c: SESSIONS.pop(c["session"].value, None)
            return self.redirect("/login")
        if p == "/":
            s = sys_stats(); ssh = load_json("ssh_users.json", []); xr = load_json("xray_users.json", []); svcs = service_list()
            active = sum(1 for x in svcs if x["status"] == "active")
            svc_rows = "".join(f"<div class='svcrow'><span>{html.escape(x['name'])}</span>{chip(x['status'])}</div>" for x in svcs)
            body = f"""<div class='grid'>
            <div class='card stat'><div class='lbl'>CPU Usage</div><div class='big'>{s.get('cpu')}%</div><div class='bar'><i style='width:{s.get('cpu')}%'></i></div><div class='sub'>Load: {', '.join(s.get('load',[]))}</div></div>
            <div class='card stat'><div class='lbl'>RAM Usage</div><div class='big'>{s.get('ram')}%</div><div class='bar'><i style='width:{s.get('ram')}%'></i></div><div class='sub'>{s.get('ram_total_gb')} GB total</div></div>
            <div class='card stat'><div class='lbl'>Disk Usage</div><div class='big'>{s.get('disk')}%</div><div class='bar'><i style='width:{s.get('disk')}%'></i></div><div class='sub'>{s.get('disk_total_gb')} GB total</div></div>
            <div class='card stat'><div class='lbl'>Services</div><div class='big'>{active} / {len(svcs)}</div><div class='sub'>services running</div></div>
            </div>
            <div class='grid'>
            <div class='card'><h3>Accounts</h3><p>SSH Accounts: <b>{len(ssh)}</b><br>Xray Accounts: <b>{len(xr)}</b></p><p><a class='btn ghost small' href='/ssh'>SSH Accounts</a> <a class='btn ghost small' href='/xray'>Xray Accounts</a></p></div>
            <div class='card'><h3>Network</h3><p>Download (RX): <b>{s.get('net_rx_gb')} GB</b><br>Upload (TX): <b>{s.get('net_tx_gb')} GB</b></p><p class='mut'>Uptime: {html.escape(s.get('uptime',''))}</p></div>
            <div class='card'><h3>Server Info</h3><div class='kv'><div><div class='k'>Domain</div><div class='v'>{html.escape(domain) or '-'}</div></div><div><div class='k'>Panel Port</div><div class='v'>{PORT}</div></div><div><div class='k'>Hostname</div><div class='v'>{html.escape(os.uname().nodename)}</div></div><div><div class='k'>OS</div><div class='v'>{html.escape(os.uname().sysname)} {html.escape(os.uname().release)}</div></div></div></div>
            </div>
            <div class='card'><h3>Services Status</h3>{svc_rows}</div>"""
            return self.send_html(page("Dashboard", body, active="/", user=user))
        if p == "/ssh":
            users = load_json("ssh_users.json", [])
            def _ssh_row(u):
                st = display_status(u)
                return (f"<tr><td><b>{html.escape(u['username'])}</b></td><td><code>{html.escape(u.get('password',''))}</code></td>"
                        f"<td>{fmt_expiry(u.get('expiry_days',30))}<br><small class='mut'>Expires: {expires_on(u.get('created',''), u.get('expiry_days',30))}</small></td>"
                        f"<td>{fmt_data(u.get('gb_limit',0))}</td><td>{fmt_logins(u.get('ip_limit',0))}</td>"
                        f"<td>{html.escape(u.get('created',''))}</td><td>{status_pill(st)}</td>"
                        f"<td><form method='post' action='/ssh/delete'><input type='hidden' name='username' value='{html.escape(u['username'])}'><button class='danger'>Delete</button></form></td></tr>")
            rows = "".join(_ssh_row(u) for u in users) or "<tr><td colspan='8' class='mut'>No SSH accounts yet.</td></tr>"
            payload = f"GET /sshws HTTP/1.1[crlf]Host: {domain}[crlf]Upgrade: websocket[crlf][crlf]"
            body = f"""<div class='card'><h3>Create SSH Account (tunnel-only)</h3><form method='post' action='/ssh/create'><label>Username</label><input name='username' placeholder='Username' required><label>Password</label><input name='password' placeholder='Password (blank = auto-generated)'><label>Days</label><input name='expiry_days' type='number' min='0' value='30'><label>GB limit (0 = unlimited)</label><input name='gb_limit' type='number' min='0' value='0'><label>IP limit (0 = unlimited)</label><input name='ip_limit' type='number' min='0' value='0'>{LIMIT_HINT}<button>Create Account</button></form>{RULE_NOTE}</div>
            <div class='card'><h3>SSH Accounts</h3><div class='tblwrap'><table><tr><th>User</th><th>Password</th><th>Expiry</th><th>Data</th><th>Logins</th><th>Created</th><th>Status</th><th></th></tr>{rows}</table></div></div>
            <div class='card'><h3>Payload</h3><div class='codebox'><button type='button' class='copybtn' onclick="copyText('ssh-payload', this)">Copy</button><pre id='ssh-payload'>{html.escape(payload)}</pre></div><p class='mut'>Host: {html.escape(domain)} · SSH WS port 80 · WS-SSL 443 · Dropbear: 109, 143</p></div>"""
            return self.send_html(page("SSH Accounts", body, active="/ssh", user=user))
        if p == "/xray":
            users = load_json("xray_users.json", [])
            def _xr_card(u):
                _links = u.get('links') or {"Link TLS": u.get('link', '')}
                st = display_status(u)
                return (f"<div class='card'><h3>{html.escape(u['protocol'].upper())} · {html.escape(u['remark'])} {status_pill(st)}</h3>"
                        f"<div class='kv'><div><div class='k'>UUID</div><div class='v'><code>{html.escape(u['id'])}</code></div></div>"
                        f"<div><div class='k'>Created</div><div class='v'>{html.escape(u.get('created',''))}</div></div>"
                        f"<div><div class='k'>Expiry</div><div class='v'>{expires_on(u.get('created',''), u.get('expiry_days',30))} ({fmt_expiry(u.get('expiry_days',30))})</div></div>"
                        f"<div><div class='k'>Data</div><div class='v'>{fmt_data(u.get('gb_limit',0))}</div></div>"
                        f"<div><div class='k'>IP Limit</div><div class='v'>{fmt_logins(u.get('ip_limit',0))}</div></div></div>"
                        f"<div style='margin-top:12px'>{links_html(_links, 'xr-' + u['id'][:8])}</div>"
                        f"<form method='post' action='/xray/delete'><input type='hidden' name='id' value='{u['id']}'><button class='danger'>Delete</button></form></div>")
            cards = "".join(_xr_card(u) for u in users) or "<div class='card'><p class='mut'>No Xray accounts yet.</p></div>"
            body = f"""<div class='card'><h3>Create Xray Account</h3><form method='post' action='/xray/create'><label>Protocol</label><select name='protocol'><option>VMess</option><option>VLESS</option><option>Trojan</option></select><label>Remark / Name</label><input name='remark' placeholder='Remark / Name' required><label>Days</label><input name='expiry_days' type='number' min='0' value='30'><label>GB limit (0 = unlimited)</label><input name='gb_limit' type='number' min='0' value='0'><label>IP limit (0 = unlimited)</label><input name='ip_limit' type='number' min='0' value='0'>{LIMIT_HINT}<button>Create Account (UUID auto)</button></form>{RULE_NOTE}</div>
            <h3 style='margin:18px 0 10px'>Xray Accounts</h3>{cards}"""
            return self.send_html(page("Xray Accounts", body, active="/xray", user=user))
        if p == "/services":
            svcs = service_list(); rows = "".join(f"<div class='svcrow'><span>{html.escape(x['name'])}</span>{chip(x['status'])}</div>" for x in svcs)
            ports = subprocess.getoutput("ss -tuln 2>/dev/null | head -n 40") if shutil.which("ss") else "ss is not available (local dev environment)"
            return self.send_html(page("Running Services", f"<div class='card'><h3>Running Services</h3>{rows}</div><div class='card'><h3>Listening Ports</h3><pre class='raw'>{html.escape(ports)}</pre><form method='post' action='/action/fix'><button>Fix SSH-WS/SSL Stack</button></form></div>", active="/services", user=user))
        if p == "/stats":
            s = sys_stats()
            return self.send_html(page("VPS Stats", f"""<div class='grid'>
            <div class='card stat'><div class='lbl'>CPU Usage</div><div class='big'>{s.get('cpu')}%</div><div class='bar'><i style='width:{s.get('cpu')}%'></i></div><div class='sub'>Load: {', '.join(s.get('load',[]))}</div></div>
            <div class='card stat'><div class='lbl'>RAM Usage</div><div class='big'>{s.get('ram')}%</div><div class='bar'><i style='width:{s.get('ram')}%'></i></div><div class='sub'>{s.get('ram_total_gb')} GB total</div></div>
            <div class='card stat'><div class='lbl'>Disk Usage</div><div class='big'>{s.get('disk')}%</div><div class='bar'><i style='width:{s.get('disk')}%'></i></div><div class='sub'>{s.get('disk_total_gb')} GB total</div></div>
            <div class='card stat'><div class='lbl'>Network</div><div class='big' style='font-size:20px'>RX {s.get('net_rx_gb')} GB</div><div class='sub'>TX {s.get('net_tx_gb')} GB · Uptime: {html.escape(s.get('uptime',''))}</div></div>
            </div><div class='card'><h3>Speedtest</h3><form method='post' action='/action/speedtest'><button>Run Speedtest</button></form></div>""", active="/stats", user=user))
        if p == "/telegram":
            cfg = load_config()
            return self.send_html(page("Telegram Bot", f"<div class='card'><h3>Telegram Bot Setup</h3><form method='post' action='/telegram'><label>Bot Token</label><input name='telegram_token' value='{html.escape(cfg.get('telegram_token',''))}' placeholder='Token from @BotFather'><label>Admin Chat ID</label><input name='telegram_admin_id' value='{html.escape(cfg.get('telegram_admin_id',''))}'><button>Save</button></form><p class='mut'>To run the bot on the VPS: <code>python3 scripts/telegram-bot.py</code> - the token is stored here, never hardcoded in code.</p></div>", active="/telegram", user=user))
        if p == "/settings":
            cfg = load_config()
            return self.send_html(page("Settings", f"""<div class='card'><h3>Settings</h3><form method='post' action='/settings'>
            <label>Domain</label><input name='domain' value='{html.escape(cfg.get('domain',''))}'>
            <label>SSH Banner</label><textarea name='banner'>{html.escape(cfg.get('banner',''))}</textarea>
            <label>Ping QoS Interface</label><input name='qos_interface' value='{html.escape(cfg.get('qos_interface','eth0'))}'>
            <label>QoS Rate</label><input name='qos_rate' value='{html.escape(cfg.get('qos_rate','950mbit'))}'>
            <label>Per-client cap (Mbps, 0 = no cap)</label><input name='per_client_cap' value='{html.escape(str(cfg.get('per_client_cap','0')))}'>
            <button>Save Settings</button></form></div>
            <div class='card'><h3>Maintenance</h3><form method='post' action='/action/fix' style='display:inline'><button>Fix Stack</button></form> <form method='post' action='/action/qos' style='display:inline'><button class='ghost'>Apply QoS (CAKE)</button></form></div>
            <div class='card'><h3>Change Panel Password</h3><form method='post' action='/password'><label>New password</label><input name='new_password' type='password' required placeholder='New password'><button>Change Password</button></form></div>""", active="/settings", user=user))
        if p == "/admins":
            creds = load_creds()
            return self.send_html(page("Panel Admins", f"<div class='card'><h3>Panel Admins</h3><p>Admin user: <b>{html.escape(creds.get('username','admin'))}</b></p><p class='mut'>The password is stored in the credentials file and is not shown here. You can change it under Settings.</p><p><a class='btn ghost small' href='/activity'>View Activity Log</a></p></div>", active="/admins", user=user))
        if p == "/activity":
            logs = load_json("activity.json", []); rows = "".join(f"<tr><td>{html.escape(l['time'])}</td><td>{html.escape(l['action'])}</td></tr>" for l in logs[:100]) or "<tr><td colspan='2' class='mut'>No activity yet.</td></tr>"
            return self.send_html(page("Activity", f"<div class='card'><h3>Activity Log</h3><div class='tblwrap'><table><tr><th>Time</th><th>Action</th></tr>{rows}</table></div></div>", active="/activity", user=user))
        return self.send_html(page("404", "<div class='card'><h3>404 - Page not found</h3><p class='mut'>The page you requested does not exist.</p></div>", user=user), code=404)

    def do_POST(self):
        p = urllib.parse.urlparse(self.path).path
        data = self.form()
        if p == "/login":
            creds = load_creds()
            if data.get("username") == creds.get("username") and data.get("password") == creds.get("password"):
                tok = secrets.token_hex(24); SESSIONS[tok] = creds.get("username"); log_activity(f"login success: {creds.get('username')}")
                return self.redirect("/", code=303, headers=[("Set-Cookie", f"session={tok}; HttpOnly; Path=/; SameSite=Lax")])
            log_activity("login failed")
            body = ("<div class='logincard'>" + BRAND +
                    "<h2>Panel Login</h2><p class='bad'>Invalid username or password</p>"
                    "<form method='post' action='/login'><label>Username</label><input name='username' required autofocus>"
                    "<label>Password</label><input name='password' type='password' required>"
                    "<button style='width:100%'>Login</button></form></div>")
            return self.send_html(page("Login Failed", body, nav=False), code=401)
        user = self.session_user()
        if not user: return self.redirect("/login")
        cfg = load_config(); domain = cfg.get("domain") or ""
        if p == "/ssh/create":
            users = load_json("ssh_users.json", [])
            uname = data.get("username", "").strip()
            pwd = data.get("password") or secrets.token_urlsafe(9)
            # Days literal (default 30); GB/IP: 0 = unlimited (default 0)
            exp = to_int(data.get("expiry_days", 30), 30); gb = to_int(data.get("gb_limit", 0), 0); ipl = to_int(data.get("ip_limit", 0), 0)
            created = time.strftime("%Y-%m-%d")
            exp_date = expires_on(created, exp)
            users.append({"username": uname, "password": pwd, "expiry_days": exp, "gb_limit": gb, "ip_limit": ipl, "used_gb": 0, "created": created, "expires": exp_date})
            save_json("ssh_users.json", users); log_activity(f"ssh create {uname}")
            # When running as root, also create the real tunnel-only system user so the account can log in.
            system_user_create(uname, pwd, exp_date)
            payload = f"GET /sshws HTTP/1.1[crlf]Host: {domain}[crlf]Upgrade: websocket[crlf][crlf]"
            body = f"""<div class='card result'><h3>🔐 SSH Account Created</h3><div class='kv'>
            <div><div class='k'>Username</div><div class='v'>{html.escape(uname)}</div></div>
            <div><div class='k'>Password</div><div class='v'><code>{html.escape(pwd)}</code></div></div>
            <div><div class='k'>Host / Domain</div><div class='v'>{html.escape(domain)}</div></div>
            <div><div class='k'>SNI</div><div class='v'>{html.escape(domain)}</div></div>
            <div><div class='k'>Created</div><div class='v'>{created}</div></div>
            <div><div class='k'>Expiry</div><div class='v'>{exp_date} ({fmt_expiry(exp)})</div></div>
            <div><div class='k'>Data</div><div class='v'>{fmt_data(gb)}</div></div>
            <div><div class='k'>IP Limit</div><div class='v'>{fmt_logins(ipl)}</div></div>
            <div><div class='k'>SSH Direct</div><div class='v'>22 · 109 · 143</div></div>
            <div><div class='k'>SSH SSL</div><div class='v'>443</div></div>
            <div><div class='k'>SSH WS</div><div class='v'>80</div></div>
            <div><div class='k'>SSH WS-SSL</div><div class='v'>443</div></div>
            </div></div>
            <div class='card'><h3>Payload</h3><div class='codebox'><button type='button' class='copybtn' onclick="copyText('new-ssh-payload', this)">Copy</button><pre id='new-ssh-payload'>{html.escape(payload)}</pre></div>
            <p style='margin-top:14px'><a class='btn ghost' href='/ssh'>Back to SSH Accounts</a></p></div>"""
            return self.send_html(page("SSH Account Created", body, active="/ssh", user=user))
        if p == "/ssh/delete":
            uname = data.get("username")
            users = [u for u in load_json("ssh_users.json", []) if u["username"] != uname]; save_json("ssh_users.json", users); log_activity(f"ssh delete {uname}")
            if uname: system_user_delete(uname)
            return self.redirect("/ssh")
        if p == "/xray/create":
            users = load_json("xray_users.json", []); proto = data.get("protocol", "VLESS").lower(); uid = str(uuid.uuid4()); remark = data.get("remark", "user")
            import base64
            def _vmess_link(port, tls_on):
                vm = {"v": "2", "ps": remark, "add": domain, "port": str(port), "id": uid, "aid": "0", "net": "ws", "type": "none", "host": domain, "path": "/vmess", "tls": "tls" if tls_on else ""}
                return "vmess://" + base64.b64encode(json.dumps(vm).encode()).decode()
            # Links per account, like the reference panel.
            # Chain: 443 -> HAProxy (SNI) -> Xray TLS fallback -> Nginx -> inbound
            #         80 -> HAProxy -> Nginx -> inbound ; 8443 -> Xray TCP/TLS direct
            if proto == "vless":
                links = {
                  "Link TLS": f"vless://{uid}@{domain}:443?encryption=none&security=tls&type=ws&host={domain}&path=%2Fvless&sni={domain}#{remark}",
                  "Link NTLS": f"vless://{uid}@{domain}:80?encryption=none&security=none&type=ws&host={domain}&path=%2Fvless#{remark}",
                  "Link TCP": f"vless://{uid}@{domain}:8443?encryption=none&security=tls&type=tcp&headerType=none&sni={domain}#{remark}-tcp",
                  "Link XHTTP TLS": f"vless://{uid}@{domain}:443?encryption=none&security=tls&type=xhttp&path=%2Fxhttp&host={domain}&mode=auto&sni={domain}#{remark}-xhttp-tls",
                  "Link XHTTP NTLS": f"vless://{uid}@{domain}:80?encryption=none&security=none&type=xhttp&path=%2Fxhttp&host={domain}&mode=auto#{remark}-xhttp-ntls",
                }
            elif proto == "trojan":
                links = {
                  "Link TLS": f"trojan://{uid}@{domain}:443?security=tls&type=ws&host={domain}&path=%2Ftrojan&sni={domain}#{remark}",
                  "Link NTLS": f"trojan://{uid}@{domain}:80?security=none&type=ws&host={domain}&path=%2Ftrojan#{remark}",
                }
            else:
                links = {"Link TLS": _vmess_link(443, True), "Link NTLS": _vmess_link(80, False)}
            link = links.get("Link TLS", "")
            exp = to_int(data.get("expiry_days", 30), 30); gb = to_int(data.get("gb_limit", 0), 0); ipl = to_int(data.get("ip_limit", 0), 0)
            created = time.strftime("%Y-%m-%d")
            exp_date = expires_on(created, exp)
            users.append({"id": uid, "protocol": proto, "remark": remark, "link": link, "links": links, "expiry_days": exp, "gb_limit": gb, "ip_limit": ipl, "used_gb": 0, "created": created, "expires": exp_date})
            save_json("xray_users.json", users); log_activity(f"xray create {proto} {remark}"); sync_xray()
            body = f"""<div class='card result'><h3>🌐 Xray Account Created</h3><div class='kv'>
            <div><div class='k'>Remark</div><div class='v'>{html.escape(remark)}</div></div>
            <div><div class='k'>Protocol</div><div class='v'>{html.escape(proto.upper())}</div></div>
            <div><div class='k'>UUID</div><div class='v'><code>{uid}</code></div></div>
            <div><div class='k'>Host / Domain</div><div class='v'>{html.escape(domain)}</div></div>
            <div><div class='k'>Created</div><div class='v'>{created}</div></div>
            <div><div class='k'>Expiry</div><div class='v'>{exp_date} ({fmt_expiry(exp)})</div></div>
            <div><div class='k'>Data</div><div class='v'>{fmt_data(gb)}</div></div>
            <div><div class='k'>IP Limit</div><div class='v'>{fmt_logins(ipl)}</div></div>
            </div></div>
            <div class='card'><h3>Links</h3>{links_html(links, 'new-xr')}
            <p><a class='btn ghost' href='/xray'>Back to Xray Accounts</a></p></div>"""
            return self.send_html(page("Xray Account Created", body, active="/xray", user=user))
        if p == "/xray/delete":
            users = [u for u in load_json("xray_users.json", []) if u["id"] != data.get("id")]; save_json("xray_users.json", users); log_activity("xray delete"); sync_xray(); return self.redirect("/xray")
        if p == "/telegram":
            cfg.update({"telegram_token": data.get("telegram_token", ""), "telegram_admin_id": data.get("telegram_admin_id", "")}); save_config(cfg); log_activity("telegram config saved"); return self.redirect("/telegram")
        if p == "/settings":
            cfg.update({k: data.get(k, cfg.get(k, "")) for k in ("domain", "banner", "qos_interface", "qos_rate", "per_client_cap")}); save_config(cfg); log_activity("settings saved"); return self.redirect("/settings")
        if p == "/password":
            creds = load_creds(); creds["password"] = data.get("new_password", ""); save_json("credentials.json", creds)
            # If the system credentials file exists, update it there too
            try:
                if os.path.exists(SYSTEM_CREDS): json.dump(creds, open(SYSTEM_CREDS, "w"), indent=2)
            except Exception: pass
            log_activity("panel password changed"); return self.redirect("/settings")
        if p == "/action/fix":
            _fix = next((c for c in ("/opt/vps-manager-suite/scripts/fix-stack.sh", os.path.join(BASE, "..", "scripts", "fix-stack.sh"), "scripts/fix-stack.sh") if os.path.exists(c)), "")
            out = subprocess.getoutput(f"bash {_fix} 2>&1 | head -n 50") if _fix else "Fix script not found on this VPS (/opt/vps-manager-suite/scripts/fix-stack.sh). Run install/update first."
            log_activity("maintenance fix-stack"); return self.send_html(page("Fix Stack", f"<div class='card'><h3>Fix SSH-WS/SSL Stack</h3><pre class='raw'>{html.escape(out)}</pre><a class='btn ghost' href='/services'>Back</a></div>", active="/services", user=user))
        if p == "/action/qos":
            log_activity("maintenance qos apply requested"); return self.send_html(page("QoS", "<div class='card'><h3>Ping QoS (CAKE)</h3><p>QoS apply request has been logged. On the VPS run: <code>sudo bash scripts/qos.sh</code></p><a class='btn ghost' href='/settings'>Back</a></div>", active="/settings", user=user))
        if p == "/action/speedtest":
            cli = shutil.which("speedtest-cli") or shutil.which("speedtest")
            out = subprocess.getoutput(f"{cli} --simple 2>&1 | head -n 20") if cli else "speedtest-cli is not installed. Install it with: apt install speedtest-cli"
            return self.send_html(page("Speedtest", f"<div class='card'><h3>Speedtest Result</h3><pre class='raw'>{html.escape(out)}</pre><a class='btn ghost' href='/stats'>Back</a></div>", active="/stats", user=user))
        return self.redirect("/")

def main():
    srv = ThreadingHTTPServer((HOST, PORT), Handler)
    proto = "http"
    # Only use the cert when both files exist and are non-empty.
    # If the cert fails to load, the panel must NOT crash - it serves HTTP and logs
    # the reason, so the user can open the panel and fix SSL later from menu option 03.
    cert_ok = (os.path.exists(CERT_FILE) and os.path.exists(KEY_FILE)
               and os.path.getsize(CERT_FILE) > 0 and os.path.getsize(KEY_FILE) > 0)
    if cert_ok:
        try:
            ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
            ctx.load_cert_chain(CERT_FILE, KEY_FILE)
            srv.socket = ctx.wrap_socket(srv.socket, server_side=True)
            proto = "https"
        except Exception as e:
            print(f"WARNING: SSL cert load failed ({e}) - panel is running over HTTP. Re-issue SSL from menu option 03.", flush=True)
    print(f"VPS Manager Panel listening on {proto}://{HOST}:{PORT}  data={DATA_DIR}", flush=True)
    try: srv.serve_forever()
    except KeyboardInterrupt: pass

if __name__ == "__main__": main()
