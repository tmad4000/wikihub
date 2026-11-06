"""
auth utilities for wikihub.

handles password hashing, API key generation/verification, and
Bearer token extraction from requests.
"""

import hashlib
import hmac
import json
import logging
import secrets
import sys
import time
from collections import defaultdict

import bcrypt
from flask import request, session
from functools import wraps

from app import db
from app.models import User, ApiKey

# sliding window: user_id -> list of monotonic timestamps
_write_timestamps = defaultdict(list)


# --- security event log ---------------------------------------------------
#
# One JSON object per line on logger "security". Gunicorn does not configure
# the root logger, so without a handler of our own INFO records would be
# dropped; attach a stderr handler (systemd captures stderr into journald)
# and stop propagation so the line is not printed twice if a root handler is
# ever added.
security_logger = logging.getLogger("security")
if not any(getattr(h, "_wikihub_security", False) for h in security_logger.handlers):
    _security_handler = logging.StreamHandler(sys.stderr)
    _security_handler.setFormatter(logging.Formatter("%(message)s"))
    _security_handler._wikihub_security = True
    security_logger.addHandler(_security_handler)
security_logger.setLevel(logging.INFO)
security_logger.propagate = False

SECURITY_OUTCOMES = ("success", "failure", "denied")
# Field names that must never be written to the security log, whatever a
# caller passes. Values for these keys are dropped, not masked.
_SECURITY_FORBIDDEN_FIELDS = frozenset({
    "password", "new_password", "token", "api_key", "key", "secret",
    "otp", "code", "authorization", "cookie", "email",
})


def client_ip():
    """The trusted client address for this request.

    ProxyFix(x_for=1) in create_app already replaced REMOTE_ADDR with the
    address nginx appended to X-Forwarded-For (nginx's realip module derives
    it from CF-Connecting-IP only for Cloudflare peers). Everything to the
    left of that entry is client-controlled, so never read the header here.
    """
    return request.remote_addr or "unknown"


def email_fingerprint(email):
    """Non-reversible label for an email: domain + first 12 hex of sha256 of
    the lowercased address. Enough to correlate attempts, not to read them."""
    email = (email or "").strip().lower()
    if not email:
        return None
    domain = email.rsplit("@", 1)[1] if "@" in email else ""
    return {
        "email_domain": domain[:100],
        "email_hash": hashlib.sha256(email.encode()).hexdigest()[:12],
    }


def security_event(event, outcome, *, user_id=None, email=None, **fields):
    """Emit one JSON line on the "security" logger. Never raises.

    `email` is never logged raw: it becomes email_domain + email_hash.
    Secret-looking keyword fields are dropped. Remaining values are coerced
    to short strings/numbers/bools so a caller cannot dump objects."""
    try:
        if outcome not in SECURITY_OUTCOMES:
            outcome = "failure"
        record = {"security_event": event, "outcome": outcome}
        try:
            record["ip"] = client_ip()
            record["ua"] = (request.headers.get("User-Agent") or "")[:200]
            record["http_method"] = request.method
            record["path"] = request.path[:300]
        except RuntimeError:
            # outside a request context (CLI, background thread)
            pass
        if user_id is None:
            try:
                from flask_login import current_user
                if current_user and current_user.is_authenticated:
                    user_id = current_user.id
            except Exception:
                pass
            if user_id is None:
                try:
                    u = getattr(request, "current_user", None)
                    user_id = getattr(u, "id", None)
                except RuntimeError:
                    pass
        if user_id is not None:
            record["user_id"] = user_id
        fp = email_fingerprint(email)
        if fp:
            record.update(fp)
        for k, v in fields.items():
            if k.lower() in _SECURITY_FORBIDDEN_FIELDS or v is None:
                continue
            if not isinstance(v, (bool, int, float)):
                v = str(v)[:200]
            record[k] = v
        security_logger.info(json.dumps(record, separators=(",", ":"), sort_keys=False))
    except Exception:  # logging must never break the request
        pass


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
        security_event(
            "auth.api_key_invalid", "failure", user_id=None,
            via="bearer", key_prefix=token[:3] if token.startswith("wh_") else "other",
        )

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

            ip = client_ip()
            ip_key = (ip_bucket_prefix, ip)
            ip_ts = _ip_write_timestamps[ip_key]
            _ip_write_timestamps[ip_key] = ip_ts = _prune_window(ip_ts, now, window)
            if effective_ip_limit is not None and len(ip_ts) >= effective_ip_limit:
                retry_after = int(ip_ts[0] + window - now) + 1
                security_event("rate_limit", "denied", scope=f"writes_ip_{ip_bucket_prefix}", limit=effective_ip_limit)
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
                    security_event("rate_limit", "denied", scope="writes_user", limit=effective_user_limit)
                    return {
                        "error": "rate_limited",
                        "message": f"Too many write requests ({effective_user_limit}/min). Retry in {retry_after}s.",
                        "retry_after": retry_after,
                    }, 429, {"Retry-After": str(retry_after)}
                timestamps.append(now)

            return f(*args, **kwargs)
        return wrapped
    return decorator


def _token_matches(presented, expected):
    if not expected or not presented:
        return False
    return hmac.compare_digest(presented.encode("utf-8"), expected.encode("utf-8"))


def check_admin_token(req=None, *, config_keys=("ADMIN_TOKEN",), log=True, log_success=True):
    """Single gate for admin-token endpoints.

    Accepts only `Authorization: Bearer <token>` (never a query string, which
    leaks into logs and history) and compares in constant time. `config_keys`
    lists the app config values that are acceptable tokens (e.g. the git-hook
    token plus the admin token for back-compat)."""
    from flask import current_app

    req = req if req is not None else request
    auth = req.headers.get("Authorization", "")
    presented = auth[7:].strip() if auth[:7].lower() == "bearer " else ""
    ok = False
    for key in config_keys:
        if _token_matches(presented, current_app.config.get(key, "") or ""):
            ok = True
    if log:
        if not ok:
            security_event("admin.auth", "denied", admin_endpoint=req.endpoint, token_present=bool(auth))
        elif log_success:
            security_event("admin.action", "success", admin_endpoint=req.endpoint)
    return ok


def set_user_email(user, new_email):
    """The one way to change a user's email address.

    Any actual change clears email_verified_at: ownership of the new address
    is unproven until the verification link is used, and verified-email
    features (pending-invite materialization, OAuth/OIDC email auto-link)
    must not trust it before then. Returns True when the address changed.
    Callers commit, then call send_verification_if_needed(user)."""
    new_email = (new_email or "").strip().lower() or None
    if (user.email or "").strip().lower() == (new_email or ""):
        return False
    user.email = new_email
    user.email_verified_at = None
    return True
