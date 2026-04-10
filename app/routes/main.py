from flask import jsonify, render_template, request
from flask_login import login_required, current_user

from app import db
from app.models import Wiki, Page, ApiKey, User
from app.routes import main_bp


@main_bp.route("/")
def index():
    return render_template("landing.html")


@main_bp.route("/explore")
def explore():
    editorial = (
        Wiki.query.join(User, Wiki.owner_id == User.id)
        .filter(User.username == "wikihub", Wiki.slug == "wiki")
        .all()
    )
    popular = Wiki.query.filter(Wiki.star_count > 0).order_by(Wiki.star_count.desc()).limit(6).all()
    recent = (
        Wiki.query.join(Page)
        .filter(Page.visibility.in_(["public", "public-edit"]))
        .order_by(Wiki.updated_at.desc())
        .limit(12)
        .all()
    )

    editorial_ids = {wiki.id for wiki in editorial}
    popular = [wiki for wiki in popular if wiki.id not in editorial_ids]
    popular_ids = {wiki.id for wiki in popular} | editorial_ids
    recent = [wiki for wiki in recent if wiki.id not in popular_ids]

    return render_template("explore.html", editorial=editorial, popular=popular, recent=recent)


@main_bp.route("/settings")
@login_required
def settings():
    api_keys = ApiKey.query.filter_by(user_id=current_user.id).order_by(ApiKey.created_at.desc()).all()
    return render_template("settings.html", api_keys=api_keys)


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
