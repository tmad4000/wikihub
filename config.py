import os


class Config:
    IDEAFLOW_CONNECTOR_SECRET = os.environ.get("IDEAFLOW_CONNECTOR_SECRET", "")
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
    # Ideaflow ID OIDC relying party (wikihub-39pe). WikiHub is an independent
    # confidential client of the Ideaflow ID authority — see
    # ~/memory/research/global-identity-architecture-2026-09-16.md. Provider
    # (client id/secret) registration is owned by another team; this app only
    # ever reads it from the environment. IDEAFLOW_OIDC_ENABLED is the kill
    # switch: the button and routes vanish (404) unless it's true AND both
    # client id/secret are set — see app.routes.auth.ideaflow_oidc_enabled().
    IDEAFLOW_OIDC_ENABLED = os.environ.get("IDEAFLOW_OIDC_ENABLED", "").lower() in ("1", "true", "yes")
    IDEAFLOW_OIDC_ISSUER = os.environ.get("IDEAFLOW_OIDC_ISSUER", "https://id.ideaflow.app/api/auth")
    IDEAFLOW_OIDC_DISCOVERY_URL = os.environ.get(
        "IDEAFLOW_OIDC_DISCOVERY_URL",
        f"{IDEAFLOW_OIDC_ISSUER}/.well-known/openid-configuration",
    )
    # Automatic cross-app sign-in (code-xbh.21.6): a signed-out browser that
    # opens a page makes one silent prompt=none round trip to Ideaflow ID and
    # comes back signed in if it has a provider session. Only active while
    # Ideaflow sign-in itself is enabled. Kill switch: IDEAFLOW_AUTO_SIGNIN=false.
    IDEAFLOW_AUTO_SIGNIN = os.environ.get("IDEAFLOW_AUTO_SIGNIN", "true").lower() in ("1", "true", "yes")
    IDEAFLOW_OIDC_CLIENT_ID = os.environ.get("IDEAFLOW_OIDC_CLIENT_ID")
    IDEAFLOW_OIDC_CLIENT_SECRET = os.environ.get("IDEAFLOW_OIDC_CLIENT_SECRET")
    # In-app "Send feedback" (WIT widget): the backend exchanges the signed-in
    # user's Ideaflow ID token here for a 15-minute create-issue-only token.
    WIT_FEEDBACK_TOKEN_URL = os.environ.get(
        "WIT_FEEDBACK_TOKEN_URL", "https://api.worldissuetracker.com/functions/v1/feedback-token"
    )
    SERVER_NAME = os.environ.get("SERVER_NAME")  # e.g. wikihub.md
    BASE_URL = os.environ.get("BASE_URL", "http://localhost:5000")
    CUSTOM_DOMAIN_TARGET = os.environ.get("CUSTOM_DOMAIN_TARGET", "domains.wikihub.md")
    ADMIN_TOKEN = os.environ.get("ADMIN_TOKEN", "")
    # Token written into each repo's hooks/wikihub.conf for the post-receive
    # sync callbacks. Keep it distinct from ADMIN_TOKEN; when unset, hooks
    # fall back to ADMIN_TOKEN for back-compat.
    HOOK_TOKEN = os.environ.get("HOOK_TOKEN", "")
    # Curator agent (inline AI sidebar on wiki reader pages). wikihub-2jn.2
    # Default ON so the feature is live as soon as the endpoint is deployed.
    # Set CURATOR_ENABLED=false to hide the panel and return 503 from /api/v1/agent/chat.
    CURATOR_ENABLED = os.environ.get("CURATOR_ENABLED", "true").lower() in ("1", "true", "yes")
    # wikihub-u9rc: PostHog analytics. Empty key = snippet skipped (dev default).
    POSTHOG_KEY = os.environ.get("POSTHOG_KEY", "")
    POSTHOG_HOST = os.environ.get("POSTHOG_HOST", "https://us.i.posthog.com")
    MAX_CONTENT_LENGTH = 50 * 1024 * 1024  # 50MB max request size
    MAX_PAGE_SIZE = 2 * 1024 * 1024  # 2MB per page
    MAX_UPLOAD_FILES = 5000  # max files in a single upload/zip
    # Default cap on wikis per user (wikihub-20ct). Per-user overrides live in
    # User.wiki_limit and win over this — see User.effective_wiki_limit().
    # NOTE (abuse surface): account creation is unauthenticated and
    # agent-native (POST /api/v1/accounts / wikihub_register_agent both mint a
    # usable key with no email/verification gate), and there is currently NO
    # distinction between self-registered agent accounts and human accounts.
    # So this default applies to every account, including throwaway agents. If
    # a lower cap for un-verified/agent accounts is ever needed, gate it here
    # rather than raising this number further.
    MAX_WIKIS_PER_USER = int(os.environ.get("MAX_WIKIS_PER_USER", "500"))
    WRITE_RATE_LIMIT_AUTHENTICATED_PER_MINUTE = int(os.environ.get("WRITE_RATE_LIMIT_AUTHENTICATED_PER_MINUTE", "180"))
    WRITE_RATE_LIMIT_AUTHENTICATED_IP_PER_MINUTE = int(os.environ.get("WRITE_RATE_LIMIT_AUTHENTICATED_IP_PER_MINUTE", "360"))
    WRITE_RATE_LIMIT_ANONYMOUS_IP_PER_MINUTE = int(os.environ.get("WRITE_RATE_LIMIT_ANONYMOUS_IP_PER_MINUTE", "10"))
    WRITE_RATE_LIMITS_IN_TESTS = os.environ.get("WRITE_RATE_LIMITS_IN_TESTS", "").lower() in ("1", "true", "yes")
    # Spam defenses (wikihub-9ras / wikihub-1ug3). See app/trust.py and app/rate_limit.py.
    TRUST_GATE_ENABLED = os.environ.get("TRUST_GATE_ENABLED", "true").lower() in ("1", "true", "yes")
    TRUST_MIN_ACCOUNT_AGE_DAYS = int(os.environ.get("TRUST_MIN_ACCOUNT_AGE_DAYS", "14"))
    # Version of the curation policy (docs/curation.md) recorded on each judgement (wikihub-f3b7).
    CURATION_POLICY_VERSION = os.environ.get("CURATION_POLICY_VERSION", "2026-11-09")
    SIGNUP_API_MAX_PER_IP_PER_HOUR = int(os.environ.get("SIGNUP_API_MAX_PER_IP_PER_HOUR", "5"))
    SIGNUP_API_MAX_PER_IP_PER_DAY = int(os.environ.get("SIGNUP_API_MAX_PER_IP_PER_DAY", "20"))
    ANON_UPLOAD_MAX_PER_IP_PER_HOUR = int(os.environ.get("ANON_UPLOAD_MAX_PER_IP_PER_HOUR", "10"))
    USER_SEARCH_MAX_PER_MINUTE = int(os.environ.get("USER_SEARCH_MAX_PER_MINUTE", "30"))
    SHARED_RATE_LIMITS_IN_TESTS = os.environ.get("SHARED_RATE_LIMITS_IN_TESTS", "").lower() in ("1", "true", "yes")
    ABUSE_LINK_DENYLIST = [d.strip() for d in os.environ.get("ABUSE_LINK_DENYLIST", "vccbusiness.com,ramerlabs.com").split(",") if d.strip()]
    MAGIC_LOGIN_TTL_SECONDS = int(os.environ.get("MAGIC_LOGIN_TTL_SECONDS", "900"))
    TESTING_LOGIN = os.environ.get("TESTING_LOGIN", "").lower() in ("1", "true", "yes")
    SESSION_COOKIE_SECURE = os.environ.get("SESSION_COOKIE_SECURE", "1").lower() in ("1", "true", "yes")
    SESSION_COOKIE_SAMESITE = "Lax"
    SESSION_COOKIE_HTTPONLY = True
    # Scope session cookie to the whole wikihub.md zone so users stay logged in
    # when moving between apex and user/wiki subdomains. Leave unset locally
    # (Flask defaults to current host) unless SESSION_COOKIE_DOMAIN is provided.
    SESSION_COOKIE_DOMAIN = os.environ.get("SESSION_COOKIE_DOMAIN") or None
