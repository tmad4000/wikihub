"""GET/POST /@<username>/<slug>/-/pages/<page_id>/annotations (wikihub-3add).

Browser endpoints for reader comments. Both methods require the CSRF token
(header ``X-CSRFToken``): POST through the global CSRFProtect, GET checked
explicitly here. Responses are JSON and never cached. See app/annotations.py
for the anchoring and privacy rules.
"""

import base64
import hashlib
import re
from datetime import datetime

from cryptography.fernet import Fernet, InvalidToken

from flask import Blueprint, current_app, jsonify, request
from flask_wtf.csrf import CSRFError, validate_csrf
from sqlalchemy import tuple_
from sqlalchemy.exc import IntegrityError

from app import db, rate_limit
from app.annotations import (
    AUDIENCE_LABELS,
    MAX_BODY_CHARS,
    MAX_CITATIONS,
    MAX_QUOTE_CHARS,
    ContentUnavailable,
    PageView,
    anchor_context,
    comment_audience,
    context_matches,
    count_occurrences,
    match_key,
    parse_request_id,
    serialize,
)
from app.auth_utils import get_current_user_from_request, security_event
from app.models import Page, PageAnnotation, User, Wiki
from app.wiki_ops import load_acl_rules

annotations_bp = Blueprint("annotations", __name__)


def _json(payload, status=200, headers=None):
    resp = jsonify(payload)
    resp.status_code = status
    resp.headers["Cache-Control"] = "no-store"
    resp.headers["Vary"] = "Cookie"
    for k, v in (headers or {}).items():
        resp.headers[k] = v
    return resp


def _err(status, code, message, **extra):
    return _json({"error": code, "message": message, **extra}, status)


def _csrf_ok():
    if not current_app.config.get("WTF_CSRF_ENABLED", True):
        return True
    token = request.headers.get("X-CSRFToken") or request.headers.get("X-CSRF-Token")
    try:
        validate_csrf(token)
        return True
    except (CSRFError, Exception):  # ValidationError subclasses vary by version
        return False


def _load(username, slug, page_id, viewer):
    """Returns (view, error_response)."""
    from app.routes.wiki import _get_owner_and_wiki_or_404

    owner, wiki, _ = _get_owner_and_wiki_or_404(username, slug)
    page = db.session.get(Page, page_id)
    if page is None or page.wiki_id != wiki.id:
        return None, _err(404, "not_found", "Page not found")
    view = PageView(owner, wiki, page, viewer, load_acl_rules(owner.username, wiki.slug))
    if not view.can_read():
        # wikihub-dkp8: exists but restricted (403 signed in, 401 anonymous).
        if viewer:
            return None, _err(403, "forbidden", "This page is restricted — you don't have access.")
        return None, _err(401, "authentication_required", "This page is restricted — sign in to check your access.")
    return view, None


THREAD_PAGE_SIZE = 50
_UNSTORABLE_TEXT = re.compile(r"[\x00\ud800-\udfff]")


def _cursor_cipher(view):
    secret = current_app.secret_key
    if isinstance(secret, str):
        secret = secret.encode()
    viewer_id = view.viewer.id if view.viewer else 0
    context = f"annotation-cursor:{view.page.id}:{viewer_id}:".encode()
    key = hashlib.sha256(context + secret).digest()
    return Fernet(base64.urlsafe_b64encode(key))


def _comment_page(view, before):
    query = (db.session.query(PageAnnotation, User.username)
             .join(User, PageAnnotation.author_id == User.id)
             .filter(PageAnnotation.wiki_id == view.wiki.id,
                     PageAnnotation.page_path == view.page.path,
                     User.suspended_at.is_(None)))
    if not view.is_owner:
        query = query.filter(PageAnnotation.owner_only.is_(False),
                             db.or_(PageAnnotation.page_id == view.page.id, PageAnnotation.page_id.is_(None)))
    if before:
        query = query.filter(tuple_(PageAnnotation.created_at, PageAnnotation.id) < before)
    query = query.order_by(PageAnnotation.created_at.desc(), PageAnnotation.id.desc())
    comments = []
    cursor = None
    rows = query.limit(THREAD_PAGE_SIZE + 1).all()
    targets = {}
    for row, username in rows[:THREAD_PAGE_SIZE]:
        comment = serialize(row, view, {row.author_id: username}, _resolve_citations(row.citations, view.viewer, targets))
        if comment is not None:
            comments.append(comment)
    if len(rows) > THREAD_PAGE_SIZE:
        last = rows[THREAD_PAGE_SIZE - 1][0]
        position = f"{last.created_at.isoformat()},{last.id}".encode()
        cursor = _cursor_cipher(view).encrypt(position).decode()
    comments.reverse()
    return comments, cursor


