#!/usr/bin/env python3
"""
VPS Manager Suite v1.0 - Web Panel (original code, stdlib only)
Listen: HTTPS if cert exists else HTTP, default port 9090 (env VPSMGR_PORT)
Data dir: env VPSMGR_DATA_DIR (default /var/lib/vps-manager, fallback for dev: ./data)
Credentials: /etc/vps-manager/credentials.json if exists, else $DATA_DIR/credentials.json
             Koi creds file na mile to RANDOM password generate hota hai (koi default
             password NAHI hai) — woh ek dafa panel ke log/stdout me print hota hai.
"""
import os, json, time, uuid, hashlib, secrets, subprocess, shutil, urllib.parse, html
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from http.cookies import SimpleCookie
import ssl

BASE = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.environ.get("VPSMGR_DATA_DIR", "/var/lib/vps-manager")
# Local dev: agar /var/lib writable nahi to ./data use karo
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
    # System credentials pehle check karo
    for cand in (SYSTEM_CREDS, path("credentials.json")):
        if os.path.exists(cand):
            try:
                with open(cand) as f: return json.load(f)
            except Exception: pass
    # Koi creds file nahi — random password banao (default password rakhna security bug hota)
    creds = {"username": "admin", "password": secrets.token_urlsafe(12)}
    save_json("credentials.json", creds)
    print(f"WARNING: credentials file nahi mili — naya admin password generate kiya: {creds['password']}  (data dir ki credentials.json me save hai, login karke badal lo)", flush=True)
    return creds

def load_config(): return load_json("config.json", {"domain": "", "banner": "Welcome to VPS Manager Suite", "qos_interface": "eth0", "qos_rate": "950mbit", "per_client_cap": "0"})

def sync_xray():
    """Xray accounts ko asal xray config me bhejo (best-effort).
    Panel sirf record rakhe aur Xray unko na pehchane — ye purana bug tha."""
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
    """Combined check — 0 limits are never enforced."""
    if is_expired(acct.get("created", ""), acct.get("expiry_days", 30)): return "Expired"
    if quota_exceeded(acct.get("used_gb", 0), acct.get("gb_limit", 0)): return "Quota Full"
    return "Active"

SESSIONS = {}

