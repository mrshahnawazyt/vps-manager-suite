# QA Report — VPS Manager Suite (Full QA Pass, 2026-11-07)

Rule applied: **pehle proper banao, khud test karo, test pass ho tab push.** Is report ke
baad bhi GitHub push NAHI kiya gaya — push parent agent karega jab woh ready ho.

Test environment: Sandbox VM (Ubuntu 24.04, root), Xray-core v25.3.6 asal binary,
Python 3 stdlib panel, test data /tmp/qa-* directories me.

---

## Sab se aham finding (real VPS par Xray/443/Dropbear na chalne ki asal wajah)

Chain saboot ke saath confirm hua:

1. `install.sh` ka certbot step `--http-01-port 8899` istemal karta tha. **HTTP-01
   challenge hamesha port 80 par aata hai** — 8899 par koi forwarding nahi thi, aur
   apt se install hue nginx/haproxy package postinst se port 80 pehle hi occupied
   tha → Let's Encrypt taqreeban hamesha fail.
2. Self-signed fallback `openssl` se banta tha, lekin **openssl apt list me tha hi
   nahi** aur command `|| true` se chup chaap fail ho jati thi → cert files banti hi nahi.
3. **Xray missing cert file ke saath bilkul start nahi hota** — asal binary se test
   kiya: `Failed to start: ... failed to parse certificate > open .../fullchain.pem:
   no such file or directory` (exit 23). → Isi liye VPS par Xray ke ports ghayab thay.
4. Panel bhi usi cert par HTTPS load karta tha (yeh pehle hi harden ho chuka tha).

Fix: shared `issue_ssl()` (lib/common.sh) — nginx/haproxy rok kar certbot standalone
(default port 80), warna openssl self-signed, phir **dono files ki existence + size
verify**, combined.pem, aur services try-restart. openssl apt list me add. Xray
install ke baad `xray run -test` bhi installer khud karta hai aur FAIL par saaf error
deta hai.

Dropbear alag bug: `DROPBEAR_EXTRA_ARGS="-p 143 -w -s -g"` me **`-s` tamam password
logins band kar deta hai** — hamare tunnel users password se login karte hain, woh
kabhi kaam hi nahi karta. Flags ab sirf `-p 143` hain.

---

## Bugs found & fixed (numbered)

1. **Certbot HTTP-01 ghalat port (8899)** → `issue_ssl()` shared function; standalone
   port 80, pehle nginx/haproxy stop. install.sh + menu option 03 dono use karte hain.
2. **openssl package missing + silent fallback fail** → apt list me add; cert verify
   step; fail par installer saaf ERROR deta hai.
3. **Xray dead agar cert missing** → upar wale fix se cert guaranteed; installer ab
   `xray run -test` karke result batata hai.
4. **Dropbear `-s -g` password logins band** → flags sirf `-p 143`.
5. **Xray accounts sirf panel record me thay, asal Xray inko pehchanta hi nahi tha**
   → naya `scripts/sync-xray.py`: accounts (expired skip) ko config ke sahi inbounds
   me daalta hai, `xray run -test` karta hai, restart karta hai. Panel create/delete
   aur menu add — teeno ab sync call karte hain. **Test: panel se bana VLESS UUID asal
   generated config me 4 inbounds me mila aur config real xray test pass.**
6. **Xray template me har inbound ke clients khaali + koi TCP inbound nahi** →
   template me seed client (`__UUID__`, install par generate) har inbound me, aur
   naya **VLESS TCP/TLS inbound 8443 (0.0.0.0)** — reference panel ki "Link TCP"
   (8443) ab asal me kaam karegi. Real binary: config test OK, 6/6 inbounds listening.
7. **Panel me VLESS ka sirf 1 link banta tha** → ab reference ki tarah **5 links**:
   TLS (443/ws), NTLS (80/ws), TCP (8443), XHTTP TLS (443), XHTTP NTLS (80) —
   panel aur menu dono me; row me saare links labeled dikhte hain.
8. **Nginx template me SSH-WS ka route hi nahi tha** → `location /sshws` →
   ws-proxy 127.0.0.1:8080 (upgrade headers + lamba read timeout). Panel/menu ke
   payload bhi `GET /sshws` ho gaye (pehle `/ws` tha jo kahin route hi nahi hota tha).
9. **ws-proxy ka Sec-WebSocket-Accept "placeholder" tha** → asal RFC 6455 accept
   calculate hota hai. **Test: RFC sample key ka mashhoor jawabi value
   `s3pPLMBiTxaQ9kYGzzhZRbK+xOo=` exact mila.**
