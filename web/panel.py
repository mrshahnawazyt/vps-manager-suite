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
from http.server import BaseHTTPRequestHandler
try:
    from http.server import ThreadingHTTPServer
except ImportError:
    # Python 3.6 (Ubuntu 18.04) has no ThreadingHTTPServer (added in
    # 3.7) - build the identical class from the threading mixin.
    from http.server import HTTPServer as _HTTPServer
    from socketserver import ThreadingMixIn as _ThreadingMixIn

    class ThreadingHTTPServer(_ThreadingMixIn, _HTTPServer):
        daemon_threads = True
from http.cookies import SimpleCookie
import ssl, sys, threading, socket

BASE = os.path.dirname(os.path.abspath(__file__))
# Pure-python QR generator (scripts/qr.py) for Xray link QR codes
_qr = None
for _qd in (os.path.join(BASE, "..", "scripts"), "/opt/vps-manager-suite/scripts"):
    try:
        sys.path.insert(0, os.path.abspath(_qd))
        import qr as _qr_mod
        _qr = _qr_mod
        break
    except Exception:
        _qr = None
_QR_CACHE = {}
# SSH Banner Designer renderer (scripts/banner_render.py) - turns the
# structured banner design in config.json into the Dropbear banner file.
_banner = None
for _bd in (os.path.join(BASE, "..", "scripts"), "/opt/vps-manager-suite/scripts"):
    try:
        sys.path.insert(0, os.path.abspath(_bd))
        import banner_render as _banner_mod
        _banner = _banner_mod
        break
    except Exception:
        _banner = None
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

def load_config(): return load_json("config.json", {"domain": "", "banner": "DDK VPS Manager", "qos_interface": "eth0", "qos_rate": "950mbit", "per_client_cap": "0"})

def _pipe_lines(argv, emit):
    """Run argv and hand each output line to emit() THE MOMENT it is produced
    (line-buffered pipe, unbuffered Python children) - this is what makes the
    panel popup a true live stream instead of a buffered dump. Returns the
    exit code (1 when the command could not even start)."""
    env = dict(os.environ); env["PYTHONUNBUFFERED"] = "1"
    try:
        proc = subprocess.Popen(argv, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                universal_newlines=True, bufsize=1, env=env)
        for line in proc.stdout:
            emit(line.rstrip("\r\n"))
        return proc.wait()
    except Exception as e:
        emit("[ERROR] Failed to start %s: %s" % (argv[0] if argv else "command", e))
        return 1

def sync_xray_script():
    for cand in ("/opt/vps-manager-suite/scripts/sync-xray.py",
                 os.path.join(BASE, "..", "scripts", "sync-xray.py"),
                 os.path.join(os.getcwd(), "scripts", "sync-xray.py")):
        if os.path.exists(cand):
            return cand
    return ""

