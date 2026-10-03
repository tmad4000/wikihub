import re
from collections import defaultdict, deque
from functools import wraps
from time import time
from urllib.parse import urlparse, quote, parse_qs

from flask import render_template, redirect, url_for, flash, request, session, current_app, abort
from flask_login import login_user, logout_user, login_required, current_user
from authlib.integrations.flask_client import OAuth
from sqlalchemy.exc import IntegrityError

from datetime import timedelta

from app import db
from app.models import (
    User,
    ApiKey,
    MagicLoginToken,
    PendingInvite,
    EmailVerificationToken,
    PasswordResetToken,
    ExternalIdentity,
    utcnow,
)
from app.auth_utils import (
    hash_password,
    check_password,
    hash_api_key,
    hash_one_time_token,
    generate_email_verification_token,
    generate_password_reset_token,
)
from app.routes import auth_bp
from app.subdomains import validate_username
from app.wiki_ops import ensure_personal_wiki, materialize_pending_invites_for
from app.credentials_hint import resolve_server_url
from app import email_service


_EMAIL_VERIFY_TTL_HOURS = 24
_PASSWORD_RESET_TTL_MINUTES = 30
_GOOGLE_OAUTH_CONTEXTS_SESSION_KEY = "google_oauth_contexts"

# "Last used" sign-in hint (code-v8l). The hint is the last login method that
# SUCCEEDED in this browser, recorded server-side by the handler that actually
# established the session, so a click, a failure or a cancelled flow can never
# write it. It holds only a short method id and is validated against the
# methods enabled right now before it is shown.
_LAST_LOGIN_METHOD_COOKIE = "wikihub_last_login_method"
_LAST_LOGIN_METHOD_MAX_AGE = 365 * 24 * 60 * 60

# Ideaflow-only sign-in (code-xbh.5). After an explicit WikiHub sign-out the
# next "Sign in with Ideaflow" asks the provider to show its account chooser
# (prompt=select_account) instead of silently reusing the provider session, so
# signing out and back in is how a person picks a different account. Any
# successful sign-in clears it. Holds only "1".
_CHOOSE_ACCOUNT_COOKIE = "wikihub_choose_account"
_CHOOSE_ACCOUNT_MAX_AGE = 30 * 24 * 60 * 60


def send_verification_if_needed(user):
    """Mint a verification token and email a verify link to the user's email,
    iff they have an email and it's not yet verified. No-op otherwise.

    Non-blocking: signup / account creation completes normally whether or not
    this returns success. Email-send failures are logged inside email_service,
    never raised."""
    if not user or not user.email or user.email_verified_at is not None:
        return

    raw, token_hash = generate_email_verification_token()
    token = EmailVerificationToken(
        user_id=user.id,
        token_hash=token_hash,
        new_email=user.email,
        expires_at=utcnow() + timedelta(hours=_EMAIL_VERIFY_TTL_HOURS),
    )
    db.session.add(token)
    db.session.commit()

    server_url = resolve_server_url(current_app, request)
    verify_url = f"{server_url}/auth/verify/{raw}"
    email_service.send_email_verification(
        to=user.email,
        verify_url=verify_url,
        username=user.username,
    )

oauth = OAuth()

_SIGNUP_WINDOW_SECONDS = 3600
_SIGNUP_MAX_PER_IP = 10
_signup_attempts = defaultdict(deque)

_LOGIN_WINDOW_SECONDS = 300
_LOGIN_MAX_PER_IP = 20
_login_attempts = defaultdict(deque)
# Separate from the per-IP dict: its keys come from a client-supplied
# X-Forwarded-For value, so sharing would let anyone poison a user's counter.
_ideaflow_confirm_failures = defaultdict(deque)

_FORGOT_PASSWORD_WINDOW_SECONDS = 3600
_FORGOT_PASSWORD_MAX_PER_EMAIL = 5
_FORGOT_PASSWORD_MAX_PER_IP = 20
_forgot_password_email_attempts = defaultdict(deque)
_forgot_password_ip_attempts = defaultdict(deque)

_USERNAME_RE = re.compile(r'^[a-z0-9_-]+$')


def _safe_next_url(fallback=None):
    """Validate the ?next= parameter to prevent open redirects.

    Order: POST form `next` → explicit ?next= → Referer header (same-origin only)
    → fallback → main.index.
    The Referer fallback means clicking "Sign in" from any page redirects back after login,
    even if the link itself didn't include ?next=.
    """
    target = request.form.get("next", "").strip() or request.args.get("next", "").strip()
    if target:
        parsed = urlparse(target)
        if not parsed.scheme and not parsed.netloc and not target.startswith("//"):
            return target

    referer = request.headers.get("Referer", "")
    if referer:
        parsed = urlparse(referer)
        # same-origin only — strip scheme/netloc and use the path
        if parsed.netloc == request.host and parsed.path and not parsed.path.startswith("/auth/"):
            return parsed.path + (f"?{parsed.query}" if parsed.query else "")

    return fallback or url_for("main.index")


def _safe_redirect_target(target, fallback=None):
    target = (target or "").strip()
    parsed = urlparse(target)
    if (
        target
        and not parsed.scheme
        and not parsed.netloc
        and not target.startswith("//")
        and not parsed.path.startswith("/auth/")
    ):
        return target
    return fallback or url_for("main.index")


def _google_oauth_context_from_request():
    context = {"next": _safe_next_url()}
    invite_email = request.args.get("email", "").strip().lower()
    invite_token = request.args.get("it", "").strip()
    if invite_email:
        context["email"] = invite_email
    if invite_token:
        context["it"] = invite_token
    return context


def _stash_google_oauth_context(state, context):
    if not state:
        return
    pending = dict(session.get(_GOOGLE_OAUTH_CONTEXTS_SESSION_KEY, {}))
    pending[state] = context
    session[_GOOGLE_OAUTH_CONTEXTS_SESSION_KEY] = pending


def _pop_google_oauth_context():
    state = request.args.get("state", "").strip()
    if not state:
        return {}
    pending = dict(session.get(_GOOGLE_OAUTH_CONTEXTS_SESSION_KEY, {}))
    context = pending.pop(state, {})
    if pending:
        session[_GOOGLE_OAUTH_CONTEXTS_SESSION_KEY] = pending
    else:
        session.pop(_GOOGLE_OAUTH_CONTEXTS_SESSION_KEY, None)
    return context


def _check_login_rate_limit():
    """return 429 response if login rate limit exceeded, else None."""
    ip = request.headers.get("X-Forwarded-For", request.remote_addr or "unknown").split(",")[0].strip()
    attempts = _login_attempts[ip]
    now = time()
    while attempts and now - attempts[0] > _LOGIN_WINDOW_SECONDS:
        attempts.popleft()
    if len(attempts) >= _LOGIN_MAX_PER_IP:
        flash("Too many login attempts. Try again in a few minutes.")
        return _render_login(), 429
    attempts.append(now)
    return None


def _enabled_login_methods():
    """Method ids of the sign-in methods enabled right now, in display order."""
    methods = []
    if current_app.config.get("GOOGLE_CLIENT_ID"):
        methods.append("google")
    if _ideaflow_enabled():
        methods.append("ideaflow")
    methods.extend(["password", "api_key"])
    return methods


