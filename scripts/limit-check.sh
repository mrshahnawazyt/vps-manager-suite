#!/usr/bin/env bash
# Limit monitor - REAL ENFORCEMENT (user-confirmed semantics)
# Days = literal (expiry = created + days). GB 0 => Unlimited (skip), IP 0 => Unlimited (skip)
# Limits > 0 are REALLY enforced:
#   - Expiry passed   -> account DISABLED (SSH: lock + kill sessions; Xray: removed from config)
#   - GB quota full   -> account DISABLED (same way)
#   - Over IP limit   -> SSH: extra sessions killed; Xray: 30 min block + removed from config
# Traffic accounting: Xray = stats API (127.0.0.1:10085), SSH = iptables UID counters.
# Counters reset on restart — used_gb is CUMULATIVE (last_counter + delta).
# Cron: */10 * * * * root bash scripts/limit-check.sh
set -u
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
source "$SCRIPT_DIR/lib/common.sh" 2>/dev/null || true
DATA="${VPSMGR_DATA_DIR:-/var/lib/vps-manager}"
# Take both account files from wherever they exist (previously, if the ssh
# file was missing, the data dir switched incorrectly)
if [[ ! -f "$DATA/ssh_users.json" && ! -f "$DATA/xray_users.json" && -d "$SCRIPT_DIR/data" ]]; then
  DATA="$SCRIPT_DIR/data"
fi
echo "[limit-check] $(date -Is) data=$DATA"
python3 - "$DATA" "$SCRIPT_DIR" <<'PY'
import json, os, re, sys, pwd, shutil, signal, subprocess, datetime

DATA_DIR = sys.argv[1]
SUITE_DIR = sys.argv[2]
GB = 1_000_000_000  # 1 GB = 1e9 bytes (decimal)
NOW = datetime.datetime.now()
TODAY = datetime.date.today()
STATS_SERVER = os.environ.get("VPSMGR_XRAY_STATS", "127.0.0.1:10085")
ACCESS_LOG = os.environ.get("VPSMGR_XRAY_ACCESS_LOG", "/var/log/xray/access.log")
CHAIN = "VPSMGR_ACCT"
SSH_PORTS = {"22", "109", "143"}
USER_RE = re.compile(r"^[A-Za-z0-9_.\-]+$")

def load(name):
    p = os.path.join(DATA_DIR, name)
    if os.path.exists(p):
        try:
            with open(p) as f: return json.load(f)
        except Exception: return []
    return []

def save(name, rows):
    p = os.path.join(DATA_DIR, name)
    tmp = p + ".tmp"
    with open(tmp, "w") as f: json.dump(rows, f, indent=2)
    os.replace(tmp, p)

def to_int(v, d=0):
    try: return int(float(str(v).strip()))
    except Exception: return d

def to_float(v, d=0.0):
    try: return float(str(v).strip())
    except Exception: return d

def fmt_lim(v):
    f = to_float(v)
    return str(int(f)) if f == int(f) else ("%.4g" % f)

def is_expired(rec):
    """Days literal: aaj > created + expiry_days to expired."""
    try:
        base = datetime.date.fromisoformat(str(rec.get("created", ""))[:10])
        return TODAY > base + datetime.timedelta(days=to_int(rec.get("expiry_days", 30), 30))
    except Exception:
        return False

def stats_email(rec):
    """Same function as in sync-xray.py — must stay identical in both places."""
    e = re.sub(r"[^A-Za-z0-9@._\-]", "_", str(rec.get("remark") or rec.get("id") or ""))
    return e or str(rec.get("id") or "")

def run(cmd, timeout=15):
    try:
        return subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    except Exception:
        return None

# ---------------- disable actions ----------------

def mark_disabled(rec, reason):
    rec["disabled"] = True
    rec["disabled_reason"] = reason
    rec["disabled_at"] = NOW.isoformat(timespec="seconds")

