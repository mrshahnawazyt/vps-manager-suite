# VPS Manager Suite - HAProxy 80/443 multiplexer
# Placeholders: __DOMAIN__, __WSPROXY_PORT__
#
# Port 80: plain TCP pass-through to the internal nginx router (the
#   router fans out to ws-proxy and the Xray loopback inbounds).
# Port 443: HAProxy TERMINATES TLS with the suite certificate bundle
#   (/etc/vps-manager/ssl/haproxy.pem) and routes by what is INSIDE the
#   encrypted stream:
#     - CONNECT requests (SSH-over-SSL clients such as HTTP Custom ask
#       for a tunnel first, then send their payload) go straight to
#       ws-proxy, which answers 200 and turns the connection into a raw
#       SSH tunnel to Dropbear.
#     - The WS / XHTTP account paths - and everything else - go to the
#       nginx router, whose locations fan out to ws-proxy (/sshws) and
#       the Xray loopback inbounds (/vmess, /vless, /trojan, /xhttp*).
#       That is how the WS-TLS account links on 443 keep working now
#       that the old Xray TLS-fallback entry point is retired.
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
    mode http
    bind *:443 ssl crt /etc/vps-manager/ssl/haproxy.pem alpn http/1.1
    timeout client 3600s
    # Present the real client IP to the backends: nginx uses it to fill
    # X-Real-IP for /sshws, and ws-proxy reads it on CONNECT requests.
    # set-header REPLACES any client-supplied value, so it cannot be
    # spoofed past this point.
    http-request set-header X-Forwarded-For %[src]
    # SSH-over-SSL: CONNECT first, payload second - ws-proxy speaks both.
    acl is_connect method CONNECT
    use_backend ws_proxy_direct if is_connect
    # Xray WS/XHTTP account paths and SSH-WS over TLS
    acl is_router_path path_beg /sshws /vmess /vless /trojan /xhttp /xhttp-vmess /xhttp-trojan
    use_backend nginx_https if is_router_path
    default_backend nginx_https

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