def _last_login_method():
    """The remembered method, or None unless the login screen offers 2+
    methods and the stored value is one of them (ignores stale/unknown ids)."""
    methods = _enabled_login_methods()
    value = request.cookies.get(_LAST_LOGIN_METHOD_COOKIE)
    if len(methods) >= 2 and value in methods:
        return value
    return None


def _remember_login_method(response, method):
    """Record `method` as last used. Call only after login_user() succeeded."""
    response.set_cookie(
        _LAST_LOGIN_METHOD_COOKIE,
        method,
        max_age=_LAST_LOGIN_METHOD_MAX_AGE,
        secure=current_app.config.get("SESSION_COOKIE_SECURE", False),
        httponly=True,
        samesite="Lax",
        domain=current_app.config.get("SESSION_COOKIE_DOMAIN"),
    )
    # A completed sign-in (any method) ends the "choose an account next time"
    # state left by an explicit sign-out.
    if request.cookies.get(_CHOOSE_ACCOUNT_COOKIE):
        response.delete_cookie(_CHOOSE_ACCOUNT_COOKIE, domain=current_app.config.get("SESSION_COOKIE_DOMAIN"))
    return response


def _mark_choose_account_next(response):
    """Record an explicit sign-out: the next Ideaflow sign-in in this browser
    sends prompt=select_account."""
    response.set_cookie(
        _CHOOSE_ACCOUNT_COOKIE,
        "1",
        max_age=_CHOOSE_ACCOUNT_MAX_AGE,
        secure=current_app.config.get("SESSION_COOKIE_SECURE", False),
        httponly=True,
        samesite="Lax",
        domain=current_app.config.get("SESSION_COOKIE_DOMAIN"),
    )
    return response


def _login_template_context():
    return {
        "last_login_method": _last_login_method(),
        "testing_login": current_app.debug and current_app.config.get("TESTING_LOGIN"),
        "prefill_email": request.values.get("email", "").strip().lower(),
        "invite_token": request.values.get("it", "").strip(),
        "next_value": _safe_next_url(fallback=""),
    }


def _render_login():
    """Re-render after a failed/rate-limited credential POST. Those posts only
    come from the legacy password / API-key page (or API clients), so errors
    go back there with the form the person was using."""
    return _render_legacy_login()


def _render_legacy_login():
    return render_template("auth/login_legacy.html", **_login_template_context())


def _render_ideaflow_login():
    """The default login page while Ideaflow ID is enabled: exactly one
    control, "Sign in with Ideaflow" (code-xbh.5). Google, email/password,
    sign-up and password reset all happen on id.ideaflow.app."""
    return render_template("auth/login.html", **_login_template_context())


def _render_forgot_password_success(email=""):
    return render_template(
        "auth/forgot_password.html",
        email=email,
        success_message="If that email is on an account, we sent a password reset link. It expires in 30 minutes.",
    )


def _check_forgot_password_rate_limit(email):
    ip = request.headers.get("X-Forwarded-For", request.remote_addr or "unknown").split(",")[0].strip()
    now = time()

    ip_attempts = _forgot_password_ip_attempts[ip]
    while ip_attempts and now - ip_attempts[0] > _FORGOT_PASSWORD_WINDOW_SECONDS:
        ip_attempts.popleft()
    if len(ip_attempts) >= _FORGOT_PASSWORD_MAX_PER_IP:
        flash("Too many password reset attempts from this IP. Try again later.")
        return render_template("auth/forgot_password.html", email=email), 429

    email_attempts = _forgot_password_email_attempts[email]
    while email_attempts and now - email_attempts[0] > _FORGOT_PASSWORD_WINDOW_SECONDS:
        email_attempts.popleft()
    if len(email_attempts) >= _FORGOT_PASSWORD_MAX_PER_EMAIL:
        flash("Too many password reset attempts for that email. Try again later.")
        return render_template("auth/forgot_password.html", email=email), 429

    ip_attempts.append(now)
    email_attempts.append(now)
    return None


def _get_valid_password_reset(raw_token):
    token_hash = hash_one_time_token(raw_token)
    row = PasswordResetToken.query.filter_by(token_hash=token_hash).first()
    if not row or row.used_at is not None or row.expires_at <= utcnow():
        return None, None
    user = db.session.get(User, row.user_id)
    if not user:
        return None, None
    return row, user


def ideaflow_oidc_enabled(config):
    """The Ideaflow ID kill switch (wikihub-39pe): the login button and the
    /auth/ideaflow* routes are only live when explicitly turned on AND a
    client id/secret are configured. Off or half-configured both mean off —
    fail closed rather than registering a broken OAuth client."""
    return bool(
        config.get("IDEAFLOW_OIDC_ENABLED")
        and config.get("IDEAFLOW_OIDC_CLIENT_ID")
        and config.get("IDEAFLOW_OIDC_CLIENT_SECRET")
    )


def init_oauth(app):
    oauth.init_app(app)
    if app.config.get("GOOGLE_CLIENT_ID"):
        oauth.register(
            name="google",
            server_metadata_url="https://accounts.google.com/.well-known/openid-configuration",
            client_id=app.config["GOOGLE_CLIENT_ID"],
            client_secret=app.config["GOOGLE_CLIENT_SECRET"],
            client_kwargs={"scope": "openid email profile"},
        )
    if ideaflow_oidc_enabled(app.config):
        oauth.register(
            name="ideaflow",
            server_metadata_url=app.config["IDEAFLOW_OIDC_DISCOVERY_URL"],
            client_id=app.config["IDEAFLOW_OIDC_CLIENT_ID"],
            client_secret=app.config["IDEAFLOW_OIDC_CLIENT_SECRET"],
            client_kwargs={
                "scope": "openid email profile",
                # The provider registers this confidential client for HTTP
                # Basic authentication at the token endpoint. Pin the method
                # so discovery/default changes cannot move the secret into
                # the request body.
                "token_endpoint_auth_method": "client_secret_basic",
                # Confidential client with S256 PKCE (wikihub-39pe design doc).
                # Authlib auto-generates and stores the code_verifier in the
                # Flask session and replays it on the token exchange.
                "code_challenge_method": "S256",
            },
        )


@auth_bp.route("/login", methods=["GET", "POST"])
def login():
    # Credentials can arrive via POST form (canonical) or GET query string
    # (discouraged — leaks to access logs/history/referer — but useful for
    # bookmarkable auto-login on trusted devices). app/__init__.py installs
    # a werkzeug log filter that redacts api_key= and password= params.
    if request.method == "POST":
        source = request.form
    elif request.args.get("api_key") or request.args.get("password"):
        source = request.args
    elif _ideaflow_enabled():
        return _render_ideaflow_login()
    else:
        # Ideaflow kill switch off: the legacy options are the login page.
        return _render_legacy_login()

    rate_limited = _check_login_rate_limit()
    if rate_limited:
        return rate_limited

    username = source.get("username", "").strip()
    password = source.get("password", "")
    api_key = source.get("api_key", "").strip()

    if api_key:
        key_hash = hash_api_key(api_key)
        key_row = ApiKey.query.filter_by(key_hash=key_hash).first()
        user = User.query.get(key_row.user_id) if key_row else None
        if not user:
            flash("Invalid API key")
            return _render_login(), 401
        login_user(user)
        if request.method == "GET":
            flash("Signed in via URL. Rotate this key if the link was shared.")
        return _remember_login_method(redirect(_safe_next_url()), "api_key")

    user = User.query.filter_by(username=username).first()
    if not user or not user.password_hash or not check_password(password, user.password_hash):
        flash("Invalid username or password")
        return _render_login(), 401

    login_user(user)
    _apply_pending_invites_on_login(user)
    if request.method == "GET":
        flash("Signed in via URL. Rotate credentials if the link was shared.")
    return _remember_login_method(redirect(_safe_next_url()), "password")


