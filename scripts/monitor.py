#!/usr/bin/env python3
"""VPS Manager Suite - fast limit monitor (original code)

One enforcement pass (also used by limit-check.sh --once):

  SSH + Xray accounts
    - Expiry passed (Days are a LITERAL count) -> DISABLE
      (SSH: password lock + session kill; Xray: removed from the config)
    - GB quota reached (limit 0 = Unlimited, skipped) -> DISABLE
    - IP limit exceeded (limit 0 = Unlimited, skipped)
      SSH: the newest sessions are killed; Xray: 30-minute block
    - Accounts disabled longer than the grace period
      (config auto_delete_hours, default 24; 0 = never) are DELETED

  Traffic accounting
    - Xray: stats API counters (cumulative, reset-safe deltas)
    - SSH: per-UID iptables OUTPUT counters (best effort)

  Connected tracking (connected.json, read by menu / panel / bot)
    - SSH sessions from `ss`, login IPs from the auth log, and the real
      client IPs the ws-proxy records for WebSocket logins (correlated
      by time, because Dropbear itself only sees the proxy)
    - Xray client IPs from the access log (recent window)

Loop mode (the vps-monitor service) runs a pass every
VPSMGR_MONITOR_INTERVAL seconds (default 4, config monitor_interval).

Env: VPSMGR_DATA_DIR, VPSMGR_XRAY_STATS, VPSMGR_XRAY_ACCESS_LOG,
     VPSMGR_AUTH_LOG, VPSMGR_MONITOR_INTERVAL
"""
import datetime, json, os, pwd, re, shutil, signal, subprocess, sys, time

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(BASE, "scripts"))
try:
    import vpsm
except Exception:
    vpsm = None

DATA_DIR = os.environ.get("VPSMGR_DATA_DIR", "/var/lib/vps-manager")
GB = 1_000_000_000
STATS_SERVER = os.environ.get("VPSMGR_XRAY_STATS", "127.0.0.1:10085")
ACCESS_LOG = os.environ.get("VPSMGR_XRAY_ACCESS_LOG", "/var/log/xray/access.log")
AUTH_LOG = os.environ.get("VPSMGR_AUTH_LOG", "/var/log/auth.log")
CHAIN = "VPSMGR_ACCT"
SSH_PORTS = {"22", "109", "143"}
USER_RE = re.compile(r"^[A-Za-z0-9_.\-]+$")
CONNECTED_WINDOW_MIN = 10


def now():
    return datetime.datetime.now()


def today():
    return datetime.date.today()


def load(name, default):
    p = os.path.join(DATA_DIR, name)
    if os.path.exists(p):
        try:
            with open(p) as f:
                return json.load(f)
        except Exception:
            return default
    return default


def save(name, data):
    p = os.path.join(DATA_DIR, name)
    tmp = p + ".tmp"
    with open(tmp, "w") as f:
        json.dump(data, f, indent=2)
    os.replace(tmp, p)


def to_int(v, d=0):
    try:
        return int(float(str(v).strip()))
    except Exception:
        return d


def to_float(v, d=0.0):
    try:
        return float(str(v).strip())
    except Exception:
        return d


def run(cmd, timeout=15):
    try:
        return subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    except Exception:
        return None


def cfg():
    c = load("config.json", {})
    return c if isinstance(c, dict) else {}


def is_expired(rec):
    try:
        base = datetime.date.fromisoformat(str(rec.get("created", ""))[:10])
        return today() > base + datetime.timedelta(days=to_int(rec.get("expiry_days", 30), 30))
    except Exception:
        return False


def stats_email(rec):
    e = re.sub(r"[^A-Za-z0-9@._\-]", "_", str(rec.get("remark") or rec.get("id") or ""))
    return e or str(rec.get("id") or "")


# ---------------------------------------------------------------------------
# disable / delete actions
# ---------------------------------------------------------------------------
def mark_disabled(rec, reason):
    rec["disabled"] = True
    rec["disabled_reason"] = reason
    rec["disabled_at"] = now().isoformat(timespec="seconds")


