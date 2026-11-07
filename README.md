# 🚀 VPS Manager Suite

**Ek hi script se poori VPS deploy + manage karo** — SSH-WS/SSL, Xray (VMess / VLESS / Trojan), HAProxy + Nginx multiplexer, CAKE QoS, aur ek dark **Web Panel (port 9090)**.

Terminal menu se bhi sab control hota hai, aur web panel se bhi — bilkul ek proper deploy suite ki tarah.

![Ubuntu](https://img.shields.io/badge/Ubuntu-22.04-orange)
![Shell](https://img.shields.io/badge/Shell-Bash-green)
![Panel](https://img.shields.io/badge/Panel-Python%20(stdlib%20only)-blue)
![Status](https://img.shields.io/badge/Status-v1.0-brightgreen)

---

## ✨ Features

- **1-Command Install** — GitHub se ek command, baaki sab khud download + install
- **Terminal Panel** — 12 options wala menu (Install, Domain, SSL, Users, Fix, QoS, Stats, Telegram, Uninstall...)
- **Web Panel (9090)** — Dashboard, SSH Accounts, Xray Accounts, Running Services, VPS Stats & Speedtest, Telegram Bot, Settings, Panel Admins + Activity Log
- **SSH** — Dropbear based, Direct / SSL / WS / WS-SSL, auto payload generate hota hai
- **Xray** — VMess / VLESS / Trojan, WS / TCP / XHTTP, har account ke 5 links + QR
- **SSL** — Let's Encrypt auto issue + auto-renew (DNS fail ho to self-signed fallback)
- **Limits System** — Days / GB / IP limit har account par, limit monitor har protocol par check karta hai
- **Ping Stability** — CAKE QoS (anti-bufferbloat) ek click par
- **Telegram Bot** — BotFather token se setup, bot admins management
- **Maintenance** — Fix SSH-WS/SSL stack, Xray repair, Install All — panel se ek click

## 📋 Requirements

| Cheez | Detail |
|---|---|
| OS | Ubuntu 22.04 LTS (fresh VPS best hai) |
| Access | root (sudo) |
| RAM | 1 GB+ recommended |
| Domain | Ek domain/subdomain jiska **A record VPS ki IP** par ho (SSL ke liye) |

## ⚡ Quick Install — Ek Command

VPS par root se ye chalao:

```bash
export GH_REPO=mrshahnawazyt/vps-manager-suite
curl -fsSL https://raw.githubusercontent.com/mrshahnawazyt/vps-manager-suite/main/install.sh -o install.sh
sudo -E bash install.sh your.domain.com
```

> `your.domain.com` ki jagah apna domain likho. Domain na do to script khud pooch lega.
> Sirf `install.sh` download hota hai — baaki saari files woh khud is repo se download karke install kar deta hai.

**Ya phir poora folder download karke:**

```bash
git clone https://github.com/mrshahnawazyt/vps-manager-suite.git
cd vps-manager-suite
sudo bash install.sh your.domain.com
```

Install ke end par **READY screen** milegi:
- Web Panel ka link: `https://your.domain.com:9090`
- Username: `admin` — Password wahi screen par generate hokar milega (aur credentials file me save hota hai)
- Saare ports aur services ki summary

Uske baad kabhi bhi terminal panel kholne ke liye:

```bash
bash menu.sh
```

## 🖥️ Terminal Menu

| # | Option | Kaam |
|---|---|---|
| 01 | Install All Components | Poora deploy (14 steps) |
| 02 | Add / Change Domain | Domain set + DNS check |
| 03 | SSL Certificate | Let's Encrypt issue / renew |
| 04 | Manage Users / Limits / Banner | SSH/Xray users, limits, SSH banner |
| 05 | Status & Ports | Services aur ports ki halat |
| 06 | Fix SSH-WS / SSL Stack | Stack dobara configure |
| 07 | Ping Stability (QoS) | CAKE QoS on/off |
| 08 | VPS Stats / Speedtest | Server info + speedtest |
| 09 | Telegram Bot | Bot setup |
| 10 | Web Panel | Panel link / login / port |
| 11 | Uninstall | Sab remove |
| 12 | Exit | — |

## 🌐 Web Panel

- **URL:** `https://<domain>:9090` — Login: `admin` + install wala password
- **Dashboard** — CPU/RAM/Disk, SSH/Xray counts, **9/9 services status**, server info
- **SSH Accounts** — Account banao, ready-made output milta hai: username, password, host, SNI, expiry, ports, location, **payload** (Copy button ke saath)
- **Xray Accounts** — VMess/VLESS/Trojan tabs, UUID auto-generate, har account ke **TLS / NTLS / TCP / XHTTP** links + QR code
- **VPS Stats** — Server ki poori info + live graph (har 2 second) + **Run Speedtest** (30-60 sec)
- **Telegram Bot** — Status, Setup bot, Bot Admins (restart ki zaroorat nahi)
- **Settings** — Domain change + Issue SSL, SSH Banner (HTML, max 2000 bytes), Ping QoS, Maintenance buttons, Panel password change

## 📏 Account Limits Rule ⚠️

Ye rule SSH aur Xray — har jagah same hai:

| Field | Matlab |
|---|---|
| **Days** | **Literal count.** Default **30**. Expiry = account banne ki date + days. (Days par koi unlimited nahi hota — 30 daala to 30 din baad expiry) |
| **GB limit (0 = unlimited)** | `0` = **Unlimited**. Number daala to utni GB ke baad limit lagegi |
| **IP limit (0 = unlimited)** | `0` = **Unlimited**. Number daala to utni IPs/logins se zyada allow nahi honge |

**Example:** Days `30`, GB `0`, IP `0` → Expiry aaj se **30 din baad** ki date, Data: **Unlimited**, IP Limit: **Unlimited**.

## 🔌 Ports

| Port | Kaam |
|---|---|
| 22 · 109 · 447 | SSH Direct |
| 80 | SSH-WS → path `/raretriccks` jaisa configured path |
| 443 | SSH-WS-SSL (TLS) + SSH-SSL (SNI/Host = domain) |
| 8443 | Xray TCP |
| 9090 | Web Panel (HTTPS) |

## 📂 Project Structure

```
vps-manager-suite/
├── install.sh          # Installer + GitHub bootstrap (ek command wala)
├── menu.sh             # Terminal panel (01-12)
├── uninstall.sh
├── lib/common.sh       # Shared functions + limit logic
├── web/panel.py        # Web panel (Python stdlib only — pip ki zaroorat nahi)
├── templates/          # nginx, haproxy, xray configs
└── scripts/            # fix-stack, qos, speedtest, limit-check, telegram-bot, ws-proxy
```

## 🔒 Security Notes

- Panel ka password install ke baad **foran change karo** (Settings → My Account)
- Apne GitHub token, Telegram token ya panel password **kabhi code/commit me mat likho**
- Domain badalne ke baad SSL dobara issue karo (Settings → Issue SSL Certificate)
- SSH users **tunnel-only** bante hain — unko VPS ka shell access nahi milta

## 🗑️ Uninstall

```bash
sudo bash uninstall.sh
```
Ya terminal menu ka option **11**.

## 🤝 Contributing

Ye project public hai — contributions welcome hain!
1. Repo ko **Fork** karo
2. Apni branch banao: `git checkout -b feature/meri-cheez`
3. Changes commit karo aur **Pull Request** kholo

Bugs ya suggestions ke liye **Issues** tab use karo.

## 📜 License

MIT License — dekho [LICENSE](LICENSE) file.

---

**Made with ❤️ by [@mrshahnawazyt](https://github.com/mrshahnawazyt)**
