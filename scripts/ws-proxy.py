#!/usr/bin/env python3
"""Simple WebSocket -> SSH proxy (stdlib only).
Listens on 0.0.0.0:8080 (env WS_PROXY_PORT), sits behind Nginx's /sshws
route, and forwards bytes to Dropbear/SSH at 127.0.0.1:109.
It computes the real Sec-WebSocket-Accept (RFC 6455), replies 101, and
then becomes a raw TCP tunnel.
"""
import socket, threading, select, os, hashlib, base64

WS_GUID = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"

def ws_accept(req: bytes) -> str:
    key = ""
    for line in req.decode("latin1", "ignore").split("\r\n"):
        if line.lower().startswith("sec-websocket-key:"):
            key = line.split(":", 1)[1].strip(); break
    if not key: return ""
    return base64.b64encode(hashlib.sha1((key + WS_GUID).encode()).digest()).decode()
LISTEN_HOST = os.environ.get("WS_PROXY_HOST", "0.0.0.0")
LISTEN_PORT = int(os.environ.get("WS_PROXY_PORT", "8080"))
TARGET = ("127.0.0.1", int(os.environ.get("SSH_PORT_INTERNAL", "109")))

def pipe(a, b):
    try:
        while True:
            r, _, _ = select.select([a], [], [], 30)
            if not r: break
            data = a.recv(8192)
            if not data: break
            b.sendall(data)
    except Exception: pass
    finally:
        for s in (a, b):
            try: s.close()
            except Exception: pass

def handle(client, addr):
    try:
        req = client.recv(4096)
        if b"upgrade: websocket" in req.lower():
            acc = ws_accept(req)
            client.sendall(f"HTTP/1.1 101 Switching Protocols\r\nUpgrade: websocket\r\nConnection: Upgrade\r\nSec-WebSocket-Accept: {acc}\r\n\r\n".encode())
        target = socket.create_connection(TARGET, timeout=10)
        threading.Thread(target=pipe, args=(client, target), daemon=True).start()
        pipe(target, client)
    except Exception as e:
        print("ws-proxy client error:", e)
        try: client.close()
        except Exception: pass

def main():
    srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    srv.bind((LISTEN_HOST, LISTEN_PORT)); srv.listen(100)
    print(f"ws-proxy listening {LISTEN_HOST}:{LISTEN_PORT} -> {TARGET}")
    while True:
        c, a = srv.accept(); threading.Thread(target=handle, args=(c, a), daemon=True).start()

if __name__ == "__main__": main()