@auth_bp.route("/login/password")
def login_password():
    """WikiHub password / API-key sign-in for existing accounts that cannot be
    reached through Ideaflow (for example an account with no email). Not on
    the default login page (code-xbh.5); it is linked only from the Ideaflow
    account-match error and confirmation screens and the For Agents page.
    The forms POST to /auth/login, which remains the credential endpoint."""
    return _render_legacy_login()


def _apply_pending_invites_on_login(user, *, invite_email=None, invite_token=None):
    """After a successful login, apply any pending invites for this user.

    Verification model: if the user arrived via an invite link carrying a
    valid ?it= token (matching a PendingInvite for their own email), the
    click itself is proof of email receipt — treat as verified, materialize.
    Token-less invite links fall through to the separate verify-email flow."""
    if not user or not user.email:
        return
    invite_email = (invite_email if invite_email is not None else request.values.get("email", "")).strip().lower()
    invite_token = (invite_token if invite_token is not None else request.values.get("it", "")).strip()
    if (
        invite_email
        and invite_token
        and invite_email == (user.email or "").lower()
        and not user.email_verified_at
        and PendingInvite.query.filter_by(
            email=invite_email, token=invite_token
        ).first()
    ):
        user.email_verified_at = utcnow()
        db.session.commit()
    applied = materialize_pending_invites_for(user)
    if applied:
        db.session.commit()


@auth_bp.route("/test-login/<username>", methods=["POST"])
def test_login(username):
    if not current_app.config.get("TESTING_LOGIN") or not current_app.debug:
        abort(404)
    user = User.query.filter_by(username=username).first()
    if not user:
        user = User(username=username, password_hash=hash_password("test12345"))
        db.session.add(user)
        db.session.flush()
        ensure_personal_wiki(user)
        db.session.commit()
    login_user(user)
    return redirect(_safe_next_url())


@auth_bp.route("/signup", methods=["GET", "POST"])
def signup():
    if request.method == "POST":
        ip = request.headers.get("X-Forwarded-For", request.remote_addr or "unknown").split(",")[0].strip()
        attempts = _signup_attempts[ip]
        now = time()
        while attempts and now - attempts[0] > _SIGNUP_WINDOW_SECONDS:
            attempts.popleft()
        if len(attempts) >= _SIGNUP_MAX_PER_IP:
            return render_template("auth/signup.html"), 429

        username = request.form.get("username", "").strip().lower()
        email = request.form.get("email", "").strip().lower() or None
        password = request.form.get("password", "")

        if not username or not password:
            flash("Username and password required")
            return render_template("auth/signup.html"), 400

        if not _USERNAME_RE.match(username) or len(username) < 2 or len(username) > 40:
            flash("Username must be 2-40 chars: lowercase letters, numbers, hyphens, or underscores")
            return render_template("auth/signup.html"), 400

        if len(password) < 8:
            flash("Password must be at least 8 characters")
            return render_template("auth/signup.html"), 400

        if User.query.filter_by(username=username).first():
            flash("Username already taken")
            return render_template("auth/signup.html"), 409

        conflict = validate_username(username)
        if conflict:
            flash(conflict)
            return render_template("auth/signup.html"), 409

        if email and User.query.filter_by(email=email).first():
            flash("Email already registered")
            return render_template("auth/signup.html"), 409

        # Token-backed one-click verify (wikihub-yjsv): if the signup came
        # via an invite link with a valid ?it= matching a PendingInvite for
        # this email, the click itself proves email receipt — mark verified
        # so the invite materializes without a separate verify-email round-
        # trip. Token-less signups fall through to the normal verify-by-
        # email flow shipped in ks5t.3.
        invite_token = (
            request.form.get("it", "").strip()
            or request.args.get("it", "").strip()
        )
        invite_verified = bool(
            email and invite_token and PendingInvite.query.filter_by(
                email=email.lower(), token=invite_token
            ).first()
        )
        user = User(
            username=username,
            email=email,
            email_verified_at=utcnow() if invite_verified else None,
            password_hash=hash_password(password),
        )
        db.session.add(user)
        db.session.flush()
        ensure_personal_wiki(user)
        db.session.commit()

        materialize_pending_invites_for(user)
        db.session.commit()
        attempts.append(now)

        # Non-blocking verification email for form signups that supply an email
        # but weren't marked verified via a pending-invite match.
        send_verification_if_needed(user)

        login_user(user)
        return _remember_login_method(redirect(url_for("wiki.user_profile", username=user.username)), "password")

    # GET — prefill email + invite token from the invite-link query params
    prefill_email = request.args.get("email", "").strip().lower()
    prefill_token = request.args.get("it", "").strip()
    if _ideaflow_enabled():
        # code-xbh.5: sign-up is the same Ideaflow flow as sign-in; the person
        # creates their account on id.ideaflow.app and WikiHub mints the local
        # account at the callback. Invite context rides along.
        return redirect(url_for(
            "auth.ideaflow_login",
            next=request.args.get("next") or None,
            email=prefill_email or None,
            it=prefill_token or None,
        ))
    # If they already have an account at that email, bounce them to login
    # with a message. Preserve the invite token so /auth/login can still
    # turn the click into a verification event (one-click verify on login).
    if prefill_email:
        existing = User.query.filter_by(email=prefill_email).first()
        if existing:
            flash("You already have an account — sign in to apply your invite.")
            login_url = url_for("auth.login", email=prefill_email, next="/shared")
            if prefill_token:
                login_url += f"&it={quote(prefill_token, safe='')}"
            return redirect(login_url)
    return render_template(
        "auth/signup.html",
        prefill_email=prefill_email,
        prefill_token=prefill_token,
    )


@auth_bp.route("/forgot", methods=["GET", "POST"])
@auth_bp.route("/forgot-password", methods=["GET", "POST"])
def forgot_password():
    if request.method == "GET":
        return render_template("auth/forgot_password.html")

    email = request.form.get("email", "").strip().lower()
    if not email:
        flash("Email required")
        return render_template("auth/forgot_password.html"), 400

    rate_limited = _check_forgot_password_rate_limit(email)
    if rate_limited:
        return rate_limited

    user = User.query.filter(User.email == email).order_by(User.id.asc()).first()
    if user:
        raw_token, token_hash = generate_password_reset_token()
        token = PasswordResetToken(
            user_id=user.id,
            token_hash=token_hash,
            expires_at=utcnow() + timedelta(minutes=_PASSWORD_RESET_TTL_MINUTES),
        )
        db.session.add(token)
        db.session.commit()

        server_url = resolve_server_url(current_app, request)
        reset_url = f"{server_url}/auth/reset/{raw_token}"
        email_service.send_password_reset(
            to=email,
            reset_url=reset_url,
            username=user.username,
        )

    return _render_forgot_password_success(email)


