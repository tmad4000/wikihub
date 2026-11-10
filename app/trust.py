"""Account trust for spam defenses (wikihub-9ras).

An account is *trusted* when it is not suspended AND any of:
  * an admin set ``users.trusted`` (the owner's accounts are set by migration);
  * it holds a per-user ``wiki_limit`` grant (WikiHub's existing privilege marker);
  * it is a system user (``@wikihub``);
  * it is at least TRUST_MIN_ACCOUNT_AGE_DAYS old (default 14) AND has a
    verified identity: verified email, linked Google account, or a linked
    Ideaflow ID (external identity).

Content owned by an untrusted account still works normally by direct link,
but:
  * external links render with rel="nofollow ugc noopener noreferrer";
  * pages send <meta name="robots" content="noindex"> and X-Robots-Tag: noindex;
  * it is left out of /explore, /activity (+RSS), /people, /llms.txt,
    /llms-full.txt, the landing page's featured wikis and GET /api/v1/wikis.
    (WikiHub serves no sitemap.xml yet; one must use trusted_user_clause()
    and app.curation.not_curated_against() when added.)

An active negative curation judgement on the account (app/curation.py,
wikihub-f3b7) withdraws trust, so every trust check above also applies to
accounts curated against.

TRUST_GATE_ENABLED=false turns the discovery/noindex gate off (the test
harness does this so ordinary tests can use brand-new accounts). Suspension
and negative curation always apply regardless of the flag.
"""

from datetime import datetime, timedelta, timezone

from flask import current_app
from sqlalchemy import and_, exists, false, or_, true

from app.models import ExternalIdentity, User

SYSTEM_TRUSTED_USERNAMES = ("wikihub",)


def gate_enabled():
    return bool(current_app.config.get("TRUST_GATE_ENABLED", True))


def _min_age():
    try:
        days = int(current_app.config.get("TRUST_MIN_ACCOUNT_AGE_DAYS", 14))
    except (TypeError, ValueError):
        days = 14
    return timedelta(days=days)


def is_trusted(user):
    if user is None or getattr(user, "suspended_at", None) is not None:
        return False
    from app.curation import is_account_negative
    if is_account_negative(user.id):
        return False
    if not gate_enabled():
        return True
    if user.trusted or user.wiki_limit is not None or user.username in SYSTEM_TRUSTED_USERNAMES:
        return True
    created = user.created_at
    if created is None:
        return False
    if created.tzinfo is None:
        created = created.replace(tzinfo=timezone.utc)
    if datetime.now(timezone.utc) - created < _min_age():
        return False
    if user.email_verified_at is not None or user.google_id:
        return True
    return ExternalIdentity.query.filter_by(user_id=user.id).first() is not None


def trusted_user_clause():
    """SQL filter on User matching exactly is_trusted()."""
    from app.curation import not_curated_against
    not_suspended = and_(User.suspended_at.is_(None), not_curated_against(user_id_col=User.id))
    if not gate_enabled():
        return not_suspended
    cutoff = datetime.now(timezone.utc) - _min_age()
    verified = or_(
        User.email_verified_at.isnot(None),
        User.google_id.isnot(None),
        exists().where(ExternalIdentity.user_id == User.id),
    )
    return and_(
        not_suspended,
        or_(
            User.trusted.is_(True),
            User.wiki_limit.isnot(None),
            User.username.in_(SYSTEM_TRUSTED_USERNAMES),
            and_(User.created_at <= cutoff, verified),
        ),
    )
