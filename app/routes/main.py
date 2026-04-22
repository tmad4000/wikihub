from flask import current_app, flash, jsonify, redirect, render_template, request, url_for
from flask_login import login_required, logout_user, current_user

from app import db
from app.acl import grants_for_user, list_all_grants, parse_acl
from app.discovery import discoverable_page_for_wiki, visible_wikis_for_owner
from app.git_sync import read_file_from_repo
from app.models import Wiki, Page, ApiKey, User, Star, Fork, MagicLoginToken, UsernameRedirect, utcnow
from app.routes import main_bp
import os
import shutil
from app.wiki_ops import delete_wiki_repos


@main_bp.route("/")
def index():
    from app.models import Wiki, Page
    from app.discovery import discoverable_wiki_ids

    # agent content negotiation: if the client asks for markdown, serve AGENTS.md
    # directly instead of the human landing page. wikihub-55jv
    accept = request.headers.get("Accept", "")
    if "text/markdown" in accept and "text/html" not in accept:
        from app.routes.agent_surfaces import agents_md
        return agents_md()

    visible_ids = discoverable_wiki_ids()
    featured = (
        Wiki.query.filter(Wiki.id.in_(visible_ids))
        .order_by(Wiki.star_count.desc(), Wiki.updated_at.desc())
        .limit(3)
        .all()
    ) if visible_ids else []
    resp = current_app.make_response(render_template("landing.html", featured_wikis=featured))
    # agent discovery: HTTP Link header pointing at /AGENTS.md. wikihub-5764
    resp.headers["Link"] = (
        '</AGENTS.md>; rel="alternate"; type="text/markdown"; title="Agent setup", '
        '</llms.txt>; rel="alternate"; type="text/plain"; title="LLM index"'
    )
    return resp


@main_bp.route("/roadmap")
def roadmap():
    return render_template("roadmap.html")


@main_bp.route("/explore")
def explore():
    # Editorial picks: curated wikis that represent the best of wikihub
    _EDITORIAL_PICKS = [
        ("wikihub", "wikihub"),
    ]
    editorial = []
    for owner_name, slug in _EDITORIAL_PICKS:
        wiki = (
            Wiki.query.join(User, Wiki.owner_id == User.id)
            .filter(User.username == owner_name, Wiki.slug == slug)
            .first()
        )
        if wiki:
            editorial.append(wiki)
    # All wikis with at least one public page, excluding editorial
    editorial_ids = {wiki.id for wiki in editorial}
    all_wikis = (
        Wiki.query.join(Page)
        .filter(Page.visibility.in_(["public", "public-edit"]))
        .order_by(Wiki.updated_at.desc())
        .all()
    )
    all_wikis = [w for w in all_wikis if w.id not in editorial_ids]

    people = _people_directory(limit=6)

    return render_template("explore.html", editorial=editorial, wikis=all_wikis, people=people)


@main_bp.route("/people")
def people_index():
    return render_template("people.html", people=_people_directory())


@main_bp.route("/settings")
@login_required
def settings():
    api_keys = ApiKey.query.filter_by(user_id=current_user.id).order_by(ApiKey.created_at.desc()).all()
    personal_wiki = Wiki.query.filter_by(owner_id=current_user.id, slug=current_user.username).first()
    project_count = (
        Wiki.query.filter(Wiki.owner_id == current_user.id, Wiki.slug != current_user.username)
        .count()
    )
    return render_template(
        "settings.html",
        api_keys=api_keys,
        personal_wiki=personal_wiki,
        project_count=project_count,
    )


@main_bp.route("/claim-email", methods=["POST"])
@login_required
def claim_email_web():
    data = request.get_json(silent=True) or {}
    email = (data.get("email") or request.form.get("email") or "").strip().lower()
    if not email:
        return {"error": "bad_request", "message": "email is required"}, 400
    existing = User.query.filter_by(email=email).first()
    if existing and existing.id != current_user.id:
        return {"error": "conflict", "message": "Email already claimed"}, 409
    current_email = (current_user.email or "").strip().lower()
    email_changed = current_email != email

    current_user.email = email
    if email_changed:
        current_user.email_verified_at = None
    db.session.commit()

    if email_changed:
        from app.routes.auth import send_verification_if_needed

        send_verification_if_needed(current_user)
    if request.is_json:
        return jsonify({"email": current_user.email})
    return jsonify({"email": current_user.email})


@main_bp.route("/shared")
@login_required
def shared():
    username = current_user.username

    # --- Shared by me: grants I've given + my unlisted pages ---
    shared_by_me = []
    my_wikis = Wiki.query.filter_by(owner_id=current_user.id).all()
    for wiki in my_wikis:
        acl_text = read_file_from_repo(username, wiki.slug, ".wikihub/acl", public=False)
        rules = parse_acl(acl_text) if acl_text else []
        grants = list_all_grants(rules)
        # unlisted pages
        unlisted = Page.query.filter(
            Page.wiki_id == wiki.id,
            Page.visibility.in_(("unlisted", "unlisted-edit")),
        ).all()
        if grants or unlisted:
            shared_by_me.append({
                "wiki": wiki,
                "grants": [{"pattern": p, "username": u, "role": r} for p, u, r in grants],
                "unlisted": unlisted,
            })

    # --- Shared with me: grants others have given me ---
    shared_with_me = []
    other_wikis = Wiki.query.join(User, Wiki.owner_id == User.id).filter(
        Wiki.owner_id != current_user.id
    ).all()
    for wiki in other_wikis:
        wiki_owner = db.session.get(User, wiki.owner_id)
        if not wiki_owner:
            continue
        acl_text = read_file_from_repo(wiki_owner.username, wiki.slug, ".wikihub/acl", public=False)
        if not acl_text:
            continue
        rules = parse_acl(acl_text)
        user_grants = grants_for_user(rules, username)
        if user_grants:
            shared_with_me.append({
                "wiki": wiki,
                "owner": wiki_owner,
                "grants": [{"pattern": p, "role": r} for p, r in user_grants],
            })

    return render_template(
        "shared.html",
        shared_by_me=shared_by_me,
        shared_with_me=shared_with_me,
    )


