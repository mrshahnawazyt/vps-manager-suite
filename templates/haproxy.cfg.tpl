# VPS Manager Suite - HAProxy 80/443 multiplexer
# Placeholders: __DOMAIN__, __WSPROXY_PORT__
#
# Port 80: plain TCP pass-through to the internal nginx router (the
#   router fans out to ws-proxy and the Xray loopback inbounds).
# Port 443: HAProxy TERMINATES TLS with the suite certificate bundle
#   (/etc/vps-manager/ssl/haproxy.pem), then a TCP-mode dispatch
#   frontend on loopback (127.0.0.1:10444, fed via PROXY protocol so
#   the real client address survives) routes by the first decrypted
#   bytes - the dispatch cannot live in the TLS frontend itself,
#   because payload() there never sees the decrypted stream:
#     - "SSH-..." -> raw SSH straight to ws-proxy -> Dropbear.
#       DarkTunnel's "Direct with SNI" mode speaks RAW SSH over TLS -
#       no HTTP at all - which an http-mode frontend can never pass.
#     - every "GET /sshws" head -> also straight to ws-proxy: minimal
#       payload clients send handshakes HAProxy's HTTP layer rejects
#       outright (see https_dispatch below).
#     - anything else -> the loopback HTTP frontend (127.0.0.1:10445),
#       which routes what is INSIDE the stream:
#         - CONNECT requests (SSH-over-SSL clients such as HTTP Custom
#           ask for a tunnel first, then send their payload) go straight
#           to ws-proxy, which answers 200 and turns the connection into
#           a raw SSH tunnel to Dropbear.
#         - The WS / XHTTP account paths - and everything else - go to
#           the nginx router, whose locations fan out to ws-proxy
#           (/sshws) and the Xray loopback inbounds (/vmess, /vless,
#           /trojan, /xhttp*). That is how the WS-TLS account links on
#           443 keep working now that the old Xray TLS-fallback entry
#           point is retired.
#   SSH and HTTP clients both send their first bytes immediately after
#   the handshake, so the inspect delay is only a cap - the decision is
#   made as soon as enough bytes arrive. A client that sends NOTHING
#   before the delay expires falls through to the HTTP frontend: a raw
#   SSH client always speaks first, while an HTTP client may hesitate.
#   Because TLS ends here, ANY SNI (e.g. a fronting host) is answered
#   with the suite certificate - SNI fronting keeps working.
global
    log /dev/log local0
    maxconn 2048
    daemon
defaults
    log global
    mode tcp
    timeout connect 5s
    timeout client 60s
    timeout server 60s

frontend http_in
    bind *:80
    # Send HTTP to the internal nginx (for WS without TLS)
    default_backend nginx_http

frontend https_in
    mode tcp
    bind *:443 ssl crt /etc/vps-manager/ssl/haproxy.pem alpn http/1.1
    timeout client 3600s
    # Config Manager SNI map (panel > Config Manager, rendered by
    # scripts/config-sync.py into /etc/vps-manager/sni.map): the SNI of
    # the TLS session terminated HERE is visible as ssl_fc_sni -
    # req.ssl_sni sees nothing once TLS is terminated, and the dispatch
    # frontend behind the PROXY hop cannot see the SNI at all (both
    # measured in simulation). The map only ever names
    # https_dispatch_in, this chain's own default entry, so a map HIT
    # routes exactly like a MISS; with an empty map (no profiles saved)
    # the acl never matches and routing is byte-identical to before.
    # The map file is created empty by install / Fix Stack / update
    # before HAProxy starts, because a missing map file fails the
    # config check.
    acl cm_sni_mapped ssl_fc_sni,map(/etc/vps-manager/sni.map) -m found
    use_backend %[ssl_fc_sni,map(/etc/vps-manager/sni.map)] if cm_sni_mapped
    # TLS termination ONLY. The protocol dispatch cannot live here:
    # payload() in this frontend does not see the decrypted stream when
    # the switching decision is made (measured: the capture is empty
    # and the ACL never matches), so the clear stream is handed to the
    # dispatch frontend on loopback, which CAN inspect it.
    default_backend https_dispatch_in

