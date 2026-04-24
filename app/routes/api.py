import json
import re
import os
import secrets
import shutil
import time
from datetime import timedelta
from collections import defaultdict
from urllib.parse import unquote

from flask import current_app, request, jsonify
from sqlalchemy import inspect

from app import db
from app.models import User, ApiKey, MagicLoginToken, UsernameRedirect, utcnow, Wiki
from app.auth_utils import (
    generate_api_key, generate_magic_login_token, hash_password, check_password,
    api_auth_required, api_auth_optional, get_current_user_from_request, rate_limit_writes,
)
from app.credentials_hint import build_client_config, resolve_server_url
from app.git_backend import _repo_path
from app.routes import api_bp
from app.subdomains import validate_username, validate_wiki_subdomain
from app import email_service
from app.url_utils import page_path_from_url_path
from app.git_sync import list_files_in_repo
from app.wiki_ops import ensure_personal_wiki, materialize_pending_invites_for

_USERNAME_RE = re.compile(r'^[a-z0-9_-]+$')
_ACCESS_REQUEST_RE = re.compile(r"^/@(?P<owner>[a-z0-9_-]+)/(?P<slug>[a-z0-9_-]+)(?:/(?P<target>.*))?$")
_access_request_timestamps = defaultdict(list)


@api_bp.route("/accounts", methods=["POST"])
def create_account():
    """agent-native registration. no browser needed.
    POST /api/v1/accounts {username?, display_name?, email?}
    -> 201 {user_id, username, api_key}"""
    data = request.get_json(silent=True) or {}

    username = data.get("username", "").strip().lower()
    if not username:
        username = "user_" + secrets.token_hex(4)

    email = data.get("email", "").strip().lower() or None
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

    # apply any pending invites addressed to this email (no-op unless verified)
    applied = materialize_pending_invites_for(user)
    if applied:
        db.session.commit()

    # Non-blocking verification email if the caller supplied an email.
    # Verification isn't required — the account is live and usable immediately.
    from app.routes.auth import send_verification_if_needed
    send_verification_if_needed(user)

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


def _append_access_request_audit(*, user_id, wiki_id, requested_path, requester_email, note):
    if not inspect(db.engine).has_table("audit_log"):
        return
    db.session.execute(
        db.text(
            """
            INSERT INTO audit_log (user_id, action, target_type, target_id, detail_json, created_at)
            VALUES (:user_id, :action, :target_type, :target_id, CAST(:detail_json AS json), NOW())
            """
        ),
        {
            "user_id": user_id,
            "action": "access.request",
            "target_type": "wiki",
            "target_id": wiki_id,
            "detail_json": json.dumps({
                "requested_path": requested_path,
                "requester_email": requester_email,
                "note": note,
            }),
        },
    )


def _resolve_access_request_target(requested_path):
    safe_path = _sanitize_redirect_path(requested_path, fallback="")
    match = _ACCESS_REQUEST_RE.match(safe_path)
    if not match:
        return None

    owner_name = match.group("owner")
    slug = match.group("slug")
    target = unquote(match.group("target") or "")
    owner = User.query.filter_by(username=owner_name).first()
    if not owner:
        return None
    wiki = Wiki.query.filter_by(owner_id=owner.id, slug=slug).first()
    if not wiki:
        return None

    repo_files = None
    target_exists = False

    if not target:
        target_exists = True
    else:
        repo_files = set(list_files_in_repo(owner.username, wiki.slug, public=False))
        if safe_path.endswith("/"):
            folder = target.strip("/")
            folder_index = f"{folder}/index.md"
            target_exists = folder_index in repo_files or any(path.startswith(folder + "/") for path in repo_files)
        else:
            literal = target
            literal_md = target if target.endswith(".md") else target + ".md"
            normalized = page_path_from_url_path(target)
            normalized_md = normalized if normalized.endswith(".md") else normalized + ".md"
            candidates = {literal, literal_md, normalized, normalized_md}
            target_exists = any(candidate in repo_files for candidate in candidates)

    return {
        "owner": owner,
        "wiki": wiki,
        "requested_path": safe_path,
        "target_exists": target_exists,
    }


def _access_request_allowed(ip, requested_path, window_seconds=300):
    key = (ip or "unknown", requested_path)
    now = time.monotonic()
    hits = [t for t in _access_request_timestamps[key] if now - t < window_seconds]
    _access_request_timestamps[key] = hits
    if hits:
        return False
    hits.append(now)
    return True


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


@api_bp.route("/access-requests", methods=["POST"])
@api_auth_optional
@rate_limit_writes(max_per_minute=5, max_per_ip_per_minute=10)
def create_access_request():
    data = request.get_json(silent=True) or {}
    requested_path = _sanitize_redirect_path(data.get("path"), fallback="")
    requester_email = (data.get("email") or "").strip().lower()
    note = (data.get("note") or "").strip()
    user = getattr(request, "current_user", None)
    ip = request.remote_addr or "unknown"

    neutral = {
        "ok": True,
        "message": "If access can be requested for this link, the owner has been notified.",
    }

    if not requested_path:
        return jsonify(neutral), 202

    target = _resolve_access_request_target(requested_path)
    if not target:
        return jsonify(neutral), 202

    if not _access_request_allowed(ip, requested_path):
        return jsonify(neutral), 202

    owner = target["owner"]
    wiki = target["wiki"]
    if not target["target_exists"]:
        return jsonify(neutral), 202

    effective_email = requester_email or (user.email.strip().lower() if user and user.email else "")
    requester_label = (
        f"@{user.username}" if user
        else effective_email
        or "Someone"
    )

    try:
        _append_access_request_audit(
            user_id=user.id if user else None,
            wiki_id=wiki.id,
            requested_path=requested_path,
            requester_email=effective_email or None,
            note=note or None,
        )
        db.session.commit()
    except Exception:
        db.session.rollback()

    if owner.email:
        base_url = resolve_server_url(current_app, request)
        email_service.send_access_request(
            to=owner.email,
            requester_label=requester_label,
            requester_email=effective_email,
            requested_url=requested_path,
            owner_username=owner.username,
            wiki_title=wiki.title or wiki.slug,
            note=note,
            server_url=base_url,
        )

    return jsonify(neutral), 202


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
        new_email = data["email"].strip().lower() or None
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