def _unavailable():
    return _err(503, "content_unavailable", "This page's content can't be read right now. Try again shortly.")


def _path_digest(path):
    return hashlib.sha256(path.encode("utf-8")).hexdigest()[:32]


def _attempt_rules(user):
    # Cheap abuse throttle charged on every write attempt (valid or not).
    return [(f"annot:try:u:{user.id}:m", rate_limit.limit_value("ANNOTATION_MAX_ATTEMPTS_PER_USER_PER_MINUTE"), 60)]


def _budget_rules(user, view):
    # Comment budgets: checked before the write, charged only after a
    # comment was actually stored (rejected, invalid or duplicate requests
    # never use them). Page buckets are keyed by a digest of the full path,
    # so long paths cannot collide.
    page_key = f"annot:p:{view.wiki.id}:{_path_digest(view.page.path)}:d"
    return [
        (f"annot:u:{user.id}:m", rate_limit.limit_value("ANNOTATION_MAX_PER_USER_PER_MINUTE"), 60),
        (f"annot:u:{user.id}:h", rate_limit.limit_value("ANNOTATION_MAX_PER_USER_PER_HOUR"), 3600),
        (f"annot:u:{user.id}:d", rate_limit.limit_value("ANNOTATION_MAX_PER_USER_PER_DAY"), 86400),
        (page_key, rate_limit.limit_value("ANNOTATION_MAX_PER_PAGE_PER_DAY"), 86400),
    ]


def _rate_limited(retry_after, desc):
    security_event("rate_limit", "denied", scope="annotations", limit=desc)
    return _json(
        {"error": "rate_limited", "message": f"Too many comments right now. Try again in {retry_after}s.", "retry_after": retry_after},
        429, {"Retry-After": str(retry_after)},
    )


# --- citations -------------------------------------------------------------

_CITATION_URL = re.compile(r"^/@([a-z0-9_-]{1,64})/([A-Za-z0-9_.-]{1,128})/([^#?\x00-\x1f]{1,600})(#:~:text=[^\s]{1,800})?$")


def _citation_target(url):
    """'/@owner/slug/page[#:~:text=...]' -> (owner User, Wiki, Page, quote)
    or None. Only same-site wiki page paths are accepted."""
    from urllib.parse import unquote

    from app.url_utils import page_path_from_url_path

    if not isinstance(url, str):
        return None
    m = _CITATION_URL.match(url)
    if not m:
        return None
    username, slug, rest, fragment = m.groups()
    owner = User.query.filter_by(username=username).first()
    if owner is None or owner.suspended_at is not None:
        return None
    wiki = Wiki.query.filter_by(owner_id=owner.id, slug=slug).first()
    if wiki is None:
        return None
    rest = unquote(rest).rstrip("/")
    if not rest or rest.startswith("-/"):
        return None
    candidates = [rest if rest.endswith(".md") else rest + ".md"]
    spaced = page_path_from_url_path(rest)
    candidates.append(spaced if spaced.endswith(".md") else spaced + ".md")
    page = None
    for candidate in candidates:
        page = Page.query.filter_by(wiki_id=wiki.id, path=candidate).first()
        if page is not None:
            break
    if page is None:
        return None
    quote = None
    if fragment:
        quote = match_key(unquote(fragment[len("#:~:text="):]))[:MAX_QUOTE_CHARS] or None
    return owner, wiki, page, quote


def _parse_citations(raw, viewer):
    """Validate submitted citations against what the AUTHOR can read.
    Returns (stored list, None) or (None, error response)."""
    if raw in (None, []):
        return [], None
    if not isinstance(raw, list) or len(raw) > MAX_CITATIONS:
        return None, _err(400, "invalid_citations", f"Send at most {MAX_CITATIONS} citations.")
    stored, seen = [], set()
    for item in raw:
        if not isinstance(item, dict):
            return None, _err(400, "invalid_citations", "Each citation needs n and url.")
        n, url = item.get("n"), item.get("url")
        if not isinstance(n, int) or isinstance(n, bool) or not 1 <= n <= 99 or n in seen:
            return None, _err(400, "invalid_citations", "Citation numbers must be unique integers 1-99.")
        target = _citation_target(url)
        if target is None:
            return None, _err(400, "invalid_citations", f"Citation [{n}] is not a page you can read.")
        owner, wiki, page, quote = target
        if not PageView(owner, wiki, page, viewer, load_acl_rules(owner.username, wiki.slug)).can_read():
            return None, _err(400, "invalid_citations", f"Citation [{n}] is not a page you can read.")
        seen.add(n)
        stored.append({"n": n, "wiki_id": wiki.id, "path": page.path, "quote": quote})
    return stored, None