backend https_dispatch_in
    mode tcp
    timeout server 3600s
    server dispatch 127.0.0.1:10444 send-proxy

frontend https_dispatch
    mode tcp
    bind 127.0.0.1:10444 accept-proxy
    timeout client 3600s
    tcp-request inspect-delay 2s
    # Gate the switch below until a full first chunk is buffered: if a
    # connection's first segment arrived with fewer than 10 bytes, the
    # payload fetches would fail undecidable and a /sshws head could be
    # misrouted to the HTTP frontend. Connections that never send 10
    # bytes fall through at the inspect-delay expiry, as before.
    tcp-request content accept if { payload(0,10) -m len 10 }
    # Raw SSH over TLS: the client's first bytes are its SSH banner.
    acl is_raw_ssh payload(0,4) -m str "SSH-"
    # EVERY /sshws head goes straight to ws-proxy, bypassing the HTTP
    # frontend: HAProxy's HTTP layer rejects the minimal handshakes
    # these SSH apps send, no matter what answers behind it (no
    # Connection header -> the 101 comes back judged an "invalid
    # response", 502; Connection but no Sec-WebSocket-Key -> the
    # request itself is 400ed before any backend runs), while ws-proxy
    # completes or tolerates every shape - proper WebSocket, minimal
    # payload, plain payload. Nothing is lost by the shorter path: the
    # PROXY header on the ws_proxy_raw backend still delivers the real
    # client IP to ws-proxy's connection log, which is all the nginx
    # hop used to add for this path.
    acl is_sshws_req payload(0,10) -m str "GET /sshws"
    use_backend ws_proxy_raw if is_raw_ssh
    use_backend ws_proxy_raw if is_sshws_req
    default_backend https_http_in

frontend https_http
    mode http
    bind 127.0.0.1:10445 accept-proxy
    timeout client 3600s
    # Present the real client IP to the backends: nginx uses it to fill
    # X-Real-IP for /sshws, and ws-proxy reads it on CONNECT requests.
    # set-header REPLACES any client-supplied value, so it cannot be
    # spoofed past this point. (%[src] is the real client thanks to the
    # PROXY headers the two hops in front add - see https_http_in.)
    http-request set-header X-Forwarded-For %[src]
    # SSH-over-SSL: CONNECT first, payload second - ws-proxy speaks both.
    acl is_connect method CONNECT
    use_backend ws_proxy_direct if is_connect
    # Xray WS/XHTTP account paths and SSH-WS over TLS
    acl is_router_path path_beg /sshws /vmess /vless /trojan /xhttp /xhttp-vmess /xhttp-trojan
    use_backend nginx_https if is_router_path
    default_backend nginx_https

backend https_http_in
    mode tcp
    timeout server 3600s
    # PROXY protocol hands the real client address to the loopback HTTP
    # frontend, whose %[src] (X-Forwarded-For, real-IP logging) would
    # otherwise only ever see 127.0.0.1.
    server loopback 127.0.0.1:10445 send-proxy

backend ws_proxy_raw
    mode tcp
    timeout server 3600s
    # ws-proxy's PROXY-protocol listener: it consumes the PROXY header
    # (which carries the real client IP into its connection log) and
    # then speaks raw SSH / payload / WebSocket from the same socket.
    server wsproxy 127.0.0.1:__WSPROXY_PROXY_PORT__ send-proxy

backend nginx_http
    mode tcp
    server nginx 127.0.0.1:18080

backend nginx_https
    mode http
    timeout server 3600s
    timeout tunnel 3600s
    server nginx 127.0.0.1:18080

backend ws_proxy_direct
    mode http
    timeout server 3600s
    timeout tunnel 3600s
    server wsproxy 127.0.0.1:__WSPROXY_PORT__
