"""Admin moderation commands (wikihub-i8lo).

    flask --app wsgi.py wikihub suspend USERNAME [--reason TEXT] [--purge] [--execute]
    flask --app wsgi.py wikihub unsuspend USERNAME [--execute]
    flask --app wsgi.py wikihub trust USERNAME [--revoke] [--execute]
    flask --app wsgi.py wikihub abuse-report [--days N] [--json]

Every mutating command is a DRY RUN unless --execute is passed: it prints
exactly what it would change and touches nothing.

suspend: sets users.suspended_at/suspended_reason. The account can no longer
authenticate (session, API key, git basic auth) and every URL it owns answers
410; it drops out of search and discovery.
--purge additionally deletes every wiki the user owns (DB rows, stars/forks
on them, bare repo + public mirror on disk), revokes all API keys and magic
login tokens. The user row is kept (suspended) so the username stays taken.
"""

import json
import os
import re
import shutil
from collections import Counter, defaultdict
from datetime import timedelta
from urllib.parse import urlparse

import click

_URL_RE = re.compile(r"https?://[^\s)\]>\"'<]+", re.IGNORECASE)

DEFAULT_LINK_DENYLIST = ("vccbusiness.com", "ramerlabs.com")


def _host_matches(host, domain):
    return host == domain or host.endswith("." + domain)


def _get_user(username):
    from app.models import User
    user = User.query.filter_by(username=username.strip().lstrip("@").lower()).first()
    if not user:
        raise click.ClickException(f"unknown user: {username}")
    return user


def purge_user_content(user):
    """Delete all wikis + keys of `user`. Caller commits."""
    from flask import current_app
    from app import db
    from app.models import ApiKey, Fork, MagicLoginToken, Star, Wiki
    from app.wiki_ops import delete_wiki_repos

    wikis = Wiki.query.filter_by(owner_id=user.id).all()
    wiki_ids = [w.id for w in wikis]
    if wiki_ids:
        Star.query.filter(Star.wiki_id.in_(wiki_ids)).delete(synchronize_session=False)
        Fork.query.filter(Fork.source_wiki_id.in_(wiki_ids)).delete(synchronize_session=False)
        Fork.query.filter(Fork.forked_wiki_id.in_(wiki_ids)).delete(synchronize_session=False)
        Wiki.query.filter(Wiki.forked_from_id.in_(wiki_ids)).update(
            {"forked_from_id": None}, synchronize_session=False)
    keys = ApiKey.query.filter_by(user_id=user.id).delete(synchronize_session=False)
    MagicLoginToken.query.filter_by(user_id=user.id).delete(synchronize_session=False)
    slugs = [w.slug for w in wikis]
    for wiki in wikis:
        db.session.delete(wiki)
    db.session.flush()
    for slug in slugs:
        delete_wiki_repos(user.username, slug)
    user_dir = os.path.join(current_app.config["REPOS_DIR"], user.username)
    if os.path.isdir(user_dir):
        shutil.rmtree(user_dir)
    return {"wikis": slugs, "api_keys_revoked": keys}


def build_abuse_report(days=30, velocity_threshold=20, same_domain_threshold=20, denylist=None):
    """Rows for accounts that look like spam. Untrusted accounts only."""
    from datetime import datetime, timezone
    from app.git_sync import read_file_from_repo
    from app.models import Page, User, Wiki
    from app.page_utils import is_content_page_path
    from app.trust import is_trusted

    denylist = tuple(d.lower() for d in (denylist or DEFAULT_LINK_DENYLIST))
    since = datetime.now(timezone.utc) - timedelta(days=days)
    rows = []
    recent = (
        Page.query.join(Wiki, Page.wiki_id == Wiki.id)
        .join(User, Wiki.owner_id == User.id)
        .filter(Page.updated_at >= since, User.suspended_at.is_(None))
        .with_entities(User.id, Wiki.slug, Page.path, Page.created_at)
        .all()
    )
    by_user = defaultdict(list)
    for user_id, slug, path, created_at in recent:
        if is_content_page_path(path) and path.endswith(".md"):
            by_user[user_id].append((slug, path, created_at))

    for user_id, pages in by_user.items():
        user = User.query.get(user_id)
        if user is None or is_trusted(user):
            continue
        created_recent = sum(1 for _s, _p, c in pages if c and c >= since)
        domains = Counter()
        denied = Counter()
        for slug, path, _c in pages:
            content = read_file_from_repo(user.username, slug, path) or ""
            for url in _URL_RE.findall(content):
                host = (urlparse(url).hostname or "").lower()
                if not host or host.endswith("wikihub.md"):
                    continue
                domains[host] += 1
                for bad in denylist:
                    if _host_matches(host, bad):
                        denied[bad] += 1
        reasons = []
        if created_recent >= velocity_threshold:
            reasons.append(f"velocity:{created_recent} pages/{days}d")
        top = domains.most_common(1)
        if top and top[0][1] >= same_domain_threshold:
            reasons.append(f"same_domain:{top[0][0]}x{top[0][1]}")
        if denied:
            reasons.append("denylist:" + ",".join(f"{d}x{n}" for d, n in denied.most_common()))
        if reasons:
            rows.append({
                "username": user.username,
                "created_at": user.created_at.isoformat() if user.created_at else None,
                "pages_recent": len(pages),
                "pages_created_recent": created_recent,
                "top_domains": domains.most_common(3),
                "reasons": reasons,
            })
    rows.sort(key=lambda r: (-len(r["reasons"]), -r["pages_recent"]))
    return rows


