"""In-app feedback via the World Issue Tracker widget (world-issue-tracker-julr.6).

The account menu's "Send feedback" item opens the WIT widget (no floating
launcher). To attribute a report to the signed-in person, the widget asks
`POST /auth/wit-feedback-token` at send time. That route exchanges the
person's Ideaflow ID token, captured at sign-in and stored encrypted in
`ideaflow_feedback_credentials`, at WIT's `/functions/v1/feedback-token` for a
15-minute `wfb1.` token that can only create issues. The ID token never
reaches the browser. Any failure returns 204, and the widget then submits
anonymously: feedback is never blocked.

Recipe: WIT docs/widget-host-integration-guide.md#menu-launched-identity-verified-install
"""

import base64
import hashlib
from datetime import datetime, timedelta, timezone

import requests
from cryptography.fernet import Fernet, InvalidToken
from flask import current_app

from app import db
from app.models import ExternalIdentity, IdeaflowFeedbackCredential

DEFAULT_TOKEN_URL = "https://api.worldissuetracker.com/functions/v1/feedback-token"
TRACKER_SLUG = "wikihub-feedback"
# An ID token this close to expiry is not worth exchanging.
_MIN_REMAINING = timedelta(seconds=30)


def _fernet():
    secret = current_app.config["SECRET_KEY"]
    if isinstance(secret, str):
        secret = secret.encode()
    key = hashlib.sha256(b"wikihub-wit-feedback-v1:" + secret).digest()
    return Fernet(base64.urlsafe_b64encode(key))


def remember_id_token(id_token, *, issuer, subject, exp):
    """Store a just-verified Ideaflow ID token. Best effort: never breaks sign-in."""
    if not id_token or not issuer or not subject or not isinstance(exp, (int, float)):
        return
    expires_at = datetime.fromtimestamp(exp, tz=timezone.utc)
    if expires_at - _MIN_REMAINING <= datetime.now(timezone.utc):
        return
    try:
        encrypted = _fernet().encrypt(id_token.encode()).decode()
        row = db.session.get(IdeaflowFeedbackCredential, (issuer, subject))
        if row is None:
            row = IdeaflowFeedbackCredential(issuer=issuer, subject=subject)
            db.session.add(row)
        row.id_token_encrypted = encrypted
        row.expires_at = expires_at
        db.session.commit()
    except Exception as exc:  # pragma: no cover - defensive
        db.session.rollback()
        current_app.logger.warning("wit-feedback: could not store ID token (%s)", type(exc).__name__)


def id_token_for_user(user_id):
    """The user's own stored, unexpired Ideaflow ID token, or None."""
    issuer = current_app.config.get("IDEAFLOW_OIDC_ISSUER")
    if not issuer:
        return None
    try:
        identity = ExternalIdentity.query.filter_by(user_id=user_id, issuer=issuer).first()
        if identity is None:
            return None
        row = db.session.get(IdeaflowFeedbackCredential, (issuer, identity.subject))
    except Exception as exc:
        db.session.rollback()
        current_app.logger.warning("wit-feedback: credential lookup failed (%s)", type(exc).__name__)
        return None
    if row is None or row.expires_at - _MIN_REMAINING <= datetime.now(timezone.utc):
        return None
    try:
        return _fernet().decrypt(row.id_token_encrypted.encode()).decode()
    except InvalidToken:
        return None


def exchange_for_feedback_token(id_token):
    """Exchange an ID token at WIT. Returns {"token", "expires_at"} or None."""
    url = current_app.config.get("WIT_FEEDBACK_TOKEN_URL") or DEFAULT_TOKEN_URL
    try:
        response = requests.post(
            url,
            json={"id_token": id_token, "app": "wikihub"},
            headers={"User-Agent": "wikihub/1.0"},
            timeout=10,
        )
    except requests.RequestException as exc:
        current_app.logger.warning("wit-feedback: exchange failed (%s)", type(exc).__name__)
        return None
    if response.status_code != 200:
        current_app.logger.info("wit-feedback: exchange refused (%s)", response.status_code)
        return None
    try:
        body = response.json()
    except ValueError:
        return None
    token = body.get("token") if isinstance(body, dict) else None
    expires_at = body.get("expires_at") if isinstance(body, dict) else None
    if not isinstance(token, str) or not token.startswith("wfb1.") or not isinstance(expires_at, (int, float)):
        return None
    return {"token": token, "expires_at": expires_at}
