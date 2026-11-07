# QA Report — VPS Manager Suite

Local QA by the maintainer's test environment (Ubuntu 24.04, Python 3.12).
Rule applied throughout: **build properly, test locally first, push only after tests pass.**

## Feature-Parity Build (2026-11-07) — PASS

This build closed all 31 gaps found by a function-by-function parity audit
against the reference feature set. Every stage was tested before the next one
started. All code is original, written in the suite's own architecture
(bash menu + `lib/common.sh`, Python-stdlib panel, shared `scripts/vpsm.py`
account core).

### Test evidence

| Test | Result |
|---|---|
| `bash -n` on every `.sh`, `py_compile` on every `.py` (final sweep) | **PASS** |
| Account lifecycle unit tests (5 links × 3 protocols, VMess payload decode, renew math incl. renewing an expired account, limits / lock / reset / status semantics) | **PASS** |
| Reference case: account created today with Days 30 → Expiry **2026-12-07**; GB 0 → Data: **Unlimited**; IP 0 → IP Limit: **Unlimited** | **PASS** |
| sync-xray: 3 test accounts present in all of their protocol inbounds (14 inbounds rendered) | **PASS** |
| sync-xray live API path (stub xray/systemctl): account add → 4 live add calls, **0 restarts**; delete → 4 live remove calls, 0 restarts | **PASS** |
| fix-xray-sync: timestamped backups created, nginx WS/XHTTP locations repaired, config rebuilt, existing users preserved | **PASS** |
| Monitor (one pass, stub stats): expiry disables the account, 6 GB used vs 5 GB limit disables it, usage reset re-bases the counter (no double-counting), a 49 h-dead account is auto-deleted, WS real-IP correlation works (proxy IP resolved to the real client IP + country) | **PASS** |
| ws-proxy raw sockets: plain listener (X-Real-IP) and PROXY-protocol listener both answer the WebSocket handshake (101) and log the correct real IP | **PASS** |
| Telegram bot (stub API): start menu renders, an unknown chat gets no reply, a bot admin is refused the Super-Admin-only page, the SSH wizard creates a real system user with the correct expiry, the Xray wizard produces a 5-link account | **PASS** |
| QR encoder vs two independent QR libraries (segno + qrcodegen), versions 1–20 × all 8 masks | **PASS — 0 mismatches in 160 combinations** |
| Panel smoke test (real HTTP): login flow, all pages return 200, Xray create shows QR codes (5 per account), a Viewer-role POST is refused (403), a disabled admin cannot log in (401), the audit log records admin + source IP, streaming repair action runs to completion | **PASS** |
| Panel re-smoke after SSL/QoS wiring: all pages 200, Settings shows the QoS and SSL cards, a domain change streams the full re-provisioning to completion | **PASS** |
| SSL helpers: a 30-day certificate reports the correct days-left value and summary; the auto-renew deploy hook is installed | **PASS** |
| set-domain (stubbed systemctl/nginx/certbot): domain saved in both stores, nginx re-rendered, SSL fallback issued, **all stored Xray links regenerated to the new domain** | **PASS** |
| QoS: status output correct, `cap 25` persists to config, rule-creation failure is graceful and explained, `off` cleans all state | **PASS** (rule creation itself — see caveats) |
| Updater: dry-run OK; real apply OK (backup created, stale shell aliases stripped, missing service units created); **negative test** — an update package with a deliberately broken `menu.sh` is rejected and the installed suite stays byte-identical | **PASS** |
| panel-admin: status, login change and password reset work; reserved/invalid ports are rejected; a valid port change rewrites the service unit | **PASS** |
| Uninstaller (fake root): a "y" run removes everything (suite, units, renewal hook, backups) and restores the default nginx/haproxy configs; an "N" run removes nothing | **PASS** |
| Product text sweep for Roman-Urdu/Hinglish words (incl. future-tense forms and everyday verbs) across `.sh` / `.py` / `.md` / `.tpl` | **0 hits** |

### Strengths re-checked (not regressed)

- Full-clean standalone `uninstall.sh` (extended for the new units and re-tested)
- `vpsupdate` one-command update (now verify-then-apply with backups)
- VLESS TLS-fallback inbound kept in the Xray template
- Panel fallback address (`http://<server-IP>:9090`) still shown in terminal menu 10
- Panel slide-in drawer UI untouched; terminal screens still in the approved 46-char box format
- Limit rule unchanged: **Days is a literal count (default 30); GB/IP limit 0 = unlimited**, now enforced by the 4-second monitor instead of a 10-minute cron poll

### Known caveats (honest)

- **QoS `tc` rule creation needs a live VPS check.** The test sandbox has no
  NET_ADMIN permission, so actual traffic-control rule creation could not run
  there. Config persistence, cap validation, status output and graceful
  failure were verified. First live check: terminal menu 07 → status/apply
  on the VPS.
- **Xray live-sync has a restart fallback.** The no-restart account add/remove
  path was verified against a stub `xray` CLI that mimics the documented API.
  Against a real Xray binary, any API failure falls back to the tested
  config-test + restart path — so the worst case is the previous behavior,
  never a broken config.
- A full end-to-end `install.sh` run on a clean VPS (apt, systemd, real
  ports) cannot be reproduced in the local test environment; the suite is
  verified on the maintainer's real VPS instead.

## Earlier milestones (summary)

- **v1.2** — Video-style sidebar panel (all English, login redirects straight
  to the Dashboard), panel-created SSH accounts became real system users,
  real limit enforcement (Xray stats API + per-UID iptables counters),
  full-clean uninstaller rewrite — all locally tested.
- **v1.1** — Exact reference case verified (Days 30 / GB 0 / IP 0 → Expiry
  2026-12-07, Data: Unlimited, IP Limit: Unlimited), 5-link VLESS accounts,
  panel survives a corrupted SSL cert, ws-proxy WebSocket handshake (101),
  Xray template renders to valid JSON.