def register_moderation_commands(group):
    @group.command("suspend")
    @click.argument("username")
    @click.option("--reason", default="", help="Moderation note stored on the account.")
    @click.option("--purge", is_flag=True, default=False, help="Also delete all wikis and revoke API keys.")
    @click.option("--execute", is_flag=True, default=False, help="Apply changes (default is a dry run).")
    def suspend_command(username, reason, purge, execute):
        """Suspend an account (dry run unless --execute)."""
        from app import db
        from app.auth_utils import security_event
        from app.models import ApiKey, Page, Wiki, utcnow

        user = _get_user(username)
        wikis = Wiki.query.filter_by(owner_id=user.id).all()
        page_count = Page.query.filter(Page.wiki_id.in_([w.id for w in wikis])).count() if wikis else 0
        keys = ApiKey.query.filter_by(user_id=user.id).count()
        mode = "EXECUTE" if execute else "DRY RUN"
        click.echo(f"[{mode}] suspend @{user.username} (id={user.id})"
                   + (" (already suspended)" if user.suspended_at else ""))
        click.echo(f"  wikis={len(wikis)} pages={page_count} api_keys={keys}")
        if purge:
            for w in wikis:
                click.echo(f"  purge wiki @{user.username}/{w.slug}")
            click.echo(f"  revoke {keys} api key(s)")
        if not execute:
            click.echo("  (no changes made; pass --execute to apply)")
            return
        if user.suspended_at is None:
            user.suspended_at = utcnow()
        if reason:
            user.suspended_reason = reason
        result = purge_user_content(user) if purge else None
        db.session.commit()
        security_event("admin.suspend", "success", user_id=user.id, purge=bool(purge),
                       wikis_deleted=len(result["wikis"]) if result else 0)
        click.echo(f"  suspended @{user.username}" + (f"; purged {len(result['wikis'])} wiki(s)" if result else ""))

    @group.command("unsuspend")
    @click.argument("username")
    @click.option("--execute", is_flag=True, default=False)
    def unsuspend_command(username, execute):
        """Lift a suspension (dry run unless --execute). Purged content is not restored."""
        from app import db
        user = _get_user(username)
        click.echo(f"[{'EXECUTE' if execute else 'DRY RUN'}] unsuspend @{user.username}")
        if execute:
            user.suspended_at = None
            user.suspended_reason = None
            db.session.commit()

    @group.command("trust")
    @click.argument("username")
    @click.option("--revoke", is_flag=True, default=False)
    @click.option("--execute", is_flag=True, default=False)
    def trust_command(username, revoke, execute):
        """Set or clear the admin `trusted` flag (dry run unless --execute)."""
        from app import db
        user = _get_user(username)
        click.echo(f"[{'EXECUTE' if execute else 'DRY RUN'}] {'untrust' if revoke else 'trust'} @{user.username}")
        if execute:
            user.trusted = not revoke
            db.session.commit()

    @group.command("abuse-report")
    @click.option("--days", default=30, show_default=True, help="Look at pages updated in this window.")
    @click.option("--velocity", default=20, show_default=True, help="Flag accounts creating >= N pages in the window.")
    @click.option("--same-domain", default=20, show_default=True, help="Flag >= N outbound links to one domain.")
    @click.option("--deny", multiple=True, help="Extra denylisted domains (repeatable).")
    @click.option("--json", "as_json", is_flag=True, default=False)
    def abuse_report_command(days, velocity, same_domain, deny, as_json):
        """List untrusted accounts that look like spam (read-only)."""
        from flask import current_app
        denylist = list(current_app.config.get("ABUSE_LINK_DENYLIST") or DEFAULT_LINK_DENYLIST) + list(deny)
        rows = build_abuse_report(days, velocity, same_domain, denylist)
        if as_json:
            click.echo(json.dumps(rows, indent=2))
            return
        if not rows:
            click.echo("no flagged accounts")
            return
        for r in rows:
            click.echo(f"@{r['username']}  pages={r['pages_recent']}  created={r['created_at']}  "
                       + "  ".join(r["reasons"]))
