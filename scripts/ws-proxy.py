#!/usr/bin/env python3
"""VPS Manager Suite - WebSocket-to-SSH proxy (original code)

Relays SSH-over-WebSocket traffic to the local SSH backend (Dropbear):

    client --WS--> nginx /sshws --> THIS PROXY --> 127.0.0.1:109
    client --TLS 443--> HAProxy --CONNECT--> THIS PROXY --> Dropbear

Three ways in, one raw tunnel out - the handler speaks HTTP until the
connection turns into a tunnel:
  1. WebSocket upgrade: it completes the handshake itself (RFC 6455
     Sec-WebSocket-Accept), replies 101, and then becomes a raw TCP
     tunnel - the payload clients used with these SSH accounts speak
     raw SSH after the upgrade.
  2. CONNECT (SSH-over-SSL on port 443): HAProxy terminates TLS and
     routes CONNECT requests here directly. The proxy answers
     "HTTP/1.1 200 OK", then reads the NEXT request on the same
     connection - HTTP Custom sends its payload right after CONNECT -
     and handles it by these same rules.
  3. Payload mode: a plain HTTP request with no WebSocket upgrade is
     answered "HTTP/1.1 200 OK" and the connection becomes a raw tunnel,
     so the SSH banner from Dropbear reaches the client.
Anything that is not HTTP at all is forwarded untouched, as before.

Real client IPs: WS users would otherwise all appear as 127.0.0.1,
which makes IP limits impossible. The proxy recovers the real IP from
  1. X-Real-IP / X-Forwarded-For headers (nginx sets them on /sshws
     from the restored client address, incl. Cloudflare), or
  2. a PROXY protocol header (v1 text + v2 binary) on the dedicated
     listener (default port 8081) when an upstream sends one,
and appends one JSON line per connection to
<VPSMGR_DATA_DIR>/ws_connections.log:
    {"ts": 1730000000.0, "ip": "203.0.113.9", "via": "x-real-ip"}
The monitor correlates those events with Dropbear's own login records
(which only see this proxy) to learn each WS user's real IP. The log
trims itself at ~1 MB.

Env: WS_PROXY_HOST, WS_PROXY_PORT (8080), WS_PROXY_PROXY_PORT (8081),
     SSH_PORT_INTERNAL (109), VPSMGR_DATA_DIR
"""
import base64, hashlib, json, os, select, socket, struct, threading, time

WS_GUID = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"
LISTEN_HOST = os.environ.get("WS_PROXY_HOST", "0.0.0.0")
LISTEN_PORT = int(os.environ.get("VPSMGR_WSPROXY_PORT", os.environ.get("WS_PROXY_PORT", "8080")))
PROXY_PORT = int(os.environ.get("VPSMGR_WSPROXY_PROXY_PORT", os.environ.get("WS_PROXY_PROXY_PORT", "8081")))
TARGET = ("127.0.0.1", int(os.environ.get("VPSMGR_WSPROXY_TARGET_PORT", os.environ.get("SSH_PORT_INTERNAL", "109"))))
DATA_DIR = os.environ.get("VPSMGR_DATA_DIR", "/var/lib/vps-manager")
LOG_PATH = os.path.join(DATA_DIR, "ws_connections.log")
PROXY_V2_SIG = b"\r\n\r\n\x00\r\nQUIT\n"
_log_lock = threading.Lock()


def log_connection(ip, via):
    try:
        with _log_lock:
            os.makedirs(DATA_DIR, exist_ok=True)
            try:
                if os.path.getsize(LOG_PATH) > 1_000_000:
                    with open(LOG_PATH, "rb") as f:
                        f.seek(-500_000, os.SEEK_END)
                        tail = f.read()
                    with open(LOG_PATH, "wb") as f:
                        f.write(tail[tail.find(b"\n") + 1:])
            except Exception:
                pass
            with open(LOG_PATH, "a") as f:
                f.write(json.dumps({"ts": time.time(), "ip": ip, "via": via}) + "\n")
    except Exception:
        pass


