#!/usr/bin/env python3
"""VPS Manager Suite - shared account store + link builder (original code)

One small library used by the terminal menu, the Telegram bot and the
maintenance scripts so that every front-end applies the SAME account rules:

  Days / Expiry : LITERAL count. Default 30. Expiry = creation + Days.
                  Days = 0 means the account expires on its creation day.
  GB limit      : 0 = Unlimited.  A value > 0 is really enforced.
  IP limit      : 0 = Unlimited.  A value > 0 is really enforced.

Records live in JSON files inside the data dir:
  ssh_users.json   - tunnel-only SSH accounts (real system users)
  xray_users.json  - VMess / VLESS / Trojan accounts

Every Xray account gets 5 links: TLS (WS), NTLS (WS), TCP (direct TLS),
XHTTP TLS and XHTTP NTLS.

CLI (used by menu.sh):
  python3 vpsm.py ssh-list | xray-list
  python3 vpsm.py links <uuid>            -> stored links of one xray account
  python3 vpsm.py build-links <proto> <id> <domain> <remark>
  python3 vpsm.py regen-links <domain>    -> rebuild every stored link set
  python3 vpsm.py server-location         -> "City, Region" (best effort)
"""
import base64, datetime, fcntl, json, os, re, sys, urllib.request

DATA_DIR = os.environ.get("VPSMGR_DATA_DIR", "/var/lib/vps-manager")

# --- transport map (single source of truth for ports and paths) -------------
WS_PATH = {"vmess": "/vmess", "vless": "/vless", "trojan": "/trojan"}
XHTTP_PATH = {"vmess": "/xhttp-vmess", "vless": "/xhttp", "trojan": "/xhttp-trojan"}
TCP_PORT = {"vless": 8443, "trojan": 8446, "vmess": 8447}
XHTTP_TLS_PORT = {"vless": 8444, "trojan": 8445, "vmess": 8448}
PROTOCOLS = ("vmess", "vless", "trojan")


# --------------------------------------------------------------------------
# small helpers
# --------------------------------------------------------------------------
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


def today():
    return datetime.date.today()


def parse_date(s):
    try:
        return datetime.date.fromisoformat(str(s)[:10])
    except Exception:
        return None


def expires_on(created, days):
    base = parse_date(created)
    if base is None:
        return ""
    return (base + datetime.timedelta(days=to_int(days, 30))).isoformat()


def is_expired(rec, on=None):
    exp = parse_date(rec.get("expires") or expires_on(rec.get("created", ""), rec.get("expiry_days", 30)))
    if exp is None:
        return False
    return (on or today()) > exp


def quota_hit(rec):
    lim = to_float(rec.get("gb_limit", 0))
    return lim > 0 and to_float(rec.get("used_gb", 0)) >= lim


def stats_email(rec):
    """Per-user tag for Xray stats/log attribution.
    sync-xray.py and the monitor use this exact same rule."""
    e = re.sub(r"[^A-Za-z0-9@._\-]", "_", str(rec.get("remark") or rec.get("id") or ""))
    return e or str(rec.get("id") or "")


STATUS_LABEL = {
    "Expired": "EXPIRED",
    "Quota": "GB-LIMIT",
    "IP Limit": "IP-LIMIT",
    "Locked": "LOCKED",
}


def display_status(rec, on=None):
    """Short status used in every list: Active / EXPIRED / GB-LIMIT / IP-LIMIT / LOCKED."""
    if rec.get("disabled"):
        return STATUS_LABEL.get(str(rec.get("disabled_reason") or ""), "LOCKED")
    if is_expired(rec, on):
        return "EXPIRED"
    if quota_hit(rec):
        return "GB-LIMIT"
    return "Active"