10. **Panel login page par "Dev fallback: admin / admin123" sab ko dikhta tha** →
    line hata di. Aur asal default-credential khatra bhi khatam: creds file na mile
    to ab **random password generate** hota hai (admin123 fallback khatam).
11. **`example.com` user-facing default domain** (menu fallback, panel defaults) →
    ab default khaali hai; menu header "set nahi — option 02 se lagao" dikhata hai.
    `YOUR_SERVER_IP` summary se hata — IP detect chain: ipify → route src →
    hostname -I. Panel summary ab cert ho to **sirf HTTPS URL** deta hai aur saaf
    likhta hai ke http wala link nahi chalega (VPS par user isi se confuse hua tha:
    panel-http 000 asal me expected tha kyunki panel HTTPS par hai).
12. **Panel `/action/fix` relative path `scripts/fix-stack.sh`** — systemd service
    ki working directory se kabhi milta hi nahi → ab /opt ka asal path pehle
    dhoondta hai, na mile to saaf message.
13. **fix-stack.sh standalone par `command not found` risk** → fallback functions
    add; xray config test step add.
14. **limit-check.sh data-dir galat switch** (ssh file na ho to poora dir badal
    jata tha) → dono files check karke switch.
15. **ufw package install hota tha** (enable kabhi nahi hua — lekin risk) → apt list
    se hata diya; installer firewall ko haath hi nahi lagata.
16. **Xray official installer `bash -c "$(curl ...)"`** — download fail par khaali
    command chup chaap "chal" jati thi → pehle file me download + size verify,
    phir run; binary na mile to saaf warning.
17. **update.sh maujood hi nahi tha** → naya `update.sh` + `vpsupdate` wrapper
    (install.sh me registered): tarball pehle verify (install.sh, menu.sh,
    update.sh, web/panel.py, lib/common.sh maujood hon tab hi aage), phir /opt
    update, panel/ws-proxy files + wrappers refresh, vps-panel/ws-proxy restart,
    aakhir me one-line `UPDATE OK`. `--dry-run` aur local-tarball test support.
18. **uninstall.sh wrappers adhoore saaf karta tha** → `menu` aur `vpsupdate`
    wrappers bhi remove; /opt folder ka note add.
19. Dashboard services card ka confusing text (`{active}/9 services (panel count)`)
    → saaf `{active}/{total} services active`.
20. README: `update.sh`/`vpsupdate` section + project structure update.

Port-conflict jaanch (task me uthaya gaya shak): **nginx template sirf
127.0.0.1:18080 sunta hai — 80/443 sirf HAProxy ke hain, koi conflict nahi.**
Asal VPS par 80 distro-nginx (package default) ne liya hua tha jo step 12 me
hamari config se replace/restart ho jata hai, aur certbot se pehle issue_ssl
usko rok deta hai.

---

## Tests run & results (checklist-wise)

### 1. Placeholder sweep — PASS
`grep -rn '<server-ip>|example.com|TODO|placeholder|YOUR_|FIXME'` code files me:
- `<server-ip>`, `YOUR_SERVER_IP`, `example.com` — **sab khatam**.
- बचे hue hits sirf HTML form `placeholder=` attributes hain (jaise
  `placeholder='Username'`, `placeholder='0 = unlimited'`) — ye asal UI hints hain,
  junk values nahi. README ke `your.domain.com` instructional hain (saath likha hai
  "ki jagah apna domain likho").

