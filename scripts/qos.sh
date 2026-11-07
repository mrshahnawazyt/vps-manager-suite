#!/usr/bin/env bash
# VPS Manager Suite - QoS / ping stability + per-user caps (original code)
#
#   qos.sh apply            Apply QoS from the suite config
#   qos.sh status           Show the active shaper + configured caps
#   qos.sh off              Remove QoS completely (qdisc + marks)
#   qos.sh cap <mbps>       Set the per-user cap in the config, then apply
#   qos.sh <iface> <rate>   Legacy form: save interface+rate, then apply
#
# How it works:
#   per_client_cap = 0  -> one CAKE root qdisc at the configured rate
#                          (the ping-stability profile)
#   per_client_cap > 0  -> HTB root at the configured total rate; the
#                          default class keeps a CAKE leaf, and EVERY SSH
#                          account gets its own HTB class capped at
#                          per_client_cap. Traffic is steered into the
#                          classes by iptables mangle OUTPUT rules that
#                          MARK packets by owner UID, so the cap follows
#                          the account no matter how it connects.
#   Applied caps are recorded in $DATA/qos_caps.json so `off` can remove
#   every rule it created, and `status` can show them.
set -u
DATA="${VPSMGR_DATA_DIR:-/var/lib/vps-manager}"
CFG="$DATA/config.json"
CAPS_FILE="$DATA/qos_caps.json"

cfg_get(){ python3 - "$CFG" "$1" "$2" <<'PY'
import json, sys, os
d = {}
if os.path.exists(sys.argv[1]):
    try: d = json.load(open(sys.argv[1]))
    except Exception: d = {}
print(d.get(sys.argv[2], sys.argv[3]))
PY
}
cfg_set(){ python3 - "$CFG" "$1" "$2" <<'PY'
import json, sys, os
p, k, v = sys.argv[1], sys.argv[2], sys.argv[3]
d = {}
if os.path.exists(p):
    try: d = json.load(open(p))
    except Exception: d = {}
try: v = int(v)
except Exception: pass
d[k] = v
os.makedirs(os.path.dirname(p), exist_ok=True)
json.dump(d, open(p, "w"), indent=2)
PY
}

detect_iface(){ ip route get 1.1.1.1 2>/dev/null | awk '{for(i=1;i<=NF;i++) if($i=="dev") print $(i+1)}' | head -n1; }
rate_mbps(){ # "950mbit" -> 950 ; "1gbit" -> 1000 ; "900" -> 900
  local r="$1"
  if [[ "$r" == *gbit* ]]; then echo $(( ${r%%gbit*} * 1000 ));
  else echo "${r%%mbit*}"; fi
}

need_root(){ [[ "${EUID:-$(id -u)}" -eq 0 ]] || { echo "Root is required: sudo bash $0 $*"; exit 1; }; }

remove_marks(){
  # delete every mangle rule this script created (recorded in the caps file)
  [[ -f "$CAPS_FILE" ]] || return 0
  python3 - "$CAPS_FILE" <<'PY' | while read -r uid mark; do
import json, sys
try: caps = json.load(open(sys.argv[1]))
except Exception: caps = {}
for _u, v in caps.items():
    print(v.get("uid", ""), v.get("mark", ""))
PY
    [[ -n "${uid:-}" && -n "${mark:-}" ]] || continue
    while iptables -t mangle -D OUTPUT -m owner --uid-owner "$uid" -m comment --comment vpsmgr-qos -j MARK --set-mark "$mark" 2>/dev/null; do :; done
  done
}

qos_off(){
  need_root off
  local iface; iface="$(cfg_get qos_interface "")"; [[ -z "$iface" ]] && iface="$(detect_iface)"; [[ -z "$iface" ]] && iface="eth0"
  tc qdisc del dev "$iface" root 2>/dev/null || true
  remove_marks
  rm -f "$CAPS_FILE"
  echo "[OK] QoS removed from $iface (qdisc + per-user marks cleaned)"
}

