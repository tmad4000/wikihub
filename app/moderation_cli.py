"""Admin moderation commands (wikihub-i8lo).

    flask --app wsgi.py wikihub suspend USERNAME [--reason TEXT] [--purge] [--execute]
    flask --app wsgi.py wikihub unsuspend USERNAME [--execute]
    flask --app wsgi.py wikihub trust USERNAME [--revoke] [--execute]
    flask --app wsgi.py wikihub abuse-report [--days N] [--json]
    flask --app wsgi.py wikihub flag TARGET --reason REASON [--polarity P] [--note TEXT] [--curator USER] [--execute]
    flask --app wsgi.py wikihub unflag TARGET [--reason REASON] [--execute]
    flask --app wsgi.py wikihub curation-list [--all] [--json]

TARGET is `username` / `@username` (account), `@username/wiki` (wiki) or
`@username/wiki/path/to/page` (page; `.md` optional).

Every mutating command is a DRY RUN unless --execute is passed: it prints
exactly what it would change and touches nothing.

suspend: sets users.suspended_at/suspended_reason. The account can no longer
authenticate (session, API key, git basic auth) and every URL it owns answers
410; it drops out of search and discovery.

flag / unflag: record or revoke an editorial curation judgement
(app/curation.py, docs/curation.md). A negative judgement keeps content
readable but shows a "Curated against" banner, noindexes it, marks its links
nofollow ugc, removes it from discovery and sorts it last in search. A
positive one (featured, official, editor_pick) adds a wiki to Explore's
editorial picks. Nothing is ever deleted; unflag sets revoked_at.
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


def resolve_curation_target(target):
    """TARGET -> (scope_type, scope_id, label). Raises ClickException."""
    from app.models import Page, User, Wiki

    raw = (target or "").strip()
    if not raw:
        raise click.ClickException("empty target")
    parts = raw.lstrip("@").strip("/").split("/", 2)
    username = parts[0].lower()
    user = User.query.filter_by(username=username).first()
    if not user:
        raise click.ClickException(f"unknown user: {username}")
    if len(parts) == 1:
        return "account", user.id, f"@{user.username}"
    wiki = Wiki.query.filter_by(owner_id=user.id, slug=parts[1]).first()
    if not wiki:
        raise click.ClickException(f"unknown wiki: @{user.username}/{parts[1]}")
    if len(parts) == 2 or not parts[2]:
        return "wiki", wiki.id, f"@{user.username}/{wiki.slug}"
    path = parts[2].strip("/")
    candidates = [path] if path.endswith(".md") else [path + ".md", path, path + "/index.md"]
    candidates += [c.replace("_", " ") for c in candidates]
    for candidate in candidates:
        page = Page.query.filter_by(wiki_id=wiki.id, path=candidate).first()
        if page:
            return "page", page.id, f"@{user.username}/{wiki.slug}/{page.path}"
    raise click.ClickException(f"unknown page: @{user.username}/{wiki.slug}/{path}")


def _active_judgements(scope_type, scope_id):
    from app.models import CurationJudgement
    return (CurationJudgement.query
            .filter_by(scope_type=scope_type, scope_id=scope_id)
            .filter(CurationJudgement.revoked_at.is_(None))
            .order_by(CurationJudgement.created_at.asc()).all())


def register_curation_commands(group):
    from app.curation import NEGATIVE_REASONS, POSITIVE_REASONS, normalize_reason, polarity_for_reason

    all_reasons = ", ".join(NEGATIVE_REASONS + POSITIVE_REASONS)

    @group.command("flag")
    @click.argument("target")
    @click.option("--reason", required=True, help=f"One of: {all_reasons} (hyphens accepted).")
    @click.option("--polarity", type=click.Choice(["negative", "positive"]), default=None,
                  help="Optional; must match the reason.")
    @click.option("--note", default="", help="Moderator note (never shown publicly).")
    @click.option("--curator", default="wikihub", show_default=True,
                  help="Username recorded as curator (null if no such user).")
    @click.option("--execute", is_flag=True, default=False, help="Apply changes (default is a dry run).")
    def flag_command(target, reason, polarity, note, curator, execute):
        """Record a curation judgement (dry run unless --execute). Never deletes content."""
        from app import db
        from app.auth_utils import security_event
        from app.models import CurationJudgement, User

        reason = normalize_reason(reason)
        expected = polarity_for_reason(reason)
        if expected is None:
            raise click.ClickException(f"unknown reason {reason!r}; use one of: {all_reasons}")
        if polarity and polarity != expected:
            raise click.ClickException(f"reason {reason!r} is {expected}, not {polarity}")
        scope_type, scope_id, label = resolve_curation_target(target)
        mode = "EXECUTE" if execute else "DRY RUN"
        existing = [j for j in _active_judgements(scope_type, scope_id)
                    if j.polarity == expected and j.reason == reason]
        click.echo(f"[{mode}] flag {label} ({scope_type} id={scope_id}) {expected}:{reason}"
                   + (" (already active; no change)" if existing else ""))
        if note:
            click.echo(f"  note: {note}")
        others = [j for j in _active_judgements(scope_type, scope_id) if j not in existing]
        for j in others:
            click.echo(f"  also active: #{j.id} {j.polarity}:{j.reason}")
        if not execute:
            click.echo("  (no changes made; pass --execute to apply)")
            return
        if existing:
            return
        curator_user = User.query.filter_by(username=(curator or "").lstrip("@").lower()).first() if curator else None
        row = CurationJudgement(scope_type=scope_type, scope_id=scope_id, polarity=expected, reason=reason,
                                curator_user_id=curator_user.id if curator_user else None, note=note or None)
        db.session.add(row)
        db.session.commit()
        security_event("admin.curation.flag", "success", judgement_id=row.id, scope_type=scope_type,
                       scope_id=scope_id, polarity=expected, reason=reason)
        click.echo(f"  recorded judgement #{row.id}")

    @group.command("unflag")
    @click.argument("target")
    @click.option("--reason", default=None, help="Revoke only this reason (default: every active judgement).")
    @click.option("--polarity", type=click.Choice(["negative", "positive"]), default=None)
    @click.option("--execute", is_flag=True, default=False, help="Apply changes (default is a dry run).")
    def unflag_command(target, reason, polarity, execute):
        """Revoke curation judgements (dry run unless --execute). Rows are kept as history."""
        from app import db
        from app.auth_utils import security_event
        from app.models import utcnow

        scope_type, scope_id, label = resolve_curation_target(target)
        reason = normalize_reason(reason) if reason else None
        matches = [j for j in _active_judgements(scope_type, scope_id)
                   if (reason is None or j.reason == reason) and (polarity is None or j.polarity == polarity)]
        mode = "EXECUTE" if execute else "DRY RUN"
        click.echo(f"[{mode}] unflag {label} ({scope_type} id={scope_id})")
        if not matches:
            click.echo("  no matching active judgement")
            return
        for j in matches:
            click.echo(f"  revoke #{j.id} {j.polarity}:{j.reason}")
        if not execute:
            click.echo("  (no changes made; pass --execute to apply)")
            return
        now = utcnow()
        for j in matches:
            j.revoked_at = now
        db.session.commit()
        for j in matches:
            security_event("admin.curation.unflag", "success", judgement_id=j.id, scope_type=scope_type,
                           scope_id=scope_id, polarity=j.polarity, reason=j.reason)

    @group.command("curation-list")
    @click.option("--all", "include_revoked", is_flag=True, default=False, help="Include revoked judgements.")
    @click.option("--json", "as_json", is_flag=True, default=False)
    def curation_list_command(include_revoked, as_json):
        """List curation judgements (read-only)."""
        from app.models import CurationJudgement, Page, User, Wiki

        query = CurationJudgement.query
        if not include_revoked:
            query = query.filter(CurationJudgement.revoked_at.is_(None))
        rows = []
        for j in query.order_by(CurationJudgement.created_at.asc()).all():
            if j.scope_type == "account":
                u = User.query.get(j.scope_id)
                target = f"@{u.username}" if u else f"account#{j.scope_id}"
            elif j.scope_type == "wiki":
                w = Wiki.query.get(j.scope_id)
                target = f"@{w.owner.username}/{w.slug}" if w else f"wiki#{j.scope_id}"
            else:
                p = Page.query.get(j.scope_id)
                target = f"@{p.wiki.owner.username}/{p.wiki.slug}/{p.path}" if p else f"page#{j.scope_id}"
            rows.append({"id": j.id, "target": target, "scope": j.scope_type, "polarity": j.polarity,
                         "reason": j.reason, "note": j.note,
                         "created_at": j.created_at.isoformat() if j.created_at else None,
                         "revoked_at": j.revoked_at.isoformat() if j.revoked_at else None})
        if as_json:
            click.echo(json.dumps(rows, indent=2))
            return
        if not rows:
            click.echo("no curation judgements")
            return
        for r in rows:
            click.echo(f"#{r['id']}  {r['target']}  {r['polarity']}:{r['reason']}"
                       + (f"  revoked {r['revoked_at']}" if r["revoked_at"] else ""))