@auth_bp.route("/reset/<token>", methods=["GET", "POST"])
def reset_password(token):
    row, user = _get_valid_password_reset(token)
    if not row or not user:
        return render_template(
            "auth/reset_password.html",
            reset_error="This password reset link expired or was already used.",
        ), 400

    if request.method == "GET":
        return render_template("auth/reset_password.html", username=user.username)

    password = request.form.get("password", "")
    confirm_password = request.form.get("confirm_password", "")
    if len(password) < 8:
        flash("Password must be at least 8 characters")
        return render_template("auth/reset_password.html", username=user.username), 400
    if password != confirm_password:
        flash("Passwords do not match")
        return render_template("auth/reset_password.html", username=user.username), 400

    user.password_hash = hash_password(password)
    row.used_at = utcnow()
    # Claiming the email is skipped when another account already verified it
    # (e.g. that person chose "not your account" at Ideaflow sign-in); the reset
    # itself still completes.
    email_taken = bool(user.email) and (
        User.query.filter(
            User.email == user.email,
            User.email_verified_at.isnot(None),
            User.id != user.id,
        ).first()
        is not None
    )
    if not email_taken:
        user.email_verified_at = utcnow()
    try:
        materialize_pending_invites_for(user)
        db.session.commit()
    except IntegrityError:
        db.session.rollback()
        user.password_hash = hash_password(password)
        row.used_at = utcnow()
        db.session.commit()

    login_user(user)
    flash("Password reset. You're signed in.")
    return redirect(url_for("wiki.user_profile", username=user.username))


@auth_bp.route("/resend-verification", methods=["POST"])
@login_required
def resend_verification():
    """Re-send the verification link to the signed-in user's current email."""
    if not current_user.email:
        flash("No email on your account. Add one in settings.")
        return redirect(url_for("main.settings"))
    if current_user.email_verified_at is not None:
        flash("Your email is already verified.")
        return redirect(url_for("main.settings"))
    send_verification_if_needed(current_user)
    flash(f"Verification email sent to {current_user.email}.")
    return redirect(request.referrer or url_for("main.settings"))


@auth_bp.route("/verify/<token>")
def verify_email(token):
    """Consume an email-verification token; sets users.email_verified_at.
    Verification is non-blocking everywhere else — this endpoint just clears
    the 'unverified' banner and lets pending invites for the address
    materialize."""
    token_hash = hash_one_time_token(token)
    row = EmailVerificationToken.query.filter_by(token_hash=token_hash).first()
    if not row or row.used_at is not None or row.expires_at <= utcnow():
        flash("This verification link is invalid or expired.")
        return redirect(url_for("auth.login"))

    user = User.query.get(row.user_id)
    if not user:
        flash("This verification link is invalid.")
        return redirect(url_for("auth.login"))

    # If the user's current email still matches the token's captured email,
    # mark verified. If it differs (user changed email in settings after
    # minting), update to the token's address and mark verified — the token
    # proves ownership of `new_email` specifically.
    if user.email != row.new_email:
        user.email = row.new_email
    user.email_verified_at = utcnow()
    row.used_at = utcnow()
    db.session.commit()

    # Pending invites scoped to this address can now apply.
    materialize_pending_invites_for(user)
    db.session.commit()

    if current_user.is_authenticated and current_user.id == user.id:
        flash("Email verified.")
        return redirect(url_for("main.settings"))
    # user clicked from a different browser / not signed in — sign them in
    login_user(user)
    flash("Email verified. You're signed in.")
    return redirect(url_for("wiki.user_profile", username=user.username))


@auth_bp.route("/logout")
@login_required
def logout():
    logout_user()
    return _mark_choose_account_next(redirect(url_for("main.index")))


@auth_bp.route("/switch-account")
def switch_account():
    """Account-menu "Switch account": sign out of WikiHub locally, then start
    an Ideaflow sign-in that shows the provider's account chooser."""
    next_url = _safe_next_url()
    if current_user.is_authenticated:
        logout_user()
    if not _ideaflow_enabled():
        return _mark_choose_account_next(redirect(url_for("auth.login", next=next_url)))
    return redirect(url_for("auth.ideaflow_login", switch=1, next=next_url))


@auth_bp.route("/magic/<token>")
def magic_login(token):
    token_hash = hash_one_time_token(token)
    token_row = MagicLoginToken.query.filter_by(token_hash=token_hash).first()
    if (
        not token_row
        or token_row.used_at is not None
        or token_row.expires_at <= utcnow()
    ):
        flash("This magic sign-in link is invalid or expired.")
        return redirect(url_for("auth.login")), 302

    user = User.query.get(token_row.user_id)
    if not user:
        flash("This magic sign-in link is invalid.")
        return redirect(url_for("auth.login")), 302

    token_row.used_at = utcnow()
    db.session.commit()
    login_user(user)
    return redirect(_safe_redirect_target(token_row.redirect_path))


# --- Google OAuth ---

@auth_bp.route("/google")
def google_login():
    try:
        client = oauth.google
    except AttributeError:
        flash("Google OAuth not configured")
        return redirect(url_for("auth.login"))
    redirect_uri = url_for("auth.google_callback", _external=True)
    response = client.authorize_redirect(redirect_uri)
    location = response.headers.get("Location", "")
    state = parse_qs(urlparse(location).query).get("state", [""])[0]
    _stash_google_oauth_context(state, _google_oauth_context_from_request())
    return response


@auth_bp.route("/google/callback")
def google_callback():
    try:
        client = oauth.google
    except AttributeError:
        flash("Google OAuth not configured")
        return redirect(url_for("auth.login"))

    token = client.authorize_access_token()
    oauth_context = _pop_google_oauth_context()
    userinfo = token.get("userinfo", {})
    google_id = userinfo.get("sub")
    email = userinfo.get("email")
    email_verified = bool(userinfo.get("email_verified"))
    name = userinfo.get("name", "")

    if not google_id:
        flash("Could not get Google user info")
        return redirect(url_for("auth.login"))

    user = _resolve_or_create_google_user(
        google_id=google_id,
        email=email,
        email_verified=email_verified,
        name=name,
    )

    login_user(user)
    _apply_pending_invites_on_login(
        user,
        invite_email=oauth_context.get("email"),
        invite_token=oauth_context.get("it"),
    )
    return _remember_login_method(redirect(_safe_redirect_target(oauth_context.get("next"))), "google")