def sys_stats():
    stats = {"cpu": 0, "ram": 0, "disk": 0, "uptime": ""}
    try:
        # CPU from /proc/stat (simple snapshot)
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
body{background:#0f172a;color:#e2e8f0;font-family:system-ui,Arial,sans-serif;margin:0}
a{color:#38bdf8} .top{background:#020617;padding:14px 20px;display:flex;justify-content:space-between;border-bottom:1px solid #1e293b}
.card{background:#1e293b;border-radius:12px;padding:16px;margin:12px 0} .grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(220px,1fr));gap:12px}
input,select,textarea{background:#0f172a;color:#fff;border:1px solid #334155;border-radius:8px;padding:9px;margin:4px 0;width:100%;box-sizing:border-box}
button,.btn{background:#0284c7;color:#fff;border:0;padding:9px 16px;border-radius:8px;cursor:pointer;text-decoration:none;display:inline-block}
table{width:100%;border-collapse:collapse} td,th{border-bottom:1px solid #334155;padding:8px;text-align:left} code{background:#020617;padding:2px 6px;border-radius:6px;word-break:break-all}
.wrap{max-width:1100px;margin:auto;padding:20px} .ok{color:#4ade80}.bad{color:#f87171} .nav a{margin-right:12px}
.login{max-width:380px;margin:80px auto} textarea{min-height:70px}
</style>"""

def page(title, body, nav=True):
    navhtml = '<div class="nav"><a href="/">Dashboard</a><a href="/ssh">SSH Accounts</a><a href="/xray">Xray Accounts</a><a href="/services">Services</a><a href="/stats">VPS Stats</a><a href="/telegram">Telegram Bot</a><a href="/settings">Settings</a><a href="/admins">Admins & Log</a><a href="/logout">Logout</a></div>' if nav else ""
    return f"<!doctype html><html><head><meta charset='utf-8'><meta name='viewport' content='width=device-width,initial-scale=1'><title>{html.escape(title)} - VPS Manager</title>{CSS}</head><body><div class='top'><b>🚀 VPS Manager Suite v1.0</b><span>Dark Panel</span></div><div class='wrap'>{navhtml}{body}</div></body></html>"

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
    def redirect(self, loc): self.send_response(302); self.send_header("Location", loc); self.end_headers()
    def form(self):
        length = int(self.headers.get("Content-Length", 0)); raw = self.rfile.read(length).decode()
        return {k: v[0] for k, v in urllib.parse.parse_qs(raw).items()}

    def do_GET(self):
        p = urllib.parse.urlparse(self.path).path
        if p == "/login":
            return self.send_html(page("Login", "<div class='card login'><h2>Panel Login</h2><form method='post' action='/login'><label>Username</label><input name='username' required><label>Password</label><input name='password' type='password' required><button>Login Karo</button></form></div>", nav=False))
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
            body = f"""<h2>Dashboard</h2><div class='grid'>
            <div class='card'><h3>CPU / Load</h3><p>Load: {', '.join(s.get('load',[]))}</p><p>Uptime: {html.escape(s.get('uptime',''))}</p></div>
            <div class='card'><h3>RAM</h3><p>{s.get('ram')}% used / {s.get('ram_total_gb')} GB</p></div>
            <div class='card'><h3>Disk</h3><p>{s.get('disk')}% used / {s.get('disk_total_gb')} GB</p></div>
            <div class='card'><h3>Network</h3><p>RX {s.get('net_rx_gb')} GB · TX {s.get('net_tx_gb')} GB</p></div>
            <div class='card'><h3>Accounts</h3><p>SSH: {len(ssh)}<br>Xray: {len(xr)}</p></div>
            <div class='card'><h3>Services</h3><p>{active}/{len(svcs)} services active</p></div>
            </div><div class='card'><h3>Server Info</h3><p>Domain: <code>{html.escape(domain)}</code><br>Panel Port: {PORT}<br>OS: {html.escape(os.uname().sysname)} {html.escape(os.uname().release)}<br>Hostname: {html.escape(os.uname().nodename)}</p></div>"""
            return self.send_html(page("Dashboard", body))
        if p == "/ssh":
            users = load_json("ssh_users.json", [])
            def _ssh_row(u):
                return (f"<tr><td>{html.escape(u['username'])}</td><td>{html.escape(u.get('password',''))}</td>"
                        f"<td>{fmt_expiry(u.get('expiry_days',30))}<br><small>Expires: {expires_on(u.get('created',''), u.get('expiry_days',30))}</small></td>"
                        f"<td>{fmt_data(u.get('gb_limit',0))}</td><td>{fmt_logins(u.get('ip_limit',0))}</td>"
                        f"<td>{html.escape(u.get('created',''))}</td><td>{account_status(u)}</td>"
                        f"<td><form method='post' action='/ssh/delete'><input type='hidden' name='username' value='{html.escape(u['username'])}'><button>Delete</button></form></td></tr>")
            rows = "".join(_ssh_row(u) for u in users)
            payload = f"GET /sshws HTTP/1.1[crlf]Host: {domain}[crlf]Upgrade: websocket[crlf][crlf]"
            body = f"""<h2>SSH Accounts</h2><div class='card'><h3>Create SSH User (tunnel-only)</h3><form method='post' action='/ssh/create'><input name='username' placeholder='Username' required><input name='password' placeholder='Password (blank = auto)'><label>Days</label><input name='expiry_days' type='number' min='0' value='30' placeholder='Days'><label>GB limit (0 = unlimited)</label><input name='gb_limit' type='number' min='0' value='0' placeholder='0 = unlimited'><label>IP limit (0 = unlimited)</label><input name='ip_limit' type='number' min='0' value='0' placeholder='0 = unlimited'><button>Create Karo</button></form><p style='color:#94a3b8'>Rule: <b>Days = literal count</b> (default 30, Expiry = creation + Days). Sirf <b>GB limit</b> aur <b>IP limit</b> par <b>0 = unlimited</b> hai. Example: Days 30 / GB 0 / IP 0 → Expiry: creation+30 days, Data: Unlimited, IP Limit: Unlimited.</p></div>
            <div class='card'><table><tr><th>User</th><th>Pass</th><th>Expiry</th><th>Data</th><th>Logins</th><th>Created</th><th>Status</th><th></th></tr>{rows}</table></div>
            <div class='card'><h3>Payload Example</h3><code>{html.escape(payload)}</code><p>Host: {html.escape(domain)} · SSH WS Port: 80/443 · Dropbear: 109, 143</p></div>"""
            return self.send_html(page("SSH Accounts", body))
        if p == "/xray":
            users = load_json("xray_users.json", [])
            def _xr_row(u):
                _links = u.get('links') or {"Link TLS": u.get('link', '')}
                _links_html = "<br>".join(f"<b>{html.escape(k)}:</b> <code>{html.escape(v)}</code>" for k, v in _links.items())
                return (f"<tr><td>{html.escape(u['protocol'])}</td><td>{html.escape(u['remark'])}</td><td>{_links_html}</td>"
                        f"<td>{fmt_expiry(u.get('expiry_days',30))}<br><small>Expires: {expires_on(u.get('created',''), u.get('expiry_days',30))}</small></td>"
                        f"<td>{fmt_data(u.get('gb_limit',0))}</td><td>{fmt_logins(u.get('ip_limit',0))}</td><td>{account_status(u)}</td>"
                        f"<td><form method='post' action='/xray/delete'><input type='hidden' name='id' value='{u['id']}'><button>Delete</button></form></td></tr>")
            rows = "".join(_xr_row(u) for u in users)
            body = f"""<h2>Xray Accounts</h2><div class='card'><h3>Create Account</h3><form method='post' action='/xray/create'><label>Protocol</label><select name='protocol'><option>VMess</option><option>VLESS</option><option>Trojan</option></select><input name='remark' placeholder='Remark / naam' required><label>Days</label><input name='expiry_days' type='number' min='0' value='30' placeholder='Days'><label>GB limit (0 = unlimited)</label><input name='gb_limit' type='number' min='0' value='0' placeholder='0 = unlimited'><label>IP limit (0 = unlimited)</label><input name='ip_limit' type='number' min='0' value='0' placeholder='0 = unlimited'><button>Create (UUID auto)</button></form><p style='color:#94a3b8'>Rule: <b>Days = literal count</b> (default 30). Sirf <b>GB limit</b> aur <b>IP limit</b> par <b>0 = unlimited</b> hai. Tabs: <b>VMess | VLESS | Trojan</b></p></div><div class='card'><table><tr><th>Protocol</th><th>Remark</th><th>Link</th><th>Expiry</th><th>Data</th><th>Logins</th><th>Status</th><th></th></tr>{rows}</table></div>"""
            return self.send_html(page("Xray Accounts", body))
        if p == "/services":
            svcs = service_list(); rows = "".join(f"<tr><td>{s['name']}</td><td class='{'ok' if s['status']=='active' else 'bad'}'>{s['status']}</td></tr>" for s in svcs)
            ports = subprocess.getoutput("ss -tuln 2>/dev/null | head -n 40") if shutil.which("ss") else "ss available nahi (local dev)"
            return self.send_html(page("Services", f"<h2>Running Services</h2><div class='card'><table><tr><th>Service</th><th>Status</th></tr>{rows}</table></div><div class='card'><h3>Ports</h3><pre>{html.escape(ports)}</pre><form method='post' action='/action/fix'><button>Fix SSH-WS/SSL Stack</button></form></div>"))
        if p == "/stats":
            s = sys_stats()
            return self.send_html(page("Stats", f"<h2>VPS Stats & Speedtest</h2><div class='card'><p>RAM {s.get('ram')}% · Disk {s.get('disk')}% · Load {s.get('load')} · RX {s.get('net_rx_gb')} GB TX {s.get('net_tx_gb')} GB</p><form method='post' action='/action/speedtest'><button>Speedtest Chalao</button></form></div>"))
        if p == "/telegram":
            cfg = load_config()
            return self.send_html(page("Telegram", f"<h2>Telegram Bot Setup</h2><div class='card'><form method='post' action='/telegram'><label>Bot Token</label><input name='telegram_token' value='{html.escape(cfg.get('telegram_token',''))}' placeholder='BotFather wala token'><label>Admin Chat ID</label><input name='telegram_admin_id' value='{html.escape(cfg.get('telegram_admin_id',''))}'><button>Save Karo</button></form><p>Bot chalane ke liye VPS par: <code>python3 scripts/telegram-bot.py</code> — token yahan save hoga, code me hardcode nahi.</p></div>"))
        if p == "/settings":
            cfg = load_config()
            return self.send_html(page("Settings", f"""<h2>Settings</h2><div class='card'><form method='post' action='/settings'>
            <label>Domain</label><input name='domain' value='{html.escape(cfg.get('domain',''))}'>
            <label>SSH Banner</label><textarea name='banner'>{html.escape(cfg.get('banner',''))}</textarea>
            <label>Ping QoS Interface</label><input name='qos_interface' value='{html.escape(cfg.get('qos_interface','eth0'))}'>
            <label>QoS Rate</label><input name='qos_rate' value='{html.escape(cfg.get('qos_rate','950mbit'))}'>
            <label>Per-client cap (Mbps, 0 = no cap)</label><input name='per_client_cap' value='{html.escape(str(cfg.get('per_client_cap','0')))}'>
            <button>Save Settings</button></form></div>
            <div class='card'><h3>Maintenance</h3><form method='post' action='/action/fix'><button>Fix Stack</button></form> <form method='post' action='/action/qos'><button>Apply QoS (CAKE)</button></form></div>
            <div class='card'><h3>Change Panel Password</h3><form method='post' action='/password'><input name='new_password' type='password' required placeholder='New password'><button>Change Karo</button></form></div>"""))
        if p == "/admins":
            logs = load_json("activity.json", []); rows = "".join(f"<tr><td>{html.escape(l['time'])}</td><td>{html.escape(l['action'])}</td></tr>" for l in logs[:100])
            creds = load_creds()
            return self.send_html(page("Admins", f"<h2>Panel Admins</h2><div class='card'><p>Admin user: <b>{html.escape(creds.get('username','admin'))}</b> (password credentials file me hai, yahan show nahi hota)</p></div><h3>Activity Log</h3><div class='card'><table><tr><th>Time</th><th>Action</th></tr>{rows}</table></div>"))
        return self.send_html(page("404", "<h2>404 - Page nahi mila</h2>"), code=404)

    def do_POST(self):
        p = urllib.parse.urlparse(self.path).path
        data = self.form()
        if p == "/login":
            creds = load_creds()
            if data.get("username") == creds.get("username") and data.get("password") == creds.get("password"):
                tok = secrets.token_hex(24); SESSIONS[tok] = creds.get("username"); log_activity(f"login success: {creds.get('username')}")
                return self.send_html(page("OK", "<p>Login ho gaya... <a href='/'>Dashboard kholo</a></p>", nav=False), headers=[("Set-Cookie", f"session={tok}; HttpOnly; Path=/; SameSite=Lax")])
            log_activity("login failed"); return self.send_html(page("Login Fail", "<div class='card login'><p class='bad'>Galat username/password</p><a href='/login'>Dobara try karo</a></div>", nav=False), code=401)
        if not self.session_user(): return self.redirect("/login")
        cfg = load_config(); domain = cfg.get("domain") or ""
        if p == "/ssh/create":
            users = load_json("ssh_users.json", [])
            pwd = data.get("password") or secrets.token_urlsafe(9)
            # Days literal (default 30); GB/IP: 0 = unlimited (default 0)
            exp = to_int(data.get("expiry_days", 30), 30); gb = to_int(data.get("gb_limit", 0), 0); ipl = to_int(data.get("ip_limit", 0), 0)
            created = time.strftime("%Y-%m-%d")
            users.append({"username": data.get("username", "").strip(), "password": pwd, "expiry_days": exp, "gb_limit": gb, "ip_limit": ipl, "used_gb": 0, "created": created, "expires": expires_on(created, exp)})
            save_json("ssh_users.json", users); log_activity(f"ssh create {data.get('username')}")
            # Real VPS par: useradd -M -s /usr/sbin/nologin <user> (install script ke notes dekho) - panel sirf record rakhta hai taaki non-root dev me safe rahe
            return self.redirect("/ssh")
        if p == "/ssh/delete":
            users = [u for u in load_json("ssh_users.json", []) if u["username"] != data.get("username")]; save_json("ssh_users.json", users); log_activity(f"ssh delete {data.get('username')}"); return self.redirect("/ssh")
        if p == "/xray/create":
            users = load_json("xray_users.json", []); proto = data.get("protocol", "VLESS").lower(); uid = str(uuid.uuid4()); remark = data.get("remark", "user")
            import base64
            def _vmess_link(port, tls_on):
                vm = {"v": "2", "ps": remark, "add": domain, "port": str(port), "id": uid, "aid": "0", "net": "ws", "type": "none", "host": domain, "path": "/vmess", "tls": "tls" if tls_on else ""}
                return "vmess://" + base64.b64encode(json.dumps(vm).encode()).decode()
            # Har account ke links — reference panel ki tarah.
            # Chain: 443 -> HAProxy(SNI) -> Xray TLS fallback -> Nginx -> inbound
            #        80  -> HAProxy -> Nginx -> inbound ; 8443 -> Xray TCP/TLS direct
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
            users.append({"id": uid, "protocol": proto, "remark": remark, "link": link, "links": links, "expiry_days": exp, "gb_limit": gb, "ip_limit": ipl, "used_gb": 0, "created": created, "expires": expires_on(created, exp)})
            save_json("xray_users.json", users); log_activity(f"xray create {proto} {remark}"); sync_xray(); return self.redirect("/xray")
        if p == "/xray/delete":
            users = [u for u in load_json("xray_users.json", []) if u["id"] != data.get("id")]; save_json("xray_users.json", users); log_activity("xray delete"); sync_xray(); return self.redirect("/xray")
        if p == "/telegram":
            cfg.update({"telegram_token": data.get("telegram_token", ""), "telegram_admin_id": data.get("telegram_admin_id", "")}); save_config(cfg); log_activity("telegram config saved"); return self.redirect("/telegram")
        if p == "/settings":
            cfg.update({k: data.get(k, cfg.get(k, "")) for k in ("domain", "banner", "qos_interface", "qos_rate", "per_client_cap")}); save_config(cfg); log_activity("settings saved"); return self.redirect("/settings")
        if p == "/password":
            creds = load_creds(); creds["password"] = data.get("new_password", ""); save_json("credentials.json", creds)
            # Agar system creds file hai to wahan bhi update ki koshish
            try:
                if os.path.exists(SYSTEM_CREDS): json.dump(creds, open(SYSTEM_CREDS, "w"), indent=2)
            except Exception: pass
            log_activity("panel password changed"); return self.redirect("/settings")
        if p == "/action/fix":
            _fix = next((c for c in ("/opt/vps-manager-suite/scripts/fix-stack.sh", os.path.join(BASE, "..", "scripts", "fix-stack.sh"), "scripts/fix-stack.sh") if os.path.exists(c)), "")
            out = subprocess.getoutput(f"bash {_fix} 2>&1 | head -n 50") if _fix else "Fix script is VPS par nahi mili (/opt/vps-manager-suite/scripts/fix-stack.sh). Pehle install/update chalao."
            log_activity("maintenance fix-stack"); return self.send_html(page("Fix", f"<div class='card'><pre>{html.escape(out)}</pre><a href='/services'>Back</a></div>"))
        if p == "/action/qos":
            log_activity("maintenance qos apply requested"); return self.send_html(page("QoS", "<div class='card'><p>QoS apply request log ho gayi. VPS par chalao: <code>sudo bash scripts/qos.sh</code></p><a href='/settings'>Back</a></div>"))
        if p == "/action/speedtest":
            cli = shutil.which("speedtest-cli") or shutil.which("speedtest")
            out = subprocess.getoutput(f"{cli} --simple 2>&1 | head -n 20") if cli else "speedtest-cli installed nahi hai — fallback: ping check panel par hi hai. Install: apt install speedtest-cli"
            return self.send_html(page("Speedtest", f"<div class='card'><pre>{html.escape(out)}</pre><a href='/stats'>Back</a></div>"))
        return self.redirect("/")

def main():
    srv = ThreadingHTTPServer((HOST, PORT), Handler)
    proto = "http"
    # Cert sirf tab use karo jab dono files maujood bhi hon aur khaali bhi na hon.
    # Cert load fail ho to panel crash NA kare — HTTP par chale aur wajah log me likhe,
    # taaki user panel khol kar ya menu 03 se SSL baad me theek kar sake.
    cert_ok = (os.path.exists(CERT_FILE) and os.path.exists(KEY_FILE)
               and os.path.getsize(CERT_FILE) > 0 and os.path.getsize(KEY_FILE) > 0)
    if cert_ok:
        try:
            ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
            ctx.load_cert_chain(CERT_FILE, KEY_FILE)
            srv.socket = ctx.wrap_socket(srv.socket, server_side=True)
            proto = "https"
        except Exception as e:
            print(f"WARNING: SSL cert load fail ({e}) — panel HTTP par chal raha hai. Menu option 03 se SSL dobara issue karo.", flush=True)
    print(f"VPS Manager Panel listening on {proto}://{HOST}:{PORT}  data={DATA_DIR}", flush=True)
    try: srv.serve_forever()
    except KeyboardInterrupt: pass

if __name__ == "__main__": main()
