"""Automatic cross-app sign-in guards (code-xbh.21.6).

When a signed-out person opens a WikiHub page in a real browser, WikiHub makes
ONE top-level redirect to Ideaflow ID with ``prompt=none``. If they already
have an Ideaflow session they come straight back signed in on the same URL; if
not, the provider answers ``login_required`` and they come back to the same URL
signed out, with no error. The redirect itself lives in ``app.routes.auth``;
this module holds the pure "may we even try?" checks so they stay easy to read
and test.

Everything here fails towards "do not redirect": a missing header, an unknown
client or an unusual host means the page is served normally.
"""

import re
from urllib.parse import urlparse

# First-party session cookie (no Max-Age/Expires) marking "already tried once in
# this browser session". Set on the redirect itself, never cleared on
# login_required, so a reload or a new tab never bounces again.
AUTO_SIGNIN_COOKIE = "ideaflow_auto_signin"

# Shared bot / unfurler / scripted-client pattern from the cross-app spec.
_BOT_UA_RE = re.compile(
    r"bot|crawl|spider|slurp|facebookexternalhit|facebookcatalog|embedly|quora link preview|outbrain|"
    r"pinterest|vkshare|w3c_validator|whatsapp|telegram|discord|slack|skype|twitter|linkedin|preview|"
    r"lighthouse|inspectiontool|ahrefs|semrush|mj12|yandex|baidu|duckduck|applebot|petalbot|bytespider|"
    r"gptbot|claude|perplexity|ccbot|python|curl|wget|go-http|node-fetch|axios|okhttp|java/",
    re.IGNORECASE,
)

# Embedded webviews, in-app browsers and native shells (WikiHub's own desktop
# app is Electron, whose UA carries an Electron/ token) keep their own sign-in flows.
_WEBVIEW_UA_RE = re.compile(
    r"FBAN|FBAV|FB_IAB|Instagram|Line/|Twitter|LinkedInApp|Snapchat|; wv\)|WebView|Electron",
    re.IGNORECASE,
)

_PREFETCH_RE = re.compile(r"prefetch|prerender", re.IGNORECASE)

# Paths that are never a reading surface for a person: the auth routes
# themselves, APIs, assets, and the documents agents fetch. Most of these are
# not HTML anyway (the response-type check would skip them), but listing them
# keeps the rule obvious and robust to a future HTML error page on them.
_SKIP_PATH_PREFIXES = (
    "/auth/",
    "/api/",
    "/static/",
    "/mcp",
    "/.well-known/",
    "/agents",
    "/AGENTS.md",
    "/llms.txt",
    "/llms-full.txt",
    "/healthz",
    "/install.sh",
    "/robots.txt",
    "/sitemap.xml",
    "/favicon.ico",
    "/login",
    "/logout",
    "/signup",
    "/register",
)

# The page responses a person reads: a normal page, or the "not found / not
# allowed" page a signed-out visitor gets for something private (signing in is
# exactly what fixes those). Redirects and server errors are left alone.
_ELIGIBLE_STATUSES = frozenset({200, 401, 403, 404})


def is_bot_or_scripted(user_agent):
    """True for crawlers, unfurlers and scripted HTTP clients, and for an
    empty User-Agent. HeadlessChrome and webdriver browsers are allowed."""
    ua = user_agent or ""
    if not ua.strip():
        return True
    return bool(_BOT_UA_RE.search(ua))


def is_embedded_webview(user_agent):
    return bool(_WEBVIEW_UA_RE.search(user_agent or ""))


def is_top_level_browser_navigation(request):
    """A real, top-level document navigation: GET, Sec-Fetch-Mode=navigate,
    Sec-Fetch-Dest=document, an Accept header asking for HTML, no XHR marker
    and no prefetch/prerender. Missing Sec-Fetch headers mean "skip" (every
    current browser sends them; scripts and old clients do not)."""
    if request.method != "GET":
        return False
    headers = request.headers
    if headers.get("Sec-Fetch-Mode", "").lower() != "navigate":
        return False
    if headers.get("Sec-Fetch-Dest", "").lower() != "document":
        return False
    if "text/html" not in headers.get("Accept", "").lower():
        return False
    # fetch()/XHR wrappers, and Android WebViews (which set it to the app id).
    if headers.get("X-Requested-With"):
        return False
    for name in ("Sec-Purpose", "Purpose", "X-Purpose", "X-Moz"):
        if _PREFETCH_RE.search(headers.get(name, "")):
            return False
    return True


def path_is_skipped(path):
    for prefix in _SKIP_PATH_PREFIXES:
        bare = prefix.rstrip("/")
        if path == bare or path.startswith(bare + "/"):
            return True
    return False


def response_is_eligible(response):
    return response.status_code in _ELIGIBLE_STATUSES and response.mimetype == "text/html"


def _host_only(host):
    return (host or "").strip().lower().split(":")[0].rstrip(".")


def base_host(config):
    return _host_only(urlparse(config.get("BASE_URL") or "").hostname or "")


def host_shares_session(host, config):
    """Hosts whose WikiHub session cookie the OAuth callback (on BASE_URL) can
    read: the BASE_URL host itself and, when SESSION_COOKIE_DOMAIN is set, its
    subdomains (user and wiki subdomains). Customer custom domains never share
    the session, so a silent attempt started there could not finish."""
    host = _host_only(host)
    if not host:
        return False
    if host == base_host(config):
        return True
    cookie_domain = _host_only((config.get("SESSION_COOKIE_DOMAIN") or "").lstrip("."))
    return bool(cookie_domain) and (host == cookie_domain or host.endswith("." + cookie_domain))


def safe_return_url(target, config):
    """Validate the URL a silent attempt returns to. Relative same-origin paths
    follow the app's normal rule (no scheme, no netloc, no //, nothing under
    /auth/). An absolute URL is accepted only for an http(s) WikiHub subdomain
    that shares the session (the page the person was reading on, e.g.
    jacobcole.wikihub.md); anything else returns None."""
    target = (target or "").strip()
    if not target:
        return None
    parsed = urlparse(target)
    if not parsed.scheme and not parsed.netloc:
        if target.startswith("/") and not target.startswith("//") and not target.startswith("/\\") \
                and not parsed.path.startswith("/auth/"):
            return target
        return None
    if parsed.scheme not in ("http", "https") or "@" in parsed.netloc or "\\" in target:
        return None
    if not host_shares_session(parsed.hostname or "", config):
        return None
    if parsed.path.startswith("/auth/"):
        return None
    return target