# RFC 6455's own example key. Minimal payload clients (DarkTunnel and
# friends) often send "Upgrade: websocket" with NO Sec-WebSocket-Key at
# all; answering with an EMPTY Sec-WebSocket-Accept then produces a
# malformed 101 that strict HTTP hops (HAProxy in http mode) reject
# outright. Computing the accept from this fixed fallback key keeps the
# 101 well-formed - the bytes after it are a raw SSH tunnel either way.
FALLBACK_WS_KEY = "dGhlIHNhbXBsZSBub25jZQ=="


def ws_accept(req):
    key = ""
    for line in req.decode("latin1", "ignore").split("\r\n"):
        if line.lower().startswith("sec-websocket-key:"):
            key = line.split(":", 1)[1].strip()
            break
    if not key:
        key = FALLBACK_WS_KEY
    return base64.b64encode(hashlib.sha1((key + WS_GUID).encode()).digest()).decode()


def header_ip(req):
    xri, xff = "", ""
    for line in req.decode("latin1", "ignore").split("\r\n")[1:]:
        if ":" not in line:
            continue
        k, v = line.split(":", 1)
        k, v = k.strip().lower(), v.strip()
        if k == "x-real-ip" and v:
            xri = v
        elif k == "x-forwarded-for" and v:
            xff = v.split(",")[0].strip()
    return xri or xff


def pipe(a, b):
    try:
        while True:
            # No idle timeout: an SSH tunnel may sit silent for a long
            # time and must not be cut by the proxy. (This used to break
            # the loop after 30 idle seconds, which killed every quiet
            # session no matter what timeouts HAProxy/nginx allowed.)
            r, _, _ = select.select([a], [], [])
            if not r:
                continue
            data = a.recv(8192)
            if not data:
                break
            b.sendall(data)
    except Exception:
        pass
    finally:
        for s in (a, b):
            try:
                s.close()
            except Exception:
                pass


def recv_exact(sock, n):
    buf = b""
    while len(buf) < n:
        chunk = sock.recv(n - len(buf))
        if not chunk:
            raise ConnectionError("eof")
        buf += chunk
    return buf


def read_proxy_header(client, peer_ip):
    """Consume a PROXY v1/v2 header; return (real_ip, leftover_bytes).
    leftover holds bytes already read that belong to the WS request."""
    first = recv_exact(client, 5)
    if first == b"PROXY":
        line = first
        while not line.endswith(b"\r\n") and len(line) < 128:
            line += recv_exact(client, 1)
        parts = line.decode("latin1", "ignore").split()
        if len(parts) >= 3 and parts[2]:
            return parts[2], b""
        return peer_ip, b""
    if first == PROXY_V2_SIG[:5]:
        sig = first + recv_exact(client, len(PROXY_V2_SIG) - 5)
        if sig != PROXY_V2_SIG:
            return peer_ip, sig
        hdr = recv_exact(client, 4)
        fam = hdr[0] >> 4
        length = struct.unpack(">H", hdr[2:4])[0]
        body = recv_exact(client, length) if length else b""
        if fam == 0x4 and len(body) >= 4:  # TCP over IPv4
            return ".".join(str(b) for b in body[:4]), b""
        if fam == 0x6 and len(body) >= 16:  # TCP over IPv6
            import ipaddress
            return str(ipaddress.IPv6Address(body[:16])), b""
        return peer_ip, b""
    return peer_ip, first


