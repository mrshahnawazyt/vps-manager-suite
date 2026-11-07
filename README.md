# 🚀 VPS Manager Suite

**Deploy and manage a complete VPS from a single script** — SSH-WS/SSL, Xray (VMess / VLESS / Trojan), HAProxy + Nginx multiplexer, CAKE QoS, and a dark **Web Panel (port 9090)**.

Everything is controlled from the terminal menu and from the web panel — a proper deploy suite.

![Ubuntu](https://img.shields.io/badge/Ubuntu-20.04%20--%2025.x-orange)
![Shell](https://img.shields.io/badge/Shell-Bash-green)
![Panel](https://img.shields.io/badge/Panel-Python%20(stdlib%20only)-blue)
![Status](https://img.shields.io/badge/Status-v1.0-brightgreen)

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
- **Terminal Menu** — 12 options (Install, Domain, SSL, Users, Fix, QoS, Stats, Telegram, Uninstall...)
- **Web Panel (9090)** — Dashboard, SSH Accounts, Xray Accounts, Running Services, VPS Stats & Speedtest, Telegram Bot, Settings, Panel Admins + Activity Log
- **SSH** — Dropbear based, Direct / SSL / WS / WS-SSL, with an auto-generated payload
- **Xray** — VMess / VLESS / Trojan over WS / TCP / XHTTP, 5 links per account + QR
- **SSL** — Let's Encrypt, auto issue + renew (self-signed fallback if DNS fails)
- **Limits System** — Days / GB / IP limit on every account, enforced by the limit monitor
- **Ping Stability** — CAKE QoS (anti-bufferbloat) in one click
- **Telegram Bot** — set up with a BotFather token, with bot admin management
- **Maintenance** — Fix SSH-WS/SSL stack, Xray repair, Install All — one click from the panel

## 📋 Requirements

| Item | Detail |
|---|---|
| OS | Ubuntu 20.04 / 22.04 / 24.04 / 25.x (a fresh VPS is best) |
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
| 01 | Install All Components | Full deploy (14 steps) |
| 02 | Add / Change Domain | Set the domain + DNS check |
| 03 | SSL Certificate | Issue / renew Let's Encrypt |
| 04 | Manage Users / Limits / Banner | SSH/Xray users, limits, SSH banner |
| 05 | Status & Ports | Service and port status |
| 06 | Fix SSH-WS / SSL Stack | Reconfigure the stack |
| 07 | Ping Stability (QoS) | CAKE QoS on/off |
| 08 | VPS Stats / Speedtest | Server info + speedtest |
| 09 | Telegram Bot | Bot setup |
| 10 | Web Panel | Panel link / login / port |
| 11 | Uninstall | Remove everything |
| 12 | Exit | — |

## 🌐 Web Panel

- **URL:** `https://<domain>:9090` — Login: `admin` + the install password
- **Dashboard** — CPU/RAM/Disk, SSH/Xray account counts, **9/9 services status**, server info
- **SSH Accounts** — create an account and get a ready-made output card: username, password, host, SNI, expiry, ports, **payload** (with a Copy button)
- **Xray Accounts** — VMess/VLESS/Trojan, auto-generated UUID, **TLS / NTLS / TCP / XHTTP TLS / XHTTP NTLS** links per account + QR code
- **VPS Stats** — full server info + live graph + **Run Speedtest**
- **Telegram Bot** — status, bot setup, bot admins (no restart needed)
- **Settings** — change domain + issue SSL, SSH banner (HTML, max 2000 bytes), Ping QoS, maintenance buttons, panel password change

## 📏 Account Limits Rule ⚠️

The same rule applies everywhere — SSH and Xray:

| Field | Meaning |
|---|---|
| **Days** | **A literal count.** Default **30**. Expiry = the account creation date + days. (There is no "unlimited" for Days — enter 30 and the account expires after 30 days.) |
| **GB limit (0 = unlimited)** | `0` = **Unlimited**. Enter a number and the account is stopped once that much data has been used — the limit is enforced. |
| **IP limit (0 = unlimited)** | `0` = **Unlimited**. Enter a number and no more than that many IPs/logins are allowed — the limit is enforced. |

**Example:** Days `30`, GB `0`, IP `0` → Expiry is **30 days after creation**, Data: **Unlimited**, IP Limit: **Unlimited**.

## 🔌 Ports

| Port | Service |
|---|---|
| 22 · 109 · 143 | SSH Direct (Dropbear: 109 · 143) |
| 80 | SSH-WS → configured path (default `/sshws`) |
| 443 | SSH-WS-SSL (TLS) + SSH-SSL (SNI/Host = domain) |
| 8443 | Xray TCP |
| 9090 | Web Panel (HTTPS) |

## 📂 Project Structure

```
vps-manager-suite/
├── install.sh          # Installer + GitHub bootstrap (the one-command install)
├── menu.sh             # Terminal menu (01-12)
├── update.sh           # One-command update: vpsupdate
├── uninstall.sh
├── lib/common.sh       # Shared functions + limit logic
├── web/panel.py        # Web panel (Python stdlib only - no pip needed)
├── templates/          # nginx, haproxy, xray configs
└── scripts/            # fix-stack, qos, speedtest, limit-check, sync-xray, telegram-bot, ws-proxy
```

## 🔄 Update

To install the latest version at any time, run one command:

```bash
sudo vpsupdate
```

It fetches the latest suite from GitHub, updates the panel/menu and restarts the panel — your accounts and data stay safe.

## 🔒 Security Notes

- Change the panel password **immediately** after installing (Settings → My Account)
- Never write your GitHub token, Telegram token or panel password into code or commits
- After changing the domain, issue SSL again (Settings → Issue SSL Certificate)
- SSH users are created **tunnel-only** — they get no shell access to the VPS

## 🗑️ Uninstall

```bash
sudo bash uninstall.sh
```

Or use terminal menu option **11**.

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