### 2. Syntax & template validation — PASS (ek note ke saath)
- `bash -n`: install.sh, menu.sh, uninstall.sh, update.sh, lib/common.sh,
  scripts/*.sh — **sab OK**.
- `py_compile`: web/panel.py, scripts/*.py — **sab OK**.
- Xray template (seed substitute karke): `python -m json.tool` valid;
  **asal Xray v25.3.6 binary se `run -test` = Configuration OK**; chala kar dekha —
  **6/6 inbounds listening** (10001, 10002, 10003, 10004, 10443, 8443).
- Xray fallback chain: TLS se 10443 par WS request bheja → fallback backend tak
  gaya aur **101 Switching Protocols wapas aaya** — chain zinda hai.
- HAProxy/Nginx binary validation: **PENDING — neeche limitations dekho.**

### 3. Panel end-to-end — PASS (sab)
Test port 19093, `VPSMGR_DATA_DIR=/tmp/qa-data`, config domain my.ddkvps.com:
- Login page 200; ghalat password → **401**; sahi login → 200 + session.
- SSH create (Days 30, GB 0, IP 0) → stored record: created 2026-11-07,
  **expires 2026-12-07**; list page par `Expires: 2026-12-07`, `Unlimited`,
  payload `GET /sshws` — **video wala exact case reproduce**.
- VLESS create → **5 links** (TLS/NTLS/TCP 8443/XHTTP TLS/XHTTP NTLS) sahi
  ports/paths ke saath; UUID asal xray config me sync hua (4 inbounds me).
- Saare pages 200: `/`, `/ssh`, `/xray`, `/services`, `/stats`, `/telegram`,
  `/settings`, `/admins`. Bina session ke `/` → 302 login.
- Password change: purana password 401, naya 200. **Flow kaam karta hai.**
- Broken-cert hardening pehle se verified tha (garbage cert → panel HTTP par
  200 deta hai, crash nahi).

### 4. Menu automated — PASS
- Option 10: asal public IP + credentials file wala **asal password** dikha;
  **poori output me `command not found` 0 dafa**.
- Option 05: services status + listening ports print hue; option 12 saaf exit.
- Option 04-b se VLESS banaya: **5 links** print, `Expiry: 2026-12-07 |
  Data: Unlimited | IP Limit: Unlimited`.

### 5. limit-check.sh — PASS (4/4 cases)
- 40 din purana, Days 30 → **EXPIRED** flagged.
- used_gb=99999 lekin GB/IP 0 → **OK (kabhi flag nahi)**.
- used 15 GB / limit 10 → **QUOTA FULL** flagged.
- Normal account → OK, sahi expiry date ke saath.
- common.sh helpers alag se: `expires_on 2026-11-07 30 = 2026-12-07`,
  quota/ip checks 0 par skip — sab sahi.

### 6. install.sh review — PASS (fixes ke baad)
- Certbot: ab `issue_ssl()` — port 80 khaali karke standalone; 8899 wala
  tareeqa khatam.
- ufw: kahin enable nahi hota; package bhi hata diya.
- Service names saheeh: nginx, haproxy, dropbear, ws-proxy, vps-panel, xray —
  units jin files ki taraf ishara karti hain woh installer asal me install
  karta hai (panel.py → /usr/local/bin/vps-panel.py, ws-proxy.py →
  /usr/local/bin/vps-ws-proxy.py).
- Cron path: suite ke /opt relocation ke baad `$SCRIPT_DIR` = /opt/... →
  cron asal jagah point karta hai (code flow se verify).
- Single-file bootstrap branch: akeli install.sh + ghalat branch de kar test —
  download fail par **graceful exit, kuch install nahi hua**.

### 7. update.sh — PASS
- `bash -n` OK. `--dry-run` local tarball se: verify OK, kuch apply nahi hua.
- Full local run (systemctl stub ke saath, temp suite dir): files apply hue,
  wrappers bane, panel.py refresh (hardened line maujood), aakhir me
  **one-line `UPDATE OK`** print hua.
- Kharab tarball par safety: zaroori file ghayab ho to update ruk jata hai,
  purana suite mehfooz rehta hai (code me verify step; dry-run se tasdeeq).

---

## Jo local test NAHI ho saka (imaandari se)

- **HAProxy `haproxy -c` aur Nginx `nginx -t` asal binaries se nahi chal sake:**
  is sandbox ki apt mirrors (fake-IP DNS proxy ki wajah se) itni kharab hain ke
  do koshishon ke bawajood packages install nahi hue. Templates ki jaanch:
  HAProxy template pehle se chalti hui shakl me hai (tabdeeli nahi ki gayi) aur
  Nginx template me sirf ek standard `location /sshws` proxy block add hua hai —
  dono ka manual syntax review kiya gaya hai. **Pehli asal VPS/mint-VM koshish par
  `haproxy -c` + `nginx -t` lazmi confirm karna** (installer khud bhi ye dono test
  chalata hai aur warning deta hai).
- **Asal systemd boot, asal Let's Encrypt issuance, aur asal Dropbear login**
  sandbox me nahi ho sakte — ye pehle fresh-VPS test me hi verify honge.
  Certbot ka naya flow standard standalone (port 80) hai jo aam taur par chalta
  hai jab domain ka A record sahi ho.
- **IP limit ka live enforcement** (connection counting) abhi bhi implemented
  nahi — limit store/display hota hai, expiry aur GB quota limit-check enforce
  karta hai. Xray ki IP counting ke liye API integration chahiye — alag feature hai.
- Panel se bane **SSH users asal system users nahi bante** (sirf menu banata
  hai useradd se) — ye pehle se aisi design thi; panel record + payload deta hai.

## Push se pehle ki halat

Saari files local par fix + test ho chuki hain. **GitHub push is QA ke baad
parent agent karega** — uske baad user ko sirf ek chhoti command milegi:
pehle `vpsupdate` (naya update system) ya fresh install ek command se.