def _generate_unique_username(*, email, name):
    """Derive a safe, collision-free username from an OAuth/OIDC identity's
    email or display name. Shared by Google and Ideaflow ID sign-in."""
    base_username = (email.split("@")[0] if email else (name or "").lower().replace(" ", ""))[:32]
    # sanitize to allowed charset, then ensure it doesn't collide with reserved names or wiki subdomains
    base_username = re.sub(r"[^a-z0-9_-]", "", base_username.lower()) or "user"
    if len(base_username) < 2:
        base_username = base_username + "user"
    username = base_username
    counter = 1
    while (
        User.query.filter_by(username=username).first()
        or validate_username(username) is not None
    ):
        username = f"{base_username}{counter}"
        counter += 1
    return username


def _resolve_or_create_google_user(*, google_id, email, email_verified, name):
    """Look up-or-create a User for a Google sign-in.

    Security (wikihub-ks5t.4): auto-linking by email is allowed ONLY when both
    sides vouch for the email — Google reports `email_verified=true` in the
    id_token AND the local candidate's `email_verified_at IS NOT NULL`. Without
    both, we create a fresh account. This blocks a takeover where an attacker
    claims someone else's email as unverified on a password account to harvest
    that person's later Google sign-in.
    """
    user = User.query.filter_by(google_id=google_id).first()
    if not user and email and email_verified:
        # Only a VERIFIED row can ever be a link target; picking it directly also
        # keeps an unverified duplicate of the same address from shadowing it.
        candidate = User.query.filter(
            User.email == email, User.email_verified_at.isnot(None)
        ).first()
        if candidate:
            candidate.google_id = google_id
            db.session.commit()
            user = candidate

    if not user:
        username = _generate_unique_username(email=email, name=name)

        user = User(
            username=username,
            email=email,
            email_verified_at=utcnow() if email and email_verified else None,
            display_name=name,
            google_id=google_id,
        )
        db.session.add(user)
        db.session.flush()
        ensure_personal_wiki(user)
        db.session.commit()

        if email and email_verified:
            applied = materialize_pending_invites_for(user)
            if applied:
                db.session.commit()
    elif email and email_verified and not user.email_verified_at:
        # existing user just linked Google AND Google asserts the email is
        # verified — treat as a verification event.
        user.email_verified_at = utcnow()
        db.session.commit()
        applied = materialize_pending_invites_for(user)
        if applied:
            db.session.commit()
    return user


# --- Ideaflow ID (OIDC relying party — wikihub-39pe) ---
#
# WikiHub is an independent confidential OIDC client of the Ideaflow ID
# authority (https://id.ideaflow.app/api/auth). See
# ~/memory/research/global-identity-architecture-2026-09-16.md for the
# cross-product architecture. Key rules enforced below:
#
#   - The immutable identity key is (issuer, subject), stored in
#     ExternalIdentity. A returning subject never consults email at all.
#   - A brand new Ideaflow subject is resolved to an existing WikiHub account
#     automatically ONLY when every one of these holds (code-d96; supersedes
#     the intermediate manual-link-only rule of wikihub-39pe):
#       * Ideaflow's `email_verified` claim is the JSON boolean `true`
#         (strict -- never truthiness, never a string);
#       * exactly one local account has that email (case-insensitive) with
#         `email_verified_at` set, i.e. local ownership was proven
#         independently by a real verification flow (the same two-sided
#         proof the Google resolver requires);
#       * that account has no link to a different subject for this issuer;
#       * that account is not privileged (a per-user `wiki_limit` grant);
#         privileged accounts need an explicit local-credential proof.
#     Where local ownership is NOT independently proven (legacy accounts whose
#     typed email was never verified), sign-in asks once for that account's
#     password (one-time ownership check) instead of forcing a trip through
#     Settings. A typed email alone never proves ownership: whoever typed it
#     may be a squatter who knows the password.
#   - The explicit signed-in linking flow (GET /auth/ideaflow/link) remains the
#     fallback for accounts that cannot be resolved automatically. It always
#     sends prompt=select_account (PR #31), so the provider shows which
#     Ideaflow account is being connected and a logged-in local session never
#     silently binds whichever person happens to hold the IdP session.
#   - Linking and first-time account creation both fail closed on conflict:
#     no operation ever silently attaches an identity to the "wrong" account,
#     including under a concurrent-request race -- that's enforced by the two
#     DB uniqueness constraints on ExternalIdentity (and the verified-email
#     unique index) plus try/except around the commit, not by pre-checks
#     alone.
#   - No global logout: /auth/logout only ever clears the local WikiHub
#     session, exactly as it does today for every other login method. It does
#     mark the browser so the next Ideaflow sign-in shows the provider's
#     account chooser (prompt=select_account); "Switch account" (?switch=1)
#     does the same immediately (code-xbh.5).

_IDEAFLOW_OAUTH_CONTEXTS_SESSION_KEY = "ideaflow_oauth_contexts"
# One-time ownership check (code-d96): held in the signed session between the
# callback and POST /auth/ideaflow/confirm.
_IDEAFLOW_PENDING_LINK_SESSION_KEY = "ideaflow_pending_link"
_IDEAFLOW_PENDING_LINK_TTL_SECONDS = 10 * 60
_IDEAFLOW_PENDING_LINK_MAX_ATTEMPTS = 5
# Server-side cap on wrong passwords per local account across all pending
# checks (the per-check counter lives in the client-held session cookie and
# can be replayed), counted in the shared login-attempt window.
_IDEAFLOW_CONFIRM_MAX_FAILURES_PER_USER = 10


def _ideaflow_callback_url():
    """Resolve the Ideaflow ID OAuth callback from the app's single
    configured BASE_URL, never the current request's Host header.

    A login started on a user/wiki subdomain (jacobcole.wikihub.md) or an
    active custom domain must still send the OIDC provider the one
    redirect_uri registered for this client — https://wikihub.md/auth/ideaflow/callback
    in production. `url_for(..., _external=True)` builds off `request.host`,
    which would drift with the originating host and be rejected by the
    provider as a redirect_uri mismatch. BASE_URL defaults to
    http://localhost:5000 locally, so dev/test flows are unaffected."""
    base_url = (current_app.config.get("BASE_URL") or "").rstrip("/")
    if not base_url:
        return url_for("auth.ideaflow_callback", _external=True)
    return base_url + url_for("auth.ideaflow_callback")


def _ideaflow_enabled():
    return ideaflow_oidc_enabled(current_app.config)


def _ideaflow_enabled_required(view):
    @wraps(view)
    def wrapped(*args, **kwargs):
        if not _ideaflow_enabled():
            abort(404)
        return view(*args, **kwargs)

    return wrapped


def _ideaflow_client():
    """Return the registered 'ideaflow' Authlib client, or None if the kill
    switch is off / it was never registered. Routes must abort(404) rather
    than error when this is None — the feature is meant to disappear
    entirely, not degrade."""
    if not _ideaflow_enabled():
        return None
    return getattr(oauth, "ideaflow", None)


def _stash_ideaflow_oauth_context(state, context):
    if not state:
        return
    pending = dict(session.get(_IDEAFLOW_OAUTH_CONTEXTS_SESSION_KEY, {}))
    pending[state] = context
    session[_IDEAFLOW_OAUTH_CONTEXTS_SESSION_KEY] = pending


