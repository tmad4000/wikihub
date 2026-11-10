"""Curation export for the Noos backbone (wikihub-f3b7, plan D3/D4).

GET /api/v1/curation[?since=ISO8601][&cursor=...][&limit=N]

Lists curation judgements (active and revoked) ordered by when they last
changed, so a mirror (Noos `curation` annotations) can poll incrementally:
pass the previous response's `next_cursor`, or a `since` timestamp. WikiHub
never calls Noos; Noos pulls this.

Field names follow plan D4: polarity, reason, scope, curator, at, note.
Public callers never see moderator notes or curator accounts, and judgements
whose target is not publicly visible (private wiki, unlisted or private page)
are left out so the export cannot reveal their names. The admin token
(Authorization: Bearer ADMIN_TOKEN) sees everything.
"""

from datetime import datetime, timezone

from flask import current_app, jsonify, request
from sqlalchemy import and_, func, or_

from app.auth_utils import check_admin_token
from app.curation import REASON_LABELS
from app.discovery import DISCOVERABLE_VISIBILITIES, discoverable_wiki_ids
from app.models import CurationJudgement, Page, User, Wiki, utcnow
from app.routes import api_bp
from app.url_utils import url_path_from_page_path

_DEFAULT_LIMIT = 500
_MAX_LIMIT = 1000


def _parse_ts(value):
    value = (value or "").strip()
    if not value:
        return None
    try:
        ts = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return False
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=timezone.utc)
    return ts


def _iso(ts):
    return ts.isoformat() if ts else None


def _cursor_ts(ts):
    # UTC with a Z suffix: a "+00:00" offset turns into a space when a client
    # forgets to URL-encode the cursor.
    return ts.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def _base_url():
    base = (current_app.config.get("BASE_URL") or "").rstrip("/")
    return base or request.host_url.rstrip("/")


@api_bp.route("/curation", methods=["GET"])
def curation_export():
    is_admin = bool(request.headers.get("Authorization")) and check_admin_token(request, log=False)

    try:
        limit = int(request.args.get("limit", _DEFAULT_LIMIT))
    except (TypeError, ValueError):
        limit = _DEFAULT_LIMIT
    limit = max(1, min(limit, _MAX_LIMIT))

    changed_at = func.coalesce(CurationJudgement.revoked_at, CurationJudgement.created_at)
    query = CurationJudgement.query

    cursor = (request.args.get("cursor") or "").strip()
    since = _parse_ts(request.args.get("since"))
    if since is False:
        return jsonify({"error": "bad_request", "message": "since must be an ISO 8601 timestamp"}), 400
    if cursor:
        ts_raw, _, id_raw = cursor.rpartition("|")
        ts = _parse_ts(ts_raw)
        if not ts or not id_raw.isdigit():
            return jsonify({"error": "bad_request", "message": "invalid cursor"}), 400
        query = query.filter(or_(changed_at > ts, and_(changed_at == ts, CurationJudgement.id > int(id_raw))))
    elif since:
        query = query.filter(changed_at >= since)

    rows = query.order_by(changed_at.asc(), CurationJudgement.id.asc()).limit(limit + 1).all()
    has_more = len(rows) > limit
    rows = rows[:limit]

    users = {u.id: u for u in User.query.filter(User.id.in_(
        {r.scope_id for r in rows if r.scope_type == "account"} | {r.curator_user_id for r in rows if r.curator_user_id}
    )).all()} if rows else {}
    wiki_ids = {r.scope_id for r in rows if r.scope_type == "wiki"}
    pages = {p.id: p for p in Page.query.filter(Page.id.in_({r.scope_id for r in rows if r.scope_type == "page"})).all()} if rows else {}
    wiki_ids |= {p.wiki_id for p in pages.values()}
    wikis = {w.id: w for w in Wiki.query.filter(Wiki.id.in_(wiki_ids)).all()} if wiki_ids else {}
    public_wiki_ids = discoverable_wiki_ids(trusted_only=False) if rows else set()
    account_ids = [r.scope_id for r in rows if r.scope_type == "account"]
    account_wikis = {}
    if account_ids:
        for w in Wiki.query.filter(Wiki.owner_id.in_(account_ids)).order_by(Wiki.slug.asc()).all():
            if is_admin or w.id in public_wiki_ids:
                account_wikis.setdefault(w.owner_id, []).append(w)

    base = _base_url()
    out = []
    for r in rows:
        item = {
            "id": r.id,
            "polarity": r.polarity,
            "reason": r.reason,
            "label": REASON_LABELS.get(r.reason, r.reason),
            "scope": {"type": r.scope_type},
            "url": None,
            "wiki_urls": [],
            "curator": "wikihub",
            "at": _iso(r.created_at),
            "revoked_at": _iso(r.revoked_at),
            "active": r.revoked_at is None,
        }
        if r.scope_type == "account":
            user = users.get(r.scope_id)
            if user is None:
                item["scope"]["missing"] = True
            else:
                item["scope"]["account"] = user.username
                item["url"] = f"{base}/@{user.username}"
                item["wiki_urls"] = [f"{base}/@{user.username}/{w.slug}" for w in account_wikis.get(user.id, [])]
        elif r.scope_type == "wiki":
            wiki = wikis.get(r.scope_id)
            if wiki is None:
                item["scope"]["missing"] = True
            else:
                if not is_admin and wiki.id not in public_wiki_ids:
                    continue
                item["scope"].update(account=wiki.owner.username, wiki=wiki.slug)
                item["url"] = f"{base}/@{wiki.owner.username}/{wiki.slug}"
                item["wiki_urls"] = [item["url"]]
        else:
            page = pages.get(r.scope_id)
            wiki = wikis.get(page.wiki_id) if page else None
            if page is None or wiki is None:
                item["scope"]["missing"] = True
            else:
                if not is_admin and page.visibility not in DISCOVERABLE_VISIBILITIES:
                    continue
                item["scope"].update(account=wiki.owner.username, wiki=wiki.slug, path=page.path)
                wiki_url = f"{base}/@{wiki.owner.username}/{wiki.slug}"
                item["url"] = f"{wiki_url}/{url_path_from_page_path(page.path, strip_md=True)}"
                item["wiki_urls"] = [wiki_url]
        if is_admin:
            item["note"] = r.note
            curator = users.get(r.curator_user_id)
            item["curator_user"] = curator.username if curator else None
        out.append(item)

    next_cursor = None
    if has_more and rows:
        last = rows[-1]
        next_cursor = f"{_cursor_ts(last.revoked_at or last.created_at)}|{last.id}"
    return jsonify({
        "judgements": out,
        "next_cursor": next_cursor,
        "has_more": has_more,
        "generated_at": _iso(utcnow()),
        "docs": f"{base}/curation",
    })
