#!/usr/bin/env python3
"""VPS Manager Suite - Telegram Bot (original code, stdlib only)

A full remote panel in Telegram: inline menus and step-by-step wizards
for SSH / Xray accounts (create, list, links, renew, limits, reset,
lock/unlock, delete, connected IPs), server status and ports, domain,
SSH banner, QoS, stack actions (fix, restarts, SSL, install) and bot
admin management.

Access control:
  - Super Admin: the single chat id in config telegram_admin_id
    (env TELEGRAM_ADMIN_ID). Only the Super Admin manages bot admins
    and runs install / destructive system actions.
  - Bot Admins: extra chat ids in config telegram_bot_admins - full
    account management, no admin management.
  - Everyone else is ignored.

Config is read through vpsm.load_config(), which merges the data-dir
config (what the web panel saves) with /etc/vps-manager/config.json, so
a token saved in the panel is seen here too.
Env: TELEGRAM_BOT_TOKEN, TELEGRAM_ADMIN_ID, TELEGRAM_API_BASE
(the API base is overridable for testing), VPSMGR_DATA_DIR.
"""
import json, os, re, subprocess, sys, threading, urllib.request, uuid

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(BASE, "scripts"))
import vpsm

API_BASE = os.environ.get("TELEGRAM_API_BASE", "https://api.telegram.org")
DATA_DIR = vpsm.DATA_DIR
SERVICES = ["xray", "nginx", "haproxy", "dropbear", "vps-panel", "ws-proxy", "vps-monitor"]
WIZ = {}          # chat_id -> {"kind":..., "step":int, "data":{...}}
PENDING = {}      # chat_id -> (kind, data)
USER_RE = re.compile(r"^[A-Za-z0-9_.\-]+$")
ICONS = {"Active": "🟢", "EXPIRED": "🔴", "GB-LIMIT": "🟠", "IP-LIMIT": "🟠", "LOCKED": "🔒"}


# --- adapters over the shared vpsm module (its lifecycle calls mutate the
# --- store themselves; the bot adds the system/service actions) ---
def load_ssh():
    return vpsm.load_store("ssh_users.json")


def load_xray():
    return vpsm.load_store("xray_users.json")


def fmt_gb(v):
    return "Unlimited" if vpsm.to_int(v, 0) == 0 else "%s GB" % vpsm.to_int(v, 0)


def fmt_ip(v):
    return "Unlimited" if vpsm.to_int(v, 0) == 0 else str(vpsm.to_int(v, 0))


def status_of(rec):
    st = vpsm.display_status(rec)
    return st, ICONS.get(st, "•")


def dead_of(rec):
    try:
        return vpsm.dead_note(rec, vpsm.auto_delete_hours())
    except Exception:
        return ""


# ---------------------------------------------------------------------------
# config + roles
# ---------------------------------------------------------------------------
def conf():
    return vpsm.load_config()


def token():
    c = conf()
    return os.environ.get("TELEGRAM_BOT_TOKEN") or str(c.get("telegram_bot_token") or c.get("telegram_token") or c.get("bot_token") or "")


def super_id():
    c = conf()
    v = os.environ.get("TELEGRAM_ADMIN_ID") or c.get("telegram_admin_id") or c.get("admin_id") or ""
    return str(v).strip()


def admin_ids():
    return [str(x) for x in (conf().get("telegram_bot_admins") or [])]


def role_of(chat):
    chat = str(chat)
    if super_id() and chat == super_id():
        return "super"
    if chat in admin_ids():
        return "admin"
    return ""


def save_conf_key(key, value):
    p = os.path.join(DATA_DIR, "config.json")
    c = {}
    if os.path.exists(p):
        try:
            with open(p) as f:
                c = json.load(f)
        except Exception:
            c = {}
    c[key] = value
    tmp = p + ".tmp"
    with open(tmp, "w") as f:
        json.dump(c, f, indent=2)
    os.replace(tmp, p)


# ---------------------------------------------------------------------------
# Telegram API
# ---------------------------------------------------------------------------
def api(method, **params):
    t = token()
    if not t:
        return None
    url = "%s/bot%s/%s" % (API_BASE, t, method)
    data = json.dumps(params).encode()
    req = urllib.request.Request(url, data=data, headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=35) as r:
            out = json.loads(r.read().decode())
            return out.get("result") if out.get("ok") else None
    except Exception as e:
        print("[bot] api %s failed: %s" % (method, e), flush=True)
        return None


