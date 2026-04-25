# Nginx config for wikihub.md prod

These files mirror what is deployed at `/etc/nginx/conf.d/` and `/etc/nginx/sites-enabled/` on the GCP prod box (`wikihub-prod` in `us-east1-b`, IP 35.237.163.58).

## Files
- `wikihub-bot-ratelimit.conf` → `/etc/nginx/conf.d/wikihub-bot-ratelimit.conf`. UA map for AI/SEO bots + global rate-limit zone (10 r/m per UA).
- `wikihub-ai-ratelimit.conf` → `/etc/nginx/conf.d/wikihub-ai-ratelimit.conf`. Per-IP rate-limit zones for /api/v1/agent/chat (6 r/m) and general /api/ (60 r/m).
- `wikihub.conf` → `/etc/nginx/sites-enabled/wikihub`. Main site config: HTTP/HTTPS server blocks, upstream wikihub_app, regex location matching /history and /commit/<sha> that 429s bots, /api/ pass-through with proxy_intercept_errors off, welcome-page interception on 404 + 429.
- `wikihub-mcp.conf` → `/etc/nginx/sites-enabled/wikihub-mcp`. mcp.wikihub.md → 127.0.0.1:4200 (the standalone Node MCP server).

## Deploy
Just `scp` these files into place on the prod box and `sudo nginx -t && sudo systemctl reload nginx`. There is no automated deploy yet — see beads wikihub-9ntv.
