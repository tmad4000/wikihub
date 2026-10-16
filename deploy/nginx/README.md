# Nginx config for wikihub.md prod

These files mirror what is deployed at `/etc/nginx/conf.d/` and `/etc/nginx/sites-enabled/` on the GCP prod box (`wikihub-prod` in `us-east1-b`, IP 35.237.163.58).

## Files
- `wikihub-cloudflare-realip.conf` → `/etc/nginx/conf.d/wikihub-cloudflare-realip.conf`. Trusts only Cloudflare's published proxy CIDRs for `CF-Connecting-IP`, so `remote_addr` and per-IP limits represent the visitor. Direct-origin requests retain their socket IP. Refresh the list from `https://api.cloudflare.com/client/v4/ips` when Cloudflare changes it; run `python3 tests/test_nginx_realip.py` to check for drift.
- `wikihub-bot-ratelimit.conf` → `/etc/nginx/conf.d/wikihub-bot-ratelimit.conf`. UA map for AI/SEO bots + global rate-limit zone (60 r/m per UA).
- `wikihub-ai-ratelimit.conf` → `/etc/nginx/conf.d/wikihub-ai-ratelimit.conf`. Per-IP rate-limit zones for /api/v1/agent/chat (12 r/m) and general /api/ (600 r/m).
- `wikihub.conf` → `/etc/nginx/sites-enabled/wikihub`. Main site config: HTTP/HTTPS server blocks, upstream wikihub_app, regex location matching /history and /commit/<sha> that 429s bots, /api/ pass-through with proxy_intercept_errors off, welcome-page interception on 404 + 429.
- `wikihub-mcp.conf` → `/etc/nginx/sites-enabled/wikihub-mcp`. mcp.wikihub.md → 127.0.0.1:4200 (the standalone Node MCP server).

## Deploy
Just `scp` these files into place on the prod box and `sudo nginx -t && sudo systemctl reload nginx`. There is no automated deploy yet — see beads wikihub-9ntv.

## Client IP and Cloudflare edge controls (wikihub-i9a5)

Cloudflare Browser Integrity Check is **off** for the `wikihub.md` zone. It challenged legitimate non-browser agent clients with error 1010. Cloudflare's default DDoS protection remains enabled. The `wikihub-cloudflare-realip.conf` file uses Cloudflare's published IPv4 and IPv6 proxy ranges to trust `CF-Connecting-IP` only from Cloudflare peers. Nginx then applies the existing AI and API per-IP limits to the visitor, rather than grouping unrelated visitors behind a Cloudflare proxy IP. Requests sent directly to the origin retain their actual socket IP.

Before changing the trust list, compare it to `https://api.cloudflare.com/client/v4/ips` with `python3 tests/test_nginx_realip.py`. Copy it to `/etc/nginx/conf.d/wikihub-cloudflare-realip.conf`, run `sudo nginx -t`, then `sudo systemctl reload nginx`. For immediate rollback, remove that file, run `sudo nginx -t`, and reload nginx; the prior proxy-IP behavior will return. To restore Browser Integrity Check separately, set the zone's `browser_check` setting to `on` in Cloudflare.

The same VM also serves `wikihub.globalbr.ai`, `ideas.globalbr.ai`, and `admitsphere.org`. Its GCP `wikihub-web` firewall rule currently permits direct-origin web traffic from `0.0.0.0/0`; a direct request can bypass Cloudflare. Beads `wikihub-vt4a` tracks a staged, reversible restriction after those hosts and any direct consumers are inventoried.