qos_apply(){
  need_root apply
  local iface rate cap total defrate
  iface="$(cfg_get qos_interface "")"; [[ -z "$iface" ]] && iface="$(detect_iface)"; [[ -z "$iface" ]] && iface="eth0"
  rate="$(cfg_get qos_rate 950mbit)"
  cap="$(cfg_get per_client_cap 0)"; [[ "$cap" =~ ^[0-9]+$ ]] || cap=0
  echo "[QoS] Interface: $iface  Rate: $rate  Per-user cap: ${cap} Mbps"
  tc qdisc del dev "$iface" root 2>/dev/null || true
  remove_marks
  rm -f "$CAPS_FILE"
  if [[ "$cap" -eq 0 ]]; then
    if tc qdisc add dev "$iface" root cake bandwidth "$rate" diffserv4 dual-dsthost nat wash ack-filter split-gso rtt 100ms 2>/dev/null \
       || tc qdisc add dev "$iface" root cake bandwidth "$rate" 2>/dev/null; then
      echo "[OK] CAKE is active (no per-user caps). Test the ping: ping -c 10 1.1.1.1"
    elif tc qdisc add dev "$iface" root fq_codel 2>/dev/null; then
      echo "[WARN] CAKE could not be applied on this system, so fq_codel"
      echo "[WARN] was enabled instead (it still controls bufferbloat)."
      echo "[WARN] CAKE (sch_cake) needs kernel 4.16 or newer. Ubuntu 18.04's"
      echo "[WARN] stock 4.15 kernel does not include it - install the HWE"
      echo "[WARN] kernel (linux-generic-hwe-18.04), reboot, and re-run"
      echo "[WARN] this option to switch to CAKE."
    else
      echo "[X] Could not apply any queueing discipline on $iface - check that iproute2 (tc) is installed and the interface exists."
      return 1
    fi
    tc qdisc show dev "$iface" | head -n 3
    return 0
  fi
  # HTB tree with per-user classes
  total="$(rate_mbps "$rate")"; [[ "$total" =~ ^[0-9]+$ && "$total" -gt 0 ]] || total=950
  mapfile -t users < <(python3 - "$DATA/ssh_users.json" <<'PY'
import json, sys, os
rows = []
if os.path.exists(sys.argv[1]):
    try: rows = json.load(open(sys.argv[1]))
    except Exception: rows = []
for r in rows:
    if r.get("username"): print(r["username"])
PY
)
  local n=${#users[@]}
  defrate=$(( total - n * cap )); (( defrate < total / 20 )) && defrate=$(( total / 20 )); (( defrate < 1 )) && defrate=1
  tc qdisc add dev "$iface" root handle 1: htb default 10 || { echo "[X] Could not create the HTB root (tc/htb unavailable?)"; return 1; }
  tc class add dev "$iface" parent 1: classid 1:1 htb rate "${total}mbit" ceil "${total}mbit"
  tc class add dev "$iface" parent 1:1 classid 1:10 htb rate "${defrate}mbit" ceil "${total}mbit"
  if ! tc qdisc add dev "$iface" parent 1:10 handle 110: cake bandwidth "${defrate}mbit" diffserv4 dual-dsthost nat wash 2>/dev/null; then
    tc qdisc add dev "$iface" parent 1:10 handle 110: fq_codel 2>/dev/null || true
    echo "[QoS] Note: CAKE leaf unavailable (needs kernel 4.16+) - fq_codel used for the default class."
  fi
  tc filter add dev "$iface" parent 1: protocol ip handle 10 fw flowid 1:10 2>/dev/null || true
  python3 - "$CAPS_FILE" <<'PY' > /dev/null
import json, sys
json.dump({}, open(sys.argv[1], "w"))
PY
  local i=10 mark uname uid caps_json="{}"
  for uname in "${users[@]}"; do
    uid="$(id -u "$uname" 2>/dev/null)" || continue
    [[ "$uid" =~ ^[0-9]+$ && "$uid" -ge 1000 ]] || continue
    i=$(( i + 1 )); mark=$i
    tc class add dev "$iface" parent 1:1 classid "1:$mark" htb rate "${cap}mbit" ceil "${cap}mbit" || continue
    tc filter add dev "$iface" parent 1: protocol ip handle "$mark" fw flowid "1:$mark" || true
    iptables -t mangle -A OUTPUT -m owner --uid-owner "$uid" -m comment --comment vpsmgr-qos -j MARK --set-mark "$mark" || true
    caps_json=$(python3 - "$CAPS_FILE" "$uname" "$uid" "$mark" "$cap" <<'PY'
import json, sys
p, u, uid, mark, cap = sys.argv[1:6]
d = json.load(open(p))
d[u] = {"uid": int(uid), "mark": int(mark), "cap_mbps": int(cap)}
json.dump(d, open(p, "w"), indent=2)
print("ok")
PY
)
    echo "[QoS] cap applied: $uname (uid $uid) -> ${cap} Mbps"
  done
  echo "[OK] QoS active on $iface: total ${total} Mbps, default class ${defrate} Mbps, per-user cap ${cap} Mbps"
}

qos_status(){
  local iface rate cap
  iface="$(cfg_get qos_interface "")"; [[ -z "$iface" ]] && iface="$(detect_iface)"; [[ -z "$iface" ]] && iface="eth0"
  rate="$(cfg_get qos_rate 950mbit)"; cap="$(cfg_get per_client_cap 0)"
  echo "Interface     : $iface"
  echo "Total rate    : $rate"
  echo "Per-user cap  : ${cap} Mbps (0 = no per-user cap)"
  echo "--- qdisc ---"
  tc qdisc show dev "$iface" 2>/dev/null | head -n 6 || echo "(none)"
  echo "--- classes ---"
  tc class show dev "$iface" 2>/dev/null | head -n 20 || echo "(none)"
  echo "--- per-user caps ---"
  if [[ -f "$CAPS_FILE" ]]; then cat "$CAPS_FILE"; else echo "(no caps applied)"; fi
}

cmd="${1:-apply}"
case "$cmd" in
  apply) qos_apply ;;
  status) qos_status ;;
  off) qos_off ;;
  cap)
    v="${2:-}"
    [[ "$v" =~ ^[0-9]+$ ]] || { echo "Usage: qos.sh cap <mbps> (0 = no per-user cap)"; exit 1; }
    cfg_set per_client_cap "$v"
    echo "[QoS] per_client_cap set to $v Mbps"
    qos_apply
    ;;
  *)
    # legacy positional form: qos.sh <interface> <rate>
    cfg_set qos_interface "$cmd"
    [[ -n "${2:-}" ]] && cfg_set qos_rate "$2"
    qos_apply
    ;;
esac
