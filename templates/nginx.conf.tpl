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
    # Internal router - only HAProxy/Xray se traffic aayega
    server {
        listen 127.0.0.1:18080;
        server_name __DOMAIN__;
        # Xray WS paths
        location /vmess { proxy_pass http://127.0.0.1:10001; proxy_http_version 1.1; proxy_set_header Upgrade $http_upgrade; proxy_set_header Connection "upgrade"; proxy_set_header Host $host; }
        location /vless { proxy_pass http://127.0.0.1:10002; proxy_http_version 1.1; proxy_set_header Upgrade $http_upgrade; proxy_set_header Connection "upgrade"; proxy_set_header Host $host; }
        location /trojan { proxy_pass http://127.0.0.1:10003; proxy_http_version 1.1; proxy_set_header Upgrade $http_upgrade; proxy_set_header Connection "upgrade"; proxy_set_header Host $host; }
        location /xhttp { proxy_pass http://127.0.0.1:10004; proxy_http_version 1.1; proxy_set_header Host $host; }
        # SSH-WS: SSH accounts ka payload isi path (/sshws) par aata hai -> ws-proxy (8080) -> Dropbear (109)
        location /sshws { proxy_pass http://127.0.0.1:8080; proxy_http_version 1.1; proxy_set_header Upgrade $http_upgrade; proxy_set_header Connection "upgrade"; proxy_set_header Host $host; proxy_read_timeout 3600s; }
        location / { return 200 "VPS Manager Suite - OK\n"; add_header Content-Type text/plain; }
    }
}
