import os
import secrets
import shutil
from datetime import timedelta

from flask import request, jsonify

from app import db
from app.models import User, ApiKey, UsernameRedirect, utcnow, Wiki
from app.auth_utils import (
    generate_api_key, hash_password, check_password, api_auth_required, get_current_user_from_request,
)
from app.git_backend import _repo_path
from app.routes import api_bp
from app.wiki_ops import ensure_personal_wiki


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

    if User.query.filter_by(username=username).first():
        return {"error": "conflict", "message": f"Username '{username}' already taken"}, 409

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

    return jsonify({
        "user_id": user.id,
        "username": user.username,
        "api_key": raw_key,
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

    return jsonify({
        "user_id": user.id,
        "username": user.username,
        "api_key": raw_key,
    })


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
            if User.query.filter_by(username=new_username).first():
                return {"error": "conflict", "message": "Username taken"}, 409
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
