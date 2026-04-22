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
from flask import request
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

    if current_user.is_authenticated:
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


def rate_limit_writes(max_per_minute=10, max_per_ip_per_minute=10):
    """reject requests when a user or IP exceeds write limits.
    per-user: authenticated users get max_per_minute writes.
    per-IP: all requests (including anonymous) get max_per_ip_per_minute writes."""
    def decorator(f):
        @wraps(f)
        def wrapped(*args, **kwargs):
            from flask import current_app
            if current_app.testing:
                return f(*args, **kwargs)

            now = time.monotonic()
            window = 60

            # IP rate limit (catches anonymous + authenticated)
            ip = request.remote_addr or "unknown"
            ip_ts = _ip_write_timestamps[ip]
            _ip_write_timestamps[ip] = ip_ts = [t for t in ip_ts if now - t < window]
            if len(ip_ts) >= max_per_ip_per_minute:
                retry_after = int(ip_ts[0] + window - now) + 1
                return {
                    "error": "rate_limited",
                    "message": f"Too many write requests from this IP ({max_per_ip_per_minute}/min). Retry in {retry_after}s.",
                    "retry_after": retry_after,
                }, 429, {"Retry-After": str(retry_after)}
            ip_ts.append(now)

            # per-user rate limit
            user = getattr(request, "current_user", None)
            if user:
                key = user.id
                timestamps = _write_timestamps[key]
                _write_timestamps[key] = timestamps = [t for t in timestamps if now - t < window]
                if len(timestamps) >= max_per_minute:
                    retry_after = int(timestamps[0] + window - now) + 1
                    return {
                        "error": "rate_limited",
                        "message": f"Too many write requests ({max_per_minute}/min). Retry in {retry_after}s.",
                        "retry_after": retry_after,
                    }, 429, {"Retry-After": str(retry_after)}
                timestamps.append(now)

            return f(*args, **kwargs)
        return wrapped
    return decorator
