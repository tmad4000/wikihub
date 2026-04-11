import os
import click

from flask import Flask
from flask_sqlalchemy import SQLAlchemy
from flask_login import LoginManager
from werkzeug.middleware.proxy_fix import ProxyFix

db = SQLAlchemy()
login_manager = LoginManager()


def create_app(config_class="config.Config"):
    app = Flask(__name__)
    app.config.from_object(config_class)
    app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1, x_host=1)

    db.init_app(app)
    login_manager.init_app(app)
    login_manager.login_view = "auth.login"

    from app.models import User

    @login_manager.user_loader
    def load_user(user_id):
        return db.session.get(User, int(user_id))

    from app.routes import main_bp, auth_bp, api_bp, wiki_bp
    app.register_blueprint(main_bp)
    app.register_blueprint(auth_bp)
    app.register_blueprint(api_bp, url_prefix="/api/v1")
    app.register_blueprint(wiki_bp)

    from app.routes.agent_chat import agent_chat_bp
    app.register_blueprint(agent_chat_bp, url_prefix="/api/v1")

    from app.git_backend import git_bp
    app.register_blueprint(git_bp)

    from app.routes.auth import init_oauth
    init_oauth(app)

    from app.url_utils import url_path_from_page_path

    @app.template_filter("page_url")
    def page_url_filter(value):
        return url_path_from_page_path(value, strip_md=True)

    os.makedirs(app.config["REPOS_DIR"], exist_ok=True)

    with app.app_context():
        db.session.execute(db.text("CREATE EXTENSION IF NOT EXISTS pg_trgm"))
        db.session.commit()
        db.create_all()
        from app.wiki_ops import ensure_official_wiki
        ensure_official_wiki()
        db.session.commit()

    @app.cli.group("wikihub")
    def wikihub_cli():
        """wikihub maintenance commands."""

    @wikihub_cli.command("reindex")
    @click.argument("wiki_ref", required=False)
    @click.option("--all", "all_wikis", is_flag=True, default=False)
    def reindex_command(wiki_ref=None, all_wikis=False):
        from app.models import User, Wiki
        from app.wiki_ops import index_repo_pages

        if all_wikis:
            wikis = Wiki.query.all()
        elif wiki_ref and "/" in wiki_ref:
            owner_name, slug = wiki_ref.split("/", 1)
            owner = User.query.filter_by(username=owner_name).first()
            wikis = [Wiki.query.filter_by(owner_id=owner.id, slug=slug).first()] if owner else []
        else:
            raise click.ClickException("pass owner/slug or use --all")

        for wiki in filter(None, wikis):
            index_repo_pages(wiki.owner.username, wiki.slug, wiki, reset=True)
            click.echo(f"reindexed {wiki.owner.username}/{wiki.slug}")
        db.session.commit()

    @wikihub_cli.command("verify")
    @click.argument("wiki_ref")
    def verify_command(wiki_ref):
        from app.models import User, Wiki
        from app.git_sync import list_files_in_repo

        owner_name, slug = wiki_ref.split("/", 1)
        owner = User.query.filter_by(username=owner_name).first()
        wiki = Wiki.query.filter_by(owner_id=owner.id, slug=slug).first() if owner else None
        if not wiki:
            raise click.ClickException(f"unknown wiki {wiki_ref}")

        repo_files = {path for path in list_files_in_repo(owner_name, slug) if path.endswith(".md")}
        db_files = {page.path for page in wiki.pages}
        missing_in_db = sorted(repo_files - db_files)
        missing_in_repo = sorted(db_files - repo_files)
        if not missing_in_db and not missing_in_repo:
            click.echo("ok")
            return
        if missing_in_db:
            click.echo("missing in db:")
            for path in missing_in_db:
                click.echo(f"  {path}")
        if missing_in_repo:
            click.echo("missing in repo:")
            for path in missing_in_repo:
                click.echo(f"  {path}")
        raise SystemExit(1)

    from flask import jsonify, request as req, render_template

    @app.errorhandler(404)
    def not_found(e):
        if req.path.startswith("/api/"):
            return jsonify({"error": "not_found", "message": "The requested resource was not found"}), 404
        return render_template("error.html", code=404, title="Page not found",
                               message="The page you're looking for doesn't exist or has been moved."), 404

    @app.errorhandler(403)
    def forbidden(e):
        if req.path.startswith("/api/"):
            return jsonify({"error": "forbidden", "message": "You don't have permission to access this resource"}), 403
        return render_template("error.html", code=403, title="Access denied",
                               message="You don't have permission to access this page."), 403

    @app.errorhandler(500)
    def internal_error(e):
        if req.path.startswith("/api/"):
            return jsonify({"error": "internal_error", "message": "Something went wrong"}), 500
        return render_template("error.html", code=500, title="Something went wrong",
                               message="An unexpected error occurred. Try again later."), 500

    @app.errorhandler(413)
    def too_large(e):
        if req.path.startswith("/api/"):
            return jsonify({"error": "too_large", "message": "Request too large (50MB max)"}), 413
        from flask import flash, redirect
        flash("Upload too large (50MB max)")
        return redirect(req.referrer or "/"), 413

    @app.errorhandler(429)
    def rate_limited(e):
        if req.path.startswith("/api/"):
            return jsonify({"error": "rate_limited", "message": "Too many requests"}), 429
        return render_template("error.html", code=429, title="Too many requests",
                               message="You're sending requests too fast. Try again in a moment."), 429

    return app
