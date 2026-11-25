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
from app.acl import resolve_visibility
from app.annotations import (
    MAX_BODY_CHARS,
    MAX_QUOTE_CHARS,
    PageView,
    count_occurrences,
    match_key,
    parse_request_id,
    serialize,
)
from app.auth_utils import get_current_user_from_request, security_event
from app.models import Page, PageAnnotation, User
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
    for row, username in rows[:THREAD_PAGE_SIZE]:
        comment = serialize(row, view, {row.author_id: username})
        if comment is not None:
            comments.append(comment)
    if len(rows) > THREAD_PAGE_SIZE:
        last = rows[THREAD_PAGE_SIZE - 1][0]
        position = f"{last.created_at.isoformat()},{last.id}".encode()
        cursor = _cursor_cipher(view).encrypt(position).decode()
    comments.reverse()
    return comments, cursor


def _limited(user, view):
    rules = [
        (f"annot:u:{user.id}:m", rate_limit.limit_value("ANNOTATION_MAX_PER_USER_PER_MINUTE"), 60),
        (f"annot:u:{user.id}:h", rate_limit.limit_value("ANNOTATION_MAX_PER_USER_PER_HOUR"), 3600),
        (f"annot:u:{user.id}:d", rate_limit.limit_value("ANNOTATION_MAX_PER_USER_PER_DAY"), 86400),
        (f"annot:w:{view.wiki.id}:{view.page.path}"[:180] + ":d", rate_limit.limit_value("ANNOTATION_MAX_PER_PAGE_PER_DAY"), 86400),
    ]
    allowed, retry_after, desc = rate_limit.check_all(rules)
    if allowed:
        return None
    security_event("rate_limit", "denied", scope="annotations", limit=desc)
    return _json(
        {"error": "rate_limited", "message": f"Too many comments right now. Try again in {retry_after}s.", "retry_after": retry_after},
        429, {"Retry-After": str(retry_after)},
    )


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
    comments, cursor = _comment_page(view, before)
    return _json({"comments": comments, "before": cursor, "revision": view.page.content_hash,
                  "viewer_id": viewer.id if viewer else None})


@annotations_bp.route("/@<username>/<slug>/-/pages/<int:page_id>/annotations", methods=["POST"])
def create_annotation(username, slug, page_id):
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
    revision = data.get("revision")
    if not isinstance(revision, str) or not revision:
        return _err(400, "invalid_revision", "revision is required.")

    view, err = _load(username, slug, page_id, viewer)
    if err:
        return err

    preview = request.args.get("preview") == "1"
    if not preview:
        body = data.get("body")
        if not isinstance(body, str) or not body.strip():
            return _err(400, "invalid_body", "Write a comment.")
        if _UNSTORABLE_TEXT.search(body):
            return _err(400, "invalid_body", "The comment contains an unsupported character.")
        if len(body) > MAX_BODY_CHARS:
            return _err(400, "invalid_body", f"Comments are limited to {MAX_BODY_CHARS} characters.")
        request_id = parse_request_id(data.get("request_id"))
        if request_id is None:
            return _err(400, "invalid_request_id", "request_id must be a UUID.")

        # Retry dedup first: check before revision and rate limits, then filter the
        # original comment against the viewer’s current access.
        existing = PageAnnotation.query.filter_by(author_id=viewer.id, request_id=request_id).first()
        if existing is not None:
            if _same_submission(existing, view, body, quote):
                names = {viewer.id: viewer.username}
                return _json({"comment": serialize(existing, view, names), "duplicate": True}, 200)
            return _err(409, "request_id_reused", "This request_id was already used for a different comment.")

    if revision != view.page.content_hash:
        return _err(409, "stale_revision", "The page changed since you loaded it. Reload and try again.",
                    revision=view.page.content_hash)

    owner_only = False
    if quote:
        key = match_key(quote)
        found = count_occurrences(view.text_key("viewer"), key)
        if found == 0:
            return _err(409, "selection_not_found", "That passage isn't in this page's text. Reload and select again.")
        if found > 1:
            return _err(409, "ambiguous_selection", "That passage appears more than once. Select a longer passage.")
        if view.is_owner:
            # Unique in the owner's view; owner-only unless the same passage is
            # also uniquely public. Conservative: any doubt -> owner-only.
            owner_only = count_occurrences(view.text_key("public"), key) != 1

    visibility = resolve_visibility(view.page.path, view.acl_rules, view.page.visibility)
    audience = "owner_only" if owner_only else visibility.split("-")[0]
    if preview:
        return _json({"audience": audience})
    if data.get("previewed_audience") != audience:
        return _err(409, "audience_changed", "Review the audience before posting.", audience=audience)

    limited = _limited(viewer, view)
    if limited is not None:
        return limited

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
    )
    db.session.add(row)
    try:
        db.session.commit()
    except IntegrityError:
        # A concurrent retry with the same request_id won the race.
        db.session.rollback()
        existing = PageAnnotation.query.filter_by(author_id=viewer.id, request_id=request_id).first()
        if existing is not None and _same_submission(existing, view, body, quote):
            names = {viewer.id: viewer.username}
            return _json({"comment": serialize(existing, view, names), "duplicate": True}, 200)
        return _err(409, "request_id_reused", "This request_id was already used for a different comment.")
    names = {viewer.id: viewer.username}
    return _json({"comment": serialize(row, view, names)}, 201)
