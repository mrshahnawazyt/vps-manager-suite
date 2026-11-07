# QA Report — VPS Manager Suite

Local QA by the maintainer's test environment (Ubuntu 24.04, Python 3.12).
Rule applied throughout: **build properly, test locally first, push only after tests pass.**

## v1.1 (2026-11-07) — PASS

- `bash -n` on all shell scripts; `py_compile` on all Python files
- Panel smoke test: login page 200, dashboard 200
- Exact reference case: SSH account "Testq", Days 30 / GB 0 / IP 0 →
  Expiry **2026-12-07**, Data: **Unlimited**, IP Limit: **Unlimited**
- Xray VLESS account → all **5 links** (TLS, NTLS, TCP, XHTTP TLS, XHTTP NTLS)
- Panel with a corrupted SSL cert: does **not** crash — serves HTTP with a warning
- Menu option 10 shows the real panel URL, username and password (no placeholders)
- ws-proxy answers a WebSocket handshake (HTTP 101)
- Xray template renders to valid JSON (6 inbounds)

Fixes found by this QA: Dropbear flags that blocked password logins removed;
SSL issuance now stops nginx/haproxy first (standalone port conflict);
`vpsupdate` self-update command added.

## v1.2 (2026-11-07) — PASS

**Panel UI (rebuilt from the reference video's frames)**
- Sidebar layout, all-English UI, stat cards (CPU/RAM/Disk), services card with status pills
- Login: success → **303 redirect straight to Dashboard** (no intermediate page);
  failure → "Invalid username or password" (401)
- SSH create → result card (Username, Password, Host, SNI, Created, Expiry, Data,
  IP Limit, Ports) + Payload box with Copy button; Xray create → card + 5 links, each with Copy
- **Panel-created SSH accounts now create real system users**
  (`useradd -M -s /usr/sbin/nologin -e <expiry>` + `chpasswd`; delete → `userdel`) — tested end to end
- Disabled accounts show their reason in the list (e.g. "Disabled (Quota)")

**Limit enforcement (real, not just display)**
- Expiry passed → SSH user locked + sessions killed; Xray account removed from config
- GB quota: Xray usage from the stats API (cumulative, no double-counting across
  restarts — tested); SSH usage from per-UID iptables counters; reaching the
  limit disables the account (tested: 2.5 GB counted vs 1 GB limit → locked)
- IP limit: Xray — 3 distinct IPs vs limit 2 → 30-minute block (tested);
  SSH — 3 sessions vs limit 2 → newest session closed, first two kept (tested)
- Accounts with GB/IP = 0 (Unlimited) are never touched by enforcement (tested)

**Terminal UI + text**
- All terminal screens in a single-column box (every line exactly 46 chars,
  verified after translation); account cards for SSH/Xray creation
- Entire product text is English (menu, installer, uninstaller, scripts,
  panel, README, docs) — verified by grep for Roman-Urdu phrases: **0 matches**
- Uninstaller rewritten: one confirmation, then full clean removal
  (6 services stopped/disabled, suite folder, data, created users;
  default nginx/haproxy/dropbear configs restored) — tested with a fake root:
  "y" removes everything, "N" removes nothing
- Ubuntu support: 20.04 / 22.04 / 24.04 / 25.x (OS check + version-tolerant
  package install; optional packages skip cleanly when unavailable)

## Known gaps (honest)

- A full end-to-end `install.sh` run on a clean Ubuntu 22.04 VPS (apt, systemd,
  real ports 443/109/143) cannot be reproduced in the local test environment;
  it is verified on the maintainer's real VPS instead (panel login + account
  creation confirmed working there on 2026-11-07).
- SSH data accounting uses per-UID output counters: it captures the dominant
  tunnel traffic but is not byte-exact proxy accounting. Xray accounting is exact (stats API).
- IP enforcement is poll-based (cron): a very brief excess between runs may
  not be caught mid-minute; clients behind one NAT share one IP.