def _sys_user(username):
    """pwd entry for a suite-managed system user, or None (never uid<1000)."""
    if not USER_RE.match(username or ""):
        return None
    try:
        ent = pwd.getpwnam(username)
    except Exception:
        return None
    return ent if ent.pw_uid >= 1000 else None


def disable_ssh(rec, reason):
    mark_disabled(rec, reason)
    ent = _sys_user(str(rec.get("username") or ""))
    if ent is None:
        return
    run(["usermod", "-L", ent.pw_name])
    run(["pkill", "-u", str(ent.pw_uid)])


def delete_ssh_system(username):
    ent = _sys_user(username)
    if ent is not None:
        run(["userdel", ent.pw_name])


# ---------------------------------------------------------------------------
# traffic accounting
# ---------------------------------------------------------------------------
def xray_stats_totals():
    xray = shutil.which("xray")
    if not xray:
        return {}
    r = run([xray, "api", "stats", "-server=" + STATS_SERVER], timeout=10)
    if r is None or not r.stdout:
        return {}
    pairs = []
    try:
        doc = json.loads(r.stdout)
        for s in doc.get("stat", []):
            pairs.append((str(s.get("name", "")), s.get("value", 0)))
    except Exception:
        for m in re.finditer(r'"name"\s*:\s*"([^"]+)"\s*,\s*"value"\s*:\s*"?(\d+)"?', r.stdout):
            pairs.append((m.group(1), int(m.group(2))))
    totals = {}
    for name, val in pairs:
        parts = name.split(">>>")
        if len(parts) == 4 and parts[0] == "user" and parts[2] == "traffic":
            try:
                totals[parts[1]] = totals.get(parts[1], 0) + int(val)
            except Exception:
                pass
    return totals


def apply_delta(rec, cur_bytes):
    """Cumulative accounting. A record flagged reset_counter (usage was
    reset from a front-end) re-bases here instead of adding the counter."""
    cur = int(cur_bytes)
    if rec.pop("reset_counter", None):
        rec["last_counter"] = cur
        return
    last = rec.get("last_counter")
    if last is None:
        delta = cur
    else:
        last = int(last)
        delta = cur - last if cur >= last else cur
    if delta > 0:
        rec["used_gb"] = round(float(rec.get("used_gb") or 0) + delta / GB, 6)
    rec["last_counter"] = cur


def ssh_counters(uids):
    """{uid: output_bytes} from the VPSMGR_ACCT iptables chain (best effort)."""
    if os.geteuid() != 0 or not shutil.which("iptables") or not uids:
        return {}

    def ipt(args):
        return run(["iptables"] + args, timeout=10)

    try:
        r = ipt(["-L", CHAIN, "-n"])
        if r is None or r.returncode != 0:
            r = ipt(["-N", CHAIN])
            if r is None or r.returncode != 0:
                return {}
        r = ipt(["-C", "OUTPUT", "-j", CHAIN])
        if r is None:
            return {}
        if r.returncode != 0:
            ipt(["-A", "OUTPUT", "-j", CHAIN])
        r = ipt(["-S", CHAIN])
        if r is None or r.returncode != 0:
            return {}
        have = {int(m.group(1)) for m in re.finditer(r"--uid-owner (\d+)", r.stdout)}
        for uid in uids:
            if uid not in have:
                ipt(["-A", CHAIN, "-m", "owner", "--uid-owner", str(uid),
                     "-m", "comment", "--comment", "vpsmgr-acct", "-j", "RETURN"])
        r = ipt(["-S", CHAIN])
        if r is None or r.returncode != 0:
            return {}
        order = [int(m.group(1)) for m in
                 (re.search(r"--uid-owner (\d+)", ln) for ln in r.stdout.splitlines()) if m]
        r = ipt(["-L", CHAIN, "-v", "-x", "-n"])
        if r is None or r.returncode != 0:
            return {}
        rows = []
        for ln in r.stdout.splitlines():
            parts = ln.split()
            if parts and parts[0].isdigit():
                try:
                    rows.append(int(parts[1]))
                except Exception:
                    pass
        if len(rows) != len(order):
            return {}
        return dict(zip(order, rows))
    except Exception:
        return {}


