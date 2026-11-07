#!/usr/bin/env bash
# Limit monitor - CORRECTED semantics (video-verified)
# Days = literal (expiry = created + days, 0 = 0 days). GB 0 => Unlimited (skip), IP 0 => Unlimited (skip)
# Sirf >0 wale limits enforce honge. Cron se chal sakta hai: */10 * * * * root bash scripts/limit-check.sh
set -u
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
source "$SCRIPT_DIR/lib/common.sh" 2>/dev/null || true
DATA="${VPSMGR_DATA_DIR:-/var/lib/vps-manager}"
# Dono account files wahi se lo jahan maujood hon (pehle ssh file hi na ho to
# data dir galat switch ho jata tha)
if [[ ! -f "$DATA/ssh_users.json" && ! -f "$DATA/xray_users.json" && -d "$SCRIPT_DIR/data" ]]; then
  DATA="$SCRIPT_DIR/data"
fi
echo "[limit-check] $(date -Is) data=$DATA"
python3 - "$DATA" <<'PY'
import json, os, sys, datetime
data_dir = sys.argv[1]
def load(name):
    p=os.path.join(data_dir,name)
    if os.path.exists(p):
        try: return json.load(open(p))
        except: return []
    return []
def is_expired(created, days):
    days=int(days or 0)
    try:
        base=datetime.date.fromisoformat(str(created)[:10])
        return datetime.date.today() > base+datetime.timedelta(days=days)
    except: return False
def quota(used, lim):
    lim=int(lim or 0)
    if lim==0: return False  # Unlimited - skip
    return float(used or 0) >= float(lim)
for name,label in (("ssh_users.json","SSH"),("xray_users.json","Xray")):
    for u in load(name):
        who=u.get("username") or u.get("remark") or u.get("id")
        if is_expired(u.get("created",""), u.get("expiry_days",0)):
            print(f"[{label}] {who}: EXPIRED (expiry_days={u.get('expiry_days')}) -> disable karna chahiye")
        elif quota(u.get("used_gb",0), u.get("gb_limit",0)):
            print(f"[{label}] {who}: QUOTA FULL (used={u.get('used_gb')} / {u.get('gb_limit')} GB) -> disable karna chahiye")
        else:
            # 0 limits yahan Active hi rahenge, kabhi expire/quota se nahi rukenge
            import datetime as _d; _exp=( _d.date.fromisoformat(str(u.get('created',''))[:10]) + _d.timedelta(days=int(u.get('expiry_days') or 30)) ).isoformat() if u.get('created') else '?'; print(f"[{label}] {who}: OK  Expiry={_exp} Data={'Unlimited' if int(u.get('gb_limit') or 0)==0 else u.get('gb_limit')} IP Limit={'Unlimited' if int(u.get('ip_limit') or 0)==0 else u.get('ip_limit')}")
PY
echo "[limit-check] done. Note: GB/IP limit 0 ho to skip; Days hamesha literal check hota hai."
