"""Redirect /@user/... URLs to the canonical subdomain when one exists.

Runs as a Flask before_request handler. Only fires on the apex host
(not when the request is already on a user/wiki subdomain — that's already
handled by subdomain_middleware).

Scope of redirects (intentionally narrow for safety):
- /@<user>                          -> https://<user>.wikihub.md/
- /@<user>/<slug>                   -> https://<sub>.wikihub.md/  (if wiki has subdomain)
                                    -> https://<user>.wikihub.md/<slug>  (fallback to user profile subdomain)
- /@<user>/<slug>/<path>            -> same rules, with /<path> appended

We DO NOT redirect:
- git smart HTTP paths (.git/...)
- .zip, .json, llms.txt, llms-full.txt, graph.json, sidebar.json, history
- POST/PUT/PATCH/DELETE — only GET/HEAD
- /@user/<slug>/settings — owner-only page, keeps working on apex
- /@user/<slug>/edit/... — keeps editor URL stable
"""

import re
from flask import request, redirect

from app.models import User, Wiki
from app.subdomains import CANONICAL_SUFFIX, is_reserved, SYSTEM_SUBDOMAIN_USERS

_USER_PATH_RE = re.compile(r"^/@([a-z0-9_-]+)(?:/([^/]+)(?:/(.*))?)?$")

# extensions / suffixes that should NOT trigger subdomain redirect
_SKIP_SUFFIXES = (
    ".git", ".git/info/refs", ".git/HEAD",
    ".zip",
    "/llms.txt", "/llms-full.txt",
    "/graph.json", "/sidebar.json", "/graph",
    "/history", "/commit",
    "/settings",
    "/reindex",
    "/preview",
    "/new", "/new-folder",
)


def _is_skipped(path: str) -> bool:
    for s in _SKIP_SUFFIXES:
        if path.endswith(s) or s + "/" in path or path.endswith(s + "/"):
            return True
    # /edit live anywhere in the tail
    if path.endswith("/edit") or "/edit/" in path:
        return True
    if ".git" in path:
        return True
    return False


def maybe_redirect():
    """before_request handler. returns a Response if redirecting, else None."""
    if request.method not in ("GET", "HEAD"):
        return None

    # If middleware rewrote the path from the user's short URL, don't bounce back.
    if request.environ.get("wikihub.rewritten_from") is not None:
        return None

    path = request.path
    if not path.startswith("/@"):
        return None
    if _is_skipped(path):
        return None

    host = (request.host or "").lower().split(":")[0]
    only_wikihub_md = host == "wikihub.md" or host.endswith(".wikihub.md")
    if not only_wikihub_md:
        return None

    m = _USER_PATH_RE.match(path)
    if not m:
        return None
    username, slug, rest = m.group(1), m.group(2), m.group(3)

    # Case 2: we're on a subdomain and the path redundantly includes /@<user>/<slug>.
    # Rewrite to the short canonical form on the same host so URL bar stays pretty.
    host_kind = request.environ.get("wikihub.host_kind")
    host_name = request.environ.get("wikihub.host_name")
    if host_kind == "user" and host_name == username:
        # on <user>.wikihub.md, strip "/@<user>" prefix
        short_tail = ""
        if slug is not None:
            short_tail = "/" + slug + (("/" + rest) if rest else "")
        qs = "?" + request.query_string.decode() if request.query_string else ""
        return redirect(f"https://{host}{short_tail or '/'}{qs}", code=301)
    if host_kind == "wiki":
        wiki = Wiki.query.filter(Wiki.subdomain == host_name).first()
        if wiki and wiki.owner.username == username and wiki.slug == slug:
            short_tail = "/" + rest if rest else "/"
            qs = "?" + request.query_string.decode() if request.query_string else ""
            return redirect(f"https://{host}{short_tail}{qs}", code=301)
    if host_kind is not None:
        # we're on a subdomain but the /@path doesn't match; leave alone
        return None

    # Case 1: we're on apex wikihub.md (or www). Redirect to canonical subdomain.
    if host != "wikihub.md" and host != "www.wikihub.md":
        return None

    user = User.query.filter(User.username == username).first()
    if not user:
        return None
    # Legacy users whose names collide with reserved subdomains keep working
    # at /@<user>/... but do NOT get a subdomain redirect — UNLESS they're
    # a system user (e.g. @wikihub) which has a special subdomain override.
    if is_reserved(username) and username not in SYSTEM_SUBDOMAIN_USERS:
        if slug is None:
            return None

    qs = "?" + request.query_string.decode() if request.query_string else ""
    scheme = "https"

    if slug is None:
        # /@user -> https://user.wikihub.md/
        target = f"{scheme}://{username}{CANONICAL_SUFFIX}/{qs}"
        return redirect(target, code=301)

    wiki = Wiki.query.filter_by(owner_id=user.id, slug=slug).first()
    if not wiki:
        return None

    tail = ("/" + rest) if rest else "/"
    if wiki.subdomain:
        target = f"{scheme}://{wiki.subdomain}{CANONICAL_SUFFIX}{tail}{qs}"
    else:
        # fall back to user profile subdomain (reserved non-system users skip)
        if is_reserved(username) and username not in SYSTEM_SUBDOMAIN_USERS:
            return None
        target = f"{scheme}://{username}{CANONICAL_SUFFIX}/{slug}{tail if rest else ''}{qs}"
        if not rest:
            target = f"{scheme}://{username}{CANONICAL_SUFFIX}/{slug}{qs}"
    return redirect(target, code=301)