def send(chat, text, kb=None):
    params = {"chat_id": chat, "text": text[:4000], "parse_mode": "HTML"}
    if kb:
        params["reply_markup"] = {"inline_keyboard": kb}
    return api("sendMessage", **params)


def edit(chat, mid, text, kb=None):
    params = {"chat_id": chat, "message_id": mid, "text": text[:4000], "parse_mode": "HTML"}
    if kb:
        params["reply_markup"] = {"inline_keyboard": kb}
    return api("editMessageText", **params)


def btn(text, data):
    return {"text": text, "callback_data": data}


def home_kb(role):
    kb = [
        [btn("📊 Status", "status"), btn("🔌 Ports", "ports")],
        [btn("👤 SSH Accounts", "ssh"), btn("🚀 Xray Accounts", "xr")],
        [btn("🌐 Domain", "dom"), btn("🎌 Banner", "ban")],
        [btn("📶 QoS", "qos"), btn("🛠 System", "sys")],
    ]
    if role == "super":
        kb.append([btn("👥 Bot Admins", "adm")])
    return kb


# ---------------------------------------------------------------------------
# system glue (the bot runs as root under systemd)
# ---------------------------------------------------------------------------
def sh(cmd, timeout=30):
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        return r.returncode, (r.stdout or "") + (r.stderr or "")
    except Exception as e:
        return 1, str(e)


def svc_state(name):
    r = subprocess.run(["systemctl", "is-active", name], capture_output=True, text=True)
    return (r.stdout or "").strip() or "unknown"


def sys_create(u, pw, exp_date):
    rc, _out = sh(["id", u], 10)
    if rc != 0:
        rc, out = sh(["useradd", "-m", "-s", "/bin/false", u], 15)
        if rc != 0:
            return out.strip()
    sh(["usermod", "-e", exp_date, u], 15)
    try:
        import crypt
        hashed = crypt.crypt(pw, crypt.mksalt(crypt.METHOD_SHA512))
        sh(["usermod", "-p", hashed, u], 15)
    except Exception:
        p = subprocess.Popen(["chpasswd"], stdin=subprocess.PIPE, text=True)
        p.communicate("%s:%s" % (u, pw), timeout=15)
    return ""


def sys_delete(u):
    if re.match(r"^[A-Za-z0-9_.\-]+$", u or ""):
        sh(["userdel", u], 15)


def sys_lock(u, locked):
    if not re.match(r"^[A-Za-z0-9_.\-]+$", u or ""):
        return
    sh(["usermod", "-L" if locked else "-U", u], 15)
    if locked:
        _rc, out = sh(["pgrep", "-u", u], 10)
        for pid in out.split():
            try:
                os.kill(int(pid), 9)
            except Exception:
                pass


def sys_expiry(u, exp_date):
    sh(["usermod", "-e", exp_date, u], 15)


def sync():
    try:
        subprocess.run([sys.executable, os.path.join(BASE, "scripts", "sync-xray.py"), "--restart"],
                       capture_output=True, timeout=120)
    except Exception as e:
        print("[bot] xray sync failed: %s" % e, flush=True)


def run_long(chat, argv, label, timeout=600):
    """Run a long stack action in the background, report the tail."""
    def work():
        try:
            r = subprocess.run(argv, capture_output=True, text=True, timeout=timeout)
            tail = ((r.stdout or "") + (r.stderr or "")).strip().splitlines()
            send(chat, "✅ <b>%s</b> finished (exit %d):\n<pre>%s</pre>" % (
                label, r.returncode, "\n".join(tail[-12:]) or "no output"))
        except Exception as e:
            send(chat, "❌ <b>%s</b> failed: %s" % (label, e))
    threading.Thread(target=work, daemon=True).start()
    send(chat, "⏳ <b>%s</b> started - I will report here when it finishes." % label)


# ---------------------------------------------------------------------------
# views
# ---------------------------------------------------------------------------
def status_text():
    c = conf()
    lines = ["🖥 <b>Server Status</b>",
             "Domain: <code>%s</code>" % (c.get("domain") or "-"),
             "Location: %s" % vpsm.server_location(),
             "",
             "<b>Services</b>"]
    for s in SERVICES:
        lines.append("%s %s" % ("🟢" if svc_state(s) == "active" else "🔴", s))
    lines.append("")
    lines.append("SSH accounts: %d | Xray accounts: %d" % (len(load_ssh()), len(load_xray())))
    return "\n".join(lines)