def sync_xray(emit=None):
    """Send Xray accounts to the real xray config (best-effort).
    With emit=None the sync runs silently (the classic behaviour); with an
    emit callable its output lines are forwarded as they are produced, for
    the animated popup."""
    cand = sync_xray_script()
    if not cand:
        if emit is not None:
            emit("[WARN] sync-xray.py was not found - the change is saved and reaches Xray on the next sync or restart.")
        return
    if emit is not None:
        _pipe_lines(["python3", cand, "--restart"], emit)
        return
    try: subprocess.run(["python3", cand, "--restart"], timeout=30, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    except Exception: pass
def save_config(c): save_json("config.json", c)

# --- Extra protocols: SlowDNS / ZiVPN UDP / OpenVPN --------------------------
SUITE_CONFIG_DIR = os.environ.get("VPSMGR_CONFIG_DIR", "/etc/vps-manager")
_HOST_LABEL = __import__("re").compile(r"^[A-Za-z0-9]([A-Za-z0-9-]{0,61}[A-Za-z0-9])?$")

def valid_hostname(v):
    v = str(v or "").strip().rstrip(".")
    if not v or len(v) > 253:
        return False
    return all(_HOST_LABEL.match(part) for part in v.split("."))

def slowdns_host(cfg=None):
    cfg = cfg if cfg is not None else load_config()
    v = str(cfg.get("slowdns_domain") or "").strip()
    if v:
        return v
    dom = str(cfg.get("domain") or "").strip()
    return ("dns." + dom) if dom else ""

def udp_host(cfg=None):
    cfg = cfg if cfg is not None else load_config()
    return str(cfg.get("udp_domain") or cfg.get("domain") or "").strip()

def slowdns_pubkey():
    try:
        with open(os.path.join(SUITE_CONFIG_DIR, "slowdns", "server.pub")) as f:
            return f.read().strip()
    except Exception:
        return ""

def zivpn_sync(emit=None):
    """Rebuild the ZiVPN password list from the SSH accounts (best-effort).
    Called after SSH account changes so one account keeps working for
    SSH, SlowDNS and ZiVPN UDP."""
    cand = suite_script("zivpn-sync.py")
    if not cand:
        return
    env = dict(os.environ); env["VPSMGR_DATA_DIR"] = DATA_DIR; env["VPSMGR_CONFIG_DIR"] = SUITE_CONFIG_DIR
    try:
        if emit is not None:
            proc = subprocess.run(["python3", cand], stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                  universal_newlines=True, timeout=20, env=env)
            for line in (proc.stdout or "").splitlines():
                emit("[OK] " + line)
        else:
            subprocess.run(["python3", cand], stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=20, env=env)
    except Exception:
        pass

def apply_protocol_domains(cfg, data):
    """Validate + apply the SlowDNS / UDP domain settings onto cfg.
    Returns (slowdns_effective_changed, notes). Invalid hostnames are
    rejected with a note and the previous value is kept. Clearing the
    SlowDNS field restores the default (dns. + server domain)."""
    notes = []
    before = slowdns_host(cfg)
    if "slowdns_domain" in data:
        v = str(data.get("slowdns_domain") or "").strip().rstrip(".")
        if v and not valid_hostname(v):
            notes.append(f"[WARN] SlowDNS Domain '{v}' is not a valid hostname - the previous value was kept.")
        else:
            cfg["slowdns_domain"] = v
    if "udp_domain" in data:
        v = str(data.get("udp_domain") or "").strip().rstrip(".")
        if v and not valid_hostname(v):
            notes.append(f"[WARN] UDP Domain '{v}' is not a valid hostname - the previous value was kept.")
        elif v != str(cfg.get("udp_domain") or ""):
            cfg["udp_domain"] = v
            notes.append(f"[OK] UDP Domain set to {v or '(server domain)'}")
    after = slowdns_host(cfg)
    if after != before:
        notes.append(f"[OK] SlowDNS Domain is now {after}")
    return (after != before), notes

def restart_slowdns(emit=None):
    try:
        subprocess.run(["systemctl", "restart", "vps-slowdns"], timeout=20,
                       stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        if emit is not None:
            emit("[OK] SlowDNS service restarted with the new domain")
    except Exception:
        if emit is not None:
            emit("[WARN] Could not restart the SlowDNS service from the panel - restart vps-slowdns to apply the new domain.")

def openvpn_users():
    return load_json("openvpn_users.json", [])

def openvpn_script():
    return suite_script("openvpn-user.sh")

CTX = threading.local()  # per-request context for the audit log

def log_activity(action):
    logs = load_json("activity.json", [])
    logs.insert(0, {"time": time.strftime("%Y-%m-%d %H:%M:%S"), "action": action,
                    "admin": getattr(CTX, "user", "") or "", "ip": getattr(CTX, "ip", "") or ""})
    save_json("activity.json", logs[:300])

# --- SSH Banner Designer -----------------------------------------------------
# Dropbear serves /etc/vps-manager/banner.txt as the pre-auth "server
# message". The designer stores a structured design in config (key
# "banner_design") and rewrites that file, rendered with ANSI colors, on
# every save. The terminal menu and the Telegram bot still write plain
# text to the same file - effective_banner_design() detects that (the file
# no longer matches the stored design's render) and the designer then
# opens in Plain style showing the live text, instead of stale fields.
BANNER_FILE = os.environ.get("VPSMGR_BANNER_FILE", "/etc/vps-manager/banner.txt")

def _banner_targets():
    out = []
    for cand in (BANNER_FILE, path("banner.txt")):
        if cand and cand not in out:
            out.append(cand)
    return out

def read_live_banner():
    """Current banner file content (str) or None when no file exists."""
    for cand in _banner_targets():
        try:
            if os.path.exists(cand):
                with open(cand, "rb") as f:
                    return f.read().decode("utf-8", "replace")
        except Exception:
            pass
    return None

def write_live_banner(rendered):
    """Write the rendered banner (str) to the live banner file.
    Returns (path_written, error)."""
    data = (rendered + "\n").encode("utf-8")
    err = ""
    for cand in _banner_targets():
        try:
            d = os.path.dirname(cand)
            if d:
                os.makedirs(d, exist_ok=True)
            with open(cand, "wb") as f:
                f.write(data)
            return cand, ""
        except Exception as e:
            err = str(e)
    return "", err

def effective_banner_design(cfg):
    """-> (design, source): the design the designer form should show.
    source is "design" when the stored design is in charge (no live file,
    or the live file is exactly its render) and "live" when the banner
    file holds other (plain) text - e.g. set from the menu or the bot."""
    design = None
    raw = cfg.get("banner_design")
    if _banner is not None and isinstance(raw, dict):
        try:
            design = _banner.normalize_design(raw)
        except Exception:
            design = None
    live = read_live_banner()
    if design is not None:
        if live is None or live.rstrip("\n") == _banner.render(design):
            return design, "design"
    if live is not None and live.strip():
        if live.strip() == "Welcome to VPS Manager Suite":
            # The pre-rebrand stock one-liner: offer the new styled default.
            return _banner.default_design("DDK VPS Manager"), "legacy"
        return _banner.design_from_plain(live, style="plain"), "live"
    if design is not None:
        return design, "design"
    text = cfg.get("banner", "") or "DDK VPS Manager"
    if str(text).strip() == "Welcome to VPS Manager Suite":
        return _banner.default_design("DDK VPS Manager"), "legacy"
    return _banner.design_from_plain(text, style="box"), "legacy"

def banner_design_from_form(data):
    lines = []
    for i in range(6):
        lines.append({"text": data.get("bd_line_%d" % i, ""),
                      "color": data.get("bd_color_%d" % i, "white")})
    return _banner.normalize_design({
        "title": data.get("bd_title", ""),
        "style": data.get("bd_style", "box"),
        "scheme": data.get("bd_scheme", "gold"),
        "lines": lines})

def save_banner_design(cfg, design, emit=None):
    """Store the design in config AND rewrite the live banner file at once.
    emit (optional) narrates the steps for the popup stream."""
    cfg["banner_design"] = design
    if design.get("title"):
        cfg["banner"] = design["title"]
    save_config(cfg)
    if emit:
        emit("[OK] Design saved - style '%s', scheme '%s'" % (design["style"], design["scheme"]))
    target, err = write_live_banner(_banner.render(design))
    if target:
        log_activity("banner design saved (%s/%s)" % (design["style"], design["scheme"]))
        if emit:
            emit("[OK] Banner file updated: %s" % target)
            emit("[OK] Clients see the new banner on their next connection - no restart needed.")
    else:
        log_activity("banner design saved but the banner file write failed")
        if emit:
            emit("[ERROR] The design is saved, but the banner file could not be written (%s)." % err)
            emit("[ERROR] Check the permissions of /etc/vps-manager/banner.txt.")
    return target

def banner_designer_card(cfg):
    """The Settings-page 'SSH Banner Designer' card (form + live preview)."""
    if _banner is None:
        return ("<div class='card'><h3>SSH Banner Designer</h3>"
                "<p class='bad'>The banner renderer (scripts/banner_render.py) was not found. Run vpsupdate, then reload this page.</p></div>")
    design, source = effective_banner_design(cfg)
    def opts(items, cur):
        out = []
        for val, label, css in items:
            sel = " selected" if val == cur else ""
            sty = " style='color:%s'" % css if css else ""
            out.append("<option value='%s'%s%s>%s</option>" % (
                html.escape(val, quote=True), sel, sty, html.escape(label)))
        return "".join(out)
    style_items = [("box", "Box Frame", ""), ("bigtitle", "Big Title + Box", ""),
                   ("lines", "Colored Lines (no frame)", ""), ("plain", "Plain Text", "")]
    scheme_items = [(name, name.title(), _banner.COLORS[_banner.SCHEMES[name][1]][1])
                    for name in ("gold", "cyan", "green", "magenta", "red", "blue", "white")]
    color_items = [(name, name.title(), pair[1]) for name, pair in _banner.COLORS.items()]
    lines = list(design["lines"])
    while len(lines) < 6:
        lines.append({"text": "", "color": "white"})
    rows = []
    for i in range(6):
        ln = lines[i]
        rows.append(
            "<div class='bdrow'><div><label>Line %d text</label>"
            "<input name='bd_line_%d' id='bd_line_%d' value='%s' maxlength='100' placeholder='Line %d (blank = not shown)'>"
            "</div><div><label>Color</label>"
            "<select name='bd_color_%d' id='bd_color_%d'>%s</select></div></div>" % (
                i + 1, i, i, html.escape(ln["text"], quote=True), i + 1,
                i, i, opts(color_items, ln["color"])))
    note = ""
    if source == "live":
        note = ("<p class='mut'>The live banner right now is plain text - it was set from the terminal "
                "menu or the Telegram bot, or was never styled - so it is loaded above as Plain style. "
                "Pick a style and save to replace it with a styled banner.</p>")
    preview = _banner.to_html(_banner.render(design))
    return ("<div class='card'><h3>SSH Banner Designer</h3>"
            "<p class='mut'>This is the server message SSH apps (HTTP Custom, DarkTunnel, NPV Tunnel) "
            "show while connecting. Style it here - colors, a box frame, a big title - and watch the "
            "live preview: it is rendered by the same code that writes the banner file, so what you "
            "see is exactly what clients get.</p>"
            "<form method='post' action='/settings/banner' id='bd_form' data-stream='SSH Banner Designer' data-reload>"
            "<label>Title</label>"
            "<input name='bd_title' id='bd_title' value='%s' maxlength='80' placeholder='Your brand name'>"
            "<div class='bdgrid'><div><label>Style</label>"
            "<select name='bd_style' id='bd_style'>%s</select></div>"
            "<div><label>Color scheme (frame &amp; title)</label>"
            "<select name='bd_scheme' id='bd_scheme'>%s</select></div></div>"
            "%s"
            "<label>Live preview</label>"
            "<pre class='bannerprev' id='bd_preview'>%s</pre>"
            "<p class='mut'>Tip: start a line with an emoji, for example ⚡ 🚫 📢 📌 - the frame "
            "alignment already accounts for emoji width.</p>"
            "<button>Save Banner Design</button></form>%s</div>") % (
                html.escape(design["title"], quote=True),
                opts(style_items, design["style"]),
                opts(scheme_items, design["scheme"]),
                "".join(rows), preview, note)

# --- Panel admins: multi-admin with roles ------------------------------------
# credentials.json is the bootstrap Super Admin (Settings / terminal managed,
# it can never be deleted or disabled here - the panel can never lock the
# owner out). Extra admins live in admins.json with PBKDF2 password hashes
# and one of the roles: viewer (read-only) / admin / super.
ROLES = ("viewer", "admin", "super")
_ROLE_ORDER = {"viewer": 0, "admin": 1, "super": 2}

def _hash_pw(pw, salt=None):
    salt = salt or os.urandom(16).hex()
    return salt, hashlib.pbkdf2_hmac("sha256", str(pw).encode(), bytes.fromhex(salt), 120000).hex()

def load_admins(): return load_json("admins.json", [])
def save_admins(rows): save_json("admins.json", rows)

def verify_login(username, password):
    """-> (username, role) or None."""
    creds = load_creds()
    if username == creds.get("username") and password and password == creds.get("password"):
        return (username, "super")
    for a in load_admins():
        if a.get("username") == username and not a.get("disabled") and password:
            _s, h = _hash_pw(password, a.get("salt", ""))
            if secrets.compare_digest(h, a.get("hash", "")):
                role = a.get("role", "viewer")
                return (username, role if role in ROLES else "viewer")
    return None

def role_at_least(role, need):
    return _ROLE_ORDER.get(role, 0) >= _ROLE_ORDER.get(need, 0)

def enabled_super_count():
    n = 1  # the bootstrap Super Admin always exists
    for a in load_admins():
        if a.get("role") == "super" and not a.get("disabled"):
            n += 1
    return n

def suite_script(name):
    for cand in (os.path.join("/opt/vps-manager-suite/scripts", name),
                 os.path.join(BASE, "..", "scripts", name),
                 os.path.join(os.getcwd(), "scripts", name)):
        if os.path.exists(cand): return cand
    return ""

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


def _date_from_iso(s):
    """date.fromisoformat() equivalent that also works on Python 3.6."""
    import datetime as _dt
    return _dt.datetime.strptime(str(s)[:10], "%Y-%m-%d").date()


def _datetime_from_iso(s):
    """datetime.fromisoformat() equivalent that also works on Python 3.6.

    Raises ValueError on bad input, like fromisoformat does.
    """
    import datetime as _dt
    import re as _re
    s = str(s).strip()
    m = _re.search(r"([+-])(\d{2}):(\d{2})(?::(\d{2}))?$", s)
    if m:  # strptime %z cannot parse a colon in the offset before 3.7
        s = s[:m.start()] + m.group(1) + m.group(2) + m.group(3) + (m.group(4) or "")
    for fmt in ("%Y-%m-%dT%H:%M:%S.%f%z", "%Y-%m-%dT%H:%M:%S%z",
                "%Y-%m-%dT%H:%M%z", "%Y-%m-%d %H:%M:%S.%f%z",
                "%Y-%m-%d %H:%M:%S%z", "%Y-%m-%d %H:%M%z",
                "%Y-%m-%dT%H:%M:%S.%f", "%Y-%m-%dT%H:%M:%S",
                "%Y-%m-%dT%H:%M", "%Y-%m-%d %H:%M:%S.%f",
                "%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%Y-%m-%d"):
        try:
            return _dt.datetime.strptime(s, fmt)
        except ValueError:
            continue
    raise ValueError("Invalid isoformat string: %r" % (s,))

def expires_on(created_ymd, days):
    """Expiry date string: ALWAYS created + Days (literal). 0 = same day as creation."""
    d = to_int(days, 30)
    try:
        import datetime as _dt
        base = _date_from_iso(created_ymd)
        return (base + _dt.timedelta(days=d)).isoformat()
    except Exception: return f"+{d} days"

def is_expired(created_ymd, days):
    """Enforcement: literal expiry check. Days=0 means expiry date = creation date."""
    d = to_int(days, 30)
    try:
        import datetime as _dt
        base = _date_from_iso(created_ymd)
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

def vnstat_section(role):
    """Traffic history via vnstat, with a one-click install when missing."""
    import html as _h
    if not shutil.which("vnstat"):
        btn = ""
        if role_at_least(role, "admin"):
            btn = ("<form method='post' action='/action/run' data-stream='Install vnstat' data-reload><input type='hidden' name='job' value='vnstat'>"
                   "<button>Install vnstat</button></form>")
        return ("<p class='mut'>vnstat is not installed yet. It records daily/monthly traffic history per interface.</p>" + btn)
    one = subprocess.getoutput("vnstat --oneline 2>/dev/null | head -n 3")
    daily = subprocess.getoutput("vnstat -d 2>/dev/null | head -n 20")
    monthly = subprocess.getoutput("vnstat -m 2>/dev/null | head -n 14")
    return (f"<pre class='raw'>{_h.escape(one)}</pre><h4>Daily</h4><pre class='raw'>{_h.escape(daily)}</pre>"
            f"<h4>Monthly</h4><pre class='raw'>{_h.escape(monthly)}</pre>")

def service_list():
    names = ["nginx", "haproxy", "xray", "dropbear", "ssh", "ws-proxy", "vps-panel", "vps-monitor", "vps-bot", "vps-slowdns", "vps-zivpn", "vps-openvpn", "cron", "systemd-resolved"]
    out = []
    for n in names:
        status = "unknown"
        try:
            r = subprocess.run(["systemctl", "is-active", n], stdout=subprocess.PIPE, stderr=subprocess.PIPE, universal_newlines=True, timeout=3)
            raw = r.stdout.strip()
            # systemctl's contract: exit code 0 AND the word "active" mean
            # the unit is running. Trust BOTH together, never the text
            # alone - a stopped service must never render as "running".
            # Non-active states keep their real word for the warn pill;
            # anything unexpected counts as NOT running (safe direction).
            if r.returncode == 0 and raw == "active":
                status = "active"
            elif raw in ("inactive", "failed", "activating", "deactivating"):
                status = raw
            else:
                status = "inactive"
        except Exception:
            status = "n/a (no systemd)"
        out.append({"name": n, "status": status})
    return out

CSS = """<style>
.qrd{margin-top:6px}
.qrd summary{cursor:pointer;color:var(--acc2);font-size:13px}
.qrbox{background:#fff;display:inline-block;padding:8px;border-radius:10px;margin-top:6px;line-height:0}
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
.svcblk{border:1px solid var(--line);border-radius:12px;background:var(--card2);padding:12px 14px;margin:0 0 10px}
.svcblk:last-child{margin-bottom:0}
.svchead{display:flex;justify-content:space-between;align-items:center;gap:10px}
.svcname{font-weight:600;color:#fff;white-space:nowrap;overflow:hidden;text-overflow:ellipsis;min-width:0}
.svchead .pill{flex:0 0 auto}
.svcbtns{display:grid;grid-template-columns:repeat(4,1fr);gap:8px;margin-top:10px}
.svcbtns form{margin:0}
.svcbtns button,.svcbtns .btn{width:100%;text-align:center;font-size:13px}
.svcbtns .btn{padding:6px 14px}
.svcbtns.wide{grid-template-columns:repeat(6,1fr)}
.svcbtns.wide form{grid-column:span 2}
.svcbtns.wide form.force{grid-column:span 3}
.brand{min-width:0}
.brand .bt{font-size:16px;line-height:1.15;min-width:0;overflow-wrap:break-word}
.sidefoot .tglink{display:flex;align-items:center;gap:8px;color:var(--acc2);font-size:13px;font-weight:600;padding:0 0 8px}
.sidefoot .tglink:hover{color:var(--acc)}
.sidefoot .tg-ic{font-size:14px}
.bannerprev{background:#05070a;border:1px solid var(--line);border-radius:12px;padding:14px;font-family:ui-monospace,Menlo,Consolas,monospace;font-size:13px;line-height:1.55;overflow-x:auto;white-space:pre;margin:4px 0 10px;min-height:64px;color:#dbe4ee}
.bdgrid{display:grid;grid-template-columns:1fr 1fr;gap:0 12px}
.bdrow{display:grid;grid-template-columns:1fr 168px;gap:0 12px}
@media (max-width:640px){.bdgrid,.bdrow{grid-template-columns:1fr}}
#pop-overlay{position:fixed;inset:0;background:rgba(2,6,10,.74);display:none;align-items:flex-start;justify-content:center;z-index:90;padding:26px 14px;overflow-y:auto}
#pop-overlay.open{display:flex}
.pop{background:var(--card);border:1px solid var(--line);border-radius:16px;width:100%;max-width:700px;box-shadow:0 18px 60px rgba(0,0,0,.6);overflow:hidden;margin-bottom:26px}
.pop-head{display:flex;justify-content:space-between;align-items:center;gap:10px;padding:13px 16px;border-bottom:1px solid var(--line);background:var(--card2)}
.pop-head .t{font-weight:700;font-size:15px;color:#fff}
.pop-x{background:var(--card);border:1px solid var(--line);color:var(--txt);border-radius:9px;width:32px;height:32px;padding:0;cursor:pointer;flex:0 0 32px;font-weight:600}
#pop-out{background:#05070a;color:#c8f5e8;font-family:ui-monospace,Menlo,Consolas,monospace;font-size:12.5px;line-height:1.55;white-space:pre-wrap;word-break:break-word;margin:0;padding:14px 16px;max-height:44vh;overflow-y:auto;min-height:84px}
#pop-result{padding:0 16px;max-height:54vh;overflow-y:auto}
#pop-result:empty{padding:0}
#pop-result .card{margin:14px 0}
.pop-foot{display:flex;justify-content:flex-end;align-items:center;gap:8px;padding:12px 16px;border-top:1px solid var(--line)}
.pop-status{color:var(--mut);font-size:12.5px;margin-right:auto}
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
function copyString(txt, btn){
  function done(){ if(btn){ var o = btn.textContent; btn.textContent = 'Copied'; setTimeout(function(){ btn.textContent = o; }, 1200); } }
  if(navigator.clipboard && navigator.clipboard.writeText){ navigator.clipboard.writeText(txt).then(done, done); }
  else { var ta = document.createElement('textarea'); ta.value = txt; document.body.appendChild(ta); ta.select(); try{ document.execCommand('copy'); }catch(e){} document.body.removeChild(ta); done(); }
}
/* --- Animated action popup -------------------------------------------------
   Any form with a data-stream="Title" attribute is submitted in the
   background instead of navigating away: the popup opens at once, shows
   "Starting...", and the server's output is then REVEALED line by line
   at a calm, readable pace (POP_PACE_MS per line). The network stream
   may deliver lines in bursts (several steps finish within the same
   millisecond), so incoming lines wait in a queue and are shown one at
   a time, auto-scrolling as they appear - the reveal pace, not the
   network, decides how fast text lands on screen. A line containing
   only :::RESULT::: switches the rest of the stream to a raw HTML
   fragment (the result card), which is rendered below the output only
   after the last queued line has been revealed. On long jobs whose
   output arrives slower than the reveal pace the display is simply
   live; if a burst ever queues more than POP_CATCHUP_AFTER lines the
   reveal temporarily quickens so it never lags far behind the stream.
   "Copy output" always copies the FULL text received so far, including
   lines still waiting in the queue. Forms with data-reload refresh the
   page behind the popup when it is closed after a finished run. */
var POP_PACE_MS = 250, POP_CATCHUP_AFTER = 40;
var popBusy = false, popReload = false, popDone = false;
var popRun = 0, popRevealer = null, popCopySource = null;

/* POP_REVEALER_BEGIN - pure pacing logic, no DOM access (unit-tested
   headlessly). showLine(line) displays one line; onDrain() fires once,
   when finish() has been called AND every queued line has been shown;
   copyText() returns every line received so far, revealed or not. */
function makeLineRevealer(showLine, onDrain, paceMs){
  var pace = paceMs || POP_PACE_MS;
  var queue = [], full = [], timer = null, streamDone = false, drained = false;
  function perTick(){
    return queue.length > POP_CATCHUP_AFTER ? Math.ceil(queue.length / 10) : 1;
  }
  function drainCheck(){
    if(streamDone && !queue.length && !drained){ drained = true; if(onDrain) onDrain(); }
  }
  function tick(){
    timer = null;
    var n = perTick();
    while(n-- > 0 && queue.length) showLine(queue.shift());
    if(queue.length) timer = setTimeout(tick, pace);
    else drainCheck();
  }
  function kick(){ if(!timer && queue.length) timer = setTimeout(tick, pace); }
  return {
    pushLine: function(line){ full.push(line); queue.push(line); kick(); },
    finish: function(){ streamDone = true; kick(); drainCheck(); },
    flushAll: function(){
      if(timer){ clearTimeout(timer); timer = null; }
      while(queue.length) showLine(queue.shift());
      drainCheck();
    },
    copyText: function(){ return full.join('\\n'); },
    pending: function(){ return queue.length; }
  };
}
/* POP_REVEALER_END */

function openPop(title){
  var ov = document.getElementById('pop-overlay'); if(!ov) return false;
  popRun++;
  document.getElementById('pop-title').textContent = title;
  document.getElementById('pop-out').textContent = 'Starting...';
  document.getElementById('pop-result').innerHTML = '';
  document.getElementById('pop-status').textContent = 'Running...';
  ov.classList.add('open'); document.body.style.overflow = 'hidden';
  return true;
}
function closePop(){
  var ov = document.getElementById('pop-overlay'); if(!ov) return;
  if(popRevealer) popRevealer.flushAll();
  ov.classList.remove('open'); document.body.style.overflow = '';
  if(popReload && popDone){ popReload = false; location.reload(); }
}
function copyPop(btn){
  var txt = popCopySource ? popCopySource() : null;
  if(txt === null || txt === undefined || txt === ''){
    var el = document.getElementById('pop-out'); if(!el) return;
    txt = el.innerText || el.textContent;
  }
  copyString(txt, btn);
}
document.addEventListener('submit', function(ev){
  var f = ev.target;
  if(!f || !f.getAttribute) return;
  var title = f.getAttribute('data-stream');
  if(title === null) return;
  if(ev.defaultPrevented) return; /* a form onsubmit (e.g. a confirm) already cancelled this */
  ev.preventDefault();
  if(popBusy) return;
  var fd;
  try { fd = new FormData(f, ev.submitter); }
  catch(e){ fd = new FormData(f); if(ev.submitter && ev.submitter.name){ fd.append(ev.submitter.name, ev.submitter.value); } }
  if(!openPop(title)){ f.submit(); return; }
  popBusy = true; popDone = false;
  popReload = f.getAttribute('data-reload') !== null;
  var myRun = popRun;
  var out = document.getElementById('pop-out'), res = document.getElementById('pop-result'), st = document.getElementById('pop-status');
  var shown = [], fragMode = false, fragText = '', linePartial = '';
  var revealer = makeLineRevealer(function(line){
    shown.push(line);
    out.textContent = shown.join('\\n');
    out.scrollTop = out.scrollHeight;
  }, function(){
    if(fragMode) res.innerHTML = fragText.replace(/^\\n+/, '');
    if(st) st.textContent = 'Finished';
    popBusy = false; popDone = true;
  });
  popRevealer = revealer;
  popCopySource = function(){ return revealer.copyText(); };
  function alive(){ return myRun === popRun; }
  function ingest(text){
    if(fragMode){ fragText += text; return; }
    linePartial += text;
    var start = 0, nl;
    while((nl = linePartial.indexOf('\\n', start)) >= 0){
      var ln = linePartial.slice(start, nl);
      if(ln.charAt(ln.length - 1) === '\\r') ln = ln.slice(0, -1);
      if(ln === ':::RESULT:::'){
        fragMode = true;
        fragText = linePartial.slice(nl + 1);
        linePartial = '';
        return;
      }
      revealer.pushLine(ln);
      start = nl + 1;
    }
    linePartial = linePartial.slice(start);
  }
  function ingestEnd(){
    if(!fragMode && linePartial){
      var ln = linePartial; linePartial = '';
      if(ln.charAt(ln.length - 1) === '\\r') ln = ln.slice(0, -1);
      if(ln === ':::RESULT:::') fragMode = true;
      else revealer.pushLine(ln);
    }
    revealer.finish();
  }
  function failLine(msg){
    revealer.pushLine('');
    revealer.pushLine(msg);
    ingestEnd();
  }
  var params = new URLSearchParams();
  fd.forEach(function(v, k){ params.append(k, v); });
  fetch(f.action, {method: 'POST', headers: {'X-Stream': 'popup'}, body: params, credentials: 'same-origin'})
  .then(function(resp){
    if(!alive()) return null;
    if(!resp.ok){
      failLine('[ERROR] The request was refused (HTTP ' + resp.status + '). If your role is Viewer, actions are read-only.');
      return null;
    }
    if(!resp.body || !resp.body.getReader){
      return resp.text().then(function(t){ if(!alive()) return; ingest(t); ingestEnd(); });
    }
    var reader = resp.body.getReader(), dec = new TextDecoder();
    function pump(){
      return reader.read().then(function(r){
        if(!alive()) return;
        if(r.done){ ingestEnd(); return; }
        ingest(dec.decode(r.value, {stream: true}));
        return pump();
      });
    }
    return pump();
  })
  .catch(function(e){
    if(!alive()) return;
    failLine('[ERROR] The connection to the panel was lost while the action was running.');
  });
});
document.addEventListener('keydown', function(ev){ if(ev.key === 'Escape') closePop(); });

/* --- SSH Banner Designer live preview (Settings page only) -----------------
   While the admin edits the designer form, ask the server to re-render the
   preview with the REAL renderer - the same code that writes the banner
   file - so the preview is exactly what SSH clients will see. Guarded by
   element presence: on every other page this does nothing. */
(function(){
  var form = document.getElementById('bd_form'); if(!form) return;
  var prev = document.getElementById('bd_preview'); if(!prev) return;
  var timer = null, seq = 0;
  function refresh(){
    var my = ++seq;
    var parts = [];
    new FormData(form).forEach(function(v, k){ parts.push(encodeURIComponent(k) + '=' + encodeURIComponent(v)); });
    fetch('/settings/banner-preview', {method: 'POST', headers: {'Content-Type': 'application/x-www-form-urlencoded'}, body: parts.join('&')})
      .then(function(r){ return r.text(); })
      .then(function(t){ if(my === seq) prev.innerHTML = t; })
      .catch(function(){});
  }
  form.addEventListener('input', function(){ if(timer) clearTimeout(timer); timer = setTimeout(refresh, 250); });
  form.addEventListener('change', refresh);
})();
</script>"""

POPUP_HTML = ("<div id='pop-overlay' onclick='if(event.target===this)closePop()'>"
              "<div class='pop'><div class='pop-head'><span class='t' id='pop-title'>Action</span>"
              "<button type='button' class='pop-x' onclick='closePop()' aria-label='Close'>✕</button></div>"
              "<pre id='pop-out'>Starting...</pre><div id='pop-result'></div>"
              "<div class='pop-foot'><span class='pop-status' id='pop-status'></span>"
              "<button type='button' class='ghost' onclick='copyPop(this)'>Copy output</button>"
              "<button type='button' onclick='closePop()'>Close</button></div></div></div>")

NAV_ITEMS = [
    ("/", "Dashboard", "▦"),
    ("/ssh", "SSH Accounts", "🔑"),
    ("/xray", "Xray Accounts", "🌐"),
    ("/openvpn", "OpenVPN", "🛡"),
    ("/services", "Running Services", "⚙"),
    ("/stats", "VPS Stats", "📊"),
    ("/telegram", "Telegram Bot", "✈"),
    ("/settings", "Settings", "🛠"),
    ("/admins", "Panel Admins", "👤"),
    ("/activity", "Activity", "🕘"),
    ("/logout", "Logout", "⏻"),
]

BRAND = ("<div class='brand'><span class='mark'>D</span>"
         "<span class='bt'>DDK VPS Manager<small>SUITE V1.0</small></span></div>")

def page(title, body, nav=True, active="", user=""):
    if not nav:
        return ("<!doctype html><html><head><meta charset='utf-8'>"
                "<meta name='viewport' content='width=device-width,initial-scale=1'>"
                f"<title>{html.escape(title)} - DDK VPS Manager</title>{CSS}</head>"
                f"<body><div class='loginwrap'>{body}</div>{JS}</body></html>")
    links = "".join(
        f"<a href='{href}' class='{'active' if href == active else ''}' onclick='toggleNav(false)'><span class='ic'>{ic}</span>{label}</a>"
        for href, label, ic in NAV_ITEMS)
    cfg = load_config(); domain = cfg.get("domain") or ""
    dom_chip = f"<span class='chip'><span class='dot'></span>{html.escape(domain)}</span>" if domain else ""
    user_chip = f"<span class='chip'>👤 {html.escape(user or 'admin')}</span>" if user else ""
    return ("<!doctype html><html><head><meta charset='utf-8'>"
            "<meta name='viewport' content='width=device-width,initial-scale=1'>"
            f"<title>{html.escape(title)} - DDK VPS Manager</title>{CSS}</head><body>"
            "<div id='nav-overlay' onclick='toggleNav(false)'></div>"
            "<div class='layout'>"
            f"<aside class='sidebar' id='sidebar'><div class='sidehead'>{BRAND}"
            "<button type='button' class='closebtn' onclick='toggleNav(false)' aria-label='Close menu'>✕</button></div>"
            f"<div class='navlabel'>Menu</div><nav class='snav'>{links}</nav>"
            "<div class='sidefoot'><a class='tglink' href='https://t.me/ddktrick2' target='_blank' rel='noopener'><span class='tg-ic'>✈</span>Telegram: @ddktrick2</a>"
            "<div>Web Panel · Port " + str(PORT) + "</div></div></aside>"
            "<div class='main'>"
            f"<div class='topbar'><button type='button' class='burger' onclick='toggleNav()' aria-label='Open menu'>☰</button><span class='title'>{html.escape(title)}</span><span class='sp'></span>{dom_chip}{user_chip}"
            "<a class='btn ghost small' href='javascript:location.reload()'>Refresh</a></div>"
            f"<div class='content'>{body}</div>"
            "</div></div>" + POPUP_HTML + JS + "</body></html>")

def chip(status):
    if status == "active": return "<span class='pill on'>● running</span>"
    if status in ("inactive", "failed"): return "<span class='pill off'>● stopped</span>"
    return f"<span class='pill warn'>● {html.escape(str(status))}</span>"

# Transport map - mirrors scripts/vpsm.py (keep the two in sync)
XR_TCP_PORT = {"vless": 8443, "trojan": 8446, "vmess": 8447}
XR_XHTTP_TLS_PORT = {"vless": 8444, "trojan": 8445, "vmess": 8448}
XR_XHTTP_PATH = {"vless": "/xhttp", "trojan": "/xhttp-trojan", "vmess": "/xhttp-vmess"}
XR_WS_PATH = {"vless": "/vless", "trojan": "/trojan", "vmess": "/vmess"}

def build_links(proto, uid, domain, remark):
    """The 5 links of one Xray account: TLS / NTLS / TCP / XHTTP TLS / XHTTP NTLS."""
    import base64 as _b64
    proto = str(proto or "").lower()
    def _vm(net, port, tls_on, path=""):
        vm = {"v": "2", "ps": remark, "add": domain, "port": str(port), "id": uid, "aid": "0",
              "net": net, "type": "none", "host": domain, "path": path or "", "tls": "tls" if tls_on else ""}
        if tls_on: vm["sni"] = domain
        return "vmess://" + _b64.b64encode(json.dumps(vm).encode()).decode()
    if proto == "vmess":
        return {
            "Link TLS": _vm("ws", 443, True, "/vmess"),
            "Link NTLS": _vm("ws", 80, False, "/vmess"),
            "Link TCP": _vm("tcp", XR_TCP_PORT["vmess"], True),
            "Link XHTTP TLS": _vm("xhttp", XR_XHTTP_TLS_PORT["vmess"], True, "/xhttp-vmess"),
            "Link XHTTP NTLS": _vm("xhttp", 80, False, "/xhttp-vmess"),
        }
    if proto == "trojan":
        return {
            "Link TLS": f"trojan://{uid}@{domain}:443?security=tls&type=ws&host={domain}&path=%2Ftrojan&sni={domain}#{remark}",
            "Link NTLS": f"trojan://{uid}@{domain}:80?security=none&type=ws&host={domain}&path=%2Ftrojan#{remark}",
            "Link TCP": f"trojan://{uid}@{domain}:{XR_TCP_PORT['trojan']}?security=tls&type=tcp&headerType=none&sni={domain}#{remark}-tcp",
            "Link XHTTP TLS": f"trojan://{uid}@{domain}:{XR_XHTTP_TLS_PORT['trojan']}?security=tls&type=xhttp&path=%2Fxhttp-trojan&host={domain}&mode=auto&sni={domain}#{remark}-xhttp-tls",
            "Link XHTTP NTLS": f"trojan://{uid}@{domain}:80?security=none&type=xhttp&path=%2Fxhttp-trojan&host={domain}&mode=auto#{remark}-xhttp-ntls",
        }
    return {
        "Link TLS": f"vless://{uid}@{domain}:443?encryption=none&security=tls&type=ws&host={domain}&path=%2Fvless&sni={domain}#{remark}",
        "Link NTLS": f"vless://{uid}@{domain}:80?encryption=none&security=none&type=ws&host={domain}&path=%2Fvless#{remark}",
        "Link TCP": f"vless://{uid}@{domain}:{XR_TCP_PORT['vless']}?encryption=none&security=tls&type=tcp&headerType=none&sni={domain}#{remark}-tcp",
        "Link XHTTP TLS": f"vless://{uid}@{domain}:{XR_XHTTP_TLS_PORT['vless']}?encryption=none&security=tls&type=xhttp&path=%2Fxhttp&host={domain}&mode=auto&sni={domain}#{remark}-xhttp-tls",
        "Link XHTTP NTLS": f"vless://{uid}@{domain}:80?encryption=none&security=none&type=xhttp&path=%2Fxhttp&host={domain}&mode=auto#{remark}-xhttp-ntls",
    }

def qr_svg_for(link):
    """Inline SVG QR for one share link (cached; pure-python generator)."""
    if _qr is None or not link:
        return ""
    if link in _QR_CACHE:
        return _QR_CACHE[link]
    try:
        s = _qr.svg(link, module=3, border=2)
    except Exception:
        s = ""
    if len(_QR_CACHE) > 300:
        _QR_CACHE.clear()
    _QR_CACHE[link] = s
    return s

def links_html(links, prefix):
    out = []
    for i, (k, v) in enumerate(links.items()):
        cid = f"{prefix}-{i}"
        svg = qr_svg_for(v)
        qr_html = f"<details class='qrd'><summary>QR code</summary><div class='qrbox'>{svg}</div></details>" if svg else ""
        out.append(f"<div class='linkrow'><div class='lh'><span class='lt'>{html.escape(k)}</span>"
                   f"<button type='button' class='small ghost' onclick=\"copyText('{cid}', this)\">Copy</button></div>"
                   f"<code id='{cid}'>{html.escape(v)}</code>{qr_html}</div>")
    return "".join(out)

def ssh_result_cards(uname, pwd, domain, created, exp, exp_date, gb, ipl, loc):
    """The result cards shown after an SSH account is created - used by the
    classic result page AND as the closing fragment of the popup stream."""
    payload = f"GET /sshws HTTP/1.1[crlf]Host: {domain}[crlf]Upgrade: websocket[crlf][crlf]"
    _cfg = load_config()
    ws_port = to_int(_cfg.get("ws_proxy_port", 8080), 8080)
    sd_host = slowdns_host(_cfg); sd_pub = slowdns_pubkey()
    sd_line = f"{html.escape(sd_host)} : 53" if sd_host else "not configured"
    if sd_pub:
        sd_line += f" · key {html.escape(sd_pub[:16])}…"
    zi_host = udp_host(_cfg)
    zi_line = f"{html.escape(zi_host)} : 5667 (app ports 6000-19999)" if zi_host else "not configured"
    _obfs = str(_cfg.get("zivpn_obfs") or "").strip()
    if _obfs:
        zi_line += f" · obfs {html.escape(_obfs)}"
    vpn_details = f"{html.escape(domain)}:443@{html.escape(uname)}:{html.escape(pwd)}"
    return f"""<div class='card result'><h3>🔐 SSH Account Created</h3><div class='kv'>
    <div><div class='k'>Username</div><div class='v'>{html.escape(uname)}</div></div>
    <div><div class='k'>Password</div><div class='v'><code>{html.escape(pwd)}</code></div></div>
    <div><div class='k'>Host / Domain</div><div class='v'>{html.escape(domain)}</div></div>
    <div><div class='k'>SNI</div><div class='v'>{html.escape(domain)}</div></div>
    <div><div class='k'>City</div><div class='v'>{html.escape(loc.get('city','') or '-')}</div></div>
    <div><div class='k'>Region</div><div class='v'>{html.escape(loc.get('region','') or '-')}</div></div>
    <div><div class='k'>Created</div><div class='v'>{created}</div></div>
    <div><div class='k'>Expiry</div><div class='v'>{exp_date} ({fmt_expiry(exp)})</div></div>
    <div><div class='k'>Data</div><div class='v'>{fmt_data(gb)}</div></div>
    <div><div class='k'>IP Limit</div><div class='v'>{fmt_logins(ipl)}</div></div>
    <div><div class='k'>SSH Direct</div><div class='v'>22 · 109 · 143 · 447</div></div>
    <div><div class='k'>SSH SSL</div><div class='v'>443</div></div>
    <div><div class='k'>SSH WS</div><div class='v'>80 → /sshws</div></div>
    <div><div class='k'>SSH WS-SSL</div><div class='v'>443 → /sshws</div></div>
    <div><div class='k'>Python Proxy</div><div class='v'>{ws_port}</div></div>
    <div><div class='k'>SlowDNS</div><div class='v'>{sd_line}</div></div>
    <div><div class='k'>ZiVPN UDP</div><div class='v'>{zi_line}</div></div>
    </div>
    <p class='mut' style='margin-top:10px'>SlowDNS: use your SSH username + password with the SlowDNS NS host above (full public key: Settings page / SlowDNS service). ZiVPN UDP: same SSH password, server = the UDP host, obfs as shown.</p>
    <h3 style='margin-top:16px'>VPN Details</h3>
    <div class='codebox'><button type='button' class='copybtn' onclick="copyText('new-ssh-vpn', this)">Copy</button><pre id='new-ssh-vpn'>{vpn_details}</pre></div>
    </div>
    <div class='card'><h3>Payload</h3><div class='codebox'><button type='button' class='copybtn' onclick="copyText('new-ssh-payload', this)">Copy</button><pre id='new-ssh-payload'>{html.escape(payload)}</pre></div>
    <p style='margin-top:14px'><a class='btn ghost' href='/ssh'>Back to SSH Accounts</a></p></div>"""

def xray_result_cards(remark, proto, uid, domain, created, exp, exp_date, gb, ipl, links):
    """The result cards shown after an Xray account is created - used by the
    classic result page AND as the closing fragment of the popup stream."""
    return f"""<div class='card result'><h3>🌐 Xray Account Created</h3><div class='kv'>
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

def openvpn_result_card(name, days, exp_date, domain, profile_text):
    nm = html.escape(str(name))
    prof = html.escape(profile_text or "")
    return f"""<div class='card result'><h3>🛡 OpenVPN Account Created</h3><div class='kv'>
    <div><div class='k'>Username</div><div class='v'>{nm}</div></div>
    <div><div class='k'>Server</div><div class='v'>{html.escape(domain)} : 1194 (UDP)</div></div>
    <div><div class='k'>Certificate expires</div><div class='v'>{exp_date} ({fmt_expiry(days)})</div></div>
    </div>
    <p style='margin-top:10px'><a class='btn' href='/openvpn/download?name={nm}'>Download .ovpn profile</a></p>
    <h3 style='margin-top:16px'>Profile (.ovpn)</h3>
    <div class='codebox'><button type='button' class='copybtn' onclick="copyText('new-ovpn-profile', this)">Copy</button><pre id='new-ovpn-profile'>{prof}</pre></div>
    <p style='margin-top:14px'><a class='btn ghost' href='/openvpn'>Back to OpenVPN Accounts</a></p></div>"""

RULE_NOTE = ("<p class='mut'>Rule: <b>Days is a literal count</b> (default 30) - Expiry = creation date + Days. "
             "Only the <b>GB limit</b> and <b>IP limit</b> treat <b>0 as unlimited</b>. "
             "Example: Days 30 / GB 0 / IP 0 -&gt; Expires: creation + 30 days, Data: Unlimited, IP Limit: Unlimited.</p>")
LIMIT_HINT = "<p class='mut' style='margin-top:-4px'>0 = Unlimited. If you set a value, the account stops automatically when the limit is reached.</p>"

STATUS_LABEL = {"Expired": "EXPIRED", "Quota": "GB-LIMIT", "IP Limit": "IP-LIMIT", "Locked": "LOCKED"}

def display_status(acct):
    """Status for lists: Active / EXPIRED / GB-LIMIT / IP-LIMIT / LOCKED."""
    if acct.get("disabled"):
        return STATUS_LABEL.get(str(acct.get("disabled_reason") or ""), "LOCKED")
    st = account_status(acct)
    return {"Expired": "EXPIRED", "Quota Full": "GB-LIMIT"}.get(st, st)

def status_pill(st):
    if st == "Active": return "<span class='pill on'>Active</span>"
    if st == "LOCKED": return "<span class='pill off'>LOCKED</span>"
    return "<span class='pill warn'>" + html.escape(st) + "</span>"

def dead_note(acct):
    """'auto-delete in 3h 12m' for a disabled account (monitor grace period)."""
    if not acct.get("disabled"): return ""
    grace = to_int(load_config().get("auto_delete_hours", 24), 24)
    if grace <= 0: return ""
    at = acct.get("disabled_at")
    if not at: return ""
    try:
        import datetime as _dt
        start = _datetime_from_iso(at)
        left = start + _dt.timedelta(hours=grace) - _dt.datetime.now()
        secs = int(left.total_seconds())
    except Exception:
        return ""
    if secs <= 0: return "auto-delete due"
    if secs >= 3600: return f"auto-delete in {secs // 3600}h {(secs % 3600) // 60}m"
    return f"auto-delete in {max(1, secs // 60)}m"

def server_location():
    """Server City/Region (best effort, cached 24h in the data dir)."""
    import urllib.request as _u
    cache = load_json("server_geo.json", {})
    try:
        if cache.get("city") and (time.time() - float(cache.get("ts", 0))) < 86400:
            return cache
    except Exception: pass
    try:
        ip = _u.urlopen(_u.Request("https://api.ipify.org", headers={"User-Agent": "vps-manager-suite"}), timeout=4).read().decode().strip()
        with _u.urlopen(_u.Request(f"http://ip-api.com/json/{ip}?fields=status,city,regionName,country", headers={"User-Agent": "vps-manager-suite"}), timeout=4) as r:
            d = json.loads(r.read().decode("utf-8", "ignore"))
        if d.get("status") == "success":
            cache = {"city": d.get("city", ""), "region": d.get("regionName", ""), "country": d.get("country", ""), "ts": time.time()}
            save_json("server_geo.json", cache)
    except Exception: pass
    return cache

def _parse_day(s):
    import datetime as _dt
    try: return _date_from_iso(s)
    except Exception: return None

def acct_renew(acct, add_days):
    """Renewal extends from the CURRENT expiry (base = max(today, expires))."""
    import datetime as _dt
    add = to_int(add_days, 30)
    exp = _parse_day(acct.get("expires") or expires_on(acct.get("created", ""), acct.get("expiry_days", 30)))
    base = max(_dt.date.today(), exp) if exp else _dt.date.today()
    new_exp = base + _dt.timedelta(days=add)
    created = _parse_day(acct.get("created", "")) or _dt.date.today()
    acct["expires"] = new_exp.isoformat()
    acct["expiry_days"] = (new_exp - created).days
    if acct.get("disabled") and acct.get("disabled_reason") == "Expired":
        acct.pop("disabled", None); acct.pop("disabled_reason", None); acct.pop("disabled_at", None)
    return acct

def acct_edit_limits(acct, gb, ip):
    acct["gb_limit"] = to_int(gb, 0); acct["ip_limit"] = to_int(ip, 0)
    if acct.get("disabled") and acct.get("disabled_reason") == "Quota":
        try: used = float(acct.get("used_gb") or 0)
        except Exception: used = 0
        if acct["gb_limit"] == 0 or used < acct["gb_limit"]:
            acct.pop("disabled", None); acct.pop("disabled_reason", None); acct.pop("disabled_at", None)
    return acct

def acct_reset_usage(acct):
    acct["used_gb"] = 0
    acct["reset_counter"] = True  # the monitor re-bases its traffic counter
    acct.pop("ip_blocked_until", None)
    if acct.get("disabled") and acct.get("disabled_reason") == "Quota":
        acct.pop("disabled", None); acct.pop("disabled_reason", None); acct.pop("disabled_at", None)
    return acct

def acct_set_lock(acct, locked):
    import datetime as _dt
    if locked:
        acct["disabled"] = True; acct["disabled_reason"] = "Locked"
        acct["disabled_at"] = _dt.datetime.now().isoformat(timespec="seconds")
    else:
        acct.pop("disabled", None); acct.pop("disabled_reason", None); acct.pop("disabled_at", None)
    return acct

TUNNEL_SHELL = "/bin/false"  # the one shell every suite SSH account uses
OLD_TUNNEL_SHELLS = ("/usr/sbin/nologin", "/sbin/nologin")  # pre-fix panel/menu shell

def _ensure_tunnel_shell():
    """Dropbear rejects password logins for accounts whose shell is not
    listed in /etc/shells ("invalid shell, rejected"), so the tunnel shell
    must be a member before any account can log in. Best-effort."""
    try:
        try:
            with open("/etc/shells") as f: listed = [ln.strip() for ln in f]
        except Exception:
            listed = []
        if TUNNEL_SHELL not in listed:
            with open("/etc/shells", "a") as f: f.write(TUNNEL_SHELL + "\n")
    except Exception:
        pass

def system_user_create(username, password, expiry_ymd):
    """Create a real tunnel-only system user (root only). Best-effort: never raises."""
    if os.geteuid() != 0: return
    try:
        _ensure_tunnel_shell()
        exists = subprocess.run(["id", username], stdout=subprocess.PIPE, stderr=subprocess.PIPE).returncode == 0
        if exists:
            # Accounts created before the shell fix may still carry a
            # nologin shell that Dropbear rejects - point them at the
            # tunnel shell so the login works again.
            try:
                import pwd
                if pwd.getpwnam(username).pw_shell in OLD_TUNNEL_SHELLS:
                    subprocess.run(["usermod", "-s", TUNNEL_SHELL, username],
                                   stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=10)
            except Exception:
                pass
        if not exists:
            r = subprocess.run(["useradd", "-M", "-s", TUNNEL_SHELL, "-e", expiry_ymd, username],
                               stdout=subprocess.PIPE, stderr=subprocess.PIPE, universal_newlines=True, timeout=10)
            if r.returncode != 0:
                log_activity(f"ssh useradd failed for {username}: {(r.stderr or '').strip()[:120]}")
                return
        r = subprocess.run(["chpasswd"], input=f"{username}:{password}\n",
                           stdout=subprocess.PIPE, stderr=subprocess.PIPE, universal_newlines=True, timeout=10)
        if r.returncode != 0:
            log_activity(f"ssh chpasswd failed for {username}: {(r.stderr or '').strip()[:120]}")
    except Exception as e:
        try: log_activity(f"ssh system user create error for {username}: {e}")
        except Exception: pass

def system_user_delete(username):
    """Remove the real system user (root only). Best-effort: never raises."""
    if os.geteuid() != 0: return
    try:
        r = subprocess.run(["userdel", username], stdout=subprocess.PIPE, stderr=subprocess.PIPE, universal_newlines=True, timeout=10)
        if r.returncode != 0:
            log_activity(f"ssh userdel failed for {username}: {(r.stderr or '').strip()[:120]}")
    except Exception as e:
        try: log_activity(f"ssh system user delete error for {username}: {e}")
        except Exception: pass

def system_user_lock(username, locked):
    """Lock: usermod -L + kill the user's sessions. Unlock: usermod -U."""
    if os.geteuid() != 0: return
    try:
        subprocess.run(["usermod", "-L" if locked else "-U", username], stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=10)
        if locked:
            uid = subprocess.getoutput(f"id -u {username} 2>/dev/null").strip()
            if uid.isdigit(): subprocess.run(["pkill", "-u", uid], stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=10)
    except Exception as e:
        try: log_activity(f"ssh lock error for {username}: {e}")
        except Exception: pass

def system_user_set_expiry(username, expiry_ymd):
    if os.geteuid() != 0: return
    try: subprocess.run(["usermod", "-e", expiry_ymd, username], stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=10)
    except Exception: pass

class Handler(BaseHTTPRequestHandler):
    # Streamed popup responses must leave the process the moment a line is
    # written: wfile stays unbuffered (this is also the library default, it
    # is pinned here so no Python version can change it under us) and the
    # socket runs with TCP_NODELAY so a small line is never held back by
    # Nagle coalescing on its way to the phone.
    wbufsize = 0
    def setup(self):
        super().setup()
        try: self.connection.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        except Exception: pass
    def log_message(self, *a): pass
    def session_user(self):
        c = SimpleCookie(self.headers.get("Cookie", ""))
        if "session" in c and c["session"].value in SESSIONS:
            v = SESSIONS[c["session"].value]
            return v.get("user") if isinstance(v, dict) else v
        return None
    def session_role(self):
        c = SimpleCookie(self.headers.get("Cookie", ""))
        if "session" in c and c["session"].value in SESSIONS:
            v = SESSIONS[c["session"].value]
            return v.get("role", "super") if isinstance(v, dict) else "super"
        return ""
    def client_ip(self):
        return self.headers.get("X-Real-IP") or self.headers.get("X-Forwarded-For", "").split(",")[0].strip() or self.client_address[0]
    def set_ctx(self):
        CTX.user = self.session_user() or ""
        CTX.ip = self.client_ip()
    def wants_popup(self):
        """True when the panel's popup JS submitted this POST (fetch with the
        X-Stream header) - the answer must then be a plain-text live stream,
        not a full page or a redirect."""
        return self.headers.get("X-Stream") == "popup"
    def stream_open(self):
        """Open a popup stream: chunked-by-flush plain text, one line per
        write. Returns (emit, result): emit(text) appends a line; result(html)
        sends the :::RESULT::: marker followed by a raw HTML fragment that
        the popup renders below the output (the finished result card)."""
        self.send_response(200)
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("X-Accel-Buffering", "no")
        self.end_headers()
        w = self.wfile
        def emit(text=""):
            try:
                w.write((str(text) + "\n").encode()); w.flush()
            except Exception: pass
        def result(fragment):
            try:
                w.write("\n:::RESULT:::\n".encode()); w.write(str(fragment).encode()); w.flush()
            except Exception: pass
        return emit, result
    def stream_cmd(self, title, argv, back="/services", back_label="Back"):
        """Run a command and stream its output to the browser line by line."""
        log_activity("run: " + " ".join(str(a) for a in argv)[:120])
        if self.wants_popup():
            emit, _result = self.stream_open()
            emit("[INFO] " + title)
            rc = _pipe_lines(argv, emit)
            emit("[OK] Finished (exit %d)." % rc if rc == 0 else "[ERROR] Finished with exit code %d." % rc)
            return
        self.send_response(200); self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Cache-Control", "no-cache"); self.send_header("X-Accel-Buffering", "no"); self.end_headers()
        head = ("<!DOCTYPE html><html><head><meta charset='utf-8'><title>" + html.escape(title) + "</title></head>"
                "<body style='background:#0b0f14;color:#dbe4ee;font-family:monospace;padding:18px'>"
                "<h3 style='font-family:system-ui'>" + html.escape(title) + "</h3><pre style='white-space:pre-wrap'>")
        try: self.wfile.write(head.encode()); self.wfile.flush()
        except Exception: return
        rc = 1
        try:
            proc = subprocess.Popen(argv, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, universal_newlines=True, bufsize=1)
            for line in proc.stdout:
                try:
                    self.wfile.write(html.escape(line).encode()); self.wfile.flush()
                except Exception:
                    proc.kill(); break
            rc = proc.wait()
        except Exception as e:
            try: self.wfile.write(("failed to start: %s" % e).encode())
            except Exception: pass
        tail = ("</pre><p style='font-family:system-ui'>Finished (exit %s). <a style='color:#38bdf8' href='%s'>%s</a></p></body></html>" % (rc, back, back_label))
        try: self.wfile.write(tail.encode()); self.wfile.flush()
        except Exception: pass
    def _ssh_op_stream(self, p, data):
        """Popup-streamed SSH account operation (renew/limits/reset/lock):
        the same mutation, side effects and audit entries as the classic
        redirect flow, narrated line by line as each step completes."""
        uname = data.get("username", "")
        users = load_json("ssh_users.json", [])
        rec = next((u for u in users if u.get("username") == uname), None)
        emit, _result = self.stream_open()
        if rec is None:
            emit(f"[ERROR] SSH account '{uname}' was not found - nothing was changed.")
            return
        if p == "/ssh/renew":
            days = to_int(data.get("days", 30), 30)
            emit(f"[INFO] Renewing SSH account '{uname}' (+{days} days)...")
            acct_renew(rec, data.get("days", 30))
            system_user_set_expiry(uname, rec.get("expires", ""))
            log_activity(f"ssh renew {uname} +{data.get('days', 30)}d -> {rec.get('expires')}")
            save_json("ssh_users.json", users)
            emit(f"[OK] '{uname}' now expires on {rec.get('expires')}")
        elif p == "/ssh/limits":
            emit(f"[INFO] Updating the limits of SSH account '{uname}'...")
            was_locked = bool(rec.get("disabled"))
            acct_edit_limits(rec, data.get("gb_limit", 0), data.get("ip_limit", 0))
            if was_locked and not rec.get("disabled"): system_user_lock(uname, False)
            log_activity(f"ssh limits {uname} gb={rec.get('gb_limit')} ip={rec.get('ip_limit')}")
            save_json("ssh_users.json", users)
            emit(f"[OK] New limits for '{uname}': data {fmt_data(rec.get('gb_limit', 0))}, IP limit {fmt_logins(rec.get('ip_limit', 0))}")
        elif p == "/ssh/reset":
            emit(f"[INFO] Resetting the usage counter of SSH account '{uname}'...")
            was_locked = bool(rec.get("disabled"))
            acct_reset_usage(rec)
            if was_locked and not rec.get("disabled"): system_user_lock(uname, False)
            log_activity(f"ssh reset usage {uname}")
            save_json("ssh_users.json", users)
            emit(f"[OK] Usage of '{uname}' is back to 0 GB")
        elif p == "/ssh/lock":
            locked = data.get("locked") == "1"
            emit(f"[INFO] {'Locking' if locked else 'Unlocking'} SSH account '{uname}'...")
            acct_set_lock(rec, locked)
            system_user_lock(uname, locked)
            log_activity(f"ssh {'lock' if locked else 'unlock'} {uname}")
            save_json("ssh_users.json", users)
            emit(f"[OK] '{uname}' is {'locked' if locked else 'unlocked'}")
        zivpn_sync()
        emit("[OK] Done - close this popup to refresh the list.")
    def _xray_op_stream(self, p, data):
        """Popup-streamed Xray account operation (renew/limits/reset/lock):
        same mutation + audit + Xray sync as the classic flow; the sync
        output itself is forwarded line by line."""
        uid = data.get("id", "")
        users = load_json("xray_users.json", [])
        rec = next((u for u in users if u.get("id") == uid), None)
        emit, _result = self.stream_open()
        if rec is None:
            emit("[ERROR] That Xray account was not found - nothing was changed.")
            return
        remark = rec.get("remark", "")
        if p == "/xray/renew":
            days = to_int(data.get("days", 30), 30)
            emit(f"[INFO] Renewing Xray account '{remark}' (+{days} days)...")
            acct_renew(rec, data.get("days", 30))
            log_activity(f"xray renew {remark} +{data.get('days', 30)}d -> {rec.get('expires')}")
            emit(f"[OK] '{remark}' now expires on {rec.get('expires')}")
        elif p == "/xray/limits":
            emit(f"[INFO] Updating the limits of Xray account '{remark}'...")
            acct_edit_limits(rec, data.get("gb_limit", 0), data.get("ip_limit", 0))
            log_activity(f"xray limits {remark} gb={rec.get('gb_limit')} ip={rec.get('ip_limit')}")
            emit(f"[OK] New limits for '{remark}': data {fmt_data(rec.get('gb_limit', 0))}, IP limit {fmt_logins(rec.get('ip_limit', 0))}")
        elif p == "/xray/reset":
            emit(f"[INFO] Resetting the usage counter of Xray account '{remark}'...")
            acct_reset_usage(rec)
            log_activity(f"xray reset usage {remark}")
            emit(f"[OK] Usage of '{remark}' is back to 0 GB")
        elif p == "/xray/lock":
            locked = data.get("locked") == "1"
            emit(f"[INFO] {'Locking' if locked else 'Unlocking'} Xray account '{remark}'...")
            acct_set_lock(rec, locked)
            log_activity(f"xray {'lock' if locked else 'unlock'} {remark}")
            emit(f"[OK] '{remark}' is {'locked' if locked else 'unlocked'}")
        save_json("xray_users.json", users)
        emit("[INFO] Syncing the change to Xray...")
        sync_xray(emit=emit)
        emit("[OK] Done - close this popup to refresh the list.")
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
        self.set_ctx()
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
            <div class='card'><h3>Python Proxy</h3><p>{chip(next((x['status'] for x in svcs if x['name'] == 'ws-proxy'), 'unknown'))}<br><span class='mut'>WebSocket port <b>{to_int(cfg.get('ws_proxy_port', 8080), 8080)}</b> · PROXY port <b>{to_int(cfg.get('ws_proxy_port', 8080), 8080) + 1}</b></span></p><p><a class='btn ghost small' href='/services'>Manage Python Proxy</a></p></div>
            <div class='card'><h3>Server Info</h3><div class='kv'><div><div class='k'>Domain</div><div class='v'>{html.escape(domain) or '-'}</div></div><div><div class='k'>Panel Port</div><div class='v'>{PORT}</div></div><div><div class='k'>Hostname</div><div class='v'>{html.escape(os.uname().nodename)}</div></div><div><div class='k'>OS</div><div class='v'>{html.escape(os.uname().sysname)} {html.escape(os.uname().release)}</div></div></div></div>
            </div>
            <div class='card'><h3>Services Status</h3>{svc_rows}</div>"""
            return self.send_html(page("Dashboard", body, active="/", user=user))
        if p == "/ssh":
            users = load_json("ssh_users.json", [])
            def _ssh_row(u):
                st = display_status(u)
                un = html.escape(u['username'])
                dn = dead_note(u)
                note = f"<br><small class='bad'>{html.escape(dn)}</small>" if dn else ""
                locked = bool(u.get("disabled") and u.get("disabled_reason") == "Locked")
                lock_btn = (f"<form method='post' action='/ssh/lock' style='display:inline' data-stream='Unlock SSH Account' data-reload><input type='hidden' name='username' value='{un}'><input type='hidden' name='locked' value='0'><button class='small ghost'>Unlock</button></form>"
                            if locked else
                            f"<form method='post' action='/ssh/lock' style='display:inline' data-stream='Lock SSH Account' data-reload><input type='hidden' name='username' value='{un}'><input type='hidden' name='locked' value='1'><button class='small ghost'>Lock</button></form>")
                acts = (f"<form method='post' action='/ssh/renew' style='display:inline' data-stream='Renew SSH Account' data-reload><input type='hidden' name='username' value='{un}'><input name='days' type='number' value='30' min='1' style='width:64px;display:inline-block;margin:0 4px 4px 0'><button class='small ghost'>Renew</button></form> "
                        f"<form method='post' action='/ssh/limits' style='display:inline' data-stream='Set SSH Limits' data-reload><input type='hidden' name='username' value='{un}'><input name='gb_limit' type='number' value='{to_int(u.get('gb_limit',0))}' min='0' title='GB limit (0 = unlimited)' style='width:56px;display:inline-block;margin:0 2px 4px 0'><input name='ip_limit' type='number' value='{to_int(u.get('ip_limit',0))}' min='0' title='IP limit (0 = unlimited)' style='width:56px;display:inline-block;margin:0 4px 4px 0'><button class='small ghost'>Set Limits</button></form> "
                        f"<form method='post' action='/ssh/reset' style='display:inline' data-stream='Reset SSH Usage' data-reload><input type='hidden' name='username' value='{un}'><button class='small ghost'>Reset Usage</button></form> "
                        f"{lock_btn} "
                        f"<form method='post' action='/ssh/delete' style='display:inline' data-stream='Delete SSH Account' data-reload><input type='hidden' name='username' value='{un}'><button class='danger'>Delete</button></form>")
                return (f"<tr><td><b>{un}</b></td><td><code>{html.escape(u.get('password',''))}</code></td>"
                        f"<td>{fmt_expiry(u.get('expiry_days',30))}<br><small class='mut'>Expires: {expires_on(u.get('created',''), u.get('expiry_days',30))}</small></td>"
                        f"<td>{fmt_data(u.get('gb_limit',0))}<br><small class='mut'>Used: {u.get('used_gb',0)} GB</small></td><td>{fmt_logins(u.get('ip_limit',0))}</td>"
                        f"<td>{html.escape(u.get('created',''))}</td><td>{status_pill(st)}{note}</td>"
                        f"<td style='min-width:300px'>{acts}</td></tr>")
            rows = "".join(_ssh_row(u) for u in users) or "<tr><td colspan='8' class='mut'>No SSH accounts yet.</td></tr>"
            payload = f"GET /sshws HTTP/1.1[crlf]Host: {domain}[crlf]Upgrade: websocket[crlf][crlf]"
            body = f"""<div class='card'><h3>Create SSH Account (tunnel-only)</h3><form method='post' action='/ssh/create' data-stream='Create SSH Account' data-reload><label>Username</label><input name='username' placeholder='Username' required><label>Password</label><input name='password' placeholder='Password (blank = auto-generated)'><label>Days</label><input name='expiry_days' type='number' min='0' value='30'><label>GB limit (0 = unlimited)</label><input name='gb_limit' type='number' min='0' value='0'><label>IP limit (0 = unlimited)</label><input name='ip_limit' type='number' min='0' value='0'>{LIMIT_HINT}<button>Create Account</button></form>{RULE_NOTE}</div>
            <div class='card'><h3>SSH Accounts</h3><div class='tblwrap'><table><tr><th>User</th><th>Password</th><th>Expiry</th><th>Data</th><th>Logins</th><th>Created</th><th>Status</th><th></th></tr>{rows}</table></div></div>
            <div class='card'><h3>Payload</h3><div class='codebox'><button type='button' class='copybtn' onclick="copyText('ssh-payload', this)">Copy</button><pre id='ssh-payload'>{html.escape(payload)}</pre></div><p class='mut'>Host: {html.escape(domain)} · SSH WS port 80 · WS-SSL 443 · Dropbear: 109, 143</p></div>"""
            return self.send_html(page("SSH Accounts", body, active="/ssh", user=user))
        if p == "/xray":
            users = load_json("xray_users.json", [])
            def _xr_card(u):
                _links = u.get('links') or {"Link TLS": u.get('link', '')}
                st = display_status(u)
                dn = dead_note(u)
                note = f" <small class='bad'>{html.escape(dn)}</small>" if dn else ""
                locked = bool(u.get("disabled") and u.get("disabled_reason") == "Locked")
                lock_btn = (f"<form method='post' action='/xray/lock' style='display:inline' data-stream='Unlock Xray Account' data-reload><input type='hidden' name='id' value='{u['id']}'><input type='hidden' name='locked' value='0'><button class='small ghost'>Unlock</button></form>"
                            if locked else
                            f"<form method='post' action='/xray/lock' style='display:inline' data-stream='Lock Xray Account' data-reload><input type='hidden' name='id' value='{u['id']}'><input type='hidden' name='locked' value='1'><button class='small ghost'>Lock</button></form>")
                acts = (f"<div style='margin-top:12px'><form method='post' action='/xray/renew' style='display:inline' data-stream='Renew Xray Account' data-reload><input type='hidden' name='id' value='{u['id']}'><input name='days' type='number' value='30' min='1' style='width:64px;display:inline-block;margin:0 4px 4px 0'><button class='small ghost'>Renew</button></form> "
                        f"<form method='post' action='/xray/limits' style='display:inline' data-stream='Set Xray Limits' data-reload><input type='hidden' name='id' value='{u['id']}'><input name='gb_limit' type='number' value='{to_int(u.get('gb_limit',0))}' min='0' title='GB limit (0 = unlimited)' style='width:56px;display:inline-block;margin:0 2px 4px 0'><input name='ip_limit' type='number' value='{to_int(u.get('ip_limit',0))}' min='0' title='IP limit (0 = unlimited)' style='width:56px;display:inline-block;margin:0 4px 4px 0'><button class='small ghost'>Set Limits</button></form> "
                        f"<form method='post' action='/xray/reset' style='display:inline' data-stream='Reset Xray Usage' data-reload><input type='hidden' name='id' value='{u['id']}'><button class='small ghost'>Reset Usage</button></form> "
                        f"{lock_btn} "
                        f"<form method='post' action='/xray/delete' style='display:inline' data-stream='Delete Xray Account' data-reload><input type='hidden' name='id' value='{u['id']}'><button class='danger'>Delete</button></form></div>")
                return (f"<div class='card'><h3>{html.escape(u['protocol'].upper())} · {html.escape(u['remark'])} {status_pill(st)}{note}</h3>"
                        f"<div class='kv'><div><div class='k'>UUID</div><div class='v'><code>{html.escape(u['id'])}</code></div></div>"
                        f"<div><div class='k'>Created</div><div class='v'>{html.escape(u.get('created',''))}</div></div>"
                        f"<div><div class='k'>Expiry</div><div class='v'>{expires_on(u.get('created',''), u.get('expiry_days',30))} ({fmt_expiry(u.get('expiry_days',30))})</div></div>"
                        f"<div><div class='k'>Data</div><div class='v'>{fmt_data(u.get('gb_limit',0))}</div></div>"
                        f"<div><div class='k'>Used</div><div class='v'>{u.get('used_gb',0)} GB</div></div>"
                        f"<div><div class='k'>IP Limit</div><div class='v'>{fmt_logins(u.get('ip_limit',0))}</div></div></div>"
                        f"<div style='margin-top:12px'>{links_html(_links, 'xr-' + u['id'][:8])}</div>"
                        f"{acts}</div>")
            cards = "".join(_xr_card(u) for u in users) or "<div class='card'><p class='mut'>No Xray accounts yet.</p></div>"
            body = f"""<div class='card'><h3>Create Xray Account</h3><form method='post' action='/xray/create' data-stream='Create Xray Account' data-reload><label>Protocol</label><select name='protocol'><option>VMess</option><option>VLESS</option><option>Trojan</option></select><label>Remark / Name</label><input name='remark' placeholder='Remark / Name' required><label>Days</label><input name='expiry_days' type='number' min='0' value='30'><label>GB limit (0 = unlimited)</label><input name='gb_limit' type='number' min='0' value='0'><label>IP limit (0 = unlimited)</label><input name='ip_limit' type='number' min='0' value='0'>{LIMIT_HINT}<button>Create Account (UUID auto)</button></form>{RULE_NOTE}</div>
            <h3 style='margin:18px 0 10px'>Xray Accounts</h3>{cards}"""
            return self.send_html(page("Xray Accounts", body, active="/xray", user=user))
        if p == "/openvpn":
            users = openvpn_users()
            def _ov_card(u):
                nm = html.escape(str(u.get("name", "")))
                st = "Revoked" if u.get("revoked") else display_status(u)
                acts = ""
                if not u.get("revoked"):
                    acts = (f"<div style='margin-top:12px'><a class='btn ghost small' href='/openvpn/download?name={nm}'>Download .ovpn</a> "
                            f"<form method='post' action='/openvpn/renew' style='display:inline' data-stream='Renew OpenVPN Account' data-reload><input type='hidden' name='name' value='{nm}'><input name='days' type='number' value='30' min='1' style='width:64px;display:inline-block;margin:0 4px 4px 0'><button class='small ghost'>Renew</button></form> "
                            f"<form method='post' action='/openvpn/revoke' style='display:inline' data-stream='Revoke OpenVPN Account' data-reload><input type='hidden' name='name' value='{nm}'><button class='danger'>Revoke</button></form></div>")
                return (f"<div class='card'><h3>🛡 {nm} {status_pill(st)}</h3>"
                        f"<div class='kv'><div><div class='k'>Server</div><div class='v'>{html.escape(domain)} : 1194 (UDP)</div></div>"
                        f"<div><div class='k'>Created</div><div class='v'>{html.escape(str(u.get('created','')))}</div></div>"
                        f"<div><div class='k'>Expiry</div><div class='v'>{html.escape(str(u.get('expires','')))} ({fmt_expiry(u.get('expiry_days',30))})</div></div></div>{acts}</div>")
            cards = "".join(_ov_card(u) for u in users) or "<div class='card'><p class='mut'>No OpenVPN accounts yet.</p></div>"
            body = f"""<div class='card'><h3>Create OpenVPN Account</h3><form method='post' action='/openvpn/create' data-stream='Create OpenVPN Account'><label>Username</label><input name='name' placeholder='Username' required><label>Days</label><input name='expiry_days' type='number' min='1' value='30'><button>Create Account (certificate auto)</button></form><p class='mut'>Each account gets its own certificate and a ready-to-import <b>.ovpn</b> profile. Days is a literal count - it is the certificate validity. OpenVPN accounts do not take GB/IP limits in this design (those limits are enforced on SSH and Xray accounts).</p></div>
            <h3 style='margin:18px 0 10px'>OpenVPN Accounts</h3>{cards}"""
            return self.send_html(page("OpenVPN Accounts", body, active="/openvpn", user=user))
        if p == "/openvpn/download":
            qs = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
            nm = (qs.get("name") or [""])[0]
            import re as _re
            prof = ""
            _rec = next((u for u in openvpn_users() if u.get("name") == nm), None)
            if _rec is not None and _rec.get("revoked"):
                prof = ""  # a revoked account's profile is never served again
            elif _re.match(r"^[A-Za-z0-9_-]{1,32}$", nm or ""):
                try:
                    with open(os.path.join(SUITE_CONFIG_DIR, "openvpn", "profiles", nm + ".ovpn")) as f:
                        prof = f.read()
                except Exception:
                    prof = ""
            if not prof:
                return self.send_html(page("OpenVPN", "<div class='card'><p>No profile found for that account (it may be revoked, or the PKI is not built yet).</p><p><a class='btn ghost' href='/openvpn'>Back to OpenVPN</a></p></div>", active="/openvpn", user=user))
            b = prof.encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/x-openvpn-profile")
            self.send_header("Content-Disposition", f"attachment; filename={nm}.ovpn")
            self.send_header("Content-Length", str(len(b)))
            self.end_headers(); self.wfile.write(b)
            return
        if p == "/services":
            role = self.session_role()
            def _svc_row(x):
                n = html.escape(x["name"])
                ctl = ""
                if role_at_least(role, "admin"):
                    def _sb(op, label):
                        return (f"<form method='post' action='/action/service' data-stream='Service Action' data-reload><input type='hidden' name='name' value='{n}'>"
                                f"<button class='small ghost' name='op' value='{op}'>{label}</button></form>")
                    ctl = (f"<div class='svcbtns'>{_sb('start', 'Start')}{_sb('stop', 'Stop')}{_sb('restart', 'Restart')}"
                           f"<a class='btn ghost small' href='/services/logs?name={n}'>Logs</a></div>")
                return (f"<div class='svcblk'><div class='svchead'><span class='svcname'>{n}</span>{chip(x['status'])}</div>{ctl}</div>")
            _svcs = service_list()
            rows = "".join(_svc_row(x) for x in _svcs)
            # --- Python Proxy card: the ws-proxy service under its product name,
            # with streamed controls (Start/Stop/Restart/Force) and a port setting.
            _ws_st = next((x["status"] for x in _svcs if x["name"] == "ws-proxy"), "unknown")
            _ws_port = to_int(load_config().get("ws_proxy_port", 8080), 8080)
            if not (1 <= _ws_port <= 65534): _ws_port = 8080
            _py_btns = ""
            if role_at_least(role, "admin"):
                def _py_form(job, label, cls="small ghost", confirm="", fcls=""):
                    oc = f" onsubmit=\"return confirm('{confirm}')\"" if confirm else ""
                    fc = f" class='{fcls}'" if fcls else ""
                    return (f"<form method='post' action='/action/run' data-stream='Python Proxy' data-reload{fc}{oc}>"
                            f"<input type='hidden' name='job' value='{job}'><button class='{cls}'>{label}</button></form>")
                _py_btns = ("<div class='svcbtns wide'>"
                            + _py_form("pyproxy-start", "Start") + _py_form("pyproxy-stop", "Stop") + _py_form("pyproxy-restart", "Restart")
                            + _py_form("pyproxy-force-start", "Force Start", "danger", "Force Start kills anything still holding the proxy ports and starts the Python Proxy. Continue?", "force")
                            + _py_form("pyproxy-force-stop", "Force Stop", "danger", "Force Stop kills anything still holding the proxy ports and leaves the Python Proxy OFF. Continue?", "force")
                            + "</div>"
                            f"<form method='post' action='/action/run' style='margin-top:10px' data-stream='Python Proxy - Set Port' data-reload><input type='hidden' name='job' value='pyproxy-setport'>"
                            f"<label>Proxy port (the PROXY-protocol listener uses port + 1)</label>"
                            f"<input name='port' type='number' min='1' max='65535' value='{_ws_port}' style='width:130px;display:inline-block;margin-right:6px'>"
                            f"<button class='small'>Set Port</button></form>")
            pyproxy = (f"<div class='card'><h3>Python Proxy</h3>"
                       f"<div class='svcblk'><div class='svchead'><span class='svcname'>ws-proxy</span>{chip(_ws_st)}</div>"
                       f"<p class='mut' style='margin:8px 0 0'>ws-proxy service · WebSocket port <b>{_ws_port}</b> · PROXY-protocol port <b>{_ws_port + 1}</b></p>"
                       f"<p class='mut' style='margin-top:8px'>The Python proxy accepts SSH-over-WebSocket connections (path /sshws) and relays them to Dropbear. "
                       f"Start / Stop / Restart / Force run with live output. Force is the heavy hand for when a normal start or stop does not work.</p>{_py_btns}</div></div>")
            ports = subprocess.getoutput("ss -tuln 2>/dev/null | head -n 40") if shutil.which("ss") else "ss is not available (local dev environment)"
            stack = ""
            if role_at_least(role, "admin"):
                stack = ("<div class='card'><h3>Stack Actions (live output)</h3>"
                         "<form method='post' action='/action/run' style='display:inline' data-stream='Install All Components' data-reload><input type='hidden' name='job' value='install'><button>Install All</button></form> "
                         "<form method='post' action='/action/run' style='display:inline' data-stream='Fix Stack' data-reload><input type='hidden' name='job' value='fix'><button class='ghost'>Fix Stack</button></form> "
                         "<form method='post' action='/action/run' style='display:inline' data-stream='Repair Xray Sync' data-reload><input type='hidden' name='job' value='xray'><button class='ghost'>Repair Xray Sync</button></form> "
                         "<form method='post' action='/action/run' style='display:inline' data-stream='Issue / Renew SSL' data-reload><input type='hidden' name='job' value='ssl'><button class='ghost'>Issue / Renew SSL</button></form> "
                         "<form method='post' action='/action/restart-all' style='display:inline' data-stream='Restart All Services'><button class='ghost'>Restart All Services</button></form></div>")
            return self.send_html(page("Running Services", f"<div class='card'><h3>Running Services</h3>{rows}</div>{pyproxy}{stack}<div class='card'><h3>Listening Ports</h3><pre class='raw'>{html.escape(ports)}</pre></div>", active="/services", user=user))
        if p == "/services/logs":
            q = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
            name = (q.get("name") or [""])[0]
            allowed = {x["name"] for x in service_list()}
            if name not in allowed:
                return self.send_html(page("Logs", "<div class='card'><h3>Unknown service</h3><p><a class='btn ghost' href='/services'>Back</a></p></div>", active="/services", user=user), code=404)
            out = subprocess.getoutput(f"journalctl -u {name} -n 60 --no-pager 2>&1 | tail -n 60")
            return self.send_html(page("Service Logs", f"<div class='card'><h3>Logs: {html.escape(name)}</h3><pre class='raw'>{html.escape(out)}</pre><p><a class='btn ghost' href='/services'>Back to Services</a></p></div>", active="/services", user=user))
        if p == "/stats":
            s = sys_stats()
            return self.send_html(page("VPS Stats", f"""<div class='grid'>
            <div class='card stat'><div class='lbl'>CPU Usage</div><div class='big'>{s.get('cpu')}%</div><div class='bar'><i style='width:{s.get('cpu')}%'></i></div><div class='sub'>Load: {', '.join(s.get('load',[]))}</div></div>
            <div class='card stat'><div class='lbl'>RAM Usage</div><div class='big'>{s.get('ram')}%</div><div class='bar'><i style='width:{s.get('ram')}%'></i></div><div class='sub'>{s.get('ram_total_gb')} GB total</div></div>
            <div class='card stat'><div class='lbl'>Disk Usage</div><div class='big'>{s.get('disk')}%</div><div class='bar'><i style='width:{s.get('disk')}%'></i></div><div class='sub'>{s.get('disk_total_gb')} GB total</div></div>
            <div class='card stat'><div class='lbl'>Network</div><div class='big' style='font-size:20px'>RX {s.get('net_rx_gb')} GB</div><div class='sub'>TX {s.get('net_tx_gb')} GB · Uptime: {html.escape(s.get('uptime',''))}</div></div>
            </div><div class='card'><h3>Speedtest</h3><form method='post' action='/action/speedtest' data-stream='Speedtest'><button>Run Speedtest</button></form></div>
            <div class='card'><h3>Traffic History (vnstat)</h3>{vnstat_section(self.session_role())}</div>""", active="/stats", user=user))
        if p == "/telegram":
            cfg = load_config()
            return self.send_html(page("Telegram Bot", f"<div class='card'><h3>Telegram Bot Setup</h3><form method='post' action='/telegram'><label>Bot Token</label><input name='telegram_token' value='{html.escape(cfg.get('telegram_token',''))}' placeholder='Token from @BotFather'><label>Admin Chat ID</label><input name='telegram_admin_id' value='{html.escape(cfg.get('telegram_admin_id',''))}'><button>Save</button></form><p class='mut'>To run the bot on the VPS: <code>python3 scripts/telegram-bot.py</code> - the token is stored here, never hardcoded in code.</p></div>", active="/telegram", user=user))
        if p == "/settings":
            cfg = load_config()
            designer = banner_designer_card(cfg)
            _sd = slowdns_host(cfg); _dom = cfg.get('domain','')
            _sd_sub = _sd[: -len(_dom) - 1] if _dom and _sd.endswith("." + _dom) else _sd
            _pub = slowdns_pubkey()
            _pub_html = f"<label>SlowDNS Public Key</label><div class='codebox'><pre>{html.escape(_pub)}</pre></div>" if _pub else "<p class='mut'>SlowDNS public key: not generated yet - it appears here after install / Fix Stack.</p>"
            return self.send_html(page("Settings", f"""<div class='card'><h3>Settings</h3><form method='post' action='/settings' data-stream='Settings' data-reload>
            <label>Domain</label><input name='domain' value='{html.escape(cfg.get('domain',''))}'>
            <p class='mut'>The SSH banner (the server message clients see) is styled in the SSH Banner Designer below.</p>
            <label>SlowDNS Domain (NS host)</label><input name='slowdns_domain' value='{html.escape(_sd)}' placeholder='dns.yourdomain.com'>
            <p class='mut'>SlowDNS needs ONE record at your DNS provider: an <b>NS record</b> for the host <b>{html.escape(_sd_sub)}</b> (full host: {html.escape(_sd)}) pointing to <b>{html.escape(_dom)}</b>. Clients then use this NS host with their SSH username + password. Changing it restarts the SlowDNS service.</p>
            {_pub_html}
            <label>UDP Domain (ZiVPN UDP)</label><input name='udp_domain' value='{html.escape(udp_host(cfg))}' placeholder='yourdomain.com'>
            <p class='mut'>The host shown for the ZiVPN UDP service on account cards. Clients connect with their SSH password (UDP 5667, app ports 6000-19999).</p>
            <label>Ping QoS Interface</label><input name='qos_interface' value='{html.escape(cfg.get('qos_interface','eth0'))}'>
            <label>QoS Rate</label><input name='qos_rate' value='{html.escape(cfg.get('qos_rate','950mbit'))}'>
            <label>Per-client cap (Mbps, 0 = no cap)</label><input name='per_client_cap' value='{html.escape(str(cfg.get('per_client_cap','0')))}'>
            <button>Save Settings</button></form></div>
            {designer}
            <div class='card'><h3>Maintenance</h3><form method='post' action='/action/fix' style='display:inline' data-stream='Fix Stack' data-reload><button>Fix Stack</button></form> <form method='post' action='/action/qos' style='display:inline' data-stream='Apply QoS' data-reload><button class='ghost'>Apply QoS</button></form> <form method='post' action='/action/qos-off' style='display:inline' data-stream='Remove QoS' data-reload><button class='ghost'>Remove QoS</button></form></div>
            <div class='card'><h3>QoS Status</h3><pre class='raw'>{html.escape(subprocess.getoutput(('bash ' + suite_script('qos.sh') + ' status') if suite_script('qos.sh') else 'echo qos.sh not found'))}</pre></div>
            <div class='card'><h3>SSL Certificate</h3><pre class='raw'>{html.escape(subprocess.getoutput(('bash ' + suite_script('ssl.sh') + ' status') if suite_script('ssl.sh') else 'echo ssl helper not found'))}</pre><form method='post' action='/action/run' style='display:inline' data-stream='Issue / Renew SSL' data-reload><input type='hidden' name='job' value='ssl'><button class='ghost'>Issue / Renew SSL</button></form></div>
            <div class='card'><h3>Change Panel Password</h3><form method='post' action='/password'><label>New password</label><input name='new_password' type='password' required placeholder='New password'><button>Change Password</button></form></div>""", active="/settings", user=user))
        if p == "/admins":
            creds = load_creds(); role = self.session_role()
            extras = load_admins()
            def _role_opts(cur):
                return "".join(f"<option value='{r}'{' selected' if r == cur else ''}>{r.title()}</option>" for r in ROLES)
            rows = [f"<tr><td><b>{html.escape(creds.get('username','admin'))}</b></td><td>Super Admin (main)</td><td>Active</td><td class='mut'>Managed in Settings / terminal - cannot be deleted or disabled</td></tr>"]
            for a in extras:
                un = html.escape(a.get("username", ""))
                st = "Disabled" if a.get("disabled") else "Active"
                acts = ""
                if role == "super":
                    acts = (f"<form method='post' action='/admins/pass' style='display:inline'><input type='hidden' name='username' value='{un}'><input name='password' type='password' placeholder='New password' style='width:130px;display:inline-block;margin:0 4px 4px 0'><button class='small ghost'>Set Password</button></form> "
                            f"<form method='post' action='/admins/role' style='display:inline'><input type='hidden' name='username' value='{un}'><select name='role' style='width:110px;display:inline-block;margin:0 4px 4px 0'>{_role_opts(a.get('role','viewer'))}</select><button class='small ghost'>Set Role</button></form> "
                            f"<form method='post' action='/admins/toggle' style='display:inline'><input type='hidden' name='username' value='{un}'><button class='small ghost'>{'Enable' if a.get('disabled') else 'Disable'}</button></form> "
                            f"<form method='post' action='/admins/delete' style='display:inline'><input type='hidden' name='username' value='{un}'><button class='danger'>Delete</button></form>")
                rows.append(f"<tr><td><b>{un}</b></td><td>{html.escape(a.get('role','viewer').title())}</td><td>{st}</td><td>{acts}</td></tr>")
            add_form = ""
            if role == "super":
                add_form = (f"<div class='card'><h3>Add Panel Admin</h3><form method='post' action='/admins/add'>"
                            f"<label>Username</label><input name='username' required><label>Password</label><input name='password' type='password' required>"
                            f"<label>Role</label><select name='role'>{_role_opts('admin')}</select>"
                            f"<p class='mut'>Viewer = read-only. Admin = full account management. Super = also manages panel admins. Passwords are stored as PBKDF2 hashes.</p><button>Add Admin</button></form></div>")
            body = (f"<div class='card'><h3>Panel Admins</h3><div class='tblwrap'><table><tr><th>User</th><th>Role</th><th>Status</th><th></th></tr>{''.join(rows)}</table></div>"
                    f"<p><a class='btn ghost small' href='/activity'>View Activity Log</a></p></div>{add_form}")
            return self.send_html(page("Panel Admins", body, active="/admins", user=user))
        if p == "/activity":
            logs = load_json("activity.json", [])
            rows = "".join(f"<tr><td>{html.escape(l.get('time',''))}</td><td>{html.escape(l.get('admin','') or '-')}</td><td>{html.escape(l.get('ip','') or '-')}</td><td>{html.escape(l.get('action',''))}</td></tr>" for l in logs[:100]) or "<tr><td colspan='4' class='mut'>No activity yet.</td></tr>"
            return self.send_html(page("Activity", f"<div class='card'><h3>Activity Log (audit)</h3><div class='tblwrap'><table><tr><th>Time</th><th>Admin</th><th>Source IP</th><th>Action</th></tr>{rows}</table></div></div>", active="/activity", user=user))
        return self.send_html(page("404", "<div class='card'><h3>404 - Page not found</h3><p class='mut'>The page you requested does not exist.</p></div>", user=user), code=404)

    def do_POST(self):
        p = urllib.parse.urlparse(self.path).path
        data = self.form()
        self.set_ctx()
        if p == "/login":
            who = verify_login(data.get("username", ""), data.get("password", ""))
            if who:
                tok = secrets.token_hex(24); SESSIONS[tok] = {"user": who[0], "role": who[1]}
                CTX.user = who[0]
                log_activity(f"login success: {who[0]} ({who[1]})")
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
        role = self.session_role()
        if not role_at_least(role, "admin"):
            return self.send_html(page("Read-only", "<div class='card'><h3>Read-only access</h3><p class='mut'>Your account has the Viewer role: you can look at everything, but changes are disabled. Ask a Super Admin to raise your role under Panel Admins.</p></div>", user=user), code=403)
        if p.startswith("/admins") and role != "super":
            return self.send_html(page("Super Admin only", "<div class='card'><h3>Super Admin only</h3><p class='mut'>Panel admin management is restricted to Super Admins.</p></div>", user=user), code=403)
        if p == "/password" and role != "super":
            return self.send_html(page("Super Admin only", "<div class='card'><h3>Super Admin only</h3><p class='mut'>The main panel password can only be changed by a Super Admin. Extra admins get their password set under Panel Admins.</p></div>", user=user), code=403)
        cfg = load_config(); domain = cfg.get("domain") or ""
        if p == "/ssh/create":
            uname = data.get("username", "").strip()
            pwd = data.get("password") or secrets.token_urlsafe(9)
            # Days literal (default 30); GB/IP: 0 = unlimited (default 0)
            exp = to_int(data.get("expiry_days", 30), 30); gb = to_int(data.get("gb_limit", 0), 0); ipl = to_int(data.get("ip_limit", 0), 0)
            created = time.strftime("%Y-%m-%d")
            exp_date = expires_on(created, exp)
            if self.wants_popup():
                emit, result = self.stream_open()
                emit("[INFO] Creating SSH account...")
                if not uname:
                    emit("[ERROR] A username is required - nothing was created.")
                    return
                emit(f"[OK] Details accepted - user '{uname}', {exp} day(s), data {fmt_data(gb)}, IP limit {fmt_logins(ipl)}")
                users = load_json("ssh_users.json", [])
                users.append({"username": uname, "password": pwd, "expiry_days": exp, "gb_limit": gb, "ip_limit": ipl, "used_gb": 0, "created": created, "expires": exp_date})
                save_json("ssh_users.json", users); log_activity(f"ssh create {uname}")
                emit("[OK] Account saved")
                emit("[INFO] Creating the tunnel-only system user...")
                system_user_create(uname, pwd, exp_date)
                if os.geteuid() != 0:
                    emit("[WARN] The panel is not running as root, so the system user step was skipped - the account record is saved.")
                elif subprocess.run(["id", uname], stdout=subprocess.PIPE, stderr=subprocess.PIPE).returncode == 0:
                    emit("[OK] System user is ready")
                else:
                    emit("[WARN] The system user could not be created - the Activity log has the details.")
                emit("[INFO] Looking up the server location...")
                _loc = server_location()
                emit("[INFO] Syncing the ZiVPN UDP password list...")
                zivpn_sync(emit=emit)
                emit(f"[OK] SSH account '{uname}' created - expires {exp_date}")
                result(ssh_result_cards(uname, pwd, domain, created, exp, exp_date, gb, ipl, _loc))
                return
            users = load_json("ssh_users.json", [])
            users.append({"username": uname, "password": pwd, "expiry_days": exp, "gb_limit": gb, "ip_limit": ipl, "used_gb": 0, "created": created, "expires": exp_date})
            save_json("ssh_users.json", users); log_activity(f"ssh create {uname}")
            # When running as root, also create the real tunnel-only system user so the account can log in.
            system_user_create(uname, pwd, exp_date)
            zivpn_sync()
            _loc = server_location()
            body = ssh_result_cards(uname, pwd, domain, created, exp, exp_date, gb, ipl, _loc)
            return self.send_html(page("SSH Account Created", body, active="/ssh", user=user))
        if p == "/ssh/delete":
            uname = data.get("username")
            if self.wants_popup():
                emit, _result = self.stream_open()
                emit(f"[INFO] Deleting SSH account '{uname}'...")
                users = [u for u in load_json("ssh_users.json", []) if u["username"] != uname]; save_json("ssh_users.json", users); log_activity(f"ssh delete {uname}")
                emit("[OK] Account record removed")
                if uname:
                    emit("[INFO] Removing the system user...")
                    system_user_delete(uname)
                zivpn_sync()
                emit(f"[OK] SSH account '{uname}' deleted")
                emit("[OK] Done - close this popup to refresh the list.")
                return
            users = [u for u in load_json("ssh_users.json", []) if u["username"] != uname]; save_json("ssh_users.json", users); log_activity(f"ssh delete {uname}")
            if uname: system_user_delete(uname)
            zivpn_sync()
            return self.redirect("/ssh")
        if p in ("/ssh/renew", "/ssh/limits", "/ssh/reset", "/ssh/lock"):
            if self.wants_popup():
                return self._ssh_op_stream(p, data)
            uname = data.get("username", "")
            users = load_json("ssh_users.json", [])
            rec = next((u for u in users if u.get("username") == uname), None)
            if rec is None: return self.redirect("/ssh")
            if p == "/ssh/renew":
                acct_renew(rec, data.get("days", 30))
                system_user_set_expiry(uname, rec.get("expires", ""))
                log_activity(f"ssh renew {uname} +{data.get('days', 30)}d -> {rec.get('expires')}")
            elif p == "/ssh/limits":
                was_locked = bool(rec.get("disabled"))
                acct_edit_limits(rec, data.get("gb_limit", 0), data.get("ip_limit", 0))
                if was_locked and not rec.get("disabled"): system_user_lock(uname, False)
                log_activity(f"ssh limits {uname} gb={rec.get('gb_limit')} ip={rec.get('ip_limit')}")
            elif p == "/ssh/reset":
                was_locked = bool(rec.get("disabled"))
                acct_reset_usage(rec)
                if was_locked and not rec.get("disabled"): system_user_lock(uname, False)
                log_activity(f"ssh reset usage {uname}")
            elif p == "/ssh/lock":
                locked = data.get("locked") == "1"
                acct_set_lock(rec, locked)
                system_user_lock(uname, locked)
                log_activity(f"ssh {'lock' if locked else 'unlock'} {uname}")
            save_json("ssh_users.json", users)
            zivpn_sync()
            return self.redirect("/ssh")
        if p == "/openvpn/create":
            import re as _re
            nm = data.get("name", "").strip()
            exp = to_int(data.get("expiry_days", 30), 30)
            if exp < 1: exp = 30
            created = time.strftime("%Y-%m-%d")
            exp_date = expires_on(created, exp)
            script = openvpn_script()
            def _profile_text():
                try:
                    with open(os.path.join(SUITE_CONFIG_DIR, "openvpn", "profiles", nm + ".ovpn")) as f:
                        return f.read()
                except Exception:
                    return ""
            def _save_record():
                users = openvpn_users()
                users.append({"name": nm, "created": created, "expiry_days": exp, "expires": exp_date})
                save_json("openvpn_users.json", users)
                log_activity(f"openvpn create {nm}")
            if self.wants_popup():
                emit, result = self.stream_open()
                emit("[INFO] Creating OpenVPN account...")
                if not _re.match(r"^[A-Za-z0-9_-]{1,32}$", nm or ""):
                    emit("[ERROR] Invalid username (letters, digits, - and _ only, max 32) - nothing was created.")
                    return
                if any(u.get("name") == nm and not u.get("revoked") for u in openvpn_users()):
                    emit(f"[ERROR] An OpenVPN account named '{nm}' already exists - nothing was created.")
                    return
                if not script:
                    emit("[ERROR] openvpn-user.sh was not found - run update / Fix Stack first. Nothing was created.")
                    return
                emit(f"[OK] Details accepted - user '{nm}', certificate valid {exp} day(s)")
                emit("[INFO] Issuing the certificate and building the profile...")
                rc = _pipe_lines(["bash", script, "add", nm, str(exp)], emit)
                if rc != 0:
                    emit("[ERROR] Certificate issue failed - see the lines above. Nothing was saved.")
                    return
                _save_record()
                emit(f"[OK] OpenVPN account '{nm}' created - certificate expires {exp_date}")
                result(openvpn_result_card(nm, exp, exp_date, domain, _profile_text()))
                return
            if not _re.match(r"^[A-Za-z0-9_-]{1,32}$", nm or ""):
                return self.redirect("/openvpn")
            if script:
                r = subprocess.run(["bash", script, "add", nm, str(exp)], stdout=subprocess.PIPE, stderr=subprocess.PIPE, universal_newlines=True, timeout=120)
                if r.returncode == 0:
                    _save_record()
                    return self.send_html(page("OpenVPN Account Created", openvpn_result_card(nm, exp, exp_date, domain, _profile_text()), active="/openvpn", user=user))
            return self.redirect("/openvpn")
        if p in ("/openvpn/renew", "/openvpn/revoke"):
            import re as _re
            nm = data.get("name", "").strip()
            script = openvpn_script()
            users = openvpn_users()
            rec = next((u for u in users if u.get("name") == nm and not u.get("revoked")), None)
            if self.wants_popup():
                emit, _result = self.stream_open()
                if rec is None or not _re.match(r"^[A-Za-z0-9_-]{1,32}$", nm or ""):
                    emit(f"[ERROR] OpenVPN account '{nm}' was not found - nothing was changed.")
                    return
                if not script:
                    emit("[ERROR] openvpn-user.sh was not found - run update / Fix Stack first.")
                    return
                if p == "/openvpn/renew":
                    days = to_int(data.get("days", 30), 30)
                    if days < 1: days = 30
                    emit(f"[INFO] Renewing the certificate of '{nm}' ({days} day(s) from today)...")
                    rc = _pipe_lines(["bash", script, "renew", nm, str(days)], emit)
                    if rc != 0:
                        emit("[ERROR] Renew failed - see the lines above.")
                        return
                    rec["expiry_days"] = days
                    rec["expires"] = expires_on(time.strftime("%Y-%m-%d"), days)
                    save_json("openvpn_users.json", users); log_activity(f"openvpn renew {nm} {days}d -> {rec['expires']}")
                    emit(f"[OK] '{nm}' renewed - new certificate expires {rec['expires']}. Download the fresh .ovpn profile from the OpenVPN page.")
                else:
                    emit(f"[INFO] Revoking the certificate of '{nm}'...")
                    rc = _pipe_lines(["bash", script, "revoke", nm], emit)
                    if rc != 0:
                        emit("[ERROR] Revoke failed - see the lines above.")
                        return
                    rec["revoked"] = True; rec["disabled"] = True
                    save_json("openvpn_users.json", users); log_activity(f"openvpn revoke {nm}")
                    emit("[INFO] Restarting OpenVPN so the revocation takes effect...")
                    try:
                        subprocess.run(["systemctl", "restart", "vps-openvpn"], timeout=20, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
                        emit("[OK] OpenVPN restarted - the revoked certificate is now refused.")
                    except Exception:
                        emit("[WARN] Could not restart OpenVPN from the panel - restart the vps-openvpn service to apply the revocation.")
                emit("[OK] Done - close this popup to refresh the list.")
                return
            if rec is not None and script:
                if p == "/openvpn/renew":
                    days = to_int(data.get("days", 30), 30)
                    if days < 1: days = 30
                    r = subprocess.run(["bash", script, "renew", nm, str(days)], stdout=subprocess.PIPE, stderr=subprocess.PIPE, universal_newlines=True, timeout=120)
                    if r.returncode == 0:
                        rec["expiry_days"] = days
                        rec["expires"] = expires_on(time.strftime("%Y-%m-%d"), days)
                        save_json("openvpn_users.json", users); log_activity(f"openvpn renew {nm} {days}d -> {rec['expires']}")
                else:
                    r = subprocess.run(["bash", script, "revoke", nm], stdout=subprocess.PIPE, stderr=subprocess.PIPE, universal_newlines=True, timeout=120)
                    if r.returncode == 0:
                        rec["revoked"] = True; rec["disabled"] = True
                        save_json("openvpn_users.json", users); log_activity(f"openvpn revoke {nm}")
                        try: subprocess.run(["systemctl", "restart", "vps-openvpn"], timeout=20, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
                        except Exception: pass
            return self.redirect("/openvpn")
        if p == "/xray/create":
            proto = data.get("protocol", "VLESS").lower(); remark = data.get("remark", "user")
            exp = to_int(data.get("expiry_days", 30), 30); gb = to_int(data.get("gb_limit", 0), 0); ipl = to_int(data.get("ip_limit", 0), 0)
            created = time.strftime("%Y-%m-%d")
            exp_date = expires_on(created, exp)
            if self.wants_popup():
                emit, result = self.stream_open()
                emit("[INFO] Creating Xray account...")
                if proto not in ("vmess", "vless", "trojan"):
                    emit(f"[ERROR] Unknown protocol '{proto}' - nothing was created.")
                    return
                emit(f"[OK] Details accepted - {proto.upper()} account '{remark}', {exp} day(s), data {fmt_data(gb)}, IP limit {fmt_logins(ipl)}")
                uid = str(uuid.uuid4())
                # 5 links per account for every protocol (see build_links).
                links = build_links(proto, uid, domain, remark)
                emit("[OK] UUID generated - 5 links built (TLS / NTLS / TCP / XHTTP TLS / XHTTP NTLS)")
                users = load_json("xray_users.json", [])
                users.append({"id": uid, "protocol": proto, "remark": remark, "link": links.get("Link TLS", ""), "links": links, "expiry_days": exp, "gb_limit": gb, "ip_limit": ipl, "used_gb": 0, "created": created, "expires": exp_date})
                save_json("xray_users.json", users); log_activity(f"xray create {proto} {remark}")
                emit("[OK] Account saved")
                emit("[INFO] Syncing the account to Xray...")
                sync_xray(emit=emit)
                emit(f"[OK] Xray account '{remark}' created - expires {exp_date}")
                result(xray_result_cards(remark, proto, uid, domain, created, exp, exp_date, gb, ipl, links))
                return
            users = load_json("xray_users.json", []); uid = str(uuid.uuid4())
            # 5 links per account for every protocol (see build_links).
            # Chain: 443 -> HAProxy (TLS termination) -> Nginx -> inbound
            #         80 -> HAProxy -> Nginx -> inbound ; TCP/XHTTP-TLS ports -> Xray direct
            links = build_links(proto, uid, domain, remark)
            link = links.get("Link TLS", "")
            users.append({"id": uid, "protocol": proto, "remark": remark, "link": link, "links": links, "expiry_days": exp, "gb_limit": gb, "ip_limit": ipl, "used_gb": 0, "created": created, "expires": exp_date})
            save_json("xray_users.json", users); log_activity(f"xray create {proto} {remark}"); sync_xray()
            body = xray_result_cards(remark, proto, uid, domain, created, exp, exp_date, gb, ipl, links)
            return self.send_html(page("Xray Account Created", body, active="/xray", user=user))
        if p == "/xray/delete":
            if self.wants_popup():
                emit, _result = self.stream_open()
                emit("[INFO] Deleting Xray account...")
                users = [u for u in load_json("xray_users.json", []) if u["id"] != data.get("id")]; save_json("xray_users.json", users); log_activity("xray delete")
                emit("[OK] Account record removed")
                emit("[INFO] Syncing the change to Xray...")
                sync_xray(emit=emit)
                emit("[OK] Xray account deleted")
                emit("[OK] Done - close this popup to refresh the list.")
                return
            users = [u for u in load_json("xray_users.json", []) if u["id"] != data.get("id")]; save_json("xray_users.json", users); log_activity("xray delete"); sync_xray(); return self.redirect("/xray")
        if p in ("/xray/renew", "/xray/limits", "/xray/reset", "/xray/lock"):
            if self.wants_popup():
                return self._xray_op_stream(p, data)
            uid = data.get("id", "")
            users = load_json("xray_users.json", [])
            rec = next((u for u in users if u.get("id") == uid), None)
            if rec is None: return self.redirect("/xray")
            if p == "/xray/renew":
                acct_renew(rec, data.get("days", 30)); log_activity(f"xray renew {rec.get('remark')} +{data.get('days', 30)}d -> {rec.get('expires')}")
            elif p == "/xray/limits":
                acct_edit_limits(rec, data.get("gb_limit", 0), data.get("ip_limit", 0)); log_activity(f"xray limits {rec.get('remark')} gb={rec.get('gb_limit')} ip={rec.get('ip_limit')}")
            elif p == "/xray/reset":
                acct_reset_usage(rec); log_activity(f"xray reset usage {rec.get('remark')}")
            elif p == "/xray/lock":
                locked = data.get("locked") == "1"
                acct_set_lock(rec, locked); log_activity(f"xray {'lock' if locked else 'unlock'} {rec.get('remark')}")
            save_json("xray_users.json", users); sync_xray()
            return self.redirect("/xray")
        if p in ("/admins/add", "/admins/delete", "/admins/toggle", "/admins/role", "/admins/pass"):
            admins = load_admins()
            uname = (data.get("username") or "").strip()
            creds = load_creds()
            if uname == creds.get("username"):
                return self.send_html(page("Protected", "<div class='card'><h3>Main Super Admin is protected</h3><p class='mut'>The bootstrap Super Admin cannot be changed here (last-super-admin protection). Manage it under Settings or from the terminal.</p><p><a class='btn ghost' href='/admins'>Back</a></p></div>", user=user), code=400)
            rec = next((a for a in admins if a.get("username") == uname), None)
            if p == "/admins/add":
                pw = data.get("password", "")
                rrole = data.get("role", "viewer")
                if not uname or len(pw) < 6 or rrole not in ROLES or rec is not None:
                    return self.send_html(page("Add Admin", "<div class='card'><h3>Could not add admin</h3><p class='mut'>Username must be new, the password needs at least 6 characters and the role must be valid.</p><p><a class='btn ghost' href='/admins'>Back</a></p></div>", user=user), code=400)
                salt, h = _hash_pw(pw)
                admins.append({"username": uname, "salt": salt, "hash": h, "role": rrole, "disabled": False})
                save_admins(admins); log_activity(f"admin add {uname} role={rrole}")
                return self.redirect("/admins")
            if rec is None: return self.redirect("/admins")
            if p == "/admins/delete":
                if rec.get("role") == "super" and not rec.get("disabled") and enabled_super_count() <= 1:
                    return self.send_html(page("Protected", "<div class='card'><h3>Last Super Admin</h3><p class='mut'>You cannot delete the last enabled Super Admin.</p><p><a class='btn ghost' href='/admins'>Back</a></p></div>", user=user), code=400)
                save_admins([a for a in admins if a.get("username") != uname]); log_activity(f"admin delete {uname}")
            elif p == "/admins/toggle":
                newdis = not rec.get("disabled")
                if newdis and rec.get("role") == "super" and enabled_super_count() <= 1:
                    return self.send_html(page("Protected", "<div class='card'><h3>Last Super Admin</h3><p class='mut'>You cannot disable the last enabled Super Admin.</p><p><a class='btn ghost' href='/admins'>Back</a></p></div>", user=user), code=400)
                rec["disabled"] = newdis; save_admins(admins); log_activity(f"admin {'disable' if newdis else 'enable'} {uname}")
            elif p == "/admins/role":
                nr = data.get("role", "viewer")
                if nr in ROLES:
                    if rec.get("role") == "super" and nr != "super" and not rec.get("disabled") and enabled_super_count() <= 1:
                        return self.send_html(page("Protected", "<div class='card'><h3>Last Super Admin</h3><p class='mut'>You cannot demote the last enabled Super Admin.</p><p><a class='btn ghost' href='/admins'>Back</a></p></div>", user=user), code=400)
                    rec["role"] = nr; save_admins(admins); log_activity(f"admin role {uname} -> {nr}")
            elif p == "/admins/pass":
                pw = data.get("password", "")
                if len(pw) >= 6:
                    salt, h = _hash_pw(pw); rec["salt"] = salt; rec["hash"] = h; save_admins(admins); log_activity(f"admin password set {uname}")
            return self.redirect("/admins")
        if p == "/action/service":
            name = data.get("name", ""); op = data.get("op", "")
            allowed = {x["name"] for x in service_list()}
            if self.wants_popup():
                emit, _result = self.stream_open()
                verbs = {"start": "Starting", "stop": "Stopping", "restart": "Restarting"}
                if name not in allowed or op not in verbs:
                    emit("[ERROR] Unknown service or action - nothing was done.")
                    return
                emit(f"[INFO] {verbs[op]} service '{name}'...")
                if name == "vps-panel" and op in ("stop", "restart"):
                    subprocess.Popen(["bash", "-c", f"sleep 1; systemctl {op} vps-panel"])
                    log_activity(f"service {op} {name}")
                    emit("[OK] The panel restart is scheduled - it comes back in a few seconds. Refresh the page shortly.")
                    return
                rc = _pipe_lines(["systemctl", op, name], emit)
                log_activity(f"service {op} {name}")
                try:
                    st = subprocess.run(["systemctl", "is-active", name], stdout=subprocess.PIPE, stderr=subprocess.PIPE, universal_newlines=True, timeout=5).stdout.strip() or "unknown"
                except Exception:
                    st = "unknown"
                if rc != 0:
                    emit(f"[ERROR] systemctl {op} {name} failed (exit {rc}) - '{name}' is {st}. Open the service Logs for details.")
                else:
                    emit(f"[OK] Service '{name}' is now {st}")
                emit("[OK] Done - close this popup to refresh the list.")
                return
            if name in allowed and op in ("start", "stop", "restart"):
                if name == "vps-panel" and op in ("stop", "restart"):
                    subprocess.Popen(["bash", "-c", f"sleep 1; systemctl {op} vps-panel"])
                else:
                    subprocess.run(["systemctl", op, name], stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=30)
                log_activity(f"service {op} {name}")
            return self.redirect("/services")
        if p == "/action/restart-all":
            if self.wants_popup():
                emit, _result = self.stream_open()
                emit("[INFO] Restarting all services...")
                for n in [x["name"] for x in service_list() if x["name"] not in ("vps-panel", "systemd-resolved")]:
                    emit(f"[INFO] Restarting {n}...")
                    _pipe_lines(["systemctl", "restart", n], emit)
                subprocess.Popen(["bash", "-c", "sleep 2; systemctl restart vps-panel"])
                log_activity("services restart all")
                emit("[OK] All services restarted. The panel itself restarts last - refresh this page in a few seconds.")
                return
            names = [x["name"] for x in service_list() if x["name"] not in ("vps-panel", "systemd-resolved")]
            for n in names:
                try: subprocess.run(["systemctl", "restart", n], stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=30)
                except Exception: pass
            subprocess.Popen(["bash", "-c", "sleep 2; systemctl restart vps-panel"])
            log_activity("services restart all")
            return self.send_html(page("Restart All", "<div class='card'><h3>All services restarted</h3><p class='mut'>The panel itself restarts last - refresh this page in a few seconds.</p><p><a class='btn ghost' href='/services'>Back to Services</a></p></div>", active="/services", user=user))
        if p == "/action/run":
            job = data.get("job", "")
            if job == "install":
                inst = next((c for c in ("/opt/vps-manager-suite/install.sh", os.path.join(BASE, "..", "install.sh")) if os.path.exists(c)), "")
                argv = ["bash", inst] if inst else ["echo", "install.sh not found - run vpsupdate first"]
                return self.stream_cmd("Install All Components", argv)
            if job == "fix":
                s = suite_script("fix-stack.sh")
                return self.stream_cmd("Fix SSH-WS/SSL Stack", ["bash", s] if s else ["echo", "fix-stack.sh not found"])
            if job == "xray":
                s = suite_script("fix-xray-sync.py")
                return self.stream_cmd("Repair Xray Sync", ["python3", s] if s else ["echo", "fix-xray-sync.py not found"])
            if job == "ssl":
                s = suite_script("ssl.sh")
                return self.stream_cmd("Issue / Renew SSL", ["bash", s, "issue"] if s else ["echo", "ssl helper not found - use terminal menu option 03"])
            if job == "vnstat":
                return self.stream_cmd("Install vnstat", ["bash", "-c", "apt-get update -qq && apt-get install -y vnstat && systemctl enable --now vnstat && echo vnstat ready"], back="/stats")
            if job in ("pyproxy-start", "pyproxy-stop", "pyproxy-restart", "pyproxy-force-start", "pyproxy-force-stop"):
                # Python Proxy controls - fixed allowlist, the subcommand is
                # derived from the job name, never from raw user input.
                ctl = suite_script("ws-proxy-ctl.sh")
                sub = job[len("pyproxy-"):]
                title = "Python Proxy - " + sub.replace("-", " ").title()
                return self.stream_cmd(title, ["bash", ctl, sub] if ctl else ["echo", "ws-proxy-ctl.sh not found - run vpsupdate first"])
            if job == "pyproxy-setport":
                ctl = suite_script("ws-proxy-ctl.sh")
                raw = (data.get("port") or "").strip()
                if not raw.isdigit() or not (1 <= int(raw) <= 65535):
                    return self.stream_cmd("Python Proxy - Set Port", ["echo", "[ERROR] Invalid port - enter an integer between 1 and 65535"])
                return self.stream_cmd("Python Proxy - Set Port", ["bash", ctl, "set-port", str(int(raw))] if ctl else ["echo", "ws-proxy-ctl.sh not found - run vpsupdate first"])
            return self.redirect("/services")
        if p == "/telegram":
            cfg.update({"telegram_token": data.get("telegram_token", ""), "telegram_admin_id": data.get("telegram_admin_id", "")}); save_config(cfg); log_activity("telegram config saved"); return self.redirect("/telegram")
        if p == "/settings":
            if self.wants_popup():
                emit, _result = self.stream_open()
                emit("[INFO] Saving settings...")
                old_domain = cfg.get("domain", "")
                cfg.update({k: data.get(k, cfg.get(k, "")) for k in ("domain", "banner", "qos_interface", "qos_rate", "per_client_cap")})
                sd_changed, sd_notes = apply_protocol_domains(cfg, data)
                save_config(cfg); log_activity("settings saved")
                emit("[OK] Settings saved")
                for _n in sd_notes: emit(_n)
                if sd_changed:
                    emit("[INFO] SlowDNS Domain changed - restarting the SlowDNS service...")
                    restart_slowdns(emit=emit)
                new_domain = cfg.get("domain", "")
                if new_domain and new_domain != old_domain:
                    sd = suite_script("set-domain.sh")
                    if sd:
                        log_activity(f"domain change {old_domain} -> {new_domain} (re-provisioning)")
                        emit(f"[INFO] Domain changed ({old_domain} -> {new_domain}) - re-provisioning the stack for the new domain...")
                        rc = _pipe_lines(["bash", sd, new_domain], emit)
                        emit("[OK] Domain change finished - the stack now uses the new domain." if rc == 0 else f"[ERROR] Domain change failed (exit {rc}) - see the lines above.")
                    else:
                        emit("[WARN] set-domain.sh was not found - the domain was saved but the stack was not re-provisioned.")
                emit("[OK] Done - close this popup to refresh the page.")
                return
            old_domain = cfg.get("domain", "")
            cfg.update({k: data.get(k, cfg.get(k, "")) for k in ("domain", "banner", "qos_interface", "qos_rate", "per_client_cap")})
            sd_changed, _sd_notes = apply_protocol_domains(cfg, data)
            save_config(cfg); log_activity("settings saved")
            if sd_changed: restart_slowdns()
            new_domain = cfg.get("domain", "")
            if new_domain and new_domain != old_domain:
                sd = suite_script("set-domain.sh")
                if sd:
                    log_activity(f"domain change {old_domain} -> {new_domain} (re-provisioning)")
                    return self.stream_cmd("Domain Change - Re-provisioning", ["bash", sd, new_domain], back="/settings")
            return self.redirect("/settings")
        if p == "/settings/banner-preview":
            # Live preview for the Banner Designer: render the posted fields
            # with the real renderer and return colored HTML for the <pre>.
            if _banner is None:
                return self.send_html("Preview unavailable - the banner renderer was not found.")
            return self.send_html(_banner.to_html(_banner.render(banner_design_from_form(data))))
        if p == "/settings/banner":
            if _banner is None:
                if self.wants_popup():
                    emit, _result = self.stream_open()
                    emit("[ERROR] The banner renderer (scripts/banner_render.py) was not found - run vpsupdate first. Nothing was saved.")
                    return
                return self.redirect("/settings")
            design = banner_design_from_form(data)
            if self.wants_popup():
                emit, _result = self.stream_open()
                emit("[INFO] Saving the SSH banner design...")
                save_banner_design(cfg, design, emit)
                emit("[OK] Done - close this popup to refresh the page.")
                return
            save_banner_design(cfg, design)
            return self.redirect("/settings")
        if p == "/password":
            creds = load_creds(); creds["password"] = data.get("new_password", ""); save_json("credentials.json", creds)
            # If the system credentials file exists, update it there too
            try:
                if os.path.exists(SYSTEM_CREDS): json.dump(creds, open(SYSTEM_CREDS, "w"), indent=2)
            except Exception: pass
            log_activity("panel password changed"); return self.redirect("/settings")
        if p == "/action/fix":
            _fix = suite_script("fix-stack.sh")
            return self.stream_cmd("Fix SSH-WS/SSL Stack", ["bash", _fix] if _fix else ["echo", "Fix script not found on this VPS. Run install/update first."])
        if p == "/action/qos":
            _q = suite_script("qos.sh")
            return self.stream_cmd("Apply QoS", ["bash", _q, "apply"] if _q else ["echo", "qos.sh not found"], back="/settings")
        if p == "/action/qos-off":
            _q = suite_script("qos.sh")
            return self.stream_cmd("Remove QoS", ["bash", _q, "off"] if _q else ["echo", "qos.sh not found"], back="/settings")
        if p == "/action/speedtest":
            if self.wants_popup():
                emit, _result = self.stream_open()
                emit("[INFO] Speedtest starting...")
                ookla = shutil.which("speedtest"); cli = shutil.which("speedtest-cli")
                rc = 1
                if ookla:
                    emit("[INFO] Measuring with the Ookla speedtest...")
                    rc = _pipe_lines([ookla, "--accept-license", "--accept-gdpr"], emit)
                elif cli:
                    emit("[INFO] Measuring with speedtest-cli (finding a server, then download and upload)...")
                    rc = _pipe_lines([cli], emit)
                else:
                    emit("[INFO] speedtest-cli is not installed - installing it first...")
                    _pipe_lines(["bash", "-c", "apt-get update -qq && apt-get install -y speedtest-cli"], emit)
                    cli = shutil.which("speedtest-cli")
                    if cli:
                        emit("[INFO] Measuring with speedtest-cli (finding a server, then download and upload)...")
                        rc = _pipe_lines([cli], emit)
                    else:
                        emit("[ERROR] speedtest-cli is still not available after the install attempt.")
                emit("[OK] Speedtest finished." if rc == 0 else f"[ERROR] Speedtest failed (exit {rc}) - see the lines above.")
                return
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
