import re
import os
import secrets
import shutil
from datetime import timedelta

from flask import current_app, request, jsonify

from app import db
from app.models import User, ApiKey, MagicLoginToken, UsernameRedirect, utcnow, Wiki
from app.auth_utils import (
    generate_api_key, generate_magic_login_token, hash_password, check_password, api_auth_required, get_current_user_from_request,
)
from app.credentials_hint import build_client_config, resolve_server_url
from app.git_backend import _repo_path
from app.routes import api_bp
from app.subdomains import validate_username, validate_wiki_subdomain
from app.wiki_ops import ensure_personal_wiki

_USERNAME_RE = re.compile(r'^[a-z0-9_-]+$')


@api_bp.route("/accounts", methods=["POST"])
def create_account():
    """agent-native registration. no browser needed.
    POST /api/v1/accounts {username?, display_name?, email?}
    -> 201 {user_id, username, api_key}"""
    data = request.get_json(silent=True) or {}

    username = data.get("username", "").strip().lower()
    if not username:
        username = "user_" + secrets.token_hex(4)

    email = data.get("email", "").strip() or None
    display_name = data.get("display_name", "").strip() or None
    password = data.get("password", "").strip() or None

    if not _USERNAME_RE.match(username) or len(username) < 2 or len(username) > 40:
        return {"error": "bad_request", "message": "Username must be 2-40 chars: lowercase letters, numbers, hyphens, or underscores"}, 400

    if User.query.filter_by(username=username).first():
        return {"error": "conflict", "message": f"Username '{username}' already taken"}, 409

    conflict = validate_username(username)
    if conflict:
        return {"error": "conflict", "message": conflict}, 409

    if email and User.query.filter_by(email=email).first():
        return {"error": "conflict", "message": "Email already registered"}, 409

    user = User(
        username=username,
        email=email,
        display_name=display_name,
        password_hash=hash_password(password) if password else None,
    )
    db.session.add(user)
    db.session.flush()  # get user.id
    ensure_personal_wiki(user)

    raw_key, key_hash, key_prefix = generate_api_key()
    api_key = ApiKey(
        user_id=user.id,
        key_hash=key_hash,
        key_prefix=key_prefix,
        label="Initial key",
    )
    db.session.add(api_key)
    db.session.commit()

    server_url = resolve_server_url(current_app, request)
    return jsonify({
        "user_id": user.id,
        "username": user.username,
        "api_key": raw_key,
        "client_config": build_client_config(user.username, raw_key, server_url),
    }), 201


@api_bp.route("/auth/token", methods=["POST"])
def get_token():
    """exchange username+password for an API key.
    POST /api/v1/auth/token {username, password}
    -> 200 {user_id, username, api_key}
    Creates a new key if the user has none, otherwise creates an additional key."""
    data = request.get_json(silent=True) or {}
    username = data.get("username", "").strip()
    password = data.get("password", "")

    if not username or not password:
        return {"error": "bad_request", "message": "username and password required"}, 400

    user = User.query.filter_by(username=username).first()
    if not user or not user.password_hash or not check_password(password, user.password_hash):
        return {"error": "unauthorized", "message": "Invalid username or password"}, 401

    # Generate a new API key for this login
    raw_key, key_hash, key_prefix = generate_api_key()
    api_key = ApiKey(
        user_id=user.id,
        key_hash=key_hash,
        key_prefix=key_prefix,
        label="Generated via /auth/token",
    )
    db.session.add(api_key)
    db.session.commit()

    server_url = resolve_server_url(current_app, request)
    return jsonify({
        "user_id": user.id,
        "username": user.username,
        "api_key": raw_key,
        "client_config": build_client_config(user.username, raw_key, server_url),
    })


def _sanitize_redirect_path(raw_path, fallback="/"):
    target = (raw_path or "").strip()
    if not target:
        return fallback
    if not target.startswith("/") or target.startswith("//"):
        return fallback
    return target


@api_bp.route("/auth/magic-link", methods=["POST"])
def create_magic_link():
    """mint a short-lived, single-use browser sign-in URL.

    auth (any of):
      - Authorization: Bearer wh_...   (API key)
      - body: {"username": "...", "password": "..."}
    body (optional): {"next": "/path"} — destination after sign-in.

    this lets a human ask an agent "give me a login link" using just a
    password, without the agent ever handing the API key to the browser.
    """
    data = request.get_json(silent=True) or {}

    # if the caller provided explicit credentials in the body, use those
    # (and only those) — don't silently fall through to a lingering
    # session cookie if the password is wrong.
    explicit_creds = "username" in data or "password" in data

    if explicit_creds:
        username = (data.get("username") or "").strip()
        password = data.get("password") or ""
        candidate = User.query.filter_by(username=username).first() if username else None
        if (
            candidate
            and candidate.password_hash
            and password
            and check_password(password, candidate.password_hash)
        ):
            user = candidate
        else:
            user = None
    else:
        user = get_current_user_from_request()

    if not user:
        return {
            "error": "unauthorized",
            "message": "Provide an Authorization: Bearer wh_... header or {username, password} in the body.",
        }, 401

    redirect_path = _sanitize_redirect_path(
        data.get("next"),
        fallback=f"/@{user.username}",
    )

    raw_token, token_hash = generate_magic_login_token()
    expires_at = utcnow() + timedelta(seconds=current_app.config["MAGIC_LOGIN_TTL_SECONDS"])
    token = MagicLoginToken(
        user_id=user.id,
        token_hash=token_hash,
        redirect_path=redirect_path,
        expires_at=expires_at,
    )
    db.session.add(token)
    db.session.commit()

    base_url = resolve_server_url(current_app, request)
    return jsonify({
        "login_url": f"{base_url}/auth/magic/{raw_token}",
        "expires_at": expires_at.isoformat(),
        "next": redirect_path,
    }), 201