def ports_text():
    _rc, out = sh(["ss", "-tlnp"], 15)
    ports = sorted({m.group(1) for m in re.finditer(r"[*:](\d+)\s", out)}, key=lambda x: int(x))
    return "🔌 <b>Listening ports</b>\n<code>%s</code>" % (" ".join(ports) if ports else "none found")


def ssh_list_kb():
    kb = []
    for u in load_ssh():
        kb.append([btn("%s %s" % (status_of(u)[1], u["username"]), "ssh:v:" + u["username"])])
    kb.append([btn("➕ New SSH Account", "ssh:new")])
    kb.append([btn("🏠 Main Menu", "home")])
    return kb


def ssh_detail(chat, mid, username):
    u = next((x for x in load_ssh() if x["username"] == username), None)
    if u is None:
        return edit(chat, mid, "Account not found.", home_kb(role_of(chat)))
    label, icon = status_of(u)
    dead = dead_of(u)
    c = conf()
    text = ("👤 <b>%s</b>\nStatus: %s %s%s\nCreated: %s | Expires: %s (%s days)\n"
            "Used: %.2f GB | Limit: %s | IP limit: %s\nLogin: <code>ssh %s@%s</code>") % (
        username, icon, label, ("\n" + dead) if dead else "",
        u.get("created", "-"), u.get("expires", "-"), u.get("expiry_days", 30),
        float(u.get("used_gb") or 0), fmt_gb(u.get("gb_limit", 0)),
        fmt_ip(u.get("ip_limit", 0)), username, c.get("domain") or "SERVER-IP")
    lockbtn = btn("🔓 Unlock", "ssh:a:%s:unl" % username) if u.get("disabled") else btn("🔒 Lock", "ssh:a:%s:lok" % username)
    kb = [
        [btn("🔄 Renew", "ssh:a:%s:ren" % username), btn("📏 Limits", "ssh:a:%s:lim" % username)],
        [btn("♻️ Reset Usage", "ssh:a:%s:rst" % username), lockbtn],
        [btn("📡 Connected", "ssh:a:%s:con" % username), btn("🗑 Delete", "ssh:a:%s:del" % username)],
        [btn("⬅️ SSH List", "ssh"), btn("🏠 Main Menu", "home")],
    ]
    edit(chat, mid, text, kb)


def xr_proto_kb():
    return [
        [btn("VMess", "xr:l:vmess"), btn("VLESS", "xr:l:vless"), btn("Trojan", "xr:l:trojan")],
        [btn("🏠 Main Menu", "home")],
    ]


def xr_list_kb(proto):
    kb = []
    for u in load_xray():
        if u["protocol"] == proto:
            kb.append([btn("%s %s" % (status_of(u)[1], u["remark"]), "xr:v:" + u["id"])])
    kb.append([btn("➕ New %s Account" % proto.upper(), "xr:new:" + proto)])
    kb.append([btn("⬅️ Protocols", "xr"), btn("🏠 Main Menu", "home")])
    return kb


def xr_detail(chat, mid, uid_):
    u = next((x for x in load_xray() if x["id"] == uid_), None)
    if u is None:
        return edit(chat, mid, "Account not found.", home_kb(role_of(chat)))
    label, icon = status_of(u)
    dead = dead_of(u)
    links = vpsm.build_links(u["protocol"], u["id"], str(conf().get("domain") or ""), u["remark"])
    text = ("🚀 <b>%s</b> (%s)\nStatus: %s %s%s\nCreated: %s | Expires: %s (%s days)\n"
            "Used: %.2f GB | Limit: %s | IP limit: %s\n\n<b>Links (tap to copy)</b>\n%s") % (
        u["remark"], u["protocol"].upper(), icon, label, ("\n" + dead) if dead else "",
        u.get("created", "-"), u.get("expires", "-"), u.get("expiry_days", 30),
        float(u.get("used_gb") or 0), fmt_gb(u.get("gb_limit", 0)),
        fmt_ip(u.get("ip_limit", 0)),
        "\n".join("<code>%s</code>" % l for l in links.values()))
    lockbtn = btn("🔓 Unlock", "xr:a:%s:unl" % uid_) if u.get("disabled") else btn("🔒 Lock", "xr:a:%s:lok" % uid_)
    kb = [
        [btn("🔄 Renew", "xr:a:%s:ren" % uid_), btn("📏 Limits", "xr:a:%s:lim" % uid_)],
        [btn("♻️ Reset Usage", "xr:a:%s:rst" % uid_), lockbtn],
        [btn("📡 Connected", "xr:a:%s:con" % uid_), btn("🗑 Delete", "xr:a:%s:del" % uid_)],
        [btn("⬅️ %s List" % u["protocol"].upper(), "xr:l:" + u["protocol"]), btn("🏠 Main Menu", "home")],
    ]
    edit(chat, mid, text, kb)