def _pop_ideaflow_oauth_context():
    state = request.args.get("state", "").strip()
    if not state:
        return {}
    pending = dict(session.get(_IDEAFLOW_OAUTH_CONTEXTS_SESSION_KEY, {}))
    context = pending.pop(state, {})
    if pending:
        session[_IDEAFLOW_OAUTH_CONTEXTS_SESSION_KEY] = pending
    else:
        session.pop(_IDEAFLOW_OAUTH_CONTEXTS_SESSION_KEY, None)
    return context


@auth_bp.route("/ideaflow")
def ideaflow_login():
    """Start a sign-in/sign-up flow (code-xbh.5). Silent SSO by default: no
    `prompt`, so a person already signed in to Ideaflow ID lands straight back
    here. The provider's account chooser (prompt=select_account) is requested
    only for the explicit "Switch account" action (`?switch=1`) or for the
    first sign-in after an explicit WikiHub sign-out. Only that allowlisted
    flag is honoured; nothing else from the query string is ever forwarded to
    the provider. The callback resolves to an existing linked account, safely
    auto-links a verified match, or mints a brand new one (see the policy
    above)."""
    client = _ideaflow_client()
    if not client:
        abort(404)
    redirect_uri = _ideaflow_callback_url()
    switch = request.args.get("switch") == "1"
    choose = switch or request.cookies.get(_CHOOSE_ACCOUNT_COOKIE) == "1"
    if choose:
        response = client.authorize_redirect(redirect_uri, prompt="select_account")
    else:
        response = client.authorize_redirect(redirect_uri)
    location = response.headers.get("Location", "")
    state = parse_qs(urlparse(location).query).get("state", [""])[0]
    context = {"next": _safe_next_url(), "mode": "signin", "switch": switch}
    # Invite links (?email=&it=) keep their one-click verification through the
    # round trip, exactly as the Google flow does.
    invite_email = request.args.get("email", "").strip().lower()
    invite_token = request.args.get("it", "").strip()
    if invite_email:
        context["email"] = invite_email
    if invite_token:
        context["it"] = invite_token
    _stash_ideaflow_oauth_context(state, context)
    return response


@auth_bp.route("/ideaflow/link")
@_ideaflow_enabled_required
@login_required
def ideaflow_link():
    """Explicit signed-in linking flow, kept as the fallback for accounts that
    cannot be resolved automatically at sign-in. The linking user's id is
    captured now and re-checked at the callback so a logout/login swap
    mid-flow can't attach the identity to a different account. A logged-in
    local session alone must never silently bind whichever person holds the
    IdP session, so this always asks the provider to show which Ideaflow
    account is being connected (prompt=select_account, PR #31)."""
    client = _ideaflow_client()
    if not client:
        abort(404)
    redirect_uri = _ideaflow_callback_url()
    # Ideaflow ID is single sign-on: without a prompt it silently returns the
    # browser's current provider account. Linking binds that identity to this
    # WikiHub account permanently, so ask the provider to show which Ideaflow
    # account is being linked (with "Use another account") first.
    response = client.authorize_redirect(redirect_uri, prompt="select_account")
    location = response.headers.get("Location", "")
    state = parse_qs(urlparse(location).query).get("state", [""])[0]
    context = {
        "next": url_for("main.settings"),
        "mode": "link",
        "user_id": current_user.id,
    }
    _stash_ideaflow_oauth_context(state, context)
    return response


@auth_bp.route("/ideaflow/callback")
def ideaflow_callback():
    client = _ideaflow_client()
    if not client:
        abort(404)

    oauth_context = _pop_ideaflow_oauth_context()
    try:
        token = client.authorize_access_token()
    except Exception:
        flash("Ideaflow ID sign-in failed or was cancelled.")
        return redirect(url_for("auth.login"))

    userinfo = token.get("userinfo") or {}
    subject = userinfo.get("sub")
    # `iss` must come from Authlib's cryptographically-validated ID-token
    # claims (parse_id_token requires it as an essential claim and checks it
    # against the discovered issuer already). Never substitute the
    # configured issuer for a missing one here — that would turn an absent
    # or malformed claim into an unverified assumption of authenticity.
    issuer = userinfo.get("iss")
    email = (userinfo.get("email") or "").strip().lower() or None
    # OIDC defines this claim as a JSON boolean. Anything else -- the string
    # "false" (truthy!), 1, "true" -- is untrusted and must never grant
    # verified-email trust, so this is an identity check, not truthiness.
    email_verified = userinfo.get("email_verified") is True
    name = userinfo.get("name") or userinfo.get("preferred_username") or ""

    if not subject or not issuer:
        flash("Could not get Ideaflow ID user info.")
        return redirect(url_for("auth.login"))

    if issuer != current_app.config["IDEAFLOW_OIDC_ISSUER"]:
        # Defense in depth: Authlib already validates the id_token's iss
        # against the discovered issuer, but a configured-vs-asserted
        # mismatch here would mean the two have drifted apart. Refuse rather
        # than silently trusting an unexpected authority.
        flash("Unexpected Ideaflow ID issuer.")
        return redirect(url_for("auth.login"))

    mode = oauth_context.get("mode", "signin")
    if mode == "link":
        return _handle_ideaflow_link_callback(
            oauth_context,
            issuer=issuer,
            subject=subject,
            # The stored snapshot is display-only, but never keep an email the
            # provider did not verify.
            email=email if email_verified else None,
        )
    return _handle_ideaflow_signin_callback(
        oauth_context, issuer=issuer, subject=subject, email=email, email_verified=email_verified, name=name
    )


def _login_via_ideaflow_identity(identity, oauth_context):
    user = db.session.get(User, identity.user_id)
    if not user:
        flash("This Ideaflow ID is linked to a WikiHub account that no longer exists.")
        return redirect(url_for("auth.login"))
    login_user(user)
    _apply_ideaflow_invite_context(user, oauth_context)
    return _remember_login_method(redirect(_safe_redirect_target(oauth_context.get("next"))), "ideaflow")


def _ideaflow_email_candidates(email):
    """Every local account whose email equals the Ideaflow email,
    case-insensitively. Unverified duplicates are legal in WikiHub (only
    verified emails are unique), so this can return several rows."""
    if not email:
        return []
    return User.query.filter(db.func.lower(User.email) == email).order_by(User.id).all()


def _ideaflow_account_is_privileged(user):
    """A per-user `wiki_limit` grant is WikiHub's only privilege marker (the
    owner's account carries one). Silent email-based binding must never hand
    such an account to whoever controls a matching address (recycled or
    changed provider emails), so it needs an explicit credential proof."""
    return user.wiki_limit is not None


def _ideaflow_conflict(message):
    """An Ideaflow sign-in that could not be matched to a WikiHub account.
    The login page then offers the WikiHub-password page as the way into the
    existing account (category "ideaflow_fallback"), so nobody is locked out
    by the single-button login page."""
    flash(message, "ideaflow_fallback")
    return redirect(url_for("auth.login"))


def _apply_ideaflow_invite_context(user, oauth_context):
    _apply_pending_invites_on_login(
        user,
        invite_email=oauth_context.get("email") or "",
        invite_token=oauth_context.get("it") or "",
    )


