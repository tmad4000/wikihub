"""WSGI middleware: rewrite requests arriving on a user or wiki subdomain
into the canonical /@user/slug URL form before Flask routes see them.

jacobcole.wikihub.md/recipes/pasta  -> PATH_INFO=/@jacobcole/recipes/pasta
recipes.wikihub.md/pasta            -> PATH_INFO=/@owner/recipes/pasta   (where owner owns the "recipes" subdomain)

Global routes (api, auth, static, agent surfaces, etc.) pass through unchanged
so users can log in, hit APIs, etc. from any subdomain.

The resolved ("host_kind", name) tuple is stashed on request.environ so the
main app knows when it's being accessed via a subdomain (used later for
choosing canonical URLs in templates).
"""

from typing import Callable, Iterable

from app.subdomains import resolve_host

# prefixes that never get rewritten, even when Host is a user/wiki subdomain.
# these are always routed globally (login, api, agent surfaces, static assets).
_GLOBAL_PREFIXES = (
    "/api/",
    "/auth/",
    "/login",
    "/logout",
    "/register",
    "/signup",
    "/settings",
    "/static/",
    "/search",
    "/explore",
    "/people",
    "/shared",
    "/roadmap",
    "/claim-email",
    "/delete-account",
    "/new",
    "/mcp",
    "/install.sh",
    "/.well-known/",
    "/llms.txt",
    "/llms-full.txt",
    "/agents",
    "/AGENTS.md",
    "/healthz",
    "/favicon.ico",
    "/robots.txt",
    "/sitemap.xml",
    "/upload",
)


def _should_rewrite(path: str) -> bool:
    if not path.startswith("/"):
        return False
    # /@<user>/... paths are already canonical — never rewrite, regardless of host.
    # (this lets internal url_for() links keep working on subdomain hosts.)
    if path.startswith("/@"):
        return False
    for prefix in _GLOBAL_PREFIXES:
        p = prefix.rstrip("/")
        if path == p or path.startswith(p + "/"):
            return False
    return True


class SubdomainMiddleware:
    def __init__(self, wsgi_app, flask_app):
        self.wsgi_app = wsgi_app
        self.flask_app = flask_app

    def __call__(self, environ, start_response):
        host = environ.get("HTTP_HOST", "")
        try:
            with self.flask_app.app_context():
                resolved = resolve_host(host)
                if resolved is not None:
                    environ["wikihub.host_kind"] = resolved[0]
                    environ["wikihub.host_name"] = resolved[1]
                    path = environ.get("PATH_INFO", "/")
                    if _should_rewrite(path):
                        prefix = self._prefix_for(resolved)
                        if prefix:
                            # keep SCRIPT_NAME empty; rewrite PATH_INFO in place
                            new_path = prefix + path if path != "/" else prefix
                            environ["PATH_INFO"] = new_path
                            environ["wikihub.rewritten_from"] = path
        except Exception:
            # DB hiccup, stale connection, etc — don't 500 the request.
            # Fall through to non-subdomain routing; the apex app still works.
            import logging
            logging.getLogger(__name__).exception("subdomain middleware failed for host=%s", host)

        return self.wsgi_app(environ, start_response)

    def _prefix_for(self, resolved) -> str:
        kind, name = resolved
        if kind == "user":
            return f"/@{name}"
        if kind == "wiki":
            # need to look up owner to build /@owner/slug prefix
            from app.models import Wiki, User
            wiki = Wiki.query.filter(Wiki.subdomain == name).first()
            if not wiki:
                return ""
            owner = User.query.get(wiki.owner_id)
            if not owner:
                return ""
            return f"/@{owner.username}/{wiki.slug}"
        return ""