def connected_text(kind, key):
    conn = {}
    p = os.path.join(DATA_DIR, "connected.json")
    if os.path.exists(p):
        try:
            with open(p) as f:
                conn = json.load(f)
        except Exception:
            conn = {}
    if kind == "ssh":
        ips = (conn.get("ssh") or {}).get(key) or {}
        head = "📡 <b>%s</b> - connected IPs" % key
    else:
        u = next((x for x in load_xray() if x["id"] == key), None)
        name = "%s/%s" % (u["protocol"], u["remark"]) if u else key
        ips = (conn.get("xray") or {}).get(name) or {}
        head = "📡 <b>%s</b> - connected IPs" % name
    if not ips:
        return head + "\nNo active connections seen in the last minutes."
    lines = [head]
    for ip, meta in ips.items():
        country = (meta or {}).get("country") or vpsm.geo_country(ip)
        lines.append("• <code>%s</code> %s" % (ip, ("(" + country + ")") if country else ""))
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# wizards + one-shot prompts
# ---------------------------------------------------------------------------
def ask(chat, text):
    send(chat, text + "\n\nSend /cancel to stop.")


def parse_nonneg(text):
    try:
        v = int(str(text).strip())
        return v if v >= 0 else None
    except Exception:
        return None


def wizard_text(chat, text):
    """Feed a text message into the active wizard. True when consumed."""
    w = WIZ.get(chat)
    if w is None:
        return False
    kind, data = w["kind"], w["data"]
    step = w["step"]
    t = text.strip()
    if kind == "ssh_new":
        fields = ["username", "password", "days", "gb", "ip"]
        prompts = ["Send a <b>password</b> for the account:",
                   "Validity in <b>days</b> (e.g. 30):",
                   "<b>Data limit in GB</b> (0 = Unlimited):",
                   "<b>IP / device limit</b> (0 = Unlimited):"]
        if step == 0:
            if not USER_RE.match(t) or len(t) < 3:
                ask(chat, "❌ Invalid username (a-z, 0-9, _ . - ; min 3). Try again:")
                return True
            if any(u["username"] == t for u in load_ssh()):
                ask(chat, "❌ That username already exists. Try another:")
                return True
            data["username"] = t
        elif step == 1:
            if len(t) < 4:
                ask(chat, "❌ Password too short (min 4). Try again:")
                return True
            data["password"] = t
        else:
            v = parse_nonneg(t)
            if v is None:
                ask(chat, "❌ Send a number (0 or more). Try again:")
                return True
            data[fields[step]] = v
        if step < 4:
            w["step"] = step + 1
            ask(chat, prompts[step])
            return True
        del WIZ[chat]
        try:
            rec = vpsm.ssh_create(data["username"], data["password"], data["days"], data["gb"], data["ip"])
        except Exception as e:
            send(chat, "❌ %s" % e)
            return True
        err = sys_create(rec["username"], data["password"], rec["expires"])
        if err:
            vpsm.ssh_delete(rec["username"])
            send(chat, "❌ System user could not be created: %s" % err)
            return True
        send(chat, "✅ SSH account <b>%s</b> created.\nExpires: %s | Data: %s | IP: %s" % (
            rec["username"], rec["expires"], fmt_gb(rec["gb_limit"]), fmt_ip(rec["ip_limit"])),
            [[btn("👤 Open Account", "ssh:v:" + rec["username"])], [btn("🏠 Main Menu", "home")]])
        return True
    if kind == "xr_new":
        fields = ["remark", "days", "gb", "ip"]
        prompts = ["Validity in <b>days</b> (e.g. 30):",
                   "<b>Data limit in GB</b> (0 = Unlimited):",
                   "<b>IP / device limit</b> (0 = Unlimited):"]
        if step == 0:
            if not t or len(t) > 40:
                ask(chat, "❌ Send a short name for the account:")
                return True
            data["remark"] = t
        else:
            v = parse_nonneg(t)
            if v is None:
                ask(chat, "❌ Send a number (0 or more). Try again:")
                return True
            data[fields[step]] = v
        if step < 3:
            w["step"] = step + 1
            ask(chat, prompts[step])
            return True
        del WIZ[chat]
        rec = vpsm.xray_create(data["proto"], data["remark"], str(uuid.uuid4()),
                               data["days"], data["gb"], data["ip"], str(conf().get("domain") or ""))
        sync()
        send(chat, "✅ %s account <b>%s</b> created.\nExpires: %s | Data: %s | IP: %s" % (
            data["proto"].upper(), rec["remark"], rec["expires"],
            fmt_gb(rec["gb_limit"]), fmt_ip(rec["ip_limit"])),
            [[btn("🚀 Open Account", "xr:v:" + rec["id"])], [btn("🏠 Main Menu", "home")]])
        return True
    return False