@api_bp.route("/accounts/me", methods=["GET"])
@api_auth_required
def get_account():
    user = request.current_user
    return jsonify({
        "user_id": user.id,
        "username": user.username,
        "display_name": user.display_name,
        "email": user.email,
        "created_at": user.created_at.isoformat(),
    })


@api_bp.route("/accounts/me", methods=["PATCH"])
@api_auth_required
def update_account():
    user = request.current_user
    data = request.get_json(silent=True) or {}
    old_username = user.username

    if "username" in data:
        new_username = data["username"].strip().lower()
        if new_username != user.username:
            if not _USERNAME_RE.match(new_username) or len(new_username) < 2 or len(new_username) > 40:
                return {"error": "bad_request", "message": "Username must be 2-40 chars: lowercase letters, numbers, hyphens, or underscores"}, 400
            if User.query.filter_by(username=new_username).first():
                return {"error": "conflict", "message": "Username taken"}, 409
            conflict = validate_username(new_username, exclude_user_id=user.id)
            if conflict:
                return {"error": "conflict", "message": conflict}, 409
            UsernameRedirect.query.filter_by(old_username=new_username).delete()
            db.session.add(
                UsernameRedirect(
                    old_username=user.username,
                    user_id=user.id,
                    expires_at=utcnow() + timedelta(days=90),
                )
            )
            user.username = new_username

    if "display_name" in data:
        user.display_name = data["display_name"].strip() or None

    if "email" in data:
        new_email = data["email"].strip() or None
        if new_email and new_email != user.email:
            if User.query.filter_by(email=new_email).first():
                return {"error": "conflict", "message": "Email taken"}, 409
            user.email = new_email

    if user.username != old_username:
        old_repo_root = os.path.dirname(_repo_path(old_username, "placeholder"))
        new_repo_root = os.path.dirname(_repo_path(user.username, "placeholder"))
        if os.path.isdir(old_repo_root):
            os.makedirs(os.path.dirname(new_repo_root), exist_ok=True)
            shutil.move(old_repo_root, new_repo_root)
        personal = Wiki.query.filter_by(owner_id=user.id, slug=old_username).first()
        if personal:
            personal.slug = user.username

    db.session.commit()
    return jsonify({
        "user_id": user.id,
        "username": user.username,
        "display_name": user.display_name,
        "email": user.email,
    })


@api_bp.route("/claim-email", methods=["POST"])
@api_auth_required
def claim_email():
    user = request.current_user
    data = request.get_json(silent=True) or {}
    email = (data.get("email") or "").strip().lower()
    if not email:
        return {"error": "bad_request", "message": "email is required"}, 400
    existing = User.query.filter_by(email=email).first()
    if existing and existing.id != user.id:
        return {"error": "conflict", "message": "Email already claimed"}, 409
    user.email = email
    db.session.commit()
    return jsonify({"email": user.email})


@api_bp.route("/keys", methods=["POST"])
@api_auth_required
def create_key():
    user = request.current_user
    data = request.get_json(silent=True) or {}
    label = data.get("label", "")

    raw_key, key_hash, key_prefix = generate_api_key()
    api_key = ApiKey(
        user_id=user.id,
        key_hash=key_hash,
        key_prefix=key_prefix,
        label=label,
    )
    db.session.add(api_key)
    db.session.commit()

    return jsonify({
        "id": api_key.id,
        "key": raw_key,
        "prefix": key_prefix,
        "label": label,
    }), 201


@api_bp.route("/keys/<int:key_id>", methods=["DELETE"])
@api_auth_required
def delete_key(key_id):
    user = request.current_user
    api_key = ApiKey.query.filter_by(id=key_id, user_id=user.id).first()
    if not api_key:
        return {"error": "not_found", "message": "API key not found"}, 404

    db.session.delete(api_key)
    db.session.commit()
    return "", 204


@api_bp.route("/keys", methods=["GET"])
@api_auth_required
def list_keys():
    user = request.current_user
    keys = ApiKey.query.filter_by(user_id=user.id).all()
    return jsonify([{
        "id": k.id,
        "prefix": k.key_prefix,
        "label": k.label,
        "last_used_at": k.last_used_at.isoformat() if k.last_used_at else None,
        "agent_name": k.agent_name,
        "created_at": k.created_at.isoformat(),
    } for k in keys])


@api_bp.route("/users/search", methods=["GET"])
def search_users():
    """search users by username, email prefix, or display name.
    GET /api/v1/users/search?q=<query>
    -> {users: [{username, display_name}]}"""
    q = request.args.get("q", "").strip().lower()
    if len(q) < 2:
        return jsonify({"users": []})
    users = User.query.filter(
        db.or_(
            User.username.ilike(f"{q}%"),
            User.email.ilike(f"{q}%"),
            User.display_name.ilike(f"%{q}%"),
        )
    ).limit(10).all()
    return jsonify({"users": [
        {"username": u.username, "display_name": u.display_name}
        for u in users
    ]})
