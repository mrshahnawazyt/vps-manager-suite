# VPS Manager Suite - HAProxy 80/443 multiplexer
# Placeholders: __DOMAIN__
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
    bind *:443
    tcp-request inspect-delay 5s
    tcp-request content accept if { req.ssl_hello_type 1 }
    # SNI based routing: own domain -> xray/nginx, everything else -> xray fallback
    acl is_own req.ssl_sni -i __DOMAIN__
    use_backend xray_tls if is_own
    default_backend xray_tls

backend nginx_http
    mode tcp
    server nginx 127.0.0.1:18080

backend xray_tls
    mode tcp
    server xray 127.0.0.1:10443