def pending_text(chat, text):
    """One-shot prompts (renew days, limits, domain, banner, ...)."""
    p = PENDING.pop(chat, None)
    if p is None:
        return False
    kind = p[0]
    t = text.strip()
    if kind in ("renew_ssh", "renew_xr"):
        days = parse_nonneg(t)
        if not days:
            send(chat, "❌ Send a positive number of days.")
            return True
        if kind == "renew_ssh":
            try:
                rec = vpsm.ssh_renew(p[1], days)
                sys_expiry(rec["username"], rec["expires"])
                if rec.get("disabled"):
                    sys_lock(rec["username"], True)
                send(chat, "✅ %s renewed - new expiry %s." % (p[1], rec["expires"]))
            except Exception as e:
                send(chat, "❌ %s" % e)
        else:
            try:
                rec = vpsm.xray_renew(p[1], days)
                sync()
                send(chat, "✅ Renewed - new expiry %s." % rec["expires"])
            except Exception as e:
                send(chat, "❌ %s" % e)
        return True
    if kind in ("lim_ssh_gb", "lim_xr_gb"):
        v = parse_nonneg(t)
        if v is None:
            send(chat, "❌ Send a number (0 = Unlimited).")
            return True
        PENDING[chat] = (kind.replace("_gb", "_ip"), p[1], v)
        ask(chat, "Now the <b>IP / device limit</b> (0 = Unlimited):")
        return True
    if kind in ("lim_ssh_ip", "lim_xr_ip"):
        v = parse_nonneg(t)
        if v is None:
            send(chat, "❌ Send a number (0 = Unlimited).")
            return True
        gb = p[2]
        try:
            if kind == "lim_ssh_ip":
                rec = vpsm.ssh_edit_limits(p[1], gb, v)
                sys_lock(rec["username"], bool(rec.get("disabled")))
            else:
                vpsm.xray_edit_limits(p[1], gb, v)
                sync()
            send(chat, "✅ Limits updated: Data %s | IP %s." % (fmt_gb(gb), fmt_ip(v)))
        except Exception as e:
            send(chat, "❌ %s" % e)
        return True
    if kind == "domain":
        if not re.match(r"^(?=.{4,253}$)([a-zA-Z0-9-]{1,63}\.)+[a-zA-Z]{2,}$", t):
            send(chat, "❌ That does not look like a valid domain.")
            return True
        save_conf_key("domain", t)
        script = os.path.join(BASE, "scripts", "set-domain.sh")
        if os.path.exists(script):
            run_long(chat, ["bash", script, t], "Domain change to %s" % t)
        else:
            vpsm.regen_links(t)
            send(chat, "✅ Domain saved as <code>%s</code> and links regenerated.\n"
                       "Run terminal menu option 02 to finish SSL provisioning." % t)
        return True
    if kind == "banner":
        save_conf_key("banner", t)
        try:
            os.makedirs("/etc/vps-manager", exist_ok=True)
            with open("/etc/vps-manager/banner.txt", "w") as f:
                f.write(t + "\n")
        except Exception:
            pass
        send(chat, "✅ SSH banner updated.")
        return True
    if kind == "qos_cap":
        v = parse_nonneg(t)
        if v is None:
            send(chat, "❌ Send a number (Mbps, 0 = no per-user cap).")
            return True
        save_conf_key("per_client_cap", v)
        run_long(chat, ["bash", os.path.join(BASE, "scripts", "qos.sh"), "apply"], "QoS apply (cap %s Mbps)" % v)
        return True
    if kind == "admin_add":
        if not t.isdigit():
            send(chat, "❌ Send the numeric Telegram chat ID.")
            return True
        ids = admin_ids()
        if t not in ids and t != super_id():
            ids.append(t)
            save_conf_key("telegram_bot_admins", ids)
        send(chat, "✅ %s is now a bot admin." % t)
        return True
    return True


