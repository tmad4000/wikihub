"""
auth utilities for wikihub.

handles password hashing, API key generation/verification, and
Bearer token extraction from requests.
"""

import hashlib
import secrets
import time
from collections import defaultdict

import bcrypt
from flask import request, session
from functools import wraps

from app import db
from app.models import User, ApiKey

# sliding window: user_id -> list of monotonic timestamps
_write_timestamps = defaultdict(list)


def hash_password(password):
    return bcrypt.hashpw(password.encode(), bcrypt.gensalt()).decode()


def check_password(password, password_hash):
    return bcrypt.checkpw(password.encode(), password_hash.encode())


def generate_api_key():
    """generate a new API key with wh_ prefix. returns (raw_key, key_hash, key_prefix)."""
    raw = "wh_" + secrets.token_urlsafe(32)
    key_hash = hashlib.sha256(raw.encode()).hexdigest()
    key_prefix = raw[:11]  # "wh_" + first 8 chars of token
    return raw, key_hash, key_prefix


def generate_magic_login_token():
    raw = "wl_" + secrets.token_urlsafe(32)
    return raw, hash_one_time_token(raw)


def generate_email_verification_token():
    raw = "ev_" + secrets.token_urlsafe(32)
    return raw, hash_one_time_token(raw)


def generate_password_reset_token():
    raw = "pr_" + secrets.token_urlsafe(32)
    return raw, hash_one_time_token(raw)


def hash_api_key(raw_key):
    return hashlib.sha256(raw_key.encode()).hexdigest()


def hash_one_time_token(raw_token):
    return hashlib.sha256(raw_token.encode()).hexdigest()


def get_current_user_from_request():
    """extract user from Bearer token, API key, or session.
    returns User or None."""
    from flask_login import current_user

    # try Bearer token
    auth_header = request.headers.get("Authorization", "")
    if auth_header.startswith("Bearer "):
        token = auth_header[7:]
        key_hash = hash_api_key(token)
        api_key = ApiKey.query.filter_by(key_hash=key_hash).first()
        if api_key:
            # update last_used and agent info
            api_key.last_used_at = db.func.now()
            agent_name = request.headers.get("X-Agent-Name")
            agent_version = request.headers.get("X-Agent-Version")
            if agent_name:
                api_key.agent_name = agent_name
            if agent_version:
                api_key.agent_version = agent_version
            db.session.commit()
            return User.query.get(api_key.user_id)

    # Only trust flask-login session auth when this request actually carries
    # a logged-in session. In tests with a long-lived app context, the
    # `current_user` proxy can otherwise outlive the request that set it.
    if session.get("_user_id") and current_user.is_authenticated:
        return current_user

    return None


def api_auth_required(f):
    """decorator for API endpoints that require authentication."""
    @wraps(f)
    def decorated(*args, **kwargs):
        user = get_current_user_from_request()
        if not user:
            return {"error": "unauthorized", "message": "Authentication required"}, 401
        request.current_user = user
        return f(*args, **kwargs)
    return decorated


def api_auth_optional(f):
    """decorator that attaches user if authenticated, but doesn't require it."""
    @wraps(f)
    def decorated(*args, **kwargs):
        request.current_user = get_current_user_from_request()
        return f(*args, **kwargs)
    return decorated


_ip_write_timestamps = defaultdict(list)


def _prune_window(timestamps, now, window):
    return [t for t in timestamps if now - t < window]


def _configured_int(name, fallback):
    from flask import current_app
    try:
        return int(current_app.config.get(name, fallback))
    except (TypeError, ValueError):
        return fallback


def rate_limit_writes(
    max_per_minute=None,
    max_per_ip_per_minute=None,
    anonymous_max_per_ip_per_minute=None,
    window_seconds=60,
):
    """Reject write bursts while allowing authenticated bulk publishing.

    Authenticated agents get a much roomier per-user and per-IP quota by
    default. Anonymous write surfaces keep the older tight IP cap unless a
    route opts into a different value.
    """
    def decorator(f):
        @wraps(f)
        def wrapped(*args, **kwargs):
            from flask import current_app
            if current_app.testing and not current_app.config.get("WRITE_RATE_LIMITS_IN_TESTS"):
                return f(*args, **kwargs)

            now = time.monotonic()
            window = int(window_seconds)
            user = getattr(request, "current_user", None)

            if user:
                effective_user_limit = (
                    max_per_minute
                    if max_per_minute is not None
                    else _configured_int("WRITE_RATE_LIMIT_AUTHENTICATED_PER_MINUTE", 180)
                )
                effective_ip_limit = (
                    max_per_ip_per_minute
                    if max_per_ip_per_minute is not None
                    else _configured_int("WRITE_RATE_LIMIT_AUTHENTICATED_IP_PER_MINUTE", 360)
                )
                ip_bucket_prefix = "auth"
            else:
                effective_user_limit = None
                effective_ip_limit = (
                    anonymous_max_per_ip_per_minute
                    if anonymous_max_per_ip_per_minute is not None
                    else (
                        max_per_ip_per_minute
                        if max_per_ip_per_minute is not None
                        else _configured_int("WRITE_RATE_LIMIT_ANONYMOUS_IP_PER_MINUTE", 10)
                    )
                )
                ip_bucket_prefix = "anon"

            ip = request.remote_addr or "unknown"
            ip_key = (ip_bucket_prefix, ip)
            ip_ts = _ip_write_timestamps[ip_key]
            _ip_write_timestamps[ip_key] = ip_ts = _prune_window(ip_ts, now, window)
            if effective_ip_limit is not None and len(ip_ts) >= effective_ip_limit:
                retry_after = int(ip_ts[0] + window - now) + 1
                scope = "authenticated requests from this IP" if user else "anonymous write requests from this IP"
                return {
                    "error": "rate_limited",
                    "message": f"Too many {scope} ({effective_ip_limit}/min). Retry in {retry_after}s.",
                    "retry_after": retry_after,
                }, 429, {"Retry-After": str(retry_after)}
            ip_ts.append(now)

            if user and effective_user_limit is not None:
                key = user.id
                timestamps = _write_timestamps[key]
                _write_timestamps[key] = timestamps = _prune_window(timestamps, now, window)
                if len(timestamps) >= effective_user_limit:
                    retry_after = int(timestamps[0] + window - now) + 1
                    return {
                        "error": "rate_limited",
                        "message": f"Too many write requests ({effective_user_limit}/min). Retry in {retry_after}s.",
                        "retry_after": retry_after,
                    }, 429, {"Retry-After": str(retry_after)}
                timestamps.append(now)

            return f(*args, **kwargs)
        return wrapped
    return decorator