def disable_ssh(rec, reason):
    """Disable an SSH account: lock the password + kill all its sessions. System accounts (uid<1000) are never touched."""
    mark_disabled(rec, reason)
    u = str(rec.get("username") or "")
    if not USER_RE.match(u):
        return
    try:
        ent = pwd.getpwnam(u)
    except Exception:
        return  # not a system user — record-level disable is enough
    if ent.pw_uid < 1000:
        return
    run(["usermod", "-L", u])
    run(["pkill", "-u", str(ent.pw_uid)])

# ---------------- Xray traffic accounting (stats API) ----------------

def xray_stats_totals():
    """{email: total_bytes} — xray api stats se. Binary/API na ho to {} (graceful)."""
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
            try: totals[parts[1]] = totals.get(parts[1], 0) + int(val)
            except Exception: pass
    return totals

def apply_delta(rec, cur_bytes):
    """Cumulative accounting: counter reset ho jaye to current value hi delta hai."""
    last = rec.get("last_counter")
    if last is None:
        delta = int(cur_bytes)
    else:
        last = int(last)
        delta = int(cur_bytes) - last if int(cur_bytes) >= last else int(cur_bytes)
    if delta > 0:
        rec["used_gb"] = round(float(rec.get("used_gb") or 0) + delta / GB, 6)
    rec["last_counter"] = int(cur_bytes)

# ---------------- SSH traffic accounting (iptables UID counters) ----------------

def ssh_counters(uids):
    """{uid: output_bytes} — VPSMGR_ACCT chain, OUTPUT par owner-match rules.
    Root/iptables na ho ya rule fail ho to {} (graceful skip, used_gb waisa hi)."""
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
                try: rows.append(int(parts[1]))
                except Exception: pass
        if len(rows) != len(order):
            return {}
        return dict(zip(order, rows))
    except Exception:
        return {}

# ---------------- SSH IP enforcement (live sessions) ----------------

def ssh_sessions():
    """{uid: [{ip, pid, start}]} — sirf SSH ports (22/109/143) par, sirf sshd/dropbear processes."""
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
    """If distinct IPs exceed the limit, keep the oldest `limit` IPs and kill the remaining sessions."""
    lim = to_int(rec.get("ip_limit", 0))
    if lim <= 0:
        return ""
    try:
        uid = pwd.getpwnam(str(rec.get("username") or "")).pw_uid
    except Exception:
        return ""
    conns = sessions.get(uid, [])
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
    for c in sorted(conns, key=lambda c: -c["start"]):  # newest first
        if c["ip"] not in allowed:
            try:
                os.kill(c["pid"], signal.SIGTERM); killed += 1
            except Exception:
                pass
    return note + " killed=%d" % killed

# ---------------- Xray IP enforcement (access log) ----------------

def xray_recent_ips():
    """{email: {ip,...}} — pichle 10 minute ke access log se. Log na ho to {}."""
    if not os.path.exists(ACCESS_LOG):
        return {}
    cutoff = NOW - datetime.timedelta(minutes=10)
    out = {}
    try:
        with open(ACCESS_LOG, errors="ignore") as f:
            lines = f.read().splitlines()[-5000:]
    except Exception:
        return {}
    for line in lines:
        m = re.match(r"(\d{4}/\d{2}/\d{2}) (\d{2}:\d{2}:\d{2})", line)
        if not m:
            continue
        try:
            ts = datetime.datetime.strptime(m.group(1) + " " + m.group(2), "%Y/%m/%d %H:%M:%S")
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
        return NOW < datetime.datetime.fromisoformat(str(until))
    except Exception:
        return False

# ---------------- sync trigger ----------------

need_sync = False

def trigger_sync():
    for cand in (os.path.join(SUITE_DIR, "scripts", "sync-xray.py"),
                 "/opt/vps-manager-suite/scripts/sync-xray.py"):
        if os.path.exists(cand):
            r = run([sys.executable, cand, "--restart"], timeout=90)
            if r is not None and r.stdout:
                last = r.stdout.strip().splitlines()
                if last:
                    print("[Xray] sync: " + last[-1][:90])
            return