def _finish_ideaflow_login(user, oauth_context):
    login_user(user)
    _apply_ideaflow_invite_context(user, oauth_context)
    return _remember_login_method(redirect(_safe_redirect_target(oauth_context.get("next"))), "ideaflow")


def _link_ideaflow_identity(user, *, issuer, subject, email, mark_email_verified=False):
    """Attach (issuer, subject) to `user` in a single commit. The DB's two
    ExternalIdentity uniqueness constraints (and the verified-email unique
    index) are the atomic guard: a concurrent link, or a second subject for
    the same account, surfaces as IntegrityError and returns False -- never an
    attachment to the wrong account."""
    db.session.add(ExternalIdentity(user_id=user.id, issuer=issuer, subject=subject, email=email))
    if mark_email_verified and user.email_verified_at is None:
        user.email_verified_at = utcnow()
    try:
        db.session.commit()
    except IntegrityError:
        db.session.rollback()
        return False
    return True


def _start_ideaflow_ownership_check(user, *, issuer, subject, email, oauth_context, reason, name=""):
    """`user` is None for reason "choose": every matching row is unverified and
    cannot be matched safely (several rows, or it is already bound to someone
    else's Ideaflow identity), so the person is offered only the way out."""
    session[_IDEAFLOW_PENDING_LINK_SESSION_KEY] = {
        "user_id": user.id if user else None,
        "issuer": issuer,
        "subject": subject,
        "email": email,
        "name": name,
        "next": oauth_context.get("next"),
        "reason": reason,
        "exp": int(time()) + _IDEAFLOW_PENDING_LINK_TTL_SECONDS,
        "attempts": 0,
    }
    return redirect(url_for("auth.ideaflow_confirm"))


def _load_ideaflow_pending_link():
    pending = session.get(_IDEAFLOW_PENDING_LINK_SESSION_KEY)
    required = ("user_id", "issuer", "subject", "email", "reason", "exp", "attempts")
    if (
        not isinstance(pending, dict)
        or any(key not in pending for key in required)
        or not isinstance(pending["exp"], int)
        or pending["exp"] < int(time())
    ):
        session.pop(_IDEAFLOW_PENDING_LINK_SESSION_KEY, None)
        return None
    return dict(pending)


def _handle_ideaflow_signin_callback(oauth_context, *, issuer, subject, email, email_verified, name):
    # Bounded retry: a lost race (a concurrent request inserted the identity or
    # the verified email first) re-resolves against the new state instead of
    # surfacing an error or attaching to the wrong account.
    for _attempt in range(2):
        # 1. Exact (issuer, subject) is the whole ballgame for a returning
        # person -- this path never looks at email at all.
        existing_identity = ExternalIdentity.query.filter_by(issuer=issuer, subject=subject).first()
        if existing_identity:
            return _login_via_ideaflow_identity(existing_identity, oauth_context)

        # 2. A brand new subject whose email matches an existing local account.
        candidates = _ideaflow_email_candidates(email)
        if candidates:
            if not email_verified:
                return _ideaflow_conflict(
                    "Ideaflow did not verify this email address, so it can't be matched to an existing "
                    "WikiHub account. Sign in to WikiHub another way, then connect Ideaflow from Settings."
                )
            verified = [u for u in candidates if u.email_verified_at is not None]
            unverified = [u for u in candidates if u.email_verified_at is None]
            if not verified and len(unverified) > 1:
                # Never guess between accounts. Nobody has proven any of them,
                # so the person may still create a fresh one.
                return _start_ideaflow_ownership_check(
                    None, issuer=issuer, subject=subject, email=email,
                    oauth_context=oauth_context, reason="choose", name=name,
                )
            if len(verified) > 1:
                return _ideaflow_conflict(
                    "More than one WikiHub account uses this email, so Ideaflow can't be matched "
                    "automatically. Sign in to the right account, then connect Ideaflow from Settings."
                )
            target = verified[0] if verified else unverified[0]
            if ExternalIdentity.query.filter_by(user_id=target.id, issuer=issuer).first():
                # Already connected to a DIFFERENT Ideaflow subject: never
                # rebind, never merge. An unverified row may be someone else's
                # squat (they connected their own identity to it), so the
                # verified owner of this Ideaflow email still gets the way out.
                if not verified:
                    return _start_ideaflow_ownership_check(
                        None, issuer=issuer, subject=subject, email=email,
                        oauth_context=oauth_context, reason="choose", name=name,
                    )
                return _ideaflow_conflict(
                    "The WikiHub account with this email is already connected to a different Ideaflow "
                    "account. Sign in to it with its usual method."
                )
            if verified and not _ideaflow_account_is_privileged(target):
                # Both sides vouch for the email (Ideaflow's strict-true claim
                # AND the local account's independently proven email).
                if _link_ideaflow_identity(target, issuer=issuer, subject=subject, email=email):
                    flash("Ideaflow is now connected to your WikiHub account.")
                    return _finish_ideaflow_login(target, oauth_context)
                continue
            # Local ownership is not independently proven (unverified typed
            # email) or the account is privileged: one-time ownership check.
            return _start_ideaflow_ownership_check(
                target,
                issuer=issuer,
                subject=subject,
                email=email,
                oauth_context=oauth_context,
                reason="privileged" if verified else "unverified",
                name=name,
            )

        # 3. No local account claims this email: a brand new person.
        user = _create_ideaflow_account(
            issuer=issuer, subject=subject, email=email, email_verified=email_verified, name=name
        )
        if user is None:
            continue
        return _login_new_ideaflow_account(user, oauth_context)

    return _ideaflow_conflict("Ideaflow ID sign-in conflict — please try again.")


def _create_ideaflow_account(*, issuer, subject, email, email_verified, name):
    """Create the one local account (plus its (issuer, subject) link) for a
    brand-new Ideaflow person, or return None on a lost race. Only a
    provider-verified email is stored; storing an unverified one would let
    anyone squat a victim's address on a local row."""
    trusted_email = email if email_verified else None
    username = _generate_unique_username(email=trusted_email, name=name)
    user = User(
        username=username,
        email=trusted_email,
        email_verified_at=utcnow() if trusted_email else None,
        display_name=name or None,
    )
    db.session.add(user)
    try:
        db.session.flush()
        ensure_personal_wiki(user)
        db.session.add(ExternalIdentity(user_id=user.id, issuer=issuer, subject=subject, email=trusted_email))
        db.session.commit()
    except IntegrityError:
        db.session.rollback()
        return None
    return user


def _login_new_ideaflow_account(user, oauth_context):
    login_user(user)
    if user.email:
        applied = materialize_pending_invites_for(user)
        if applied:
            db.session.commit()
    return _remember_login_method(redirect(_safe_redirect_target(oauth_context.get("next"))), "ideaflow")


