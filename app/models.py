from datetime import datetime, timezone
from flask_login import UserMixin
from sqlalchemy.dialects.postgresql import TSVECTOR
from app import db


def utcnow():
    return datetime.now(timezone.utc)


class User(UserMixin, db.Model):
    __tablename__ = "users"

    id = db.Column(db.Integer, primary_key=True)
    username = db.Column(db.String(64), unique=True, nullable=False, index=True)
    display_name = db.Column(db.String(128))
    # Uniqueness enforced only for verified emails (partial index below), so
    # two users may claim the same unverified email — first to verify wins.
    email = db.Column(db.String(256), nullable=True)
    email_verified_at = db.Column(db.DateTime(timezone=True), nullable=True)
    password_hash = db.Column(db.String(256), nullable=True)  # null for oauth-only users
    google_id = db.Column(db.String(256), unique=True, nullable=True)
    llm_api_key_encrypted = db.Column(db.Text, nullable=True)  # encrypted Anthropic API key for Curator
    created_at = db.Column(db.DateTime(timezone=True), default=utcnow, nullable=False)

    wikis = db.relationship("Wiki", backref="owner", lazy="dynamic")
    api_keys = db.relationship("ApiKey", backref="user", lazy="dynamic")
    stars = db.relationship("Star", backref="user", lazy="dynamic")

    __table_args__ = (
        db.Index(
            "ux_users_email_verified",
            "email",
            unique=True,
            postgresql_where=db.text("email_verified_at IS NOT NULL"),
        ),
    )


class Wiki(db.Model):
    __tablename__ = "wikis"

    id = db.Column(db.Integer, primary_key=True)
    owner_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=False)
    slug = db.Column(db.String(128), nullable=False)
    title = db.Column(db.String(256))
    description = db.Column(db.Text)
    subdomain = db.Column(db.String(63), unique=True, nullable=True)
    forked_from_id = db.Column(db.Integer, db.ForeignKey("wikis.id"), nullable=True)
    star_count = db.Column(db.Integer, default=0, nullable=False)
    fork_count = db.Column(db.Integer, default=0, nullable=False)
    created_at = db.Column(db.DateTime(timezone=True), default=utcnow, nullable=False)
    updated_at = db.Column(db.DateTime(timezone=True), default=utcnow, onupdate=utcnow, nullable=False)

    pages = db.relationship("Page", backref="wiki", lazy="dynamic", cascade="all, delete-orphan")
    forked_from = db.relationship("Wiki", remote_side=[id], backref="forks")

    __table_args__ = (
        db.UniqueConstraint("owner_id", "slug", name="uq_wiki_owner_slug"),
    )


class Page(db.Model):
    """derived metadata index for markdown pages. content lives in git."""
    __tablename__ = "pages"

    id = db.Column(db.Integer, primary_key=True)
    wiki_id = db.Column(db.Integer, db.ForeignKey("wikis.id"), nullable=False)
    path = db.Column(db.Text, nullable=False)  # e.g. "wiki/agents.md"
    title = db.Column(db.String(512))
    visibility = db.Column(db.String(32), default="private", nullable=False)
    frontmatter_json = db.Column(db.JSON)
    excerpt = db.Column(db.String(200))  # ~200 chars for search results
    content_hash = db.Column(db.String(64))
    author = db.Column(db.String(256), nullable=True)  # original author (preserved for audit even when anonymous)
    # anonymous posting (wikihub-7b2r) — hides author in API/UI when anonymous=True,
    # claimable (only meaningful when anonymous) lets any authed user claim first-come-first-served.
    anonymous = db.Column(db.Boolean, default=False, nullable=False)
    claimable = db.Column(db.Boolean, default=False, nullable=False)
    created_at = db.Column(db.DateTime(timezone=True), default=utcnow, nullable=False)
    updated_at = db.Column(db.DateTime(timezone=True), default=utcnow, onupdate=utcnow, nullable=False)

    # tsvector column for full-text search — populated at index time from content
    search_vector = db.Column(TSVECTOR)

    __table_args__ = (
        db.UniqueConstraint("wiki_id", "path", name="uq_page_wiki_path"),
        db.Index("ix_page_visibility", "visibility"),
        db.Index("ix_page_search", "search_vector", postgresql_using="gin"),
    )


class Wikilink(db.Model):
    """tracks [[wikilink]] references between pages for graph/resolution"""
    __tablename__ = "wikilinks"

    id = db.Column(db.Integer, primary_key=True)
    source_page_id = db.Column(db.Integer, db.ForeignKey("pages.id", ondelete="CASCADE"), nullable=False)
    target_path = db.Column(db.Text, nullable=False)  # raw link target before resolution
    target_page_id = db.Column(db.Integer, db.ForeignKey("pages.id", ondelete="SET NULL"), nullable=True)

    source_page = db.relationship("Page", foreign_keys=[source_page_id])
    target_page = db.relationship("Page", foreign_keys=[target_page_id])


