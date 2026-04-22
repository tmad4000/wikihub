"""transactional email via Cloudflare Email Service.

Env vars:
  CLOUDFLARE_EMAIL_TOKEN    API token with Account → Email Sending → Edit scope
  CLOUDFLARE_ACCOUNT_ID     Account UUID (Tmad4000 account for prod)
  EMAIL_FROM                Default sender, e.g. 'WikiHub <noreply@wikihub.md>'
  EMAIL_MODE                'live' (default) or 'mock' (queue in memory, never send)
"""
from __future__ import annotations

import os
import logging
import threading
from typing import Optional
from urllib.parse import quote

import requests


log = logging.getLogger(__name__)

_CF_SEND_URL = "https://api.cloudflare.com/client/v4/accounts/{account_id}/email/sending/send"

_mock_lock = threading.Lock()
_mock_outbox: list[dict] = []


def _mask(email: str) -> str:
    if not email or "@" not in email:
        return "***"
    local, _, domain = email.partition("@")
    return f"{local[:1]}***@{domain}"


def _mode() -> str:
    return (os.environ.get("EMAIL_MODE") or "live").strip().lower()


def _default_from() -> str:
    return os.environ.get("EMAIL_FROM") or "WikiHub <noreply@wikihub.md>"


def send(
    to: str,
    subject: str,
    html: str,
    text: str,
    *,
    from_addr: Optional[str] = None,
    reply_to: Optional[str] = None,
) -> bool:
    """Send a single transactional email. Returns True on success, False on failure.
    Failure is logged but never raised — a broken email path must not 500 the app."""
    to = (to or "").strip().lower()
    if not to or "@" not in to:
        log.error("email.invalid_to to=%s", _mask(to))
        return False

    sender = from_addr or _default_from()
    payload = {
        "to": to,
        "from": sender,
        "subject": subject,
        "html": html,
        "text": text,
    }
    if reply_to:
        payload["reply_to"] = reply_to

    if _mode() == "mock":
        with _mock_lock:
            _mock_outbox.append(payload)
        log.info("email.mock to=%s subject=%r", _mask(to), subject)
        return True

    token = os.environ.get("CLOUDFLARE_EMAIL_TOKEN")
    account_id = os.environ.get("CLOUDFLARE_ACCOUNT_ID")
    if not token or not account_id:
        log.error("email.missing_config to=%s", _mask(to))
        return False

    url = _CF_SEND_URL.format(account_id=quote(account_id, safe=""))
    try:
        r = requests.post(
            url,
            headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
            json=payload,
            timeout=10,
        )
    except requests.RequestException as e:
        log.error("email.network_error to=%s err=%s", _mask(to), e)
        return False

    if r.status_code >= 400:
        log.error(
            "email.http_error to=%s status=%s body=%s",
            _mask(to), r.status_code, r.text[:400],
        )
        return False

    log.info("email.sent to=%s subject=%r", _mask(to), subject)
    return True


def mock_outbox() -> list[dict]:
    """Return a snapshot of the mock outbox (EMAIL_MODE=mock)."""
    with _mock_lock:
        return list(_mock_outbox)


def mock_clear() -> None:
    """Empty the mock outbox between tests."""
    with _mock_lock:
        _mock_outbox.clear()


def _escape(s: str) -> str:
    return (s or "").replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def send_email_verification(
    *,
    to: str,
    verify_url: str,
    username: str,
) -> bool:
    """Send the 'verify your email' link sent right after signup (or when a
    user changes their email). Non-blocking — the user can use the app
    before clicking; verification just stops the 'unverified' banner and
    lets pending invites for that address materialize."""
    subject = "Verify your WikiHub email"
    text = (
        f"Hi @{username},\n\n"
        f"Click this link to confirm your email address on WikiHub:\n{verify_url}\n\n"
        f"This link expires in 24 hours. If you didn't sign up for WikiHub, "
        f"you can ignore this email.\n\n"
        f"— WikiHub"
    )
    html = f"""\
<p>Hi <strong>@{_escape(username)}</strong>,</p>
<p>Click the link to confirm your email address on WikiHub:</p>
<p><a href="{verify_url}">{verify_url}</a></p>
<p style="color:#887d6e;font-size:0.875rem;">This link expires in 24 hours. If you didn't sign up for WikiHub, you can ignore this email.</p>
<p style="color:#887d6e;font-size:0.875rem;margin-top:2rem;">— WikiHub</p>
"""
    return send(to, subject, html, text)


def send_share_invite_existing_user(
    *,
    to: str,
    inviter_name: str,
    wiki_owner: str,
    wiki_slug: str,
    wiki_title: str,
    role: str,
    server_url: str = "https://wikihub.md",
) -> bool:
    """Send 'X shared a wiki with you' to a user who already has an account."""
    wiki_url = f"{server_url}/@{quote(wiki_owner, safe='')}/{quote(wiki_slug, safe='')}"
    subject = f"{inviter_name} shared {wiki_title} with you on WikiHub"
    text = (
        f"{inviter_name} shared the wiki \"{wiki_title}\" with you on WikiHub.\n\n"
        f"Your access level: {role}\n"
        f"Open the wiki: {wiki_url}\n\n"
        f"— WikiHub"
    )
    html = f"""\
<p>{_escape(inviter_name)} shared the wiki <strong>{_escape(wiki_title)}</strong> with you on WikiHub.</p>
<p>Your access level: <code>{_escape(role)}</code></p>
<p><a href="{wiki_url}">Open the wiki →</a></p>
<p style="color:#887d6e;font-size:0.875rem;margin-top:2rem;">— WikiHub</p>
"""
    return send(to, subject, html, text)


def send_share_invite_pending(
    *,
    to: str,
    inviter_name: str,
    wiki_owner: str,
    wiki_slug: str,
    wiki_title: str,
    role: str,
    server_url: str = "https://wikihub.md",
    token: Optional[str] = None,
) -> bool:
    """Send 'X invited you — sign up to get access' to a not-yet-registered email.

    The `token` arg is the PendingInvite.token — when included as ?it=, the
    click itself becomes proof of email ownership (1-click verify). Without it,
    signup still works but falls back to the separate verify-email round-trip."""
    params = f"email={quote(to, safe='')}"
    if token:
        params += f"&it={quote(token, safe='')}"
    signup_url = f"{server_url}/auth/signup?{params}"
    subject = f"{inviter_name} invited you to {wiki_title} on WikiHub"
    text = (
        f"{inviter_name} invited you to \"{wiki_title}\" on WikiHub with {role} access.\n\n"
        f"Sign up (free) to open the wiki: {signup_url}\n\n"
        f"Your invite will apply automatically once your email is verified.\n\n"
        f"— WikiHub"
    )
    html = f"""\
<p>{_escape(inviter_name)} invited you to the wiki <strong>{_escape(wiki_title)}</strong> on WikiHub.</p>
<p>Your access level: <code>{_escape(role)}</code></p>
<p><a href="{signup_url}">Create your WikiHub account →</a></p>
<p style="color:#887d6e;font-size:0.875rem;">Your invite will apply automatically once your email is verified.</p>
<p style="color:#887d6e;font-size:0.875rem;margin-top:2rem;">— WikiHub</p>
"""
    return send(to, subject, html, text)