def dead_note(rec, grace_hours, now=None):
    """'auto-delete in 3h 12m' for a disabled record, else ''."""
    if not rec.get("disabled") or not grace_hours or grace_hours <= 0:
        return ""
    at = rec.get("disabled_at")
    if not at:
        return ""
    try:
        start = datetime.datetime.fromisoformat(str(at))
    except Exception:
        return ""
    now = now or datetime.datetime.now()
    left = start + datetime.timedelta(hours=grace_hours) - now
    secs = int(left.total_seconds())
    if secs <= 0:
        return "auto-delete due"
    if secs >= 3600:
        return "auto-delete in %dh %dm" % (secs // 3600, (secs % 3600) // 60)
    return "auto-delete in %dm" % max(1, secs // 60)


# --------------------------------------------------------------------------
# JSON store with an inter-process lock (menu, panel, bot, monitor share it)
# --------------------------------------------------------------------------
def _path(name):
    return os.path.join(DATA_DIR, name)


def load_store(name):
    p = _path(name)
    if os.path.exists(p):
        try:
            with open(p) as f:
                d = json.load(f)
            return d if isinstance(d, list) else []
        except Exception:
            return []
    return []


def save_store(name, rows):
    os.makedirs(DATA_DIR, exist_ok=True)
    p = _path(name)
    tmp = p + ".tmp"
    with open(tmp, "w") as f:
        json.dump(rows, f, indent=2)
    os.replace(tmp, p)


class store_lock:
    """ flock on <data>/accounts.lock so writers never interleave. """

    def __enter__(self):
        os.makedirs(DATA_DIR, exist_ok=True)
        self._f = open(_path("accounts.lock"), "a+")
        fcntl.flock(self._f, fcntl.LOCK_EX)
        return self

    def __exit__(self, *exc):
        try:
            fcntl.flock(self._f, fcntl.LOCK_UN)
            self._f.close()
        except Exception:
            pass
        return False


def load_config():
    p = _path("config.json")
    if os.path.exists(p):
        try:
            with open(p) as f:
                d = json.load(f)
            return d if isinstance(d, dict) else {}
        except Exception:
            return {}
    return {}


def auto_delete_hours():
    return to_int(load_config().get("auto_delete_hours", 24), 24)


# --------------------------------------------------------------------------
# link builder - 5 links per protocol
# --------------------------------------------------------------------------
def _vmess_link(uid, domain, remark, net, port, tls_on, path=""):
    vm = {
        "v": "2", "ps": remark, "add": domain, "port": str(port), "id": uid,
        "aid": "0", "net": net, "type": "none", "host": domain,
        "path": path or "", "tls": "tls" if tls_on else "",
    }
    if tls_on:
        vm["sni"] = domain
    return "vmess://" + base64.b64encode(json.dumps(vm).encode()).decode()


def build_links(proto, uid, domain, remark):
    """Return the 5 connection links for one account, in display order."""
    proto = str(proto or "").lower()
    if proto not in PROTOCOLS:
        raise ValueError("unknown protocol: %s" % proto)
    ws = WS_PATH[proto]
    xh = XHTTP_PATH[proto]
    tag = remark
    if proto == "vmess":
        return {
            "Link TLS": _vmess_link(uid, domain, tag, "ws", 443, True, ws),
            "Link NTLS": _vmess_link(uid, domain, tag, "ws", 80, False, ws),
            "Link TCP": _vmess_link(uid, domain, tag + "-tcp", "tcp", TCP_PORT[proto], True),
            "Link XHTTP TLS": _vmess_link(uid, domain, tag + "-xhttp-tls", "xhttp", XHTTP_TLS_PORT[proto], True, xh),
            "Link XHTTP NTLS": _vmess_link(uid, domain, tag + "-xhttp-ntls", "xhttp", 80, False, xh),
        }
    if proto == "vless":
        q = "encryption=none"
        return {
            "Link TLS": f"vless://{uid}@{domain}:443?{q}&security=tls&type=ws&host={domain}&path=%2Fvless&sni={domain}#{tag}",
            "Link NTLS": f"vless://{uid}@{domain}:80?{q}&security=none&type=ws&host={domain}&path=%2Fvless#{tag}",
            "Link TCP": f"vless://{uid}@{domain}:{TCP_PORT[proto]}?{q}&security=tls&type=tcp&headerType=none&sni={domain}#{tag}-tcp",
            "Link XHTTP TLS": f"vless://{uid}@{domain}:{XHTTP_TLS_PORT[proto]}?{q}&security=tls&type=xhttp&path=%2Fxhttp&host={domain}&mode=auto&sni={domain}#{tag}-xhttp-tls",
            "Link XHTTP NTLS": f"vless://{uid}@{domain}:80?{q}&security=none&type=xhttp&path=%2Fxhttp&host={domain}&mode=auto#{tag}-xhttp-ntls",
        }
    # trojan (the account id is the trojan password)
    return {
        "Link TLS": f"trojan://{uid}@{domain}:443?security=tls&type=ws&host={domain}&path=%2Ftrojan&sni={domain}#{tag}",
        "Link NTLS": f"trojan://{uid}@{domain}:80?security=none&type=ws&host={domain}&path=%2Ftrojan#{tag}",
        "Link TCP": f"trojan://{uid}@{domain}:{TCP_PORT[proto]}?security=tls&type=tcp&headerType=none&sni={domain}#{tag}-tcp",
        "Link XHTTP TLS": f"trojan://{uid}@{domain}:{XHTTP_TLS_PORT[proto]}?security=tls&type=xhttp&path=%2Fxhttp-trojan&host={domain}&mode=auto&sni={domain}#{tag}-xhttp-tls",
        "Link XHTTP NTLS": f"trojan://{uid}@{domain}:80?security=none&type=xhttp&path=%2Fxhttp-trojan&host={domain}&mode=auto#{tag}-xhttp-ntls",
    }


def regen_links(domain):
    """Rebuild every stored Xray link set for a (new) domain. Returns count."""
    n = 0
    with store_lock():
        rows = load_store("xray_users.json")
        for r in rows:
            try:
                r["links"] = build_links(r.get("protocol"), r.get("id"), domain, r.get("remark", "user"))
                r["link"] = r["links"].get("Link TLS", "")
                n += 1
            except Exception:
                pass
        save_store("xray_users.json", rows)
    return n


# --------------------------------------------------------------------------
# account lifecycle (record level - callers do the system/service actions)
# --------------------------------------------------------------------------
def find(rows, key, value):
    for r in rows:
        if str(r.get(key)) == str(value):
            return r
    return None


def new_record(days, gb, ip):
    d = today()
    days = to_int(days, 30)
    return {
        "expiry_days": days,
        "gb_limit": to_int(gb, 0),
        "ip_limit": to_int(ip, 0),
        "used_gb": 0,
        "created": d.isoformat(),
        "expires": (d + datetime.timedelta(days=days)).isoformat(),
    }


def ssh_create(username, password, days, gb, ip):
    with store_lock():
        rows = load_store("ssh_users.json")
        if find(rows, "username", username):
            raise ValueError("SSH user already exists: %s" % username)
        rec = {"username": username, "password": password}
        rec.update(new_record(days, gb, ip))
        rows.append(rec)
        save_store("ssh_users.json", rows)
        return rec


def xray_create(proto, remark, uid, days, gb, ip, domain):
    proto = str(proto or "").lower()
    with store_lock():
        rows = load_store("xray_users.json")
        rec = {"id": uid, "protocol": proto, "remark": remark}
        rec.update(new_record(days, gb, ip))
        links = build_links(proto, uid, domain, remark)
        rec["links"] = links
        rec["link"] = links.get("Link TLS", "")
        rows.append(rec)
        save_store("xray_users.json", rows)
        return rec


def _mutate(store, key, value, fn):
    with store_lock():
        rows = load_store(store)
        rec = find(rows, key, value)
        if rec is None:
            raise ValueError("account not found: %s" % value)
        fn(rec)
        save_store(store, rows)
        return rec


def ssh_delete(username):
    with store_lock():
        rows = load_store("ssh_users.json")
        rec = find(rows, "username", username)
        save_store("ssh_users.json", [r for r in rows if r.get("username") != username])
        return rec


def xray_delete(uid):
    with store_lock():
        rows = load_store("xray_users.json")
        rec = find(rows, "id", uid)
        save_store("xray_users.json", [r for r in rows if r.get("id") != uid])
        return rec


def _renew(rec, add_days):
    """Renewal extends from the CURRENT expiry (never shortens an account
    that still has time left): base = max(today, stored expires)."""
    add = to_int(add_days, 30)
    exp = parse_date(rec.get("expires") or expires_on(rec.get("created", ""), rec.get("expiry_days", 30)))
    base = max(today(), exp) if exp else today()
    new_exp = base + datetime.timedelta(days=add)
    created = parse_date(rec.get("created", "")) or today()
    rec["expires"] = new_exp.isoformat()
    rec["expiry_days"] = (new_exp - created).days
    if rec.get("disabled") and rec.get("disabled_reason") == "Expired":
        rec.pop("disabled", None)
        rec.pop("disabled_reason", None)
        rec.pop("disabled_at", None)


def ssh_renew(username, add_days):
    return _mutate("ssh_users.json", "username", username, lambda r: _renew(r, add_days))


def xray_renew(uid, add_days):
    return _mutate("xray_users.json", "id", uid, lambda r: _renew(r, add_days))


def _edit_limits(rec, gb, ip):
    rec["gb_limit"] = to_int(gb, 0)
    rec["ip_limit"] = to_int(ip, 0)
    # Raising the GB limit above what was used (or to unlimited) revives a
    # quota-locked account; other lock reasons are left untouched.
    if rec.get("disabled") and rec.get("disabled_reason") == "Quota":
        if rec["gb_limit"] == 0 or to_float(rec.get("used_gb", 0)) < rec["gb_limit"]:
            rec.pop("disabled", None)
            rec.pop("disabled_reason", None)
            rec.pop("disabled_at", None)


def ssh_edit_limits(username, gb, ip):
    return _mutate("ssh_users.json", "username", username, lambda r: _edit_limits(r, gb, ip))


def xray_edit_limits(uid, gb, ip):
    return _mutate("xray_users.json", "id", uid, lambda r: _edit_limits(r, gb, ip))


def _reset_usage(rec):
    rec["used_gb"] = 0
    # The monitor owns the traffic counters: this flag tells it to re-base
    # last_counter on the next reading instead of adding the full counter.
    rec["reset_counter"] = True
    rec.pop("ip_blocked_until", None)
    if rec.get("disabled") and rec.get("disabled_reason") == "Quota":
        rec.pop("disabled", None)
        rec.pop("disabled_reason", None)
        rec.pop("disabled_at", None)


def ssh_reset_usage(username):
    return _mutate("ssh_users.json", "username", username, _reset_usage)


def xray_reset_usage(uid):
    return _mutate("xray_users.json", "id", uid, _reset_usage)


def _set_lock(rec, locked):
    if locked:
        rec["disabled"] = True
        rec["disabled_reason"] = "Locked"
        rec["disabled_at"] = datetime.datetime.now().isoformat(timespec="seconds")
    else:
        rec.pop("disabled", None)
        rec.pop("disabled_reason", None)
        rec.pop("disabled_at", None)


def ssh_set_lock(username, locked):
    return _mutate("ssh_users.json", "username", username, lambda r: _set_lock(r, locked))


def xray_set_lock(uid, locked):
    return _mutate("xray_users.json", "id", uid, lambda r: _set_lock(r, locked))


# --------------------------------------------------------------------------
# geo lookups (best effort, cached, no API key)
# --------------------------------------------------------------------------
def _http_json(url, timeout=4):
    req = urllib.request.Request(url, headers={"User-Agent": "vps-manager-suite"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8", "ignore"))


def public_ip():
    for url in ("https://api.ipify.org", "https://ifconfig.me/ip"):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "vps-manager-suite"})
            with urllib.request.urlopen(req, timeout=4) as r:
                ip = r.read().decode().strip()
            if re.match(r"^\d{1,3}(\.\d{1,3}){3}$", ip):
                return ip
        except Exception:
            pass
    return ""


def server_location():
    """{'city':..,'region':..,'country':..} for this server, cached 24h."""
    cache_p = _path("server_geo.json")
    cache = {}
    if os.path.exists(cache_p):
        try:
            with open(cache_p) as f:
                cache = json.load(f)
        except Exception:
            cache = {}
    if cache.get("city") and (datetime.datetime.now().timestamp() - float(cache.get("ts", 0))) < 86400:
        return cache
    ip = public_ip()
    if not ip:
        return cache
    try:
        d = _http_json("http://ip-api.com/json/%s?fields=status,city,regionName,country" % ip)
        if d.get("status") == "success":
            cache = {"city": d.get("city", ""), "region": d.get("regionName", ""),
                     "country": d.get("country", ""), "ip": ip,
                     "ts": datetime.datetime.now().timestamp()}
            try:
                with open(cache_p, "w") as f:
                    json.dump(cache, f)
            except Exception:
                pass
    except Exception:
        pass
    return cache


def geo_country(ip):
    """Country (and city) for one client IP, cached in geo_cache.json."""
    if not ip or not re.match(r"^\d{1,3}(\.\d{1,3}){3}$", str(ip)):
        return ""
    if ip.startswith(("10.", "192.168.", "172.16.", "172.17.", "172.18.", "172.19.",
                      "172.2", "172.30.", "172.31.", "127.")):
        return "LAN"
    cache_p = _path("geo_cache.json")
    cache = {}
    if os.path.exists(cache_p):
        try:
            with open(cache_p) as f:
                cache = json.load(f)
        except Exception:
            cache = {}
    ent = cache.get(ip)
    if ent:
        return ent.get("label", "")
    label = ""
    try:
        d = _http_json("http://ip-api.com/json/%s?fields=status,country,city" % ip, timeout=3)
        if d.get("status") == "success":
            label = d.get("country", "")
    except Exception:
        label = ""
    cache[ip] = {"label": label, "ts": datetime.datetime.now().timestamp()}
    try:
        if len(cache) > 500:
            cache = dict(sorted(cache.items(), key=lambda kv: kv[1].get("ts", 0))[-400:])
        with open(cache_p, "w") as f:
            json.dump(cache, f)
    except Exception:
        pass
    return label


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------
def _fmt_list(rows, kind):
    out = []
    grace = auto_delete_hours()
    for r in rows:
        name = r.get("username") if kind == "ssh" else "%s/%s" % (r.get("protocol", "?"), r.get("remark", "?"))
        out.append({
            "name": name,
            "key": r.get("username") if kind == "ssh" else r.get("id"),
            "status": display_status(r),
            "expires": r.get("expires", ""),
            "expiry_days": r.get("expiry_days", 30),
            "gb_limit": r.get("gb_limit", 0),
            "ip_limit": r.get("ip_limit", 0),
            "used_gb": r.get("used_gb", 0),
            "created": r.get("created", ""),
            "dead_note": dead_note(r, grace),
        })
    return out


def main(argv):
    if not argv:
        print(__doc__)
        return 1
    cmd = argv[0]
    try:
        if cmd == "ssh-list":
            print(json.dumps(_fmt_list(load_store("ssh_users.json"), "ssh"), indent=1))
        elif cmd == "xray-list":
            print(json.dumps(_fmt_list(load_store("xray_users.json"), "xray"), indent=1))
        elif cmd == "links":
            rows = load_store("xray_users.json")
            rec = find(rows, "id", argv[1])
            if rec is None:
                print("account not found")
                return 1
            for k, v in (rec.get("links") or {}).items():
                print("%s: %s" % (k, v))
        elif cmd == "build-links":
            links = build_links(argv[1], argv[2], argv[3], argv[4])
            for k, v in links.items():
                print("%s: %s" % (k, v))
        elif cmd == "regen-links":
            print("regenerated %d link sets for %s" % (regen_links(argv[1]), argv[1]))
        elif cmd == "server-location":
            loc = server_location()
            print(", ".join(x for x in (loc.get("city", ""), loc.get("region", "")) if x) or "-")
        elif cmd == "ssh-delete":
            print("deleted" if ssh_delete(argv[1]) else "not found")
        elif cmd == "xray-delete":
            print("deleted" if xray_delete(argv[1]) else "not found")
        elif cmd == "ssh-renew":
            r = ssh_renew(argv[1], argv[2]); print("expires: %s" % r["expires"])
        elif cmd == "xray-renew":
            r = xray_renew(argv[1], argv[2]); print("expires: %s" % r["expires"])
        elif cmd == "ssh-limits":
            ssh_edit_limits(argv[1], argv[2], argv[3]); print("limits saved")
        elif cmd == "xray-limits":
            xray_edit_limits(argv[1], argv[2], argv[3]); print("limits saved")
        elif cmd == "ssh-reset":
            ssh_reset_usage(argv[1]); print("usage reset")
        elif cmd == "xray-reset":
            xray_reset_usage(argv[1]); print("usage reset")
        elif cmd == "ssh-lock":
            ssh_set_lock(argv[1], True); print("locked")
        elif cmd == "ssh-unlock":
            ssh_set_lock(argv[1], False); print("unlocked")
        elif cmd == "xray-lock":
            xray_set_lock(argv[1], True); print("locked")
        elif cmd == "xray-unlock":
            xray_set_lock(argv[1], False); print("unlocked")
        else:
            print("unknown command: %s" % cmd)
            return 1
    except (ValueError, IndexError) as e:
        print("error: %s" % e)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