# ---------------------------------------------------------------------------
# account actions shared by callbacks
# ---------------------------------------------------------------------------
def ssh_action(chat, mid, username, act):
    rec = next((u for u in load_ssh() if u["username"] == username), None)
    if rec is None:
        return edit(chat, mid, "Account not found.", home_kb(role_of(chat)))
    if act == "ren":
        PENDING[chat] = ("renew_ssh", username)
        return ask(chat, "🔄 Renew <b>%s</b> - send the number of days to ADD:" % username)
    if act == "lim":
        PENDING[chat] = ("lim_ssh_gb", username)
        return ask(chat, "📏 New limits for <b>%s</b> - send the <b>data limit in GB</b> (0 = Unlimited):" % username)
    if act == "rst":
        vpsm.ssh_reset_usage(username)
        return ssh_detail(chat, mid, username)
    if act in ("lok", "unl"):
        vpsm.ssh_set_lock(username, act == "lok")
        sys_lock(username, act == "lok")
        return ssh_detail(chat, mid, username)
    if act == "con":
        return edit(chat, mid, connected_text("ssh", username),
                    [[btn("⬅️ Account", "ssh:v:" + username)], [btn("🏠 Main Menu", "home")]])
    if act == "del":
        return edit(chat, mid, "🗑 Delete SSH account <b>%s</b>? This also removes the system user." % username,
                    [[btn("✅ Yes, delete", "ssh:dely:" + username), btn("❌ Cancel", "ssh:v:" + username)]])
    return None


def xr_action(chat, mid, uid_, act):
    rec = next((u for u in load_xray() if u["id"] == uid_), None)
    if rec is None:
        return edit(chat, mid, "Account not found.", home_kb(role_of(chat)))
    if act == "ren":
        PENDING[chat] = ("renew_xr", uid_)
        return ask(chat, "🔄 Renew <b>%s</b> - send the number of days to ADD:" % rec["remark"])
    if act == "lim":
        PENDING[chat] = ("lim_xr_gb", uid_)
        return ask(chat, "📏 New limits for <b>%s</b> - send the <b>data limit in GB</b> (0 = Unlimited):" % rec["remark"])
    if act == "rst":
        vpsm.xray_reset_usage(uid_)
        sync()
        return xr_detail(chat, mid, uid_)
    if act in ("lok", "unl"):
        vpsm.xray_set_lock(uid_, act == "lok")
        sync()
        return xr_detail(chat, mid, uid_)
    if act == "con":
        return edit(chat, mid, connected_text("xray", uid_),
                    [[btn("⬅️ Account", "xr:v:" + uid_)], [btn("🏠 Main Menu", "home")]])
    if act == "del":
        return edit(chat, mid, "🗑 Delete %s account <b>%s</b>?" % (rec["protocol"].upper(), rec["remark"]),
                    [[btn("✅ Yes, delete", "xr:dely:" + uid_), btn("❌ Cancel", "xr:v:" + uid_)]])
    return None