# ---------------- main ----------------

def fmt_used(rec):
    try: return "%.2f" % float(rec.get("used_gb") or 0)
    except Exception: return "0.00"

def main():
    global need_sync
    # ---- SSH accounts ----
    ssh = load("ssh_users.json")
    ssh_before = json.dumps(ssh, sort_keys=True)
    # system UIDs for accounting (only users that actually exist)
    uid_by_name = {}
    for u in ssh:
        if u.get("disabled") is True:
            continue
        try:
            uid_by_name[str(u.get("username"))] = pwd.getpwnam(str(u.get("username"))).pw_uid
        except Exception:
            pass
    counters = ssh_counters(set(uid_by_name.values()))
    sessions = None  # lazy — sirf zaroorat par
    for u in ssh:
        who = u.get("username") or "?"
        if u.get("disabled") is True:
            print("[SSH] %s: DISABLED (%s)" % (who, u.get("disabled_reason", "?")))
            continue
        if is_expired(u):
            disable_ssh(u, "Expired")
            print("[SSH] %s: DISABLED (Expired)" % who)
            continue
        uid = uid_by_name.get(str(who))
        if uid is not None and uid in counters:
            apply_delta(u, counters[uid])
        gl = to_float(u.get("gb_limit", 0))
        if gl > 0 and float(u.get("used_gb") or 0) >= gl:
            disable_ssh(u, "Quota")
            print("[SSH] %s: DISABLED (Quota %s/%s GB)" % (who, fmt_used(u), fmt_lim(gl)))
            continue
        note = ""
        if to_int(u.get("ip_limit", 0)) > 0:
            if sessions is None:
                sessions = ssh_sessions()
            note = enforce_ssh_ip(u, sessions)
        print("[SSH] %s: Active used=%sGB%s" % (who, fmt_used(u), note))
    if json.dumps(ssh, sort_keys=True) != ssh_before:
        save("ssh_users.json", ssh)

    # ---- Xray accounts ----
    xr = load("xray_users.json")
    xr_before = json.dumps(xr, sort_keys=True)
    totals = xray_stats_totals()
    recent_ips = None
    for u in xr:
        who = u.get("remark") or u.get("id") or "?"
        if u.get("disabled") is True:
            print("[Xray] %s: DISABLED (%s)" % (who, u.get("disabled_reason", "?")))
            continue
        # Clear an expired IP block (the user returns to the synced config)
        if u.get("ip_blocked_until") and not ip_block_active(u):
            u.pop("ip_blocked_until", None)
            need_sync = True
        if is_expired(u):
            mark_disabled(u, "Expired")
            need_sync = True
            print("[Xray] %s: DISABLED (Expired)" % who)
            continue
        email = stats_email(u)
        if email in totals:
            apply_delta(u, totals[email])
        gl = to_float(u.get("gb_limit", 0))
        if gl > 0 and float(u.get("used_gb") or 0) >= gl:
            mark_disabled(u, "Quota")
            need_sync = True
            print("[Xray] %s: DISABLED (Quota %s/%s GB)" % (who, fmt_used(u), fmt_lim(gl)))
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
                u["ip_blocked_until"] = (NOW + datetime.timedelta(minutes=30)).isoformat(timespec="seconds")
                u["ip_block_reason"] = "IP Limit"
                need_sync = True
                note += " -> BLOCKED 30m"
        print("[Xray] %s: Active used=%sGB%s" % (who, fmt_used(u), note))
    if json.dumps(xr, sort_keys=True) != xr_before:
        save("xray_users.json", xr)

    if need_sync:
        trigger_sync()

try:
    main()
except Exception as e:
    print("[limit-check] ERROR: %s (some action may be incomplete)" % e)
PY
echo "[limit-check] done. GB/IP limit 0 = Unlimited (skip); >0 is really enforced."
