import os


class Config:
    SECRET_KEY = os.environ.get("SECRET_KEY", "change-me")
    SQLALCHEMY_DATABASE_URI = os.environ["DATABASE_URL"].replace("postgres://", "postgresql://", 1) if "DATABASE_URL" in os.environ else "postgresql://localhost/wikihub"
    SQLALCHEMY_TRACK_MODIFICATIONS = False
    # pool_pre_ping tests each connection before use; eliminates the
    # "SSL SYSCALL error: EOF detected" 500s when Postgres drops idle conns.
    # Needed because SubdomainMiddleware hits the DB on every request.
    SQLALCHEMY_ENGINE_OPTIONS = {"pool_pre_ping": True, "pool_recycle": 300}
    TEMPLATES_AUTO_RELOAD = True
    REPOS_DIR = os.environ.get("REPOS_DIR", os.path.join(os.path.dirname(__file__), "repos"))
    GOOGLE_CLIENT_ID = os.environ.get("GOOGLE_CLIENT_ID")
    GOOGLE_CLIENT_SECRET = os.environ.get("GOOGLE_CLIENT_SECRET")
    SERVER_NAME = os.environ.get("SERVER_NAME")  # e.g. wikihub.md
    BASE_URL = os.environ.get("BASE_URL", "http://localhost:5000")
    ADMIN_TOKEN = os.environ.get("ADMIN_TOKEN", "")
    # wikihub-2jn.2: default-off flag for the Curator agent. DB override via
    # admin_settings.curator_enabled wins when present; env is the fallback.
    CURATOR_ENABLED = os.environ.get("CURATOR_ENABLED", "").lower() in ("1", "true", "yes")
    # wikihub-u9rc: PostHog analytics. Empty key = snippet skipped (dev default).
    POSTHOG_KEY = os.environ.get("POSTHOG_KEY", "")
    POSTHOG_HOST = os.environ.get("POSTHOG_HOST", "https://us.i.posthog.com")
    MAX_CONTENT_LENGTH = 50 * 1024 * 1024  # 50MB max request size
    MAX_PAGE_SIZE = 2 * 1024 * 1024  # 2MB per page
    MAX_UPLOAD_FILES = 5000  # max files in a single upload/zip
    MAX_WIKIS_PER_USER = 50
    MAGIC_LOGIN_TTL_SECONDS = int(os.environ.get("MAGIC_LOGIN_TTL_SECONDS", "900"))
    TESTING_LOGIN = os.environ.get("TESTING_LOGIN", "").lower() in ("1", "true", "yes")
    SESSION_COOKIE_SECURE = os.environ.get("SESSION_COOKIE_SECURE", "1").lower() in ("1", "true", "yes")
    SESSION_COOKIE_SAMESITE = "Lax"
    SESSION_COOKIE_HTTPONLY = True
    # Scope session cookie to the whole wikihub.md zone so users stay logged in
    # when moving between apex and user/wiki subdomains. Leave unset locally
    # (Flask defaults to current host) unless SESSION_COOKIE_DOMAIN is provided.
    SESSION_COOKIE_DOMAIN = os.environ.get("SESSION_COOKIE_DOMAIN") or None
