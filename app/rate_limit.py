"""Postgres-backed fixed-window rate limiter (wikihub-1ug3).

Unlike the per-process deques used elsewhere, counts live in the
``rate_limit_buckets`` table so every gunicorn worker (and restart) shares
them. Each call is one atomic upsert.

Limits used today (also published by GET /api/v1/me/capabilities):
  signup (POST /api/v1/accounts):   SIGNUP_API_MAX_PER_IP_PER_HOUR (5) and
                                     SIGNUP_API_MAX_PER_IP_PER_DAY (20) per client IP
  anonymous upload (/new-anonymous): ANON_UPLOAD_MAX_PER_IP_PER_HOUR (10) per client IP
  user search (/api/v1/users/search): USER_SEARCH_MAX_PER_MINUTE (30) per account
"""

import random
from datetime import datetime, timedelta, timezone

from flask import current_app
from sqlalchemy import text

from app import db

LIMIT_DEFAULTS = {
    "SIGNUP_API_MAX_PER_IP_PER_HOUR": 5,
    "SIGNUP_API_MAX_PER_IP_PER_DAY": 20,
    "ANON_UPLOAD_MAX_PER_IP_PER_HOUR": 10,
    "USER_SEARCH_MAX_PER_MINUTE": 30,
}


def limit_value(name):
    try:
        return int(current_app.config.get(name, LIMIT_DEFAULTS[name]))
    except (TypeError, ValueError):
        return LIMIT_DEFAULTS[name]


def enabled():
    if current_app.testing and not current_app.config.get("SHARED_RATE_LIMITS_IN_TESTS"):
        return False
    return True


def hit(key, limit, window_seconds):
    """Count one event for `key`. Returns (allowed, retry_after_seconds).

    Commits its own statement; call it before staging other ORM changes."""
    if not enabled() or limit is None:
        return True, 0
    now = datetime.now(timezone.utc)
    epoch = int(now.timestamp())
    start_epoch = epoch - (epoch % window_seconds)
    window_start = datetime.fromtimestamp(start_epoch, tz=timezone.utc)
    count = db.session.execute(
        text(
            "INSERT INTO rate_limit_buckets (key, window_start, count) VALUES (:k, :w, 1) "
            "ON CONFLICT (key, window_start) DO UPDATE SET count = rate_limit_buckets.count + 1 "
            "RETURNING count"
        ),
        {"k": key[:200], "w": window_start},
    ).scalar()
    if random.random() < 0.01:
        db.session.execute(
            text("DELETE FROM rate_limit_buckets WHERE window_start < :cutoff"),
            {"cutoff": now - timedelta(days=2)},
        )
    db.session.commit()
    if count > limit:
        return False, start_epoch + window_seconds - epoch
    return True, 0


def check_all(rules):
    """rules: iterable of (key, limit, window_seconds). Every bucket is counted;
    returns (allowed, max_retry_after, failing_limit_desc)."""
    allowed, retry, desc = True, 0, None
    for key, limit, window in rules:
        ok, after = hit(key, limit, window)
        if not ok:
            allowed = False
            if after > retry:
                retry, desc = after, f"{limit} per {window}s"
    return allowed, retry, desc


def signup_limited_response(scope, hour_limit_name, day_limit_name=None):
    """Shared per-IP signup check. Returns a Flask (body, status, headers)
    tuple when the caller is over the limit, else None."""
    from app.auth_utils import client_ip, security_event

    ip = client_ip()
    rules = [(f"{scope}:ip:{ip}:h", limit_value(hour_limit_name), 3600)]
    if day_limit_name:
        rules.append((f"{scope}:ip:{ip}:d", limit_value(day_limit_name), 86400))
    allowed, retry_after, desc = check_all(rules)
    if allowed:
        return None
    security_event("rate_limit", "denied", scope=scope, limit=desc)
    return (
        {
            "error": "rate_limited",
            "message": f"Too many new accounts from this network ({desc}). Retry in {retry_after}s.",
            "retry_after": retry_after,
        },
        429,
        {"Retry-After": str(retry_after)},
    )
