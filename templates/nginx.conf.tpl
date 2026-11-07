# VPS Manager Suite - Nginx internal router template
# Placeholders: __DOMAIN__, __PANEL_PORT__
user www-data;
worker_processes auto;
pid /run/nginx.pid;
events { worker_connections 1024; }
http {
    access_log /var/log/nginx/access.log;
    error_log /var/log/nginx/error.log;
    sendfile on;
    # Cloudflare real-IP restore: when traffic arrives through Cloudflare,
    # recover the visitor IP from CF-Connecting-IP (Cloudflare ranges only)
    set_real_ip_from 103.21.244.0/22;
    set_real_ip_from 103.22.200.0/22;
    set_real_ip_from 103.31.4.0/22;
    set_real_ip_from 104.16.0.0/13;
    set_real_ip_from 104.24.0.0/14;
    set_real_ip_from 108.162.192.0/18;
    set_real_ip_from 131.0.72.0/22;
    set_real_ip_from 141.101.64.0/18;
    set_real_ip_from 162.158.0.0/15;
    set_real_ip_from 172.64.0.0/13;
    set_real_ip_from 173.245.48.0/20;
    set_real_ip_from 188.114.96.0/20;
    set_real_ip_from 190.93.240.0/20;
    set_real_ip_from 197.234.240.0/22;
    set_real_ip_from 198.41.128.0/17;
    set_real_ip_from 2400:cb00::/32;
    set_real_ip_from 2606:4700::/32;
    set_real_ip_from 2803:f800::/32;
    set_real_ip_from 2405:b500::/32;
    set_real_ip_from 2405:8100::/32;
    set_real_ip_from 2a06:98c0::/29;
    set_real_ip_from 2c0f:f9d8::/32;
    real_ip_header CF-Connecting-IP;
    real_ip_recursive on;
    # Internal router - only HAProxy/Xray traffic reaches here
    server {
        listen 127.0.0.1:18080;
        server_name __DOMAIN__;
        # Xray WS paths
        location /vmess { proxy_pass http://127.0.0.1:10001; proxy_http_version 1.1; proxy_set_header Upgrade $http_upgrade; proxy_set_header Connection "upgrade"; proxy_set_header Host $host; proxy_set_header X-Real-IP $remote_addr; }
        location /vless { proxy_pass http://127.0.0.1:10002; proxy_http_version 1.1; proxy_set_header Upgrade $http_upgrade; proxy_set_header Connection "upgrade"; proxy_set_header Host $host; proxy_set_header X-Real-IP $remote_addr; }
        location /trojan { proxy_pass http://127.0.0.1:10003; proxy_http_version 1.1; proxy_set_header Upgrade $http_upgrade; proxy_set_header Connection "upgrade"; proxy_set_header Host $host; proxy_set_header X-Real-IP $remote_addr; }
        location /xhttp { proxy_pass http://127.0.0.1:10004; proxy_http_version 1.1; proxy_set_header Host $host; proxy_set_header X-Real-IP $remote_addr; }
        location /xhttp-vmess { proxy_pass http://127.0.0.1:10005; proxy_http_version 1.1; proxy_set_header Host $host; proxy_set_header X-Real-IP $remote_addr; }
        location /xhttp-trojan { proxy_pass http://127.0.0.1:10006; proxy_http_version 1.1; proxy_set_header Host $host; proxy_set_header X-Real-IP $remote_addr; }
        # SSH-WS: SSH account payloads arrive on this path (/sshws) -> ws-proxy (8080) -> Dropbear (109)
        location /sshws { proxy_pass http://127.0.0.1:8080; proxy_http_version 1.1; proxy_set_header Upgrade $http_upgrade; proxy_set_header Connection "upgrade"; proxy_set_header Host $host; proxy_set_header X-Real-IP $remote_addr; proxy_set_header X-Forwarded-For $remote_addr; proxy_read_timeout 3600s; }
        location / { return 200 "VPS Manager Suite - OK\n"; add_header Content-Type text/plain; }
    }
}
