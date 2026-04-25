# Static fallback page

`welcome.html` is the friendly fallback served by nginx on:
- Upstream 404 (Flask returns 404 → nginx substitutes welcome.html via error_page directive)
- 429 from the bot-block regex location or the global bot rate-limit
- 429 from the per-IP AI rate-limit (with a JSON body for non-GET, see wikihub.conf @welcome_redirect)

Deployed to `/var/www/wikihub-static/welcome.html` on the prod box. Form posts to /api/v1/feedback (anon-friendly).