class BufferedConn:
    """Socket wrapper with a byte buffer: one HTTP request head can be
    read exactly (up to CRLFCRLF) while bytes that arrived in the same
    packet - an SSH banner sent right behind a payload request, say -
    stay buffered for the tunnel phase instead of being swallowed."""

    def __init__(self, sock, initial=b""):
        self.sock = sock
        self.buf = initial

    def read_head(self):
        """Return one HTTP request head including the trailing CRLFCRLF.
        A bare SSH banner line (raw client, no HTTP) is returned as-is.
        Bytes after the head stay buffered."""
        while b"\r\n\r\n" not in self.buf:
            if self.buf.startswith(b"SSH-") and b"\r\n" in self.buf:
                line, self.buf = self.buf.split(b"\r\n", 1)
                return line + b"\r\n"
            if len(self.buf) > 262144:
                raise ConnectionError("request head too large")
            chunk = self.sock.recv(65536)
            if not chunk:
                raise ConnectionError("eof while reading the request head")
            self.buf += chunk
        head, self.buf = self.buf.split(b"\r\n\r\n", 1)
        return head + b"\r\n\r\n"

    def take_buffered(self):
        data, self.buf = self.buf, b""
        return data


KNOWN_METHODS = (b"GET", b"POST", b"PUT", b"HEAD", b"DELETE", b"OPTIONS", b"PATCH")


def serve(client, addr, use_proxy):
    try:
        peer_ip = addr[0]
        real_ip, via, leftover = peer_ip, "peer", b""
        if use_proxy:
            real_ip, leftover = read_proxy_header(client, peer_ip)
            if real_ip != peer_ip:
                via = "proxy"
        conn = BufferedConn(client, leftover)
        mode, first_bytes = "", b""
        # Speak HTTP until the connection becomes a raw tunnel. An
        # SSH-over-SSL client passes through two request phases on ONE
        # connection: CONNECT first (answered 200), then its payload.
        while not mode:
            head = conn.read_head()
            if not head:
                client.close()
                return
            hip = header_ip(head)
            if hip:
                real_ip, via = hip, "x-real-ip"
            method = head.split(b" ", 1)[0].strip().upper()
            if b"upgrade: websocket" in head.lower():
                acc = ws_accept(head)
                client.sendall(("HTTP/1.1 101 Switching Protocols\r\nUpgrade: websocket\r\n"
                                "Connection: Upgrade\r\nSec-WebSocket-Accept: %s\r\n\r\n" % acc).encode())
                mode, first_bytes = "websocket", conn.take_buffered()
            elif method == b"CONNECT":
                client.sendall(b"HTTP/1.1 200 OK\r\n\r\n")
                # Not a tunnel yet - loop and handle the next request
                # (the payload) on this same connection by the same rules.
            elif method in KNOWN_METHODS:
                # Payload mode: plain HTTP, no upgrade. Confirm the
                # request, then pipe raw bytes both ways so the SSH
                # banner from the backend reaches the client.
                client.sendall(b"HTTP/1.1 200 OK\r\n\r\n")
                mode, first_bytes = "payload", conn.take_buffered()
            else:
                # Not HTTP at all - forward everything untouched.
                mode, first_bytes = "raw", head + conn.take_buffered()
        log_connection(real_ip, via)
        print("[OK] ws-proxy: %s tunnel from %s (via %s) -> SSH %s:%d"
              % (mode, real_ip, via, TARGET[0], TARGET[1]), flush=True)
        target = socket.create_connection(TARGET, timeout=10)
        if first_bytes:
            target.sendall(first_bytes)
        threading.Thread(target=pipe, args=(client, target), daemon=True).start()
        pipe(target, client)
    except Exception as e:
        print("[ERR] ws-proxy client error:", e, flush=True)
        try:
            client.close()
        except Exception:
            pass


def listener(port, use_proxy):
    srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    srv.bind((LISTEN_HOST, port))
    srv.listen(100)
    print("ws-proxy listening %s:%d (%s) -> %s" % (LISTEN_HOST, port, "PROXY" if use_proxy else "plain", TARGET), flush=True)
    while True:
        c, a = srv.accept()
        threading.Thread(target=serve, args=(c, a, use_proxy), daemon=True).start()


def main():
    threading.Thread(target=listener, args=(PROXY_PORT, True), daemon=True).start()
    listener(LISTEN_PORT, False)


if __name__ == "__main__":
    main()
