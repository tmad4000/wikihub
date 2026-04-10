from flask import jsonify, render_template, request
from flask_login import login_required, current_user

from app import db
from app.discovery import discoverable_page_for_wiki, visible_wikis_for_owner
from app.models import Wiki, Page, ApiKey, User
from app.routes import main_bp


@main_bp.route("/")
def index():
    return render_template("landing.html")


@main_bp.route("/explore")
def explore():
    editorial = (
        Wiki.query.join(User, Wiki.owner_id == User.id)
        .filter(User.username == "wikihub", Wiki.slug == "wikihub")
        .all()
    )
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
    current_user.email = email
    db.session.commit()
    if request.is_json:
        return jsonify({"email": current_user.email})
    return jsonify({"email": current_user.email})


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
