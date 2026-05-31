"""
/api root discovery endpoint.

Lives outside the /api/v1 prefix so agents hitting /api (naturally, to
discover versioning) get a tiny JSON map pointing at the current version
and its capability surfaces. No auth required; safe to cache.

Also serves wikihub-uonp compatibility routes for /api/wikis/<owner>/<slug>
so agents that drop the /v1 segment get a useful 401/200 instead of 404.
"""

from flask import Blueprint, jsonify, request

api_root_bp = Blueprint("api_root", __name__)


def _discovery_payload():
    return {
        "name": "wikihub",
        "current_version": "v1",
        "versions": {
            "v1": {
                "base": "/api/v1",
                "openapi": "/api/v1/openapi.json",
                "capabilities": "/api/v1/me/capabilities",
                "docs": "/docs/api",
            },
        },
        "deprecated_versions": [],
        "feedback": "/api/v1/feedback",
        "request_id_header": "X-Request-ID",
    }


@api_root_bp.route("/api", methods=["GET", "HEAD"])
@api_root_bp.route("/api/", methods=["GET", "HEAD"])
def api_discovery():
    resp = jsonify(_discovery_payload())
    resp.headers["Cache-Control"] = "public, max-age=300"
    return resp


def _unauthorized_json(path):
    """wikihub-uonp: standardized 401 response with WWW-Authenticate and
    machine-readable sign_in_url hint, per ticket acceptance criteria.
    """
    from flask import make_response
    body = {
        "error": "authentication_required",
        "message": "Authentication required to access this resource",
        "sign_in_url": "https://wikihub.md/auth/login",
        "hint": "Send Bearer token in Authorization header. See /api for version discovery.",
    }
    resp = make_response(jsonify(body), 401)
    resp.headers["WWW-Authenticate"] = 'Bearer realm="wikihub"'
    resp.headers["Cache-Control"] = "no-store"
    return resp


def _not_found_json(path, hint="The requested resource was not found"):
    """wikihub-uonp: 404 JSON with discovery hint pointing at /api version map."""
    body = {
        "error": "not_found",
        "message": hint,
        "discovery": "/api",
        "current_version_base": "/api/v1",
    }
    return jsonify(body), 404


@api_root_bp.route("/api/wikis/<owner>/<slug>", methods=["GET"])
def api_wikis_compat(owner, slug):
    """wikihub-uonp: compatibility shim for /api/wikis/<owner>/<slug> (without
    /v1). Returns:
      - 200 + wiki metadata for the owner or a user with read access
      - 200 if any path in the wiki is publicly readable (anon discovery)
      - 401 + WWW-Authenticate: Bearer for an unauthenticated request to
        a wiki where no path is public
      - 404 if user/wiki does not exist (ambiguous to avoid leaking existence
        of private wikis on a real account; but 401 wins over 404 for
        agents needing the auth hint)
    """
    from app.models import User, Wiki
    from app.auth_utils import get_current_user_from_request
    from app.acl import resolve_visibility
    from app.wiki_ops import load_acl_rules, sync_wiki_counters

    owner_user = User.query.filter_by(username=owner).first()
    user = get_current_user_from_request()
    if not owner_user:
        if not user:
            # ambiguous response: don't leak that this owner doesn't exist
            return _unauthorized_json(request.path)
        return _not_found_json(request.path, "User not found")

    wiki = Wiki.query.filter_by(owner_id=owner_user.id, slug=slug).first()
    if not wiki:
        if not user:
            return _unauthorized_json(request.path)
        return _not_found_json(request.path, "Wiki not found")

    # ACL — does this wiki have any public-readable content?
    try:
        rules = load_acl_rules(owner_user.username, wiki.slug)
    except Exception:
        rules = []
    root_vis = resolve_visibility("", rules)
    is_owner = bool(user and user.id == owner_user.id)
    is_anon_readable = root_vis in ("public", "public-edit", "unlisted", "unlisted-edit")

    if not is_owner and not is_anon_readable and not user:
        return _unauthorized_json(request.path)

    sync_wiki_counters(wiki)
    return jsonify({
        "id": wiki.id,
        "owner": owner_user.username,
        "slug": wiki.slug,
        "title": wiki.title,
        "description": wiki.description,
        "subdomain": wiki.subdomain,
        "star_count": wiki.star_count,
        "fork_count": wiki.fork_count,
        "page_count": wiki.pages.count(),
        "created_at": wiki.created_at.isoformat(),
        "updated_at": wiki.updated_at.isoformat(),
        "canonical_api": f"/api/v1/wikis/{owner_user.username}/{wiki.slug}",
        "web_url": f"/@{owner_user.username}/{wiki.slug}",
    })


@api_root_bp.route("/api/wikis/<owner>/<slug>/pages/<path:page_path>", methods=["GET"])
def api_wiki_page_compat(owner, slug, page_path):
    """wikihub-uonp: /api/wikis/<owner>/<slug>/pages/<path> — returns 401 with
    Bearer hint when unauth'd to a private page, else hands off to canonical."""
    from app.models import User, Wiki, Page
    from app.auth_utils import get_current_user_from_request
    from app.acl import can_read
    from app.wiki_ops import load_acl_rules
    from app.url_utils import page_path_from_url_path

    owner_user = User.query.filter_by(username=owner).first()
    user = get_current_user_from_request()
    if not owner_user:
        return _unauthorized_json(request.path) if not user else _not_found_json(request.path, "User not found")
    wiki = Wiki.query.filter_by(owner_id=owner_user.id, slug=slug).first()
    if not wiki:
        return _unauthorized_json(request.path) if not user else _not_found_json(request.path, "Wiki not found")

    # wikihub-vbug: use same lookup as wiki render — try raw + .md before
    # falling back to underscore->space, so paths whose filename actually
    # contains an underscore resolve correctly.
    candidates = [page_path]
    if not page_path.endswith(".md"):
        candidates.append(page_path + ".md")
    if "_" in page_path:
        space = page_path_from_url_path(page_path)
        candidates.append(space)
        if not space.endswith(".md"):
            candidates.append(space + ".md")
    page = None
    for cand in candidates:
        page = Page.query.filter_by(wiki_id=wiki.id, path=cand).first()
        if page:
            break

    try:
        rules = load_acl_rules(owner_user.username, wiki.slug)
    except Exception:
        rules = []
    username = user.username if user else None
    acl_lookup_path = page.path if page else page_path_from_url_path(page_path)
    if not can_read(acl_lookup_path, rules, user=username):
        if not user:
            return _unauthorized_json(request.path)
        return _not_found_json(request.path, "Page not found or not accessible")

    if not page:
        return _not_found_json(request.path, "Page not found")
    return jsonify({
        "path": page.path,
        "title": page.title,
        "visibility": page.visibility,
        "excerpt": page.excerpt,
        "canonical_api": f"/api/v1/wikis/{owner_user.username}/{wiki.slug}/pages/{page.path}",
    })
