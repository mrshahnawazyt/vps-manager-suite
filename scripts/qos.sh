#!/usr/bin/env bash
# CAKE QoS - Ping Stability
# Usage: qos.sh [interface] [rate e.g. 900mbit]   defaults: auto-detect, 950mbit
set -u
IFACE="${1:-$(ip route get 1.1.1.1 2>/dev/null | awk '{for(i=1;i<=NF;i++) if($i=="dev") print $(i+1)}' | head -n1)}"
RATE="${2:-950mbit}"
[[ -z "$IFACE" ]] && IFACE="eth0"
echo "[QoS] Interface: $IFACE  Rate: $RATE  (CAKE)"
if [[ "${EUID:-$(id -u)}" -ne 0 ]]; then echo "Root chahiye: sudo bash $0"; exit 1; fi
tc qdisc del dev "$IFACE" root 2>/dev/null || true
tc qdisc add dev "$IFACE" root cake bandwidth "$RATE" diffserv4 dual-dsthost nat wash ack-filter split-gso rtt 100ms || tc qdisc add dev "$IFACE" root cake bandwidth "$RATE"
# Per-client cap example (commented): tc class / filter yahan apni policy ke hisab se lagao
tc qdisc show dev "$IFACE"
echo "[OK] CAKE lag gaya. Ping test: ping -c 10 1.1.1.1"