# ---------------------------------------------------------------------------
# callback router
# ---------------------------------------------------------------------------
def on_callback(chat, mid, data, role):
    parts = data.split(":")
    head = parts[0]
    if data == "home":
        return edit(chat, mid, "🏠 <b>VPS Manager</b> - choose an action:", home_kb(role))
    if data == "status":
        return edit(chat, mid, status_text(), home_kb(role))
    if data == "ports":
        return edit(chat, mid, ports_text(), home_kb(role))
    if data == "ssh":
        return edit(chat, mid, "👤 <b>SSH Accounts</b>", ssh_list_kb())
    if data == "ssh:new":
        WIZ[chat] = {"kind": "ssh_new", "step": 0, "data": {}}
        return ask(chat, "➕ New SSH account - send the <b>username</b>:")
    if head == "ssh" and len(parts) >= 3 and parts[1] == "v":
        return ssh_detail(chat, mid, parts[2])
    if head == "ssh" and len(parts) >= 4 and parts[1] == "a":
        return ssh_action(chat, mid, parts[2], parts[3])
    if head == "ssh" and len(parts) >= 3 and parts[1] == "dely":
        vpsm.ssh_delete(parts[2])
        sys_delete(parts[2])
        return edit(chat, mid, "🗑 SSH account <b>%s</b> deleted." % parts[2], ssh_list_kb())
    if data == "xr":
        return edit(chat, mid, "🚀 <b>Xray Accounts</b> - choose a protocol:", xr_proto_kb())
    if head == "xr" and len(parts) >= 3 and parts[1] == "l":
        return edit(chat, mid, "🚀 <b>%s Accounts</b>" % parts[2].upper(), xr_list_kb(parts[2]))
    if head == "xr" and len(parts) >= 3 and parts[1] == "new":
        WIZ[chat] = {"kind": "xr_new", "step": 0, "data": {"proto": parts[2]}}
        return ask(chat, "➕ New %s account - send a <b>name</b> for it:" % parts[2].upper())
    if head == "xr" and len(parts) >= 3 and parts[1] == "v":
        return xr_detail(chat, mid, parts[2])
    if head == "xr" and len(parts) >= 4 and parts[1] == "a":
        return xr_action(chat, mid, parts[2], parts[3])
    if head == "xr" and len(parts) >= 3 and parts[1] == "dely":
        rec = vpsm.xray_delete(parts[2])
        sync()
        return edit(chat, mid, "🗑 Account <b>%s</b> deleted." % (rec["remark"] if rec else parts[2]), home_kb(role))
    if data == "dom":
        c = conf()
        return edit(chat, mid, "🌐 <b>Domain</b>\nCurrent: <code>%s</code>\nLocation: %s" % (
            c.get("domain") or "-", vpsm.server_location()),
            [[btn("✏️ Change Domain", "dom:chg")], [btn("🏠 Main Menu", "home")]])
    if data == "dom:chg":
        PENDING[chat] = ("domain",)
        return ask(chat, "Send the new domain (e.g. my.example.com):")
    if data == "ban":
        c = conf()
        return edit(chat, mid, "🎌 <b>SSH Banner</b>\nCurrent: <code>%s</code>" % (c.get("banner") or "-"),
                    [[btn("✏️ Change Banner", "ban:chg")], [btn("🏠 Main Menu", "home")]])
    if data == "ban:chg":
        PENDING[chat] = ("banner",)
        return ask(chat, "Send the new SSH banner text:")
    if data == "qos":
        rc, out = sh(["bash", os.path.join(BASE, "scripts", "qos.sh"), "status"], 30)
        return edit(chat, mid, "📶 <b>QoS</b>\n<pre>%s</pre>" % (out.strip()[:1500] or "no output"),
                    [[btn("▶️ Apply QoS", "qos:apply"), btn("⏹ Remove QoS", "qos:off")],
                     [btn("🎚 Per-user Cap", "qos:cap")], [btn("🏠 Main Menu", "home")]])
    if data == "qos:apply":
        return run_long(chat, ["bash", os.path.join(BASE, "scripts", "qos.sh"), "apply"], "QoS apply")
    if data == "qos:off":
        return run_long(chat, ["bash", os.path.join(BASE, "scripts", "qos.sh"), "off"], "QoS remove")
    if data == "qos:cap":
        PENDING[chat] = ("qos_cap",)
        return ask(chat, "Send the per-user cap in Mbps (0 = no per-user cap). QoS is re-applied after:")
    if data == "sys":
        kb = [[btn("🔧 Fix Stack", "sys:fix"), btn("♻️ Restart All", "sys:rall")],
              [btn("🔐 Issue / Renew SSL", "sys:ssl"), btn("📦 Install All", "sys:install")]]
        for s in SERVICES:
            kb.append([btn("🔁 Restart %s" % s, "sys:rsvc:" + s)])
        kb.append([btn("🏠 Main Menu", "home")])
        return edit(chat, mid, "🛠 <b>System</b>", kb)
    if data == "sys:fix":
        return run_long(chat, ["bash", os.path.join(BASE, "scripts", "fix-stack.sh")], "Fix Stack")
    if data == "sys:rall":
        for s in SERVICES:
            sh(["systemctl", "restart", s], 60)
        return edit(chat, mid, "♻️ All services restarted.\n\n" + status_text(), home_kb(role))
    if data == "sys:ssl":
        script = os.path.join(BASE, "scripts", "ssl.sh")
        if os.path.exists(script):
            return run_long(chat, ["bash", script, "issue"], "SSL issue / renew")
        return send(chat, "SSL helper not found - use terminal menu option 03.")
    if data == "sys:install":
        if role != "super":
            return send(chat, "⛔ Install All is Super Admin only.")
        return run_long(chat, ["bash", os.path.join(BASE, "install.sh")], "Install All", timeout=1200)
    if head == "sys" and len(parts) >= 3 and parts[1] == "rsvc":
        rc, _o = sh(["systemctl", "restart", parts[2]], 60)
        return edit(chat, mid, "🔁 %s restart %s.\n\n%s" % (parts[2], "OK" if rc == 0 else "FAILED", status_text()), home_kb(role))
    if data == "adm":
        if role != "super":
            return send(chat, "⛔ Super Admin only.")
        ids = admin_ids()
        kb = [[btn("🗑 Remove %s" % i, "adm:del:" + i)] for i in ids]
        kb.append([btn("➕ Add Admin", "adm:add")])
        kb.append([btn("🏠 Main Menu", "home")])
        return edit(chat, mid, "👥 <b>Bot Admins</b>\nSuper Admin: <code>%s</code>\nAdmins: %s" % (
            super_id(), ", ".join(ids) if ids else "none"), kb)
    if head == "adm" and len(parts) >= 3 and parts[1] == "del":
        if role != "super":
            return send(chat, "⛔ Super Admin only.")
        ids = [i for i in admin_ids() if i != parts[2]]
        save_conf_key("telegram_bot_admins", ids)
        return edit(chat, mid, "👥 Admin %s removed." % parts[2], home_kb(role))
    if data == "adm:add":
        if role != "super":
            return send(chat, "⛔ Super Admin only.")
        PENDING[chat] = ("admin_add",)
        return ask(chat, "Send the Telegram chat ID of the new admin (they can get it from @userinfobot):")
    return None