class Star(db.Model):
    __tablename__ = "stars"

    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=False)
    wiki_id = db.Column(db.Integer, db.ForeignKey("wikis.id"), nullable=False)
    created_at = db.Column(db.DateTime(timezone=True), default=utcnow, nullable=False)

    wiki = db.relationship("Wiki", backref="stars_rel")

    __table_args__ = (
        db.UniqueConstraint("user_id", "wiki_id", name="uq_star_user_wiki"),
    )


class Fork(db.Model):
    """explicit fork record (supplements wiki.forked_from_id for querying)"""
    __tablename__ = "forks"

    id = db.Column(db.Integer, primary_key=True)
    source_wiki_id = db.Column(db.Integer, db.ForeignKey("wikis.id"), nullable=False)
    forked_wiki_id = db.Column(db.Integer, db.ForeignKey("wikis.id"), nullable=False)
    user_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=False)
    created_at = db.Column(db.DateTime(timezone=True), default=utcnow, nullable=False)

    __table_args__ = (
        db.UniqueConstraint("source_wiki_id", "user_id", name="uq_fork_source_user"),
    )


class PendingInvite(db.Model):
    """a share grant addressed to an email that has no account yet.
    materialized into an ACL grant when a user signs up and verifies that email."""
    __tablename__ = "pending_invites"

    id = db.Column(db.Integer, primary_key=True)
    wiki_id = db.Column(db.Integer, db.ForeignKey("wikis.id", ondelete="CASCADE"), nullable=False)
    pattern = db.Column(db.String(512), nullable=False)  # e.g. "*" or "research/*"
    email = db.Column(db.String(256), nullable=False, index=True)
    role = db.Column(db.String(16), nullable=False)  # "read" | "edit"
    # Random per-invite token embedded in the invite-email URL as ?it=.
    # Valid token at signup/login = proof the user received our email = one-click
    # verify. Nullable so pre-token rows (from before wikihub-yjsv) keep working
    # via the fallback verify-email flow.
    token = db.Column(db.String(64), nullable=True, index=True)
    invited_by_id = db.Column(db.Integer, db.ForeignKey("users.id", ondelete="SET NULL"), nullable=True)
    created_at = db.Column(db.DateTime(timezone=True), default=utcnow, nullable=False)

    wiki = db.relationship("Wiki")
    invited_by = db.relationship("User", foreign_keys=[invited_by_id])

    __table_args__ = (
        db.UniqueConstraint("wiki_id", "pattern", "email", name="uq_pending_invite"),
    )


class ApiKey(db.Model):
    __tablename__ = "api_keys"

    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=False)
    key_hash = db.Column(db.String(256), nullable=False, unique=True)
    key_prefix = db.Column(db.String(16), nullable=False)  # "wh_" + first 8 chars of token
    label = db.Column(db.String(128))
    last_used_at = db.Column(db.DateTime(timezone=True))
    agent_name = db.Column(db.String(256))  # from X-Agent-Name header
    agent_version = db.Column(db.String(64))  # from X-Agent-Version header
    created_at = db.Column(db.DateTime(timezone=True), default=utcnow, nullable=False)


class MagicLoginToken(db.Model):
    __tablename__ = "magic_login_tokens"

    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=False, index=True)
    token_hash = db.Column(db.String(256), nullable=False, unique=True, index=True)
    redirect_path = db.Column(db.String(512), nullable=False, default="/")
    expires_at = db.Column(db.DateTime(timezone=True), nullable=False)
    used_at = db.Column(db.DateTime(timezone=True), nullable=True)
    created_at = db.Column(db.DateTime(timezone=True), default=utcnow, nullable=False)

    user = db.relationship("User")


class EmailVerificationToken(db.Model):
    """one-time token sent to a user's email at signup (or when they add/change
    email). consuming the token sets users.email_verified_at."""
    __tablename__ = "email_verification_tokens"

    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True)
    token_hash = db.Column(db.String(256), nullable=False, unique=True)
    new_email = db.Column(db.String(256), nullable=False)
    expires_at = db.Column(db.DateTime(timezone=True), nullable=False)
    used_at = db.Column(db.DateTime(timezone=True), nullable=True)
    created_at = db.Column(db.DateTime(timezone=True), default=utcnow, nullable=False)

    user = db.relationship("User")


class UsernameRedirect(db.Model):
    __tablename__ = "username_redirects"

    id = db.Column(db.Integer, primary_key=True)
    old_username = db.Column(db.String(64), unique=True, nullable=False, index=True)
    user_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=False)
    expires_at = db.Column(db.DateTime(timezone=True), nullable=False)
    created_at = db.Column(db.DateTime(timezone=True), default=utcnow, nullable=False)

    user = db.relationship("User")


class WikiSlugRedirect(db.Model):
    __tablename__ = "wiki_slug_redirects"

    id = db.Column(db.Integer, primary_key=True)
    owner_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=False)
    old_slug = db.Column(db.String(128), nullable=False, index=True)
    wiki_id = db.Column(db.Integer, db.ForeignKey("wikis.id"), nullable=False)
    created_at = db.Column(db.DateTime(timezone=True), default=utcnow, nullable=False)

    __table_args__ = (
        db.UniqueConstraint("owner_id", "old_slug", name="uq_slug_redirect_owner_slug"),
    )

    wiki = db.relationship("Wiki")
