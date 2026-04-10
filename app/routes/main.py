from flask import render_template

from app.models import Wiki, Page
from app.routes import main_bp


@main_bp.route("/")
def index():
    return render_template("landing.html")


@main_bp.route("/explore")
def explore():
    # featured: most-starred public wikis
    featured = Wiki.query.filter(Wiki.star_count > 0).order_by(Wiki.star_count.desc()).limit(6).all()

    # recent: most recently updated wikis that have at least one public page
    recent = Wiki.query.join(Page).filter(
        Page.visibility.in_(["public", "public-edit"])
    ).order_by(Wiki.updated_at.desc()).limit(12).all()

    # deduplicate (featured might overlap with recent)
    featured_ids = {w.id for w in featured}
    recent = [w for w in recent if w.id not in featured_ids]

    return render_template("explore.html", featured=featured, recent=recent)
