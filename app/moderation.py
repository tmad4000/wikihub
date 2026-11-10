"""Request-level moderation hooks (wikihub-i8lo, wikihub-9ras) and crawler
hygiene for auth endpoints.

before_request:
  * OAuth callbacks hit without state/code answer 400 immediately — no DB,
    no session, no redirect chain (crawlers hammer these).
  * Any URL owned by a user (/@user/..., including subdomain/custom-domain
    rewrites, git paths, and /api/v1/wikis/<user>/...) returns 410 Gone when
    that user is suspended.
  * Otherwise, if the owner is untrusted (app.trust), mark the response
    noindex.
  * For /@user/... URLs, record the effective curation judgement
    (app.curation) for the account, wiki and page in the URL: base.html
    renders the "Curated against" banner and <meta name="wikihub:curation">
    from it, and a negative judgement marks the response noindex.

after_request:
  * X-Robots-Tag: noindex for untrusted owners' content and for every /auth/*
    response.
"""

import re

from flask import jsonify, render_template, request

_OWNER_PATH_RE = re.compile(r"^/(?:@|api/v1/wikis/@?)([a-z0-9_-]+)(?=[/.]|$)")
_READER_PATH_RE = re.compile(r"^/@([a-z0-9_-]+)(?:/([^/]+)(?:/(.*))?)?$")
_OAUTH_CALLBACK_PATHS = ("/auth/ideaflow/callback", "/auth/google/callback")


def owner_from_path(path):
    m = _OWNER_PATH_RE.match(path or "")
    return m.group(1) if m else None


def _gone(path):
    if path.startswith("/api/"):
        return jsonify({"error": "gone", "message": "This account has been suspended"}), 410
    if ".git" in path or path.endswith((".txt", ".json", ".zip", ".rss")):
        return "Gone: this account has been suspended\n", 410, {"Content-Type": "text/plain; charset=utf-8"}
    return render_template(
        "error.html", code=410, title="Account suspended",
        message="This content is no longer available.",
    ), 410


def before_request():
    path = request.path
    if path in _OAUTH_CALLBACK_PATHS:
        args = request.args
        if not args.get("state") or not (args.get("code") or args.get("error")):
            resp = jsonify({"error": "bad_request", "message": "Missing OAuth state/code"})
            resp.status_code = 400
            resp.headers["X-Robots-Tag"] = "noindex, nofollow"
            resp.headers["Cache-Control"] = "no-store"
            return resp
        return None

    username = owner_from_path(path)
    if not username:
        return None
    from app.models import User
    from app.trust import is_trusted

    owner = User.query.filter_by(username=username).first()
    if owner is None:
        return None
    if owner.suspended_at is not None:
        return _gone(path)
    if not is_trusted(owner):
        request.environ["wikihub.noindex"] = True
    reader = _READER_PATH_RE.match(path)
    if reader:
        from app.curation import mark_request
        mark_request(owner.username, reader.group(2), reader.group(3))
    return None


def after_request(response):
    if request.path.startswith("/auth/") or request.environ.get("wikihub.noindex"):
        if "X-Robots-Tag" not in response.headers:
            response.headers["X-Robots-Tag"] = "noindex"
    return response


def owner_untrusted(username):
    """True when external links in this owner's content must be nofollow."""
    if not username:
        return False
    # per-request cache (request.environ, not g: g can outlive a request
    # when an outer app context is active, e.g. in the test harness)
    cache = request.environ.setdefault("wikihub.untrusted_owner", {})
    if username not in cache:
        from app.models import User
        from app.trust import is_trusted
        user = User.query.filter_by(username=username).first()
        cache[username] = not is_trusted(user) if user else True
    return cache[username]


def content_needs_ugc(username, slug=None, page_path=None):
    """rel="nofollow ugc" for rendered content: the owner is untrusted, or
    the account, wiki or page is curated against (wikihub-f3b7)."""
    if owner_untrusted(username):
        return True
    from app.curation import request_negative_for
    return request_negative_for(username, slug, page_path)