@main_bp.route("/delete-account", methods=["POST"])
@login_required
def delete_account_api():
    """legacy JSON-body delete — /settings/delete-account is the primary path."""
    data = request.get_json(silent=True) or {}
    if data.get("confirm") != current_user.username:
        return {"error": "bad_request", "message": "Type your username to confirm"}, 400

    user_id = current_user.id
    username = current_user.username

    # delete git repos on disk for every wiki, then the user directory
    wikis = Wiki.query.filter_by(owner_id=user_id).all()
    for wiki in wikis:
        delete_wiki_repos(username, wiki.slug)
    user_dir = os.path.join(current_app.config["REPOS_DIR"], username)
    if os.path.isdir(user_dir):
        shutil.rmtree(user_dir)

    # clear DB: stars given by this user, stars on their wikis, forks, keys, tokens, redirects
    wiki_ids = [w.id for w in wikis]
    Star.query.filter(Star.user_id == user_id).delete()
    if wiki_ids:
        Star.query.filter(Star.wiki_id.in_(wiki_ids)).delete()
        Fork.query.filter(Fork.source_wiki_id.in_(wiki_ids)).delete()
        Fork.query.filter(Fork.forked_wiki_id.in_(wiki_ids)).delete()
    Fork.query.filter(Fork.user_id == user_id).delete()
    ApiKey.query.filter_by(user_id=user_id).delete()
    MagicLoginToken.query.filter_by(user_id=user_id).delete()
    UsernameRedirect.query.filter_by(user_id=user_id).delete()

    for wiki in wikis:
        db.session.delete(wiki)

    db.session.delete(current_user._get_current_object())
    db.session.commit()

    logout_user()
    return jsonify({"deleted": True})


@main_bp.route("/settings/llm-key", methods=["POST"])
@login_required
def save_llm_key():
    """Save user's Anthropic API key (encrypted at rest)."""
    import base64, hashlib
    from cryptography.fernet import Fernet

    data = request.get_json(silent=True) or {}
    raw_key = (data.get("key") or "").strip()
    if not raw_key:
        return {"error": "bad_request", "message": "key is required"}, 400
    if not raw_key.startswith("sk-ant-"):
        return {"error": "bad_request", "message": "Key should start with sk-ant-"}, 400

    # Derive encryption key from app SECRET_KEY
    secret = current_app.config["SECRET_KEY"]
    fernet_key = base64.urlsafe_b64encode(hashlib.sha256(secret.encode()).digest())
    f = Fernet(fernet_key)
    encrypted = f.encrypt(raw_key.encode()).decode()

    current_user.llm_api_key_encrypted = encrypted
    db.session.commit()
    return jsonify({"saved": True, "prefix": raw_key[:12] + "..."})


@main_bp.route("/settings/llm-key", methods=["DELETE"])
@login_required
def delete_llm_key():
    """Remove user's stored Anthropic API key."""
    current_user.llm_api_key_encrypted = None
    db.session.commit()
    return jsonify({"deleted": True})


def get_user_llm_key(user):
    """Decrypt and return a user's Anthropic API key, or None."""
    import base64, hashlib
    from cryptography.fernet import Fernet
    from flask import current_app

    if not user or not user.llm_api_key_encrypted:
        return None
    try:
        secret = current_app.config["SECRET_KEY"]
        fernet_key = base64.urlsafe_b64encode(hashlib.sha256(secret.encode()).digest())
        f = Fernet(fernet_key)
        return f.decrypt(user.llm_api_key_encrypted.encode()).decode()
    except Exception:
        return None


def _people_directory(limit=None):
    cards = []

    for user in User.query.order_by(User.created_at.asc()).all():
        visible_wikis = visible_wikis_for_owner(user, current_user)
        personal_wiki = next((wiki for wiki in visible_wikis if wiki.slug == user.username), None)
        project_wikis = [wiki for wiki in visible_wikis if wiki.slug != user.username]
        profile_page = discoverable_page_for_wiki(
            personal_wiki.id,
            viewer_is_owner=bool(current_user.is_authenticated and current_user.id == user.id),
        ) if personal_wiki else None

        cards.append(
            {
                "user": user,
                "personal_wiki": personal_wiki,
                "project_count": len(project_wikis),
                "visible_wiki_count": len(visible_wikis),
                "total_stars": sum(wiki.star_count for wiki in visible_wikis),
                "profile_excerpt": profile_page.excerpt if profile_page else None,
                "profile_is_public": bool(profile_page),
                "latest_wikis": project_wikis[:3],
            }
        )

    cards.sort(
        key=lambda card: (
            -card["visible_wiki_count"],
            -card["total_stars"],
            card["user"].username.lower(),
        )
    )

    if limit is not None:
        return cards[:limit]
    return cards
