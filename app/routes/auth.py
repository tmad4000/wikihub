import re
from collections import defaultdict, deque
from time import time
from urllib.parse import urlparse, quote, parse_qs

from flask import render_template, redirect, url_for, flash, request, session, current_app, abort
from flask_login import login_user, logout_user, login_required, current_user
from authlib.integrations.flask_client import OAuth

from datetime import timedelta

from app import db
from app.models import User, ApiKey, MagicLoginToken, PendingInvite, EmailVerificationToken, PasswordResetToken, utcnow
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


def _login_template_context():
    return {
        "testing_login": current_app.debug and current_app.config.get("TESTING_LOGIN"),
        "prefill_email": request.values.get("email", "").strip().lower(),
        "invite_token": request.values.get("it", "").strip(),
        "next_value": _safe_next_url(fallback=""),
    }


def _render_login():
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
    else:
        return _render_login()

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
        return redirect(_safe_next_url())

    user = User.query.filter_by(username=username).first()
    if not user or not user.password_hash or not check_password(password, user.password_hash):
        flash("Invalid username or password")
        return _render_login(), 401

    login_user(user)
    _apply_pending_invites_on_login(user)
    if request.method == "GET":
        flash("Signed in via URL. Rotate credentials if the link was shared.")
    return redirect(_safe_next_url())


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
        return redirect(url_for("wiki.user_profile", username=user.username))

    # GET — prefill email + invite token from the invite-link query params
    prefill_email = request.args.get("email", "").strip().lower()
    prefill_token = request.args.get("it", "").strip()
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
    user.email_verified_at = utcnow()
    row.used_at = utcnow()
    materialize_pending_invites_for(user)
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
    return redirect(url_for("main.index"))


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
    return redirect(_safe_redirect_target(oauth_context.get("next")))


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
        candidate = User.query.filter_by(email=email).first()
        if candidate and candidate.email_verified_at is not None:
            candidate.google_id = google_id
            db.session.commit()
            user = candidate

    if not user:
        base_username = (email.split("@")[0] if email else name.lower().replace(" ", ""))[:32]
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