def _resolve_citations(stored, viewer, cache):
    """Per-viewer, navigable citations: only targets THIS viewer can read
    are returned; a quote (and its text-fragment link) only if the words are
    in this viewer's own view of the target page."""
    from app.reader_assistant import _text_fragment
    from app.url_utils import url_path_from_page_path

    out = []
    for item in stored or []:
        key = (item.get("wiki_id"), item.get("path"))
        if key not in cache:
            cache[key] = None
            wiki = db.session.get(Wiki, key[0]) if key[0] else None
            page = Page.query.filter_by(wiki_id=wiki.id, path=key[1]).first() if wiki else None
            owner = db.session.get(User, wiki.owner_id) if wiki else None
            if page is not None and owner is not None and owner.suspended_at is None:
                target = PageView(owner, wiki, page, viewer, load_acl_rules(owner.username, wiki.slug))
                if target.can_read():
                    cache[key] = target
        target = cache[key]
        if target is None:
            continue
        url = f"/@{target.owner.username}/{target.wiki.slug}/{url_path_from_page_path(target.page.path, strip_md=True)}"
        quote = item.get("quote")
        if quote:
            try:
                if count_occurrences(target.text_key("viewer"), match_key(quote), limit=1) < 1:
                    quote = None
            except ContentUnavailable:
                quote = None
        out.append({"n": item["n"], "title": target.page.title or target.page.path,
                    "url": url + (_text_fragment(quote) if quote else ""), "quote": quote or None})
    return out


def _same_submission(row, view, body, quote):
    return (row.wiki_id == view.wiki.id and row.page_path == view.page.path and row.page_id == view.page.id
            and row.body == body and (row.quote or "") == quote)


@annotations_bp.route("/@<username>/<slug>/-/pages/<int:page_id>/annotations", methods=["GET"])
def list_annotations(username, slug, page_id):
    if not _csrf_ok():
        return _err(400, "csrf_failed", "Missing or invalid CSRF token.")
    viewer = get_current_user_from_request()
    view, err = _load(username, slug, page_id, viewer)
    if err:
        return err
    before = None
    if "before" in request.args:
        try:
            token = request.args["before"]
            if len(token) > 512:
                raise ValueError
            position = _cursor_cipher(view).decrypt(token.encode("ascii")).decode()
            timestamp, row_id = position.split(",")
            before = (datetime.fromisoformat(timestamp), int(row_id))
            if before[0].tzinfo is None or not 0 < before[1] <= 2147483647:
                raise ValueError
        except (InvalidToken, ValueError):
            return _err(400, "invalid_cursor", "Invalid comment cursor.")
    try:
        comments, cursor = _comment_page(view, before)
    except ContentUnavailable:
        return _unavailable()
    return _json({"comments": comments, "before": cursor, "revision": view.page.content_hash,
                  "viewer_id": viewer.id if viewer else None})


