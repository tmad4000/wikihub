"""
GET /api/v1/me/capabilities — machine-readable snapshot of what the
authenticated caller can do, for agents that want to introspect the API
before attempting operations.

Auth: same as /accounts/me (Bearer API key or session).
"""

import time
from datetime import datetime, timedelta, timezone

from flask import jsonify, request

from app import db
from app.auth_utils import api_auth_required, _write_timestamps
from app.models import Wiki
from app.routes import api_bp


# Keep these in sync with the decorators applied to write endpoints in
# api_wikis.py. The default rate_limit_writes() in auth_utils uses 10/min;
# we surface that as the authoritative limit. If we later raise it, update
# both places together.
_WRITE_LIMIT_PER_MIN = 10

# Feedback limits come from api_feedback.py. Authenticated submitters get
# the higher bucket; we expose the auth limit here since this endpoint is
# auth-only.
_FEEDBACK_LIMIT_PER_MIN = 60

# Platform-wide quotas. Kept as constants here so agents get a stable
# answer. TODO(quotas): source these from config once we have real limits.
_MAX_WIKIS_PER_USER = 100
_MAX_PAGES_PER_WIKI = None  # unlimited for now
_MAX_PAGE_SIZE_BYTES = 1_048_576  # 1 MiB — matches renderer/page practical limits


def _writes_remaining(user_id):
    """Best-effort remaining-writes count from auth_utils._write_timestamps.
    Returns (remaining, reset_at_iso). If the bucket hasn't been touched
    yet, remaining == limit and reset_at is one minute from now."""
    now_mono = time.monotonic()
    window = 60.0
    timestamps = [t for t in _write_timestamps.get(user_id, []) if now_mono - t < window]
    used = len(timestamps)
    remaining = max(0, _WRITE_LIMIT_PER_MIN - used)
    if timestamps:
        reset_mono_delta = (timestamps[0] + window) - now_mono
    else:
        reset_mono_delta = window
    reset_at = datetime.now(timezone.utc) + timedelta(seconds=max(0, reset_mono_delta))
    return remaining, reset_at.isoformat()


@api_bp.route("/me/capabilities", methods=["GET"])
@api_auth_required
def get_capabilities():
    user = request.current_user

    # User's owned wikis. Grants-based memberships are not tracked in the
    # DB today (they live in each wiki's .wikihub/acl), so for now only
    # owner-role wikis appear here. TODO(capabilities): walk ACL files to
    # surface writer/reader memberships once we have an efficient index.
    try:
        owned = Wiki.query.filter_by(owner_id=user.id).all()
    except Exception:
        # session was poisoned earlier in this request context; reset and retry
        db.session.rollback()
        owned = Wiki.query.filter_by(owner_id=user.id).all()
    wikis = [
        {"slug": f"@{user.username}/{w.slug}", "role": "owner"}
        for w in owned
    ]

    writes_remaining, writes_reset = _writes_remaining(user.id)

    # Feedback-per-minute: we don't peek into api_feedback's bucket on
    # purpose (it's keyed differently for anon vs auth; exposing exact
    # remaining would leak implementation details). Report limit and a
    # one-minute reset; `remaining` defaults to limit.
    feedback_reset = (datetime.now(timezone.utc) + timedelta(seconds=60)).isoformat()

    return jsonify({
        "user_id": user.id,
        "username": user.username,
        "wikis": wikis,
        "rate_limits": {
            "writes_per_minute": {
                "limit": _WRITE_LIMIT_PER_MIN,
                "remaining": writes_remaining,
                "reset_at": writes_reset,
            },
            "feedback_per_minute": {
                "limit": _FEEDBACK_LIMIT_PER_MIN,
                "remaining": _FEEDBACK_LIMIT_PER_MIN,  # TODO: wire real counter
                "reset_at": feedback_reset,
            },
        },
        "features": {
            "bulk_endpoints": False,
            "idempotency_keys": False,
            "git_push": True,
        },
        "quotas": {
            "max_wikis_per_user": _MAX_WIKIS_PER_USER,
            "max_pages_per_wiki": _MAX_PAGES_PER_WIKI,
            "max_page_size_bytes": _MAX_PAGE_SIZE_BYTES,
        },
    })
