#!/usr/bin/env python3
"""Simple WebSocket -> SSH proxy placeholder (stdlib only).
Listens 127.0.0.1:18081 / external via HAProxy, forwards bytes to Dropbear/SSH 127.0.0.1:109 (or 22).
Real WS handshake ka minimal handling: Upgrade header dekh ke raw TCP tunnel bana deta hai.
Production me isko hardened proxy se replace kar sakte ho.
"""
import socket, threading, select, os
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
        if b"Upgrade: websocket" in req or b"upgrade: websocket" in req.lower():
            client.sendall(b"HTTP/1.1 101 Switching Protocols\r\nUpgrade: websocket\r\nConnection: Upgrade\r\nSec-WebSocket-Accept: placeholder\r\n\r\n")
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