@annotations_bp.route("/@<username>/<slug>/-/pages/<int:page_id>/annotations", methods=["POST"])
def create_annotation(username, slug, page_id):
    """Create a comment, or with {"preview": true} return what would be
    created (audience, canonical page path, anchoring) without writing.

    The commit must carry the audience the visitor approved; it is
    recomputed here and a change is a 409, never a silent widening."""
    # CSRF for POST is enforced by the app-wide CSRFProtect before we run.
    if not request.is_json:
        return _err(415, "unsupported_media_type", "Send JSON.")
    viewer = get_current_user_from_request()
    if viewer is None:
        return _err(401, "authentication_required", "Sign in to comment.")
    data = request.get_json(silent=True)
    if not isinstance(data, dict):
        return _err(400, "invalid_json", "Send a JSON object.")
    # The tab must still belong to the identity it loaded as (sign-out or an
    # account switch in another tab must not post under the new identity).
    viewer_id = data.get("viewer_id")
    if not isinstance(viewer_id, int) or isinstance(viewer_id, bool):
        return _err(400, "invalid_viewer_id", "viewer_id (the signed-in user's id) is required.")
    if viewer_id != viewer.id:
        return _err(409, "viewer_mismatch", "You're signed in as a different account now. Reload the page.")
    preview = data.get("preview") is True

    body = data.get("body")
    if not preview:
        if not isinstance(body, str) or not body.strip():
            return _err(400, "invalid_body", "Write a comment.")
        if _UNSTORABLE_TEXT.search(body):
            return _err(400, "invalid_body", "The comment contains an unsupported character.")
        body = body.strip()
        if len(body) > MAX_BODY_CHARS:
            return _err(400, "invalid_body", f"Comments are limited to {MAX_BODY_CHARS} characters.")
    quote = data.get("quote", "")
    if quote is None:
        quote = ""
    if not isinstance(quote, str):
        return _err(400, "invalid_quote", "quote must be a string.")
    if _UNSTORABLE_TEXT.search(quote):
        return _err(400, "invalid_quote", "The passage contains an unsupported character.")
    quote = match_key(quote)
    if len(quote) > MAX_QUOTE_CHARS:
        return _err(400, "invalid_quote", f"Select at most {MAX_QUOTE_CHARS} characters.")
    request_id = None
    if not preview:
        request_id = parse_request_id(data.get("request_id"))
        if request_id is None:
            return _err(400, "invalid_request_id", "request_id must be a UUID.")
    revision = data.get("revision")
    if not isinstance(revision, str) or not revision:
        return _err(400, "invalid_revision", "revision is required.")

    view, err = _load(username, slug, page_id, viewer)
    if err:
        return err

    allowed, retry_after, desc = rate_limit.check_all(_attempt_rules(viewer))
    if not allowed:
        return _rate_limited(retry_after, desc)

    if not preview:
        # Retry dedup first: a retried request returns the original comment
        # even if the page changed since or the visitor is now rate limited.
        existing = PageAnnotation.query.filter_by(author_id=viewer.id, request_id=request_id).first()
        if existing is not None:
            return _duplicate(existing, view, viewer, body, quote)

    if revision != view.page.content_hash:
        return _err(409, "stale_revision", "The page changed since you loaded it. Reload and try again.",
                    revision=view.page.content_hash)

    owner_only = False
    prefix = suffix = None
    try:
        if quote:
            text_key = view.text_key("viewer")
            found = count_occurrences(text_key, quote, limit=2)
            if found == 0:
                return _err(409, "selection_not_found", "That passage isn't in this page's text. Reload and select again.")
            if found > 1:
                return _err(409, "ambiguous_selection", "That passage appears more than once. Select a longer passage.")
            prefix, suffix = anchor_context(text_key, quote)
            if view.is_owner:
                # Owner-only unless this exact occurrence (same context) is
                # also uniquely in the public text. Any doubt -> owner-only.
                owner_only = context_matches(view.text_key("public"), quote, prefix, suffix) != 1
    except ContentUnavailable:
        return _unavailable()
    audience = comment_audience(view.page, owner_only)
    page_ref = f"@{view.owner.username}/{view.wiki.slug}/{view.page.path}"

    if preview:
        return _json({"preview": {
            "audience": audience, "audience_label": AUDIENCE_LABELS[audience], "owner_only": owner_only,
            "page_path": page_ref, "page_id": view.page.id, "quote": quote, "revision": view.page.content_hash,
        }})

    if data.get("audience") != audience:
        return _err(409, "audience_changed", "Who can read this comment has changed. Review it again before posting.",
                    audience=audience, audience_label=AUDIENCE_LABELS[audience], page_path=page_ref)
    citations, err = _parse_citations(data.get("citations"), viewer)
    if err:
        return err

    budgets = _budget_rules(viewer, view)
    allowed, retry_after, desc = rate_limit.peek_all(budgets)
    if not allowed:
        return _rate_limited(retry_after, desc)

    row = PageAnnotation(
        wiki_id=view.wiki.id,
        page_path=view.page.path,
        page_id=view.page.id,
        author_id=viewer.id,
        request_id=request_id,
        body=body,
        quote=quote,
        owner_only=owner_only,
        revision=view.page.content_hash,
        anchor_prefix=prefix,
        anchor_suffix=suffix,
        citations=citations,
    )
    db.session.add(row)
    try:
        db.session.commit()
    except IntegrityError:
        # A concurrent retry with the same request_id won the race.
        db.session.rollback()
        existing = PageAnnotation.query.filter_by(author_id=viewer.id, request_id=request_id).first()
        if existing is not None:
            return _duplicate(existing, view, viewer, body, quote)
        return _err(409, "request_id_reused", "This request_id was already used for a different comment.")
    rate_limit.charge_all(budgets)
    names = {viewer.id: viewer.username}
    try:
        comment = serialize(row, view, names, _resolve_citations(row.citations, viewer, {}))
    except ContentUnavailable:
        return _unavailable()
    return _json({"comment": comment}, 201)


def _duplicate(existing, view, viewer, body, quote):
    if not _same_submission(existing, view, body, quote):
        return _err(409, "request_id_reused", "This request_id was already used for a different comment.")
    try:
        comment = serialize(existing, view, {viewer.id: viewer.username},
                            _resolve_citations(existing.citations, viewer, {}))
    except ContentUnavailable:
        return _unavailable()
    return _json({"comment": comment, "duplicate": True}, 200)