# ---------------------------------------------------------------------------
# SSH sessions + IP enforcement
# ---------------------------------------------------------------------------
def ssh_sessions():
    """{uid: [{ip, pid, start}]} - established sshd/dropbear sessions only."""
    if not shutil.which("ss"):
        return {}
    r = run(["ss", "-tnp", "state", "established"], timeout=10)
    if r is None or not r.stdout:
        return {}
    out = {}
    for line in r.stdout.splitlines():
        parts = line.split()
        if len(parts) < 5:
            continue
        m = re.search(r"pid=(\d+)", line)
        if not m:
            continue
        if parts[3].rsplit(":", 1)[-1] not in SSH_PORTS:
            continue
        pid = int(m.group(1))
        ip = parts[4].rsplit(":", 1)[0].strip("[]")
        try:
            uid = os.stat("/proc/%d" % pid).st_uid
            cmd = open("/proc/%d/cmdline" % pid, "rb").read().decode(errors="ignore")
            if "sshd" not in cmd and "dropbear" not in cmd:
                continue
            stat = open("/proc/%d/stat" % pid).read()
            start = int(stat.rsplit(")", 1)[1].split()[19])
        except Exception:
            continue
        out.setdefault(uid, []).append({"ip": ip, "pid": pid, "start": start})
    return out


def enforce_ssh_ip(rec, sessions):
    lim = to_int(rec.get("ip_limit", 0))
    if lim <= 0:
        return ""
    ent = _sys_user(str(rec.get("username") or ""))
    if ent is None:
        return ""
    conns = sessions.get(ent.pw_uid, [])
    if not conns:
        return ""
    first_seen = {}
    for c in conns:
        first_seen[c["ip"]] = min(first_seen.get(c["ip"], c["start"]), c["start"])
    ips = sorted(first_seen, key=lambda ip: first_seen[ip])
    note = " IPs=%d/%d" % (len(ips), lim)
    if len(ips) <= lim:
        return note
    allowed = set(ips[:lim])
    killed = 0
    for c in sorted(conns, key=lambda c: -c["start"]):
        if c["ip"] not in allowed:
            try:
                os.kill(c["pid"], signal.SIGTERM)
                killed += 1
            except Exception:
                pass
    return note + " killed=%d" % killed


# ---------------------------------------------------------------------------
# log following: auth log, ws-proxy connections, xray access log
# ---------------------------------------------------------------------------
def _follow(path, state, key, max_bytes=2_000_000):
    """Read only the new bytes of a log file since the last pass.
    Returns the new text; rotation/truncation resets the offset."""
    try:
        size = os.path.getsize(path)
    except Exception:
        return ""
    off = int(state.get(key, 0))
    if off > size or size - off > max_bytes:
        off = max(0, size - max_bytes) if size > max_bytes else 0
        if key not in state:
            off = size  # first sight: start at the end, history is not replayed
    try:
        with open(path, "rb") as f:
            f.seek(off)
            data = f.read()
        state[key] = off + len(data)
        return data.decode("utf-8", "ignore")
    except Exception:
        return ""


