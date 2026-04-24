"""
Public feedback submission endpoint.

POST /api/v1/feedback — accepts bug/feature/comment/praise reports from
any caller (authenticated or anonymous). Rate limited per-IP to reduce
abuse; if the caller is logged in the submission is associated with them.
"""

import hashlib
import secrets
import time
from collections import defaultdict
from datetime import date, datetime, timezone

from flask import jsonify, request

from app import db
from app.auth_utils import api_auth_optional
from app.models import Feedback, utcnow
from app.routes import api_bp


_ALLOWED_KINDS = {"bug", "feature", "comment", "praise"}
_MAX_SUBJECT = 200
_MAX_BODY = 10_000
_MAX_EMAIL = 256


# ---------------------------------------------------------------------------
# Minimal in-memory rate limiter, keyed on IP for anon and user_id for auth.
# TODO(rate-limit): replace with a shared limiter (Redis or a proper middleware)
# once the project grows a cross-endpoint limiter. The write limiter in
# auth_utils.rate_limit_writes is per-user; this one needs per-IP for anon,
# so we can't reuse it directly yet.
# ---------------------------------------------------------------------------
_feedback_buckets = defaultdict(list)  # key -> list[monotonic timestamps]
_ANON_PER_MIN = 10
_AUTH_PER_MIN = 60


def _check_rate_limit(key, limit):
    now = time.monotonic()
    window = 60.0
    bucket = [t for t in _feedback_buckets[key] if now - t < window]
    _feedback_buckets[key] = bucket
    if len(bucket) >= limit:
        retry_after = int(bucket[0] + window - now) + 1
        return False, retry_after
    bucket.append(now)
    return True, None


def _client_ip():
    # ProxyFix is already applied in create_app, so request.remote_addr is
    # the real client IP behind any trusted proxy.
    return request.remote_addr or "0.0.0.0"


def _hash_ip(ip):
    # Daily salt — prevents long-term IP linkage while keeping per-day dedupe
    # possible for abuse review. Salt rotates on UTC calendar boundary.
    salt = date.today().isoformat()
    return hashlib.sha256(f"{ip}|{salt}".encode()).hexdigest()


def _new_feedback_id():
    return "fb_" + secrets.token_urlsafe(6).replace("-", "").replace("_", "")[:10]


def _bad(msg, field=None):
    body = {"error": "bad_request", "message": msg}
    if field:
        body["field"] = field
    return body, 400


@api_bp.route("/feedback", methods=["POST"])
@api_auth_optional
def submit_feedback():
    user = getattr(request, "current_user", None)

    # Rate limit before parsing to keep cost low under abuse. Guard against
    # a stale ORM instance by catching any load error and treating as anon.
    user_id = None
    if user is not None:
        try:
            user_id = user.id
        except Exception:
            user = None

    if user_id is not None:
        rl_key = f"user:{user_id}"
        rl_limit = _AUTH_PER_MIN
    else:
        rl_key = f"ip:{_client_ip()}"
        rl_limit = _ANON_PER_MIN
    ok, retry_after = _check_rate_limit(rl_key, rl_limit)
    if not ok:
        return (
            {
                "error": "rate_limited",
                "message": f"Too many feedback requests ({rl_limit}/min). Retry in {retry_after}s.",
                "retry_after": retry_after,
            },
            429,
            {"Retry-After": str(retry_after)},
        )

    data = request.get_json(silent=True) or {}

    kind = (data.get("kind") or "").strip().lower()
    if kind not in _ALLOWED_KINDS:
        return _bad(
            f"kind must be one of {sorted(_ALLOWED_KINDS)}",
            field="kind",
        )

    subject = (data.get("subject") or "").strip()
    if not subject:
        return _bad("subject is required", field="subject")
    if len(subject) > _MAX_SUBJECT:
        return _bad(f"subject must be <= {_MAX_SUBJECT} chars", field="subject")

    body = data.get("body") or ""
    if not isinstance(body, str):
        return _bad("body must be a string", field="body")
    if len(body) > _MAX_BODY:
        return _bad(f"body must be <= {_MAX_BODY} chars", field="body")

    context = data.get("context")
    if context is not None and not isinstance(context, dict):
        return _bad("context must be an object", field="context")

    contact_email = data.get("contact_email")
    if contact_email is not None:
        if not isinstance(contact_email, str) or len(contact_email) > _MAX_EMAIL:
            return _bad(
                f"contact_email must be a string <= {_MAX_EMAIL} chars",
                field="contact_email",
            )
        contact_email = contact_email.strip() or None

    fb = Feedback(
        id=_new_feedback_id(),
        kind=kind,
        subject=subject,
        body=body,
        context_json=context,
        contact_email=contact_email,
        user_id=user_id,
        ip_hash=_hash_ip(_client_ip()),
        status="received",
    )
    db.session.add(fb)
    db.session.commit()

    received_at = fb.created_at
    if received_at.tzinfo is None:
        received_at = received_at.replace(tzinfo=timezone.utc)

    return (
        jsonify(
            {
                "id": fb.id,
                "received_at": received_at.isoformat(),
                "status": fb.status,
            }
        ),
        201,
    )
