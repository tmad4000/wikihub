"""
auth utilities for wikihub.

handles password hashing, API key generation/verification, and
Bearer token extraction from requests.
"""

import hashlib
import secrets

import bcrypt
from flask import request
from functools import wraps

from app import db
from app.models import User, ApiKey


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