SSHD_OK = re.compile(r"Accepted (?:password|publickey) for (\S+) from (\d{1,3}(?:\.\d{1,3}){3})")
DROP_OK = re.compile(r"Password auth succeeded for '([^']+)' from (\d{1,3}(?:\.\d{1,3}){3})")
LOG_TS = re.compile(r"^(\w{3})\s+(\d{1,2}) (\d{2}):(\d{2}):(\d{2})")
MONTHS = {m: i + 1 for i, m in enumerate(
    ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"])}


def _syslog_ts(line):
    m = LOG_TS.match(line)
    if not m:
        return None
    try:
        return datetime.datetime(today().year, MONTHS[m.group(1)], int(m.group(2)),
                                 int(m.group(3)), int(m.group(4)), int(m.group(5)))
    except Exception:
        return None


def harvest_auth(text, auth_seen, ws_events):
    """auth_seen: {user: {ip: ts_epoch}} - successful SSH logins.
    Loopback logins (WebSocket users) are matched to the real IP the
    ws-proxy saw connecting at nearly the same moment."""
    for line in text.splitlines():
        m = SSHD_OK.search(line) or DROP_OK.search(line)
        if not m:
            continue
        user, ip = m.group(1), m.group(2)
        ts = _syslog_ts(line) or now()
        epoch = ts.timestamp()
        if ip.startswith("127."):
            real = ""
            best = None
            for ev in ws_events:
                d = abs(ev.get("ts", 0) - epoch)
                if d <= 20 and (best is None or d < best):
                    best, real = d, ev.get("ip", "")
            ip = real or ip
        if ip:
            auth_seen.setdefault(user, {})[ip] = epoch


def harvest_ws(text, ws_events):
    for line in text.splitlines():
        try:
            ev = json.loads(line)
        except Exception:
            continue
        if ev.get("ip") and ev.get("ts"):
            ws_events.append({"ip": ev["ip"], "ts": float(ev["ts"])})
    cutoff = time.time() - 3600
    ws_events[:] = [e for e in ws_events if e["ts"] >= cutoff][-2000:]


XR_LINE_TS = re.compile(r"^(\d{4})/(\d{2})/(\d{2}) (\d{2}):(\d{2}):(\d{2})")


def harvest_xray(text, xr_seen):
    """xr_seen: {email: {ip: ts_epoch}} from the Xray access log."""
    for line in text.splitlines():
        m = XR_LINE_TS.match(line)
        if not m:
            continue
        try:
            ts = datetime.datetime(int(m.group(1)), int(m.group(2)), int(m.group(3)),
                                   int(m.group(4)), int(m.group(5)), int(m.group(6))).timestamp()
        except Exception:
            continue
        em = re.search(r"email: (\S+)", line)
        ipm = re.search(r"\b(\d{1,3}(?:\.\d{1,3}){3}):\d+\b", line)
        if em and ipm:
            xr_seen.setdefault(em.group(1), {})[ipm.group(1)] = ts


def xray_recent_ips():
    """{email: {ip,...}} inside the enforcement window (10 minutes)."""
    if not os.path.exists(ACCESS_LOG):
        return {}
    cutoff = now() - datetime.timedelta(minutes=10)
    out = {}
    try:
        with open(ACCESS_LOG, errors="ignore") as f:
            lines = f.read().splitlines()[-5000:]
    except Exception:
        return {}
    for line in lines:
        m = XR_LINE_TS.match(line)
        if not m:
            continue
        try:
            ts = datetime.datetime(int(m.group(1)), int(m.group(2)), int(m.group(3)),
                                   int(m.group(4)), int(m.group(5)), int(m.group(6)))
        except Exception:
            continue
        if ts < cutoff:
            continue
        em = re.search(r"email: (\S+)", line)
        ipm = re.search(r"\b(\d{1,3}(?:\.\d{1,3}){3}):\d+\b", line)
        if em and ipm:
            out.setdefault(em.group(1), set()).add(ipm.group(1))
    return out


def ip_block_active(rec):
    until = rec.get("ip_blocked_until")
    if not until:
        return False
    try:
        return now() < datetime.datetime.fromisoformat(str(until))
    except Exception:
        return False


# ---------------------------------------------------------------------------
# sync trigger
# ---------------------------------------------------------------------------
need_sync = False


def trigger_sync(log):
    for cand in (os.path.join(BASE, "scripts", "sync-xray.py"),
                 "/opt/vps-manager-suite/scripts/sync-xray.py"):
        if os.path.exists(cand):
            r = run([sys.executable, cand, "--restart"], timeout=90)
            if r is not None and r.stdout:
                last = r.stdout.strip().splitlines()
                if last:
                    log("[Xray] sync: " + last[-1][:90])
            return


# ---------------------------------------------------------------------------
# one enforcement pass
# ---------------------------------------------------------------------------
def fmt_used(rec):
    try:
        return "%.2f" % float(rec.get("used_gb") or 0)
    except Exception:
        return "0.00"


def prune(seen, window_min=CONNECTED_WINDOW_MIN):
    cutoff = time.time() - window_min * 60
    for k in list(seen):
        ips = seen[k]
        for ip in list(ips):
            if ips[ip] < cutoff:
                del ips[ip]
        if not ips:
            del seen[k]


def run_pass(log=print):
    global need_sync
    need_sync = False
    conf = cfg()
    grace = to_int(conf.get("auto_delete_hours", 24), 24)
    state = load("monitor_state.json", {})
    if not isinstance(state, dict):
        state = {}
    auth_seen = state.get("auth_seen") or {}
    xr_seen = state.get("xr_seen") or {}
    ws_events = state.get("ws_events") or []

    # ---- follow logs (real IPs + connected tracking) ----
    harvest_ws(_follow(os.path.join(DATA_DIR, "ws_connections.log"), state, "ws_off"), ws_events)
    harvest_auth(_follow(AUTH_LOG, state, "auth_off"), auth_seen, ws_events)
    harvest_xray(_follow(ACCESS_LOG, state, "xr_off"), xr_seen)
    prune(auth_seen)
    prune(xr_seen)

    # ---- SSH accounts ----
    ssh = load("ssh_users.json", [])
    ssh_before = json.dumps(ssh, sort_keys=True)
    uid_by_name = {}
    for u in ssh:
        if u.get("disabled") is True:
            continue
        ent = _sys_user(str(u.get("username") or ""))
        if ent is not None:
            uid_by_name[str(u.get("username"))] = ent.pw_uid
    counters = ssh_counters(set(uid_by_name.values()))
    sessions = None
    keep = []
    for u in ssh:
        who = str(u.get("username") or "?")
        if u.get("disabled") is True:
            # auto-delete after the grace period
            if grace > 0 and u.get("disabled_at"):
                try:
                    at = datetime.datetime.fromisoformat(str(u["disabled_at"]))
                    if now() - at > datetime.timedelta(hours=grace):
                        delete_ssh_system(who)
                        log("[SSH] %s: AUTO-DELETED (disabled > %dh)" % (who, grace))
                        continue
                except Exception:
                    pass
            keep.append(u)
            continue
        if is_expired(u):
            disable_ssh(u, "Expired")
            log("[SSH] %s: DISABLED (Expired)" % who)
            keep.append(u)
            continue
        uid = uid_by_name.get(who)
        if uid is not None and uid in counters:
            apply_delta(u, counters[uid])
        elif u.pop("reset_counter", None):
            pass  # no counter source: the reset simply takes effect
        gl = to_float(u.get("gb_limit", 0))
        if gl > 0 and float(u.get("used_gb") or 0) >= gl:
            disable_ssh(u, "Quota")
            log("[SSH] %s: DISABLED (Quota %s/%s GB)" % (who, fmt_used(u), gl))
            keep.append(u)
            continue
        note = ""
        if to_int(u.get("ip_limit", 0)) > 0:
            if sessions is None:
                sessions = ssh_sessions()
            note = enforce_ssh_ip(u, sessions)
        log("[SSH] %s: Active used=%sGB%s" % (who, fmt_used(u), note))
        keep.append(u)
    ssh = keep
    if json.dumps(ssh, sort_keys=True) != ssh_before:
        save("ssh_users.json", ssh)

    # ---- Xray accounts ----
    xr = load("xray_users.json", [])
    xr_before = json.dumps(xr, sort_keys=True)
    totals = xray_stats_totals()
    recent_ips = None
    keep = []
    for u in xr:
        who = u.get("remark") or u.get("id") or "?"
        if u.get("disabled") is True:
            if grace > 0 and u.get("disabled_at"):
                try:
                    at = datetime.datetime.fromisoformat(str(u["disabled_at"]))
                    if now() - at > datetime.timedelta(hours=grace):
                        need_sync = True
                        log("[Xray] %s: AUTO-DELETED (disabled > %dh)" % (who, grace))
                        continue
                except Exception:
                    pass
            keep.append(u)
            continue
        if u.get("ip_blocked_until") and not ip_block_active(u):
            u.pop("ip_blocked_until", None)
            need_sync = True
        if is_expired(u):
            mark_disabled(u, "Expired")
            need_sync = True
            log("[Xray] %s: DISABLED (Expired)" % who)
            keep.append(u)
            continue
        email = stats_email(u)
        if email in totals:
            apply_delta(u, totals[email])
        elif u.pop("reset_counter", None):
            pass
        gl = to_float(u.get("gb_limit", 0))
        if gl > 0 and float(u.get("used_gb") or 0) >= gl:
            mark_disabled(u, "Quota")
            need_sync = True
            log("[Xray] %s: DISABLED (Quota %s/%s GB)" % (who, fmt_used(u), gl))
            keep.append(u)
            continue
        note = ""
        lim = to_int(u.get("ip_limit", 0))
        if ip_block_active(u):
            note = " IP-BLOCKED"
        elif lim > 0:
            if recent_ips is None:
                recent_ips = xray_recent_ips()
            ips = recent_ips.get(email, set())
            note = " IPs=%d/%d" % (len(ips), lim)
            if len(ips) > lim:
                u["ip_blocked_until"] = (now() + datetime.timedelta(minutes=30)).isoformat(timespec="seconds")
                u["ip_block_reason"] = "IP Limit"
                need_sync = True
                note += " -> BLOCKED 30m"
        log("[Xray] %s: Active used=%sGB%s" % (who, fmt_used(u), note))
        keep.append(u)
    xr = keep
    if json.dumps(xr, sort_keys=True) != xr_before:
        save("xray_users.json", xr)

    if need_sync:
        trigger_sync(log)

    # ---- connected.json for the front-ends ----
    connected = {"updated": now().isoformat(timespec="seconds"), "ssh": {}, "xray": {}}
    name_by_uid = {}
    for u in ssh:
        ent = _sys_user(str(u.get("username") or ""))
        if ent is not None:
            name_by_uid[ent.pw_uid] = str(u.get("username"))
    live = ssh_sessions()
    for uid, conns in live.items():
        uname = name_by_uid.get(uid)
        if not uname:
            continue
        for c in conns:
            ip = c["ip"]
            if ip.startswith("127."):
                seen = auth_seen.get(uname) or {}
                if seen:
                    ip = max(seen, key=lambda k: seen[k])
            connected["ssh"].setdefault(uname, {})[ip] = {"country": "", "last": now().isoformat(timespec="seconds")}
    for uname, ips in auth_seen.items():
        for ip in ips:
            connected["ssh"].setdefault(uname, {}).setdefault(
                ip, {"country": "", "last": now().isoformat(timespec="seconds")})
    email_to_name = {}
    for u in xr:
        email_to_name[stats_email(u)] = "%s/%s" % (u.get("protocol", "?"), u.get("remark", "?"))
    for email, ips in xr_seen.items():
        name = email_to_name.get(email)
        if not name:
            continue
        for ip in ips:
            connected["xray"].setdefault(name, {})[ip] = {"country": "", "last": now().isoformat(timespec="seconds")}
    # geo labels (cached lookups, best effort - never blocks for long)
    if vpsm is not None:
        for section in ("ssh", "xray"):
            for _name, ips in connected[section].items():
                for ip, meta in ips.items():
                    try:
                        meta["country"] = vpsm.geo_country(ip)
                    except Exception:
                        meta["country"] = ""
    save("connected.json", connected)

    state["auth_seen"] = auth_seen
    state["xr_seen"] = xr_seen
    state["ws_events"] = ws_events
    save("monitor_state.json", state)
    return 0


def main():
    once = "--once" in sys.argv
    interval = to_int(os.environ.get("VPSMGR_MONITOR_INTERVAL", ""), 0)
    if interval <= 0:
        interval = to_int(cfg().get("monitor_interval", 4), 4)
    interval = max(2, interval)
    print("[monitor] %s data=%s interval=%ss" % (now().isoformat(timespec="seconds"), DATA_DIR, interval), flush=True)
    while True:
        try:
            run_pass()
        except Exception as e:
            print("[monitor] ERROR: %s (pass incomplete)" % e, flush=True)
        if once:
            return 0
        time.sleep(interval)


if __name__ == "__main__":
    sys.exit(main())
