"""Admin-auth and admin-settings helpers.

wikihub-3w46: session-based admin auth via ``User.is_admin``. ``ADMIN_TOKEN``
query-param / header auth is kept as a fallback for scripts and the existing
post-receive hook endpoints.

wikihub-2jn.2: ``admin_settings`` key/value reads for server-wide toggles
(e.g. ``curator_enabled``). DB value wins over env default when present.
"""
from __future__ import annotations

from functools import wraps

from flask import current_app, jsonify, render_template, request
from flask_login import current_user

from app import db
from app.models import AdminSetting


_TRUE = ("1", "true", "yes", "on")
_FALSE = ("0", "false", "no", "off")


def check_admin_token(req) -> bool:
    """Fallback: ``?token=`` query-param or ``X-Admin-Token`` header matches
    ``ADMIN_TOKEN`` config. Preserved so scripts and the post-receive hook
    continue to work without a session."""
    token = req.args.get("token") or req.headers.get("X-Admin-Token") or ""
    expected = current_app.config.get("ADMIN_TOKEN", "")
    return bool(expected and token == expected)


def is_admin_request(req) -> bool:
    """True iff the request is from a logged-in admin user OR carries a valid
    ADMIN_TOKEN fallback credential."""
    try:
        if current_user.is_authenticated and getattr(current_user, "is_admin", False):
            return True
    except Exception:
        pass
    return check_admin_token(req)


def require_admin(view):
    """Decorator for admin-only routes. 403s non-admin HTML, 401s non-admin API."""
    @wraps(view)
    def wrapper(*args, **kwargs):
        if is_admin_request(request):
            return view(*args, **kwargs)
        if request.path.startswith("/api/"):
            return jsonify({"error": "forbidden", "message": "Admin access required"}), 403
        # HTML: 403 page for logged-in non-admins, redirect to login for anon.
        if not current_user.is_authenticated:
            from flask import redirect, url_for
            return redirect(url_for("auth.login", next=request.path))
        return render_template("error.html", code=403, title="Admin only",
                               message="You need admin privileges to view this page."), 403
    return wrapper


def get_setting(key: str) -> str | None:
    row = AdminSetting.query.filter_by(key=key).first()
    return row.value if row else None


def set_setting(key: str, value: str | None) -> None:
    row = AdminSetting.query.filter_by(key=key).first()
    if row is None:
        row = AdminSetting(key=key, value=value)
        db.session.add(row)
    else:
        row.value = value
    db.session.commit()


def _parse_bool(value: str | None):
    if value is None:
        return None
    v = value.strip().lower()
    if v in _TRUE:
        return True
    if v in _FALSE:
        return False
    return None


def curator_enabled() -> bool:
    """DB override wins; env default (CURATOR_ENABLED) is the fallback."""
    try:
        db_value = _parse_bool(get_setting("curator_enabled"))
    except Exception:
        db_value = None
    if db_value is not None:
        return db_value
    return bool(current_app.config.get("CURATOR_ENABLED", False))
