import logging
import os
import re

import click

from flask import Flask
from flask_sqlalchemy import SQLAlchemy
from flask_login import LoginManager
from flask_wtf.csrf import CSRFProtect
from flask_cors import CORS
from werkzeug.middleware.proxy_fix import ProxyFix

db = SQLAlchemy()
login_manager = LoginManager()
csrf = CSRFProtect()

# Redact credentials passed via GET query string (/auth/login?api_key=...)
# from werkzeug's access log. gunicorn/nginx need their own redaction in prod.
_SECRET_IN_URL = re.compile(r'(api_key|password)=[^&"\'\s]+')


class _RedactQueryParams(logging.Filter):
    def filter(self, record):
        if isinstance(record.msg, str):
            record.msg = _SECRET_IN_URL.sub(r'\1=REDACTED', record.msg)
        if record.args:
            record.args = tuple(
                _SECRET_IN_URL.sub(r'\1=REDACTED', a) if isinstance(a, str) else a
                for a in record.args
            )
        return True


logging.getLogger("werkzeug").addFilter(_RedactQueryParams())


def create_app(config_class="config.Config"):
    app = Flask(__name__)
    app.config.from_object(config_class)
    # keep em dashes, arrows, ellipses, etc. as literal UTF-8 in JSON responses
    # instead of \uXXXX escapes (flask defaults to ensure_ascii=True). wikihub-u6by
    app.json.ensure_ascii = False
    app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1, x_host=1)

    from app.subdomain_middleware import SubdomainMiddleware
    app.wsgi_app = SubdomainMiddleware(app.wsgi_app, app)

    db.init_app(app)
    login_manager.init_app(app)
    login_manager.login_view = "auth.login"
    # wikihub-m8zi: iOS Safari (ITP, Private Browsing, in-app browsers) strips
    # the Referer header on form POSTs. Flask-WTF's default WTF_CSRF_SSL_STRICT=True
    # rejects HTTPS POSTs without a same-origin Referer, breaking the API-key
    # login form on iPad with an unactionable 400 "The referrer header is missing".
    # Disable the Referer check; CSRF defense is now token + SameSite=Lax cookies
    # (already configured in config.py). Matches GitHub/GitLab.
    app.config.setdefault("WTF_CSRF_SSL_STRICT", False)
    csrf.init_app(app)

    # CORS for API endpoints (wikihub-gzj). Read endpoints are fully open;
    # write endpoints require Authorization bearer tokens (not cookies), so
    # CSRF is not a concern for cross-origin requests.
    CORS(
        app,
        resources={r"/api/*": {"origins": "*"}},
        methods=["GET", "POST", "PUT", "DELETE", "PATCH", "HEAD", "OPTIONS"],
        allow_headers=["Authorization", "Content-Type", "X-Request-ID"],
        expose_headers=["X-Request-ID", "X-RateLimit-Remaining", "X-RateLimit-Reset"],
        supports_credentials=False,
        max_age=600,
        # Do not intercept exceptions — let Flask's normal error handlers run
        # (and let the session teardown happen) before adding CORS headers.
        # intercept_exceptions=True (the default) monkey-patches
        # handle_exception/handle_user_exception and can interfere with
        # SQLAlchemy session state when errors occur.
        intercept_exceptions=False,
    )

    from app.models import User

    @login_manager.user_loader
    def load_user(user_id):
        user = db.session.get(User, int(user_id))
        if user is not None and user.suspended_at is not None:
            return None  # suspended accounts lose existing sessions too
        return user

    from app.routes import main_bp, auth_bp, api_bp, wiki_bp
    app.register_blueprint(main_bp)
    app.register_blueprint(auth_bp)
    app.register_blueprint(api_bp, url_prefix="/api/v1")
    app.register_blueprint(wiki_bp)

    from app.routes.agent_chat import agent_chat_bp
    app.register_blueprint(agent_chat_bp, url_prefix="/api/v1")

    from app.routes.api_root import api_root_bp
    app.register_blueprint(api_root_bp)

    from app.git_backend import git_bp
    app.register_blueprint(git_bp)

    from app.connector import connector_bp
    app.register_blueprint(connector_bp)
    csrf.exempt(connector_bp)

    csrf.exempt(api_bp)
    csrf.exempt(agent_chat_bp)
    csrf.exempt(api_root_bp)
    csrf.exempt(git_bp)

    from app.routes.auth import init_oauth
    init_oauth(app)

    from app import moderation
    app.before_request(moderation.before_request)
    app.after_request(moderation.after_request)

    from app.canonical_redirect import maybe_redirect
    app.before_request(maybe_redirect)

    def _mark_html_cacheability(response):
        """wikihub-miq4: only anonymous, successful HTML may be kept offline.

        The service worker stores a navigation response ONLY when it carries
        `X-WikiHub-Offline-Cache: public` and no `private`/`no-store`
        Cache-Control. Signed-in (session or Bearer) responses can contain
        private pages and titles, so they are marked `private` and never get
        the opt-in header; a shared device must not replay them offline.
        """
        from flask import request as _req
        from flask_login import current_user as _cu

        existing = response.headers.get("Cache-Control", "")
        authenticated = bool(getattr(_cu, "is_authenticated", False)) or bool(_req.headers.get("Authorization"))
        if authenticated:
            if "private" not in existing and "no-store" not in existing:
                response.headers["Cache-Control"] = (existing + ", private") if existing else "private"
            response.headers.pop("X-WikiHub-Offline-Cache", None)
            return
        if response.status_code == 200 and "private" not in existing and "no-store" not in existing:
            response.headers["X-WikiHub-Offline-Cache"] = "public"

    @app.after_request
    def _disable_proxy_html_transforms(response):
        """Cloudflare's HTML transformations (Email Obfuscation, legacy Auto
        Minify) strip <br> tags from rendered markdown — losing every soft
        line break for users writing one-line-per-thought (wikihub-eiv7).
        Cache-Control: no-transform is the HTTP-standard signal to proxies
        not to mutate the body. We append rather than replace existing
        Cache-Control values so per-route directives still apply."""
        ct = response.headers.get("Content-Type", "")
        if not ct.startswith("text/html"):
            return response
        _mark_html_cacheability(response)
        existing = response.headers.get("Cache-Control", "")
        if "no-transform" in existing:
            return response
        if existing:
            response.headers["Cache-Control"] = existing + ", no-transform"
        else:
            response.headers["Cache-Control"] = "no-transform"
        return response

    from app.url_utils import url_path_from_page_path

    @app.template_filter("page_url")
    def page_url_filter(value):
        return url_path_from_page_path(value, strip_md=True)

    _version_cache = []

    def _app_version():
        # Short git SHA of the running checkout (production deploys are a git
        # pull), computed once per process; attached to in-app feedback.
        if not _version_cache:
            version = os.environ.get("WIKIHUB_VERSION", "")
            if not version:
                try:
                    import subprocess
                    version = subprocess.run(
                        ["git", "-C", os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                         "rev-parse", "--short", "HEAD"],
                        capture_output=True, text=True, timeout=5,
                    ).stdout.strip()
                except Exception:
                    version = ""
            _version_cache.append(version or "dev")
        return _version_cache[0]

    from app.renderer import page_display_title
    app.jinja_env.globals["page_display_title"] = page_display_title

    @app.context_processor
    def inject_link_preview():
        # Absolute og:url / og:image for link previews. On user/wiki hosts the
        # subdomain middleware rewrote PATH_INFO; the shared URL is the
        # pre-rewrite path on the same host.
        from flask import has_request_context, request as _request, url_for as _url_for
        if not has_request_context():
            return {}
        path = _request.environ.get("wikihub.rewritten_from") or _request.path
        return {
            "og_url": _request.host_url.rstrip("/") + path,
            "og_image": _url_for("static", filename="icons/icon-512.png", _external=True),
        }

    @app.context_processor
    def inject_feature_flags():
        # Expose feature flags to all templates (e.g. reader.html curator panel). wikihub-2jn.2
        from app.routes.auth import ideaflow_oidc_enabled
        return {
            "curator_enabled": bool(app.config.get("CURATOR_ENABLED", False)),
            "ideaflow_login_enabled": ideaflow_oidc_enabled(app.config),
            "app_version": _app_version(),
        }

    @app.context_processor
    def inject_site_host():
        # wikihub-m0pr: on a user/wiki/custom host the top bar shows that
        # site's identity (left, links to "/" = the site's home) and a small
        # [[wikihub]] mark to the platform apex. None on the apex itself.
        from flask import has_request_context, request as _request
        if not has_request_context():
            return {"site_host": None}
        kind = _request.environ.get("wikihub.host_kind")
        name = _request.environ.get("wikihub.host_name")
        if not kind or not name:
            return {"site_host": None}
        cache = _request.environ.get("wikihub.site_host")
        if cache is None:
            from app.subdomains import site_for_host
            try:
                cache = site_for_host(kind, name) or {}
            except Exception:
                cache = {}
            if cache:
                from app.subdomains import CANONICAL_SUFFIX, LOCAL_SUFFIX
                host = (_request.host or "").split(":")[0].lower()
                if host.endswith(CANONICAL_SUFFIX):
                    platform_url = "https://" + CANONICAL_SUFFIX.lstrip(".") + "/"
                elif host.endswith(LOCAL_SUFFIX):
                    platform_url = _request.scheme + "://" + LOCAL_SUFFIX.lstrip(".") + "/"
                else:
                    platform_url = "https://" + CANONICAL_SUFFIX.lstrip(".") + "/"
                cache = dict(cache, host=host, platform_url=platform_url)
                if cache.get("wiki_id"):
                    # The top bar shows the wiki's title on every page of its
                    # host, error pages included. A viewer who cannot read the
                    # wiki sees the hostname they typed instead (plan 3d).
                    from app.auth_utils import get_current_user_from_request
                    from app.discovery import viewer_can_read_wiki
                    from app.models import Wiki as _Wiki
                    wiki_row = db.session.get(_Wiki, cache["wiki_id"])
                    if wiki_row is None or not viewer_can_read_wiki(wiki_row, get_current_user_from_request()):
                        cache = dict(cache, title=host)
            _request.environ["wikihub.site_host"] = cache
        return {"site_host": cache or None}

    os.makedirs(app.config["REPOS_DIR"], exist_ok=True)

    with app.app_context():
        db.session.execute(db.text("CREATE EXTENSION IF NOT EXISTS pg_trgm"))
        db.session.commit()
        db.create_all()
        # idempotent in-place migrations (SQLAlchemy create_all does not alter existing tables)
        db.session.execute(db.text(
            "ALTER TABLE wikis ADD COLUMN IF NOT EXISTS subdomain VARCHAR(63) UNIQUE"
        ))
        db.session.execute(db.text(
            "ALTER TABLE wikilinks ADD COLUMN IF NOT EXISTS in_private_band BOOLEAN NOT NULL DEFAULT FALSE"
        ))
        db.session.commit()
        # trust & safety columns (migrations/2026-11-03_spam_defenses.sql).
        # Only ALTER when a column is actually missing: ALTER TABLE takes an
        # ACCESS EXCLUSIVE lock even when IF NOT EXISTS makes it a no-op, and
        # gunicorn workers boot while siblings are already serving requests
        # (a booting worker holding wikis while waiting on users can deadlock
        # a reader that holds users and wants wikis). Commit per statement.
        existing = {
            name for (name,) in db.session.execute(db.text(
                "SELECT column_name FROM information_schema.columns "
                "WHERE table_schema = current_schema() AND table_name = 'users'"
            ))
        }
        db.session.commit()
        for column, ddl in (
            ("trusted", "ALTER TABLE users ADD COLUMN IF NOT EXISTS trusted BOOLEAN NOT NULL DEFAULT false"),
            ("suspended_at", "ALTER TABLE users ADD COLUMN IF NOT EXISTS suspended_at TIMESTAMPTZ NULL"),
            ("suspended_reason", "ALTER TABLE users ADD COLUMN IF NOT EXISTS suspended_reason TEXT NULL"),
        ):
            if column not in existing:
                db.session.execute(db.text(ddl))
                db.session.commit()
        from app.wiki_ops import ensure_official_wiki
        ensure_official_wiki()
        db.session.commit()

    @app.cli.group("wikihub")
    def wikihub_cli():
        """wikihub maintenance commands."""

    @wikihub_cli.command("reindex")
    @click.argument("wiki_ref", required=False)
    @click.option("--all", "all_wikis", is_flag=True, default=False)
    @click.option(
        "--no-reset", "no_reset", is_flag=True, default=False,
        help="Update Page rows in place (keeps page ids, stars, proposals) instead of deleting and recreating them.",
    )
    def reindex_command(wiki_ref=None, all_wikis=False, no_reset=False):
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
            index_repo_pages(wiki.owner.username, wiki.slug, wiki, reset=not no_reset)
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

    @wikihub_cli.command("activate-custom-domain")
    @click.argument("hostname")
    @click.option("--tls-status", default="active", type=click.Choice(["pending", "active", "error"]))
    def activate_custom_domain_command(hostname, tls_status):
        """Mark an ownership-verified domain ready after the TLS/DNS cutover."""
        from app.custom_domains import normalize_custom_hostname
        from app.git_sync import append_event_to_repo
        from app.models import CustomDomain, utcnow

        try:
            normalized = normalize_custom_hostname(hostname)
        except ValueError as exc:
            raise click.ClickException(str(exc)) from exc
        domain = CustomDomain.query.filter_by(hostname=normalized).first()
        if not domain:
            raise click.ClickException(f"unknown custom domain: {normalized}")
        if not domain.verified_at:
            raise click.ClickException("domain ownership must be verified before activation")
        if tls_status != "active":
            raise click.ClickException("TLS must be active before custom-domain activation")
        replaced = [
            existing.hostname
            for existing in CustomDomain.query.filter(
                CustomDomain.wiki_id == domain.wiki_id,
                CustomDomain.id != domain.id,
                CustomDomain.status == "active",
            ).all()
        ]
        if replaced:
            CustomDomain.query.filter(
                CustomDomain.wiki_id == domain.wiki_id,
                CustomDomain.id != domain.id,
                CustomDomain.status == "active",
            ).update({"status": "verified"}, synchronize_session=False)
            db.session.flush()
        domain.status = "active"
        domain.tls_status = tls_status
        domain.updated_at = utcnow()
        append_event_to_repo(
            domain.wiki.owner.username,
            domain.wiki.slug,
            "custom_domain.activate",
            hostname=normalized,
            tls_status=tls_status,
            replaced_hostnames=replaced,
            actor="wikihub-cli",
        )
        db.session.commit()
        click.echo(f"active: {normalized} (tls={tls_status})")

    @wikihub_cli.command("rebuild-mirrors")
    @click.option("--all", "all_wikis", is_flag=True, default=False)
    @click.argument("wiki_ref", required=False)
    def rebuild_mirrors_command(all_wikis=False, wiki_ref=None):
        """full rebuild of public mirrors (safety net for incremental updates)."""
        from app.models import User, Wiki
        from app.git_sync import regenerate_public_mirror
        from app.wiki_ops import load_acl_rules

        if all_wikis:
            wikis = Wiki.query.all()
        elif wiki_ref and "/" in wiki_ref:
            owner_name, slug = wiki_ref.split("/", 1)
            owner = User.query.filter_by(username=owner_name).first()
            wikis = [Wiki.query.filter_by(owner_id=owner.id, slug=slug).first()] if owner else []
        else:
            raise click.ClickException("pass owner/slug or use --all")

        for wiki in filter(None, wikis):
            acl_rules = load_acl_rules(wiki.owner.username, wiki.slug)
            regenerate_public_mirror(wiki.owner.username, wiki.slug, acl_rules)
            click.echo(f"rebuilt mirror {wiki.owner.username}/{wiki.slug}")

    from app.moderation_cli import register_curation_commands, register_moderation_commands
    register_moderation_commands(wikihub_cli)
    register_curation_commands(wikihub_cli)

    from flask import jsonify, request as req, render_template

    @app.errorhandler(404)
    def not_found(e):
        if req.path.startswith("/api/"):
            return jsonify({"error": "not_found", "message": "The requested resource was not found"}), 404
        # wikihub-3rjt: agents requesting .md / Accept: text/markdown for a
        # missing or private page must NOT receive an HTML body — that would
        # parse as the page's markdown content. Return JSON 4xx with a
        # WWW-Authenticate hint when unauthenticated.
        accept_hdr = req.headers.get("Accept", "")
        wants_md = "text/markdown" in accept_hdr
        is_md_url = req.path.endswith(".md")
        if wants_md or (is_md_url and "text/html" not in accept_hdr):
            from flask_login import current_user as _cu
            status = 401 if not _cu.is_authenticated else 404
            body = {
                "error": "authentication_required" if status == 401 else "not_found",
                "message": "Page is private, does not exist, or you lack access",
                "sign_in_url": "https://wikihub.md/auth/login",
            }
            resp = jsonify(body)
            resp.status_code = status
            resp.headers["Cache-Control"] = "no-store"
            if status == 401:
                resp.headers["WWW-Authenticate"] = 'Bearer realm="wikihub"'
            return resp
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
        # wikihub-bug-fix: generate a short correlation id so users can quote
        # it in bug reports. Logged server-side; surfaced in the 500 page so
        # future incidents like the proposals-grant 500 are debuggable without
        # an SSH+log-tail dance.
        import uuid as _uuid
        ref = _uuid.uuid4().hex[:8]
        try:
            app.logger.error("internal_error ref=%s path=%s", ref, req.path)
        except Exception:
            pass
        if req.path.startswith("/api/"):
            return jsonify({"error": "internal_error", "message": "Something went wrong", "reference": ref}), 500
        return render_template(
            "error.html",
            code=500,
            title="Something went wrong",
            message="An unexpected error occurred on our end.",
            reference=ref,
            retry_url=req.full_path if req.query_string else req.path,
        ), 500

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
