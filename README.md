# 🚀 VPS Manager Suite

**Deploy and manage a complete VPS from a single script** — SSH-WS/SSL, Xray (VMess / VLESS / Trojan), HAProxy + Nginx multiplexer, CAKE QoS, a fast limit monitor, and a dark **Web Panel (port 9090)**.

Everything is controlled from three places with the same power: the **terminal menu**, the **web panel**, and a **Telegram bot** that works as a full remote panel.

![Ubuntu](https://img.shields.io/badge/Ubuntu-18.04%20--%2025.x-orange)
![Shell](https://img.shields.io/badge/Shell-Bash-green)
![Panel](https://img.shields.io/badge/Panel-Python%20(stdlib%20only)-blue)
![Status](https://img.shields.io/badge/Status-Feature--Parity%20Build-brightgreen)

---

## ⚡ Install — One Command

Run this single command as root on your VPS — everything else happens automatically:

```bash
export GH_REPO=mrshahnawazyt/vps-manager-suite; curl -fsSL https://raw.githubusercontent.com/mrshahnawazyt/vps-manager-suite/main/install.sh -o install.sh && sudo -E bash install.sh
```

The script asks for your domain itself. After the install, use `vpsmenu` for the menu and `vpsupdate` for updates.

---

## ✨ Features

- **1-Command Install** — one command from GitHub downloads and installs everything
- **Terminal Menu** — 12 options (Install, Domain, SSL, Users, Status, Fix, QoS, Stats, Telegram, Panel, Uninstall)
- **Web Panel (9090)** — Dashboard, SSH Accounts, Xray Accounts, Services, VPS Stats & Speedtest, Telegram Bot, Settings, Panel Admins, Activity/Audit Log
- **Full Account Lifecycle** — Create, Renew, Edit Limits, Reset Usage, Lock, Unlock and Delete for SSH and Xray accounts — from the terminal, the panel **and** the Telegram bot
- **SSH** — Dropbear based, Direct / SSL / WS / WS-SSL, with an auto-generated payload
- **SlowDNS** — DNSTT DNS tunnel on UDP 53 straight into the SSH service: clients use their normal **SSH username + password** with the SlowDNS NS host (Settings → SlowDNS Domain, default `dns.` + your domain)
- **ZiVPN UDP** — UDP 5667 (app ports 6000-19999 redirected to it); the password list is **synced from the SSH accounts**, so one account works for SSH, SlowDNS and ZiVPN UDP
- **OpenVPN** — per-user certificates (UDP 1194) with create / renew / revoke in the panel and a ready-to-import **.ovpn** profile download on every account
- **Xray** — VMess / VLESS / Trojan, **5 links per account for every protocol**: WS, WS-TLS, TCP-TLS, XHTTP-TLS, XHTTP-NTLS — plus a QR code for each link in the panel
- **Fast Limit Monitor** — expiry, GB quota and IP limits enforced every **4 seconds** (SSH and Xray), with distinct Active / EXPIRED / GB-LIMIT / IP-LIMIT / LOCKED states and auto-delete of long-dead accounts (grace period, countdown shown)
- **Connected Users View** — live connected IPs for SSH and Xray, with the real client IP (PROXY-protocol + header recovery) and country, plus session kick
- **SSL Tools** — Let's Encrypt issue, **force renew**, **auto-renew dry-run test**, days-left display, **re-apply certificate to all services**, and an auto-renew deploy hook (self-signed fallback if DNS fails)
- **Domain Change = Full Re-provisioning** — new domain re-renders the stack, re-issues SSL, and regenerates every stored Xray link automatically
- **Multi-Admin Panel** — Viewer / Admin / Super Admin roles, per-action permissions, and an **audit log** that records the admin and the source IP of every action
- **Service Control** — per-service Start / Stop / Restart, service log viewer, and Restart All from the panel; Install All / Fix Stack / Xray Repair stream live progress to the browser
- **Traffic Stats** — vnstat daily/monthly traffic on the Stats page (one-click install)
- **Ping Stability** — CAKE QoS (anti-bufferbloat) plus **per-user bandwidth caps** for individual SSH accounts
- **Telegram Bot** — a full remote panel: inline menus and wizards for accounts, limits, connected users, domain, banner, QoS and system actions; Super Admin plus extra bot admins
- **Verified Updater** — `vpsupdate` verifies the new files (syntax + sanity checks) before applying anything, and keeps timestamped backups of the previous version
- **Panel Management** — change the panel port (with validation), change login, reset password, view panel logs, reinstall the panel keeping the same login — all from the terminal
- **Full-Clean Uninstall** — one confirmation removes the whole suite and restores the default configs

## 📋 Requirements

| Item | Detail |
|---|---|
| OS | Ubuntu 18.04 / 20.04 / 22.04 / 24.04 / 25.x (a fresh VPS is best) |
| Access | root (sudo) |
| RAM | 1 GB+ recommended |
| Domain | A domain/subdomain whose **A record points to the VPS IP** (needed for SSL) |

## 📦 Install Details

- Only `install.sh` is downloaded — the installer fetches the rest of the files from this repo itself.
- You can also pass the domain directly: `sudo -E bash install.sh your.domain.com` — if you don't, the script asks for it.
- Prefer the full folder? `git clone https://github.com/mrshahnawazyt/vps-manager-suite.git`, then run `sudo bash install.sh` inside it.

At the end of the install you get a **READY screen** with:
- The Web Panel link: `https://your.domain.com:9090`
- Username: `admin` — the password is generated and shown on that screen (and saved in the credentials file)
- A summary of all ports and services

Open the terminal menu at any time with:

```bash
vpsmenu
```

## 🖥️ Terminal Menu

| # | Option | What it does |
|---|---|---|
| 01 | Install All Components | Full deploy |
| 02 | Add / Change Domain | Set the domain + DNS check, then full re-provisioning (SSL re-issue, all Xray links regenerated) |
| 03 | SSL Certificate | Submenu: Issue / Force Renew / Dry-Run Test / Re-apply to Services / Status with days left |
| 04 | Manage Users / Limits / Banner | Full lifecycle for SSH + Xray accounts (per-protocol Xray menus, Show Links, Connected IPs), SSH banner |
| 05 | Status & Ports | Live status screen: server IP, location, SSL days left, account counts, services, ports |
| 06 | Fix SSH-WS / SSL Stack | Component repair pipeline with verify + self-heal |
| 07 | Ping Stability (QoS) | CAKE QoS on/off, status, per-user bandwidth caps |
| 08 | VPS Stats / Speedtest | Server info + speedtest |
| 09 | Telegram Bot | Bot service management: token/admin setup, start/stop/restart, logs, bot admins, remove |
| 10 | Web Panel | Panel link/login, change port or login, reset password, logs, reinstall, remove panel only |
| 11 | Uninstall | Remove everything (full clean) |
| 12 | Exit | — |

## 🌐 Web Panel

- **URL:** `https://<domain>:9090` — Login: `admin` + the install password
- **Dashboard** — CPU/RAM/Disk, SSH/Xray account counts, services status, server info
- **SSH Accounts** — create an account and get a ready-made output card: username, password, host, SNI, expiry, ports, server City/Region, **payload** (with a Copy button); every account row also has Renew / Set Limits / Reset Usage / Lock-Unlock / Delete
- **Xray Accounts** — VMess/VLESS/Trojan, auto-generated UUID, **5 links per account** (WS / WS-TLS / TCP-TLS / XHTTP-TLS / XHTTP-NTLS), each with a Copy button and a **QR code**; same lifecycle actions as SSH
- **Connected Users** — live connected IPs with country, for SSH and Xray accounts
- **Services** — per-service Start / Stop / Restart, log viewer, Restart All, plus streaming Install All / Fix Stack / Xray Repair actions
- **VPS Stats** — full server info + **vnstat traffic** (daily/monthly) + **Run Speedtest**
- **Telegram Bot** — bot status and setup; the bot itself is a full remote panel (see below)
- **Panel Admins** — multiple logins with **Viewer** (read-only), **Admin** and **Super Admin** roles; the main admin can never be removed
- **Activity / Audit Log** — every action with the admin name and the source IP
- **Settings** — change domain (full re-provisioning, streamed live), SSL tools, SSH banner (HTML, max 2000 bytes), Ping QoS with per-user caps, panel password change

## 🧩 SNI Manager

The **SNI Manager** page in the panel (sidebar → SNI Manager, route
`/configs`, alias `/sni-manager`) manages ready-to-import connection
profiles for your own nodes (for example separate US / UK / DE servers):

- **First-party profiles only.** A profile's domain must be the server's
  configured domain, a subdomain of it, or the configured SlowDNS / UDP
  host. Any other hostname is rejected by the panel and by the sync
  script. The SNI of a profile is always its own domain — there is no
  free SNI field.
- **Protocols:** SSH TLS, VMess, VLESS, Trojan, OpenVPN, SlowDNS, each
  with its normal port (443 / 443 / 443 / 443 / 1194 / 53). For SSH TLS
  the payload is generated with `Host` set to the profile domain. Each
  profile also carries the protocol's real connection path (`/sshws`,
  `/vmess`, `/vless`, `/trojan`; OpenVPN / SlowDNS have none), included
  in the export and in the API output.
- **Storage.** Profiles live in the panel's existing JSON store
  (`config-profiles.json` in the panel data dir) — there is no database
  and no migration. Profiles saved before the `path` key existed load
  unchanged, are treated as using their protocol's default path, and
  gain the key the next time they are saved.
- **Locked Config export.** Each profile can be exported as a base64
  code (with a QR code) that clients import directly. "Locked" means the
  profile carries no editable raw host field beyond your own domain.
  Base64 is an encoding, not encryption — share the code like a password.
- **API.** `GET /api/configs` returns the active profiles as JSON. It
  requires the `X-Config-Token` header; the token is shown (and can be
  regenerated) on the SNI Manager page. Requests without the token
  get `401`.
- **Sync.** Saving a profile runs `scripts/config-sync.sh`, which
  re-renders the HAProxy SNI map (`/etc/vps-manager/sni.map`) and the
  nginx profile map, and — only when a map changed — validates the live
  HAProxy config with `haproxy -c` and reloads HAProxy gracefully, so
  established connections are not dropped. The panel-triggered sync is
  the normal mechanism; if you also want a periodic safety net, one
  optional cron line is enough:
  `* * * * * root VPSMGR_NO_RELOAD=0 bash /opt/vps-manager-suite/scripts/config-sync.sh >/dev/null 2>&1`

## 🤖 Telegram Bot

The bot is a complete remote panel, not just a status checker:

- Inline menus + step-by-step wizards: SSH and Xray create/list/detail, links, renew, edit limits, reset usage, lock/unlock, delete, connected users
- Status & Ports, Domain change (full re-provisioning), Banner, QoS (status / apply / remove / per-user cap)
- System actions: Fix Stack, per-service restart, Restart All, SSL, Install All (long actions report their output back to the chat)
- Roles: one **Super Admin** plus extra **bot admins**; only the Super Admin manages bot admins and runs Install All; unknown chats are ignored

## 📏 Account Limits Rule ⚠️

The same rule applies everywhere — SSH and Xray:

| Field | Meaning |
|---|---|
| **Days** | **A literal count.** Default **30**. Expiry = the account creation date + days. (There is no "unlimited" for Days — enter 30 and the account expires after 30 days.) |
| **GB limit (0 = unlimited)** | `0` = **Unlimited**. Enter a number and the account is stopped once that much data has been used — the limit is enforced. |
| **IP limit (0 = unlimited)** | `0` = **Unlimited**. Enter a number and no more than that many IPs/logins are allowed — the limit is enforced. |

**Example:** Days `30`, GB `0`, IP `0` → Expiry is **30 days after creation**, Data: **Unlimited**, IP Limit: **Unlimited**.

Enforcement runs every **4 seconds** (`vps-monitor` service): expired accounts are locked, quota-hit accounts are disabled, extra IPs are cut — and accounts left dead past the grace period (default 24 h) are deleted automatically, with the countdown shown in the account lists.

## 🔌 Ports

| Port | Service |
|---|---|
| 22 · 109 · 143 | SSH Direct (Dropbear: 109 · 143) |
| 80 | SSH-WS + Xray WS / XHTTP (non-TLS) via Nginx |
| 443 | SSH-WS-SSL (TLS) + SSH-SSL (SNI/Host = domain) + Xray WS-TLS / XHTTP-TLS via Nginx |
| 8080 | WebSocket proxy (direct WS listener) |
| 8081 | WebSocket proxy PROXY-protocol listener (real-IP chain, internal) |
| 8443 · 8446 · 8447 | Xray TCP-TLS — VLESS · Trojan · VMess |
| 8444 · 8445 · 8448 | Xray XHTTP-TLS — VLESS · Trojan · VMess |
| 10443 | Xray VLESS TLS fallback |
| 53 (UDP) | SlowDNS (DNSTT) — needs the NS record below |
| 5667 (UDP) | ZiVPN UDP server (app ports 6000-19999 UDP redirect to it) |
| 1194 (UDP) | OpenVPN |
| 9090 | Web Panel (HTTPS) |

### SlowDNS — one DNS record (one-time setup)

SlowDNS works only after you delegate its host to the VPS. At your DNS
provider create **one NS record**:

| Type | Host | Value (target) |
|---|---|---|
| NS | the SlowDNS host, e.g. `dns.my7` for `dns.my7.ddkvps.com` | your server domain, e.g. `my7.ddkvps.com` |

In words: an NS record for **dns.my7.ddkvps.com** pointing to
**my7.ddkvps.com** (adjust to your own domain). The exact record for your
setup is shown in the panel under **Settings → SlowDNS Domain** and at the
end of the installer. The SlowDNS host itself stays editable in Settings;
changing it restarts the SlowDNS service.

## 📂 Project Structure

```
vps-manager-suite/
├── install.sh          # Installer + GitHub bootstrap (the one-command install)
├── menu.sh             # Terminal menu (01-12)
├── update.sh           # Verified updater with backups: vpsupdate
├── uninstall.sh        # Full-clean uninstaller
├── lib/common.sh       # Shared functions, limit logic, SSL tools
├── web/panel.py        # Web panel (Python stdlib only - no pip needed)
├── templates/          # nginx, haproxy, xray configs
└── scripts/            # monitor, sync-xray, fix-xray-sync, vpsm (account core),
                        # telegram-bot, ws-proxy, qr, ssl, set-domain,
                        # panel-admin, qos, fix-stack, speedtest, limit-check
```

## 🔄 Update

To install the latest version at any time, run one command:

```bash
sudo vpsupdate
```

The updater downloads the new files, **verifies them first** (shell + Python syntax checks and size sanity — a broken update is rejected and nothing changes), backs up the current version to `/var/backups/vps-manager-suite/` (the last 3 backups are kept), then applies the update and restarts the services. Your accounts and data stay safe.

## 🔒 Security Notes

- Change the panel password **immediately** after installing (Settings → My Account)
- Never write your GitHub token, Telegram token or panel password into code or commits
- Changing the domain re-issues SSL and regenerates all account links automatically — no manual step is needed afterwards
- SSH users are created **tunnel-only** — they get no shell access to the VPS
- Give extra panel logins the **Viewer** role when they only need to look; every admin action is recorded in the audit log with its source IP

## 🗑️ Uninstall

```bash
sudo bash uninstall.sh
```

Or use terminal menu option **11**. One confirmation, then everything is removed — suite folder, services, monitor, bot, data — and the default nginx/haproxy configs are restored.

## 🤝 Contributing

This project is public — contributions are welcome!
1. **Fork** the repo
2. Create your branch: `git checkout -b feature/my-change`
3. Commit your changes and open a **Pull Request**

For bugs and suggestions, use the **Issues** tab.

## 📜 License

MIT License — see the [LICENSE](LICENSE) file.

---

**Made with ❤️ by [@mrshahnawazyt](https://github.com/mrshahnawazyt)**
