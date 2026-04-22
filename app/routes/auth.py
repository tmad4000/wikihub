import re
from collections import defaultdict, deque
from time import time
from urllib.parse import urlparse

from flask import render_template, redirect, url_for, flash, request, session, current_app, abort
from flask_login import login_user, logout_user, login_required
from authlib.integrations.flask_client import OAuth

from app import db
from app.models import User, ApiKey, MagicLoginToken, PendingInvite, utcnow
from app.auth_utils import hash_password, check_password, hash_api_key, hash_one_time_token
from app.routes import auth_bp
from app.subdomains import validate_username
from app.wiki_ops import ensure_personal_wiki, materialize_pending_invites_for

oauth = OAuth()

_SIGNUP_WINDOW_SECONDS = 3600
_SIGNUP_MAX_PER_IP = 10
_signup_attempts = defaultdict(deque)

_LOGIN_WINDOW_SECONDS = 300
_LOGIN_MAX_PER_IP = 20
_login_attempts = defaultdict(deque)

_USERNAME_RE = re.compile(r'^[a-z0-9_-]+$')


def _safe_next_url(fallback=None):
    """Validate the ?next= parameter to prevent open redirects.

    Order: explicit ?next= → Referer header (same-origin only) → fallback → main.index.
    The Referer fallback means clicking "Sign in" from any page redirects back after login,
    even if the link itself didn't include ?next=.
    """
    target = request.args.get("next", "")
    if target:
        parsed = urlparse(target)
        if not parsed.scheme and not parsed.netloc:
            return target

    referer = request.headers.get("Referer", "")
    if referer:
        parsed = urlparse(referer)
        # same-origin only — strip scheme/netloc and use the path
        if parsed.netloc == request.host and parsed.path and not parsed.path.startswith("/auth/"):
            return parsed.path + (f"?{parsed.query}" if parsed.query else "")

    return fallback or url_for("main.index")


def _check_login_rate_limit():
    """return 429 response if login rate limit exceeded, else None."""
    ip = request.headers.get("X-Forwarded-For", request.remote_addr or "unknown").split(",")[0].strip()
    attempts = _login_attempts[ip]
    now = time()
    while attempts and now - attempts[0] > _LOGIN_WINDOW_SECONDS:
        attempts.popleft()
    if len(attempts) >= _LOGIN_MAX_PER_IP:
        flash("Too many login attempts. Try again in a few minutes.")
        return render_template("auth/login.html"), 429
    attempts.append(now)
    return None


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
        return render_template(
            "auth/login.html",
            testing_login=current_app.debug and current_app.config.get("TESTING_LOGIN"),
            prefill_email=request.args.get("email", "").strip().lower(),
        )

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
            return render_template("auth/login.html"), 401
        login_user(user)
        if request.method == "GET":
            flash("Signed in via URL. Rotate this key if the link was shared.")
        return redirect(_safe_next_url())

    user = User.query.filter_by(username=username).first()
    if not user or not user.password_hash or not check_password(password, user.password_hash):
        flash("Invalid username or password")
        return render_template("auth/login.html"), 401

    login_user(user)
    _apply_pending_invites_on_login(user)
    if request.method == "GET":
        flash("Signed in via URL. Rotate credentials if the link was shared.")
    return redirect(_safe_next_url())


def _apply_pending_invites_on_login(user):
    """After a successful login, apply any pending invites for this user.

    Verification model: if the signed-in user's email matches the ?email=
    query param (set by an invite link) and they have pending invites at that
    address, treat the login as proof of email ownership and mark verified.
    Otherwise only materialize when the email is already verified.
    """
    if not user or not user.email:
        return
    invite_email = request.args.get("email", "").strip().lower()
    if (
        invite_email
        and invite_email == (user.email or "").lower()
        and not user.email_verified_at
        and PendingInvite.query.filter_by(email=invite_email).first()
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
        email = request.form.get("email", "").strip() or None
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

        # If the signup email matches a pending invite, the person clicked a
        # link we sent to that address — that's a valid proof of email
        # ownership for wikihub's purposes. Mark verified so the invite
        # materializes. Without this, invited users would land with accounts
        # but no access (see wikihub-skp7).
        has_pending_invite = bool(
            email and PendingInvite.query.filter_by(email=email.lower()).first()
        )
        user = User(
            username=username,
            email=email,
            email_verified_at=utcnow() if has_pending_invite else None,
            password_hash=hash_password(password),
        )
        db.session.add(user)
        db.session.flush()
        ensure_personal_wiki(user)
        db.session.commit()

        materialize_pending_invites_for(user)
        db.session.commit()
        attempts.append(now)

        login_user(user)
        return redirect(url_for("wiki.user_profile", username=user.username))

    # GET — prefill email from the invite-link query param
    prefill_email = request.args.get("email", "").strip().lower()
    # If they already have an account at that email, bounce them to login
    # with a message. Otherwise they'd just get "email already registered"
    # on submit, which is hostile to someone following an invite link.
    if prefill_email:
        existing = User.query.filter_by(email=prefill_email).first()
        if existing:
            flash("You already have an account — sign in to apply your invite.")
            return redirect(url_for("auth.login", email=prefill_email, next="/shared-with-me"))
    return render_template("auth/signup.html", prefill_email=prefill_email)


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
    return redirect(token_row.redirect_path or url_for("main.index"))


# --- Google OAuth ---

@auth_bp.route("/google")
def google_login():
    try:
        client = oauth.google
    except AttributeError:
        flash("Google OAuth not configured")
        return redirect(url_for("auth.login"))
    redirect_uri = url_for("auth.google_callback", _external=True)
    return client.authorize_redirect(redirect_uri)


@auth_bp.route("/google/callback")
def google_callback():
    try:
        client = oauth.google
    except AttributeError:
        flash("Google OAuth not configured")
        return redirect(url_for("auth.login"))

    token = client.authorize_access_token()
    userinfo = token.get("userinfo", {})
    google_id = userinfo.get("sub")
    email = userinfo.get("email")
    name = userinfo.get("name", "")

    if not google_id:
        flash("Could not get Google user info")
        return redirect(url_for("auth.login"))

    user = User.query.filter_by(google_id=google_id).first()
    if not user and email:
        user = User.query.filter_by(email=email).first()
        if user:
            user.google_id = google_id
            db.session.commit()

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
            email_verified_at=utcnow() if email else None,
            display_name=name,
            google_id=google_id,
        )
        db.session.add(user)
        db.session.flush()
        ensure_personal_wiki(user)
        db.session.commit()

        # Google verified the email — apply any pending invites now
        applied = materialize_pending_invites_for(user)
        if applied:
            db.session.commit()
    elif email and not user.email_verified_at:
        # existing user just linked Google — treat as verification event
        user.email_verified_at = utcnow()
        db.session.commit()
        applied = materialize_pending_invites_for(user)
        if applied:
            db.session.commit()

    login_user(user)
    return redirect(url_for("main.index"))
