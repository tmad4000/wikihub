from flask import render_template, redirect, url_for, flash, request, session
from flask_login import login_user, logout_user, login_required
from authlib.integrations.flask_client import OAuth

from app import db
from app.models import User
from app.auth_utils import hash_password, check_password
from app.routes import auth_bp

oauth = OAuth()


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
    if request.method == "POST":
        username = request.form.get("username", "").strip()
        password = request.form.get("password", "")

        user = User.query.filter_by(username=username).first()
        if not user or not user.password_hash or not check_password(password, user.password_hash):
            flash("Invalid username or password")
            return render_template("auth/login.html"), 401

        login_user(user)
        next_page = request.args.get("next", url_for("main.index"))
        return redirect(next_page)

    return render_template("auth/login.html")


@auth_bp.route("/signup", methods=["GET", "POST"])
def signup():
    if request.method == "POST":
        username = request.form.get("username", "").strip().lower()
        email = request.form.get("email", "").strip() or None
        password = request.form.get("password", "")

        if not username or not password:
            flash("Username and password required")
            return render_template("auth/signup.html"), 400

        if len(password) < 8:
            flash("Password must be at least 8 characters")
            return render_template("auth/signup.html"), 400

        if User.query.filter_by(username=username).first():
            flash("Username already taken")
            return render_template("auth/signup.html"), 409

        if email and User.query.filter_by(email=email).first():
            flash("Email already registered")
            return render_template("auth/signup.html"), 409

        user = User(
            username=username,
            email=email,
            password_hash=hash_password(password),
        )
        db.session.add(user)
        db.session.commit()

        login_user(user)
        return redirect(url_for("wiki.user_profile", username=user.username))

    return render_template("auth/signup.html")


@auth_bp.route("/logout")
@login_required
def logout():
    logout_user()
    return redirect(url_for("main.index"))


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

    # find existing user by google_id or email
    user = User.query.filter_by(google_id=google_id).first()
    if not user and email:
        user = User.query.filter_by(email=email).first()
        if user:
            user.google_id = google_id
            db.session.commit()

    if not user:
        # generate username from email or name
        base_username = (email.split("@")[0] if email else name.lower().replace(" ", ""))[:32]
        username = base_username
        counter = 1
        while User.query.filter_by(username=username).first():
            username = f"{base_username}{counter}"
            counter += 1

        user = User(
            username=username,
            email=email,
            display_name=name,
            google_id=google_id,
        )
        db.session.add(user)
        db.session.commit()

    login_user(user)
    return redirect(url_for("main.index"))
