#!/usr/bin/env python3
"""VPS Manager Suite - Telegram Bot (original, stdlib only)
Config: telegram_token + telegram_admin_id in /etc/vps-manager/config.json
Or env: TELEGRAM_BOT_TOKEN, TELEGRAM_ADMIN_ID
Commands: /start /status /stats /users
Never hardcode the token in the code.
"""
import json, os, urllib.request, urllib.parse, time, subprocess

CONFIG = os.environ.get("VPSMGR_CONFIG", "/etc/vps-manager/config.json")

def load_cfg():
    cfg = {}
    try:
        with open(CONFIG) as f: cfg = json.load(f)
    except Exception: pass
    cfg["token"] = os.environ.get("TELEGRAM_BOT_TOKEN", cfg.get("telegram_token", ""))
    cfg["admin"] = str(os.environ.get("TELEGRAM_ADMIN_ID", cfg.get("telegram_admin_id", "")))
    return cfg

def api(token, method, **kw):
    url = f"https://api.telegram.org/bot{token}/{method}"
    data = urllib.parse.urlencode(kw).encode()
    with urllib.request.urlopen(urllib.request.Request(url, data=data), timeout=15) as r:
        return json.load(r)

def main():
    cfg = load_cfg()
    if not cfg["token"]:
        print("The Telegram token is not set. Save the token + admin ID in Panel > Telegram Bot."); return
    print("Bot polling started... (Ctrl+C to stop)")
    offset = None
    while True:
        try:
            params = {"timeout": 20}
            if offset: params["offset"] = offset
            res = api(cfg["token"], "getUpdates", **params)
            for upd in res.get("result", []):
                offset = upd["update_id"] + 1
                msg = upd.get("message", {}); chat = str(msg.get("chat", {}).get("id", "")); text = msg.get("text", "")
                if cfg["admin"] and chat != cfg["admin"]:
                    api(cfg["token"], "sendMessage", chat_id=chat, text="Unauthorized."); continue
                reply = "Commands: /status /stats"
                if text.startswith("/status"):
                    reply = subprocess.getoutput("systemctl is-active nginx haproxy xray dropbear 2>/dev/null; uptime")
                elif text.startswith("/stats"):
                    reply = subprocess.getoutput("free -h; df -h /")
                elif text.startswith("/start"):
                    reply = "VPS Manager Bot is ready. Send /status."
                api(cfg["token"], "sendMessage", chat_id=chat, text=reply[:4000])
        except KeyboardInterrupt: break
        except Exception as e: print("Error:", e); time.sleep(5)

if __name__ == "__main__": main()
