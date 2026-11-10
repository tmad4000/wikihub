"""Curation judgement export (wikihub-f3b7; plan D3/D4 and 3c item 9).

GET /api/v1/curation[?since=ISO8601][&cursor=...][&limit=N]

WikiHub is the authoritative writer of its curation judgements. This lists
every judgement, in force or not (status active, retracted, superseded or
expired), ordered by (updated_at, id), so a mirror such as Noos can upsert
idempotently by `id`, poll incrementally (pass the previous response's
`next_cursor`, or `since` = last sync time) and propagate revocations.
WikiHub never calls the mirror.

Each item is an attributed, versioned *judgement* (curator, reason, public
evidence, policy_version, affected_version, lifecycle timestamps, reply
link) plus `enforcement`: what WikiHub itself does about it on its own
surfaces while it is in force. Consumers decide how far to trust the
judgement; the enforcement list is WikiHub's decision only.

Public callers never see moderator notes or curator accounts, and
judgements on a target that is not publicly visible (private wiki,
unlisted or private page) are left out so the export cannot reveal their
names. The admin token (Authorization: Bearer ADMIN_TOKEN) sees everything.
"""

from datetime import datetime, timezone

from flask import current_app, jsonify, request
from sqlalchemy import and_, or_

from app.auth_utils import check_admin_token
from app.curation import CURATOR_HANDLE, CURATOR_NAME, ENFORCEMENT, REASON_LABELS, effective_status
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

    CJ = CurationJudgement
    query = CJ.query
    cursor = (request.args.get("cursor") or "").strip()
    since = _parse_ts(request.args.get("since"))
    if since is False:
        return jsonify({"error": "bad_request", "message": "since must be an ISO 8601 timestamp"}), 400
    if cursor:
        ts_raw, _, id_raw = cursor.rpartition("|")
        ts = _parse_ts(ts_raw)
        if not ts or not id_raw.isdigit():
            return jsonify({"error": "bad_request", "message": "invalid cursor"}), 400
        query = query.filter(or_(CJ.updated_at > ts, and_(CJ.updated_at == ts, CJ.id > int(id_raw))))
    elif since:
        query = query.filter(CJ.updated_at >= since)

    rows = query.order_by(CJ.updated_at.asc(), CJ.id.asc()).limit(limit + 1).all()
    has_more = len(rows) > limit
    rows = rows[:limit]

    user_ids = {r.scope_id for r in rows if r.scope_type == "account"} | {r.curator_user_id for r in rows if r.curator_user_id}
    users = {u.id: u for u in User.query.filter(User.id.in_(user_ids)).all()} if user_ids else {}
    page_ids = {r.scope_id for r in rows if r.scope_type == "page"}
    pages = {p.id: p for p in Page.query.filter(Page.id.in_(page_ids)).all()} if page_ids else {}
    wiki_ids = {r.scope_id for r in rows if r.scope_type == "wiki"} | {p.wiki_id for p in pages.values()}
    wikis = {w.id: w for w in Wiki.query.filter(Wiki.id.in_(wiki_ids)).all()} if wiki_ids else {}
    public_wiki_ids = discoverable_wiki_ids(trusted_only=False) if rows else set()
    account_ids = [r.scope_id for r in rows if r.scope_type == "account"]
    account_wikis = {}
    if account_ids:
        for w in Wiki.query.filter(Wiki.owner_id.in_(account_ids)).order_by(Wiki.slug.asc()).all():
            if is_admin or w.id in public_wiki_ids:
                account_wikis.setdefault(w.owner_id, []).append(w)

    base = _base_url()
    now = utcnow()
    out = []
    for r in rows:
        status = effective_status(r, now)
        item = {
            "id": r.id,
            "polarity": r.polarity,
            "reason": r.reason,
            "label": REASON_LABELS.get(r.reason, r.reason),
            "status": status,
            "in_force": status == "active",
            "scope": {"type": r.scope_type},
            "url": None,
            "wiki_urls": [],
            "curator": {"name": CURATOR_NAME, "handle": CURATOR_HANDLE},
            "evidence": list(r.evidence or []),
            "affected_version": r.affected_version,
            "policy_version": r.policy_version,
            "reply_url": r.reply_url,
            "superseded_by": r.superseded_by,
            "at": _iso(r.created_at),
            "created_at": _iso(r.created_at),
            "updated_at": _iso(r.updated_at),
            "review_at": _iso(r.review_at),
            "expires_at": _iso(r.expires_at),
            "enforcement": {"wikihub": list(ENFORCEMENT[r.polarity]) if status == "active" else []},
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

    next_cursor = f"{_cursor_ts(rows[-1].updated_at)}|{rows[-1].id}" if has_more and rows else None
    return jsonify({
        "publisher": base,
        "authoritative": True,
        "judgements": out,
        "next_cursor": next_cursor,
        "has_more": has_more,
        "generated_at": _iso(now),
        "docs": f"{base}/curation",
    })