# ---------------------------------------------------------------------------
# update loop
# ---------------------------------------------------------------------------
def on_message(chat, text, role):
    t = (text or "").strip()
    if t in ("/start", "/menu"):
        WIZ.pop(chat, None)
        PENDING.pop(chat, None)
        return send(chat, "👋 <b>VPS Manager Bot</b>\nChoose an action:", home_kb(role))
    if t == "/cancel":
        WIZ.pop(chat, None)
        PENDING.pop(chat, None)
        return send(chat, "Cancelled.", home_kb(role))
    if wizard_text(chat, t):
        return None
    if pending_text(chat, t):
        return None
    return send(chat, "Use /start to open the menu.", home_kb(role))


def main():
    if not token():
        print("[bot] No bot token configured (panel Settings or telegram_bot_token). Exiting.", flush=True)
        return 1
    if not super_id():
        print("[bot] WARNING: no Super Admin chat id configured - nobody can use the bot.", flush=True)
    print("[bot] started; super admin set: %s" % bool(super_id()), flush=True)
    offset = 0
    while True:
        res = api("getUpdates", offset=offset, timeout=25)
        if res is None:
            import time as _t
            _t.sleep(3)
            continue
        for upd in res:
            offset = upd["update_id"] + 1
            try:
                if "callback_query" in upd:
                    q = upd["callback_query"]
                    chat = q["message"]["chat"]["id"]
                    role = role_of(chat)
                    api("answerCallbackQuery", callback_query_id=q["id"])
                    if role:
                        on_callback(chat, q["message"]["message_id"], q.get("data", ""), role)
                elif "message" in upd:
                    m = upd["message"]
                    chat = m["chat"]["id"]
                    role = role_of(chat)
                    if role:
                        on_message(chat, m.get("text", ""), role)
            except Exception as e:
                print("[bot] update error: %s" % e, flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