@auth_bp.route("/ideaflow/confirm", methods=["GET", "POST"])
@_ideaflow_enabled_required
def ideaflow_confirm():
    """One-time ownership check (code-d96). Reached only from the Ideaflow
    callback when the matching local account's ownership is not independently
    proven (a legacy unverified email) or is privileged. The person proves
    they own THAT local account with its password; only then is the Ideaflow
    identity bound. This replaces forcing everyone through Settings > Connect."""
    pending = _load_ideaflow_pending_link()
    if pending is None:
        flash("That Ideaflow confirmation expired. Please continue with Ideaflow again.")
        return redirect(url_for("auth.login"))
    user = db.session.get(User, pending["user_id"]) if pending["user_id"] else None
    if pending["reason"] != "choose" and not user:
        session.pop(_IDEAFLOW_PENDING_LINK_SESSION_KEY, None)
        flash("That WikiHub account no longer exists.")
        return redirect(url_for("auth.login"))

    def _render(status=200):
        return render_template(
            "auth/ideaflow_confirm.html",
            username=user.username if user else "",
            email=pending["email"],
            has_password=bool(user and user.password_hash),
            reason=pending["reason"],
        ), status

    if request.method == "GET":
        return _render()

    if user is None:
        # "choose" offers no password check, only /ideaflow/confirm/new.
        return _render(400)
    rate_limited = _check_login_rate_limit()
    if rate_limited:
        return rate_limited
    if not user.password_hash:
        return _render(400)

    failures = _ideaflow_confirm_failures[user.id]
    now = time()
    while failures and now - failures[0] > _LOGIN_WINDOW_SECONDS:
        failures.popleft()
    if len(failures) >= _IDEAFLOW_CONFIRM_MAX_FAILURES_PER_USER:
        flash("Too many incorrect attempts for this account. Try again in a few minutes.")
        return _render(429)

    if not check_password(request.form.get("password", ""), user.password_hash):
        failures.append(now)
        pending["attempts"] += 1
        if pending["attempts"] >= _IDEAFLOW_PENDING_LINK_MAX_ATTEMPTS:
            session.pop(_IDEAFLOW_PENDING_LINK_SESSION_KEY, None)
            flash("Too many incorrect attempts. Please continue with Ideaflow again.")
            return redirect(url_for("auth.login"))
        session[_IDEAFLOW_PENDING_LINK_SESSION_KEY] = pending
        flash("Incorrect password.")
        return _render(401)

    issuer, subject, email = pending["issuer"], pending["subject"], pending["email"]
    oauth_context = {"next": pending.get("next")}
    session.pop(_IDEAFLOW_PENDING_LINK_SESSION_KEY, None)

    # Re-check under the proven password: nothing may have changed since the
    # callback, and every check is backed by the DB constraints at commit.
    existing = ExternalIdentity.query.filter_by(issuer=issuer, subject=subject).first()
    if existing:
        if existing.user_id == user.id:
            return _finish_ideaflow_login(user, oauth_context)
        return _ideaflow_conflict("This Ideaflow account is already connected to a different WikiHub account.")
    if ExternalIdentity.query.filter_by(user_id=user.id, issuer=issuer).first():
        return _ideaflow_conflict(
            "That WikiHub account is already connected to a different Ideaflow account."
        )
    # Ideaflow verified this email and the password just proved this local
    # account is theirs, so the local email is now proven too.
    same_email = bool(user.email) and user.email.lower() == email
    if not _link_ideaflow_identity(user, issuer=issuer, subject=subject, email=email, mark_email_verified=same_email):
        return _ideaflow_conflict("Ideaflow sign-in conflicted with another request. Please try again.")
    flash("Ideaflow is now connected to your WikiHub account.")
    return _finish_ideaflow_login(user, oauth_context)


@auth_bp.route("/ideaflow/confirm/cancel", methods=["POST"])
@_ideaflow_enabled_required
def ideaflow_confirm_cancel():
    session.pop(_IDEAFLOW_PENDING_LINK_SESSION_KEY, None)
    return redirect(url_for("auth.login"))


@auth_bp.route("/ideaflow/confirm/new", methods=["POST"])
@_ideaflow_enabled_required
def ideaflow_confirm_new():
    """"Not your account?" escape hatch. Only offered when the ONLY local
    account using this email is an UNVERIFIED one: nobody has proven that row
    is theirs, so a person whose Ideaflow email is provider-verified must not
    be blocked by it (a squatter, a typo, or a forgotten legacy password).
    Mirrors what Google sign-in already does in the same situation: a fresh
    account is created and the unverified row is left untouched. A verified or
    privileged account is never bypassed this way."""
    pending = _load_ideaflow_pending_link()
    session.pop(_IDEAFLOW_PENDING_LINK_SESSION_KEY, None)
    if pending is None or pending["reason"] not in ("unverified", "choose"):
        flash("That Ideaflow confirmation expired. Please continue with Ideaflow again.")
        return redirect(url_for("auth.login"))

    issuer, subject, email = pending["issuer"], pending["subject"], pending["email"]
    oauth_context = {"next": pending.get("next")}
    existing = ExternalIdentity.query.filter_by(issuer=issuer, subject=subject).first()
    if existing:
        return _login_via_ideaflow_identity(existing, oauth_context)
    if any(u.email_verified_at is not None for u in _ideaflow_email_candidates(email)):
        # A verified owner appeared since the callback; automatic resolution
        # (not this escape hatch) must decide.
        return _ideaflow_conflict("A verified WikiHub account now uses this email. Please continue with Ideaflow again.")
    # `pending` is only ever created for a strictly verified Ideaflow email.
    user = _create_ideaflow_account(
        issuer=issuer, subject=subject, email=email, email_verified=True, name=pending.get("name") or ""
    )
    if user is None:
        return _ideaflow_conflict("Ideaflow sign-in conflicted with another request. Please try again.")
    return _login_new_ideaflow_account(user, oauth_context)


def _handle_ideaflow_link_callback(oauth_context, *, issuer, subject, email):
    linking_user_id = oauth_context.get("user_id")
    if not linking_user_id or not current_user.is_authenticated or current_user.id != linking_user_id:
        # The flow must finish in the same signed-in session that started
        # it (wikihub-39pe race safety) — a logout, a different login, or an
        # expired session in between all land here rather than guessing.
        flash("Ideaflow ID linking must be completed in the same signed-in session that started it.")
        return redirect(url_for("main.settings") if current_user.is_authenticated else url_for("auth.login"))

    existing_identity = ExternalIdentity.query.filter_by(issuer=issuer, subject=subject).first()
    if existing_identity:
        if existing_identity.user_id == current_user.id:
            flash("This Ideaflow ID is already linked to your account.")
        else:
            flash("This Ideaflow ID is already linked to a different WikiHub account.")
        return redirect(url_for("main.settings"))

    already_linked = ExternalIdentity.query.filter_by(user_id=current_user.id, issuer=issuer).first()
    if already_linked:
        flash("Your account is already linked to a different Ideaflow ID.")
        return redirect(url_for("main.settings"))

    db.session.add(ExternalIdentity(user_id=current_user.id, issuer=issuer, subject=subject, email=email))
    try:
        db.session.commit()
    except IntegrityError:
        # Someone else linked this exact subject (or this account got a
        # different link) between our checks above and the commit — fail
        # closed rather than attaching to the wrong account.
        db.session.rollback()
        flash("This Ideaflow ID was just linked elsewhere — please try again.")
        return redirect(url_for("main.settings"))

    flash("Ideaflow ID linked to your account.")
    return redirect(url_for("main.settings"))
