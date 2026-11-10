"""Editorial curation: negative ("curated against") and positive judgements
(wikihub-f3b7). Design: Noos semantic-web plan D4, docs/curation.md.

A judgement targets an account, a wiki or a page. Effects resolve per page
through its wiki and owner: a page is curated against when the page, its
wiki or its owner carries an active negative judgement.

Negative judgement effects (content is never deleted):
  * banner at the top of every page in scope + <meta name="wikihub:curation">;
  * noindex (meta + X-Robots-Tag) and rel="nofollow ugc" on external links;
  * excluded from /explore, /activity (+RSS), /people, /llms.txt,
    /llms-full.txt, the landing featured list and GET /api/v1/wikis;
  * sorted last in site search;
  * a `curation` field on wiki and page API objects.
An account-scope negative judgement also withdraws trust (app/trust.py), so
every existing trust check applies to it. Unlike the trust gate, curation
applies whether or not TRUST_GATE_ENABLED is set: it is an explicit act.

Positive judgements (featured, official, editor_pick) feed Explore's
editorial area. When a page has both, the negative judgement wins.
"""

from sqlalchemy import and_, exists, true

NEGATIVE_REASONS = ("promotional", "affiliate_farm", "automated_link_placement", "low_quality", "off_topic")
POSITIVE_REASONS = ("featured", "official", "editor_pick")
SCOPE_TYPES = ("account", "wiki", "page")
_SPECIFICITY = {"page": 0, "wiki": 1, "account": 2}

REASON_LABELS = {
    "promotional": "promotional",
    "affiliate_farm": "affiliate farm",
    "automated_link_placement": "automated link placement",
    "low_quality": "low quality",
    "off_topic": "off topic",
    "featured": "featured",
    "official": "official",
    "editor_pick": "editor's pick",
}


def normalize_reason(value):
    """CLI/API spelling -> stored spelling: 'affiliate-farm' -> 'affiliate_farm'."""
    return (value or "").strip().lower().replace("-", "_").replace(" ", "_")


def polarity_for_reason(reason):
    if reason in NEGATIVE_REASONS:
        return "negative"
    if reason in POSITIVE_REASONS:
        return "positive"
    return None


def meta_content(judgement):
    """Value for <meta name="wikihub:curation">, e.g. 'negative:promotional'."""
    return f"{judgement['polarity']}:{judgement['reason']}" if judgement else ""


def _norm_url_path(path):
    path = (path or "").strip("/")
    if path.endswith(".md"):
        path = path[:-3]
    return path


# ---------------------------------------------------------------------------
# Snapshot of active judgements. The table is small (tens to hundreds of
# rows), so one query per request is cheaper than per-object lookups.
# ---------------------------------------------------------------------------

def _load_snapshot():
    from app import db
    from app.models import CurationJudgement, Page, User, Wiki
    from app.url_utils import url_param_from_page_path

    rows = CurationJudgement.query.filter(CurationJudgement.revoked_at.is_(None)).all()
    snap = {"rows": rows, "by_scope": {}, "page_names": {}, "wiki_names": {}, "account_names": {}}
    for row in rows:
        snap["by_scope"].setdefault((row.scope_type, row.scope_id), []).append(row)

    wiki_ids = [r.scope_id for r in rows if r.scope_type == "wiki"]
    page_ids = [r.scope_id for r in rows if r.scope_type == "page"]
    account_ids = [r.scope_id for r in rows if r.scope_type == "account"]
    if account_ids:
        for uid, uname in db.session.query(User.id, User.username).filter(User.id.in_(account_ids)):
            snap["account_names"][uname] = uid
    if wiki_ids:
        for wid, slug, uname in (db.session.query(Wiki.id, Wiki.slug, User.username)
                                 .join(User, Wiki.owner_id == User.id).filter(Wiki.id.in_(wiki_ids))):
            snap["wiki_names"][(uname, slug)] = wid
    if page_ids:
        for pid, path, slug, uname in (db.session.query(Page.id, Page.path, Wiki.slug, User.username)
                                       .join(Wiki, Page.wiki_id == Wiki.id)
                                       .join(User, Wiki.owner_id == User.id)
                                       .filter(Page.id.in_(page_ids))):
            snap["page_names"][(uname, slug, _norm_url_path(url_param_from_page_path(path)))] = pid
            snap["page_names"][(uname, slug, _norm_url_path(path))] = pid
    return snap


def snapshot():
    """Active judgements, cached for the current request (fresh outside one)."""
    from flask import has_request_context, request
    if has_request_context():
        snap = request.environ.get("wikihub.curation_snapshot")
        if snap is None:
            snap = _load_snapshot()
            request.environ["wikihub.curation_snapshot"] = snap
        return snap
    return _load_snapshot()


def serialize(row, *, include_note=False):
    out = {
        "id": row.id,
        "polarity": row.polarity,
        "reason": row.reason,
        "label": REASON_LABELS.get(row.reason, row.reason),
        "scope": row.scope_type,
        "at": row.created_at.isoformat() if row.created_at else None,
    }
    if include_note:
        out["note"] = row.note
    return out


def _pick(rows):
    """Negative beats positive; then the most specific scope; then newest."""
    if not rows:
        return None
    negatives = [r for r in rows if r.polarity == "negative"]
    pool = negatives or rows
    pool.sort(key=lambda r: (_SPECIFICITY[r.scope_type], -(r.created_at.timestamp() if r.created_at else 0)))
    return pool[0]


def effective_row(owner_id=None, wiki_id=None, page_id=None, snap=None):
    snap = snap if snap is not None else snapshot()
    if not snap["rows"]:
        return None
    rows = []
    for scope_type, scope_id in (("account", owner_id), ("wiki", wiki_id), ("page", page_id)):
        if scope_id is not None:
            rows.extend(snap["by_scope"].get((scope_type, scope_id), ()))
    return _pick(rows)


def effective(owner_id=None, wiki_id=None, page_id=None):
    """Public curation dict for an object, or None."""
    row = effective_row(owner_id, wiki_id, page_id)
    return serialize(row) if row else None


def effective_by_names(username, slug=None, url_path=None):
    """Resolve from URL components (/@username/slug/url_path)."""
    snap = snapshot()
    if not snap["rows"] or not username:
        return None
    owner_id = snap["account_names"].get(username)
    wiki_id = snap["wiki_names"].get((username, slug)) if slug else None
    page_id = None
    if slug and url_path:
        page_id = snap["page_names"].get((username, slug, _norm_url_path(url_path)))
    if owner_id is None and wiki_id is None and page_id is None:
        return None
    row = effective_row(owner_id, wiki_id, page_id, snap)
    return serialize(row) if row else None


def negative_ids(scope_type):
    return {sid for (st, sid), rows in snapshot()["by_scope"].items()
            if st == scope_type and any(r.polarity == "negative" for r in rows)}


def is_account_negative(user_id):
    return user_id is not None and user_id in negative_ids("account")


def positive_wiki_ids():
    """Wikis with a positive wiki-scope or page-scope judgement, newest first."""
    from app.models import Page
    snap = snapshot()
    ordered = sorted((r for r in snap["rows"] if r.polarity == "positive" and r.scope_type in ("wiki", "page")),
                     key=lambda r: -(r.created_at.timestamp() if r.created_at else 0))
    page_ids = [r.scope_id for r in ordered if r.scope_type == "page"]
    page_wiki = dict(Page.query.with_entities(Page.id, Page.wiki_id).filter(Page.id.in_(page_ids)).all()) if page_ids else {}
    out = []
    for r in ordered:
        wid = r.scope_id if r.scope_type == "wiki" else page_wiki.get(r.scope_id)
        if wid is not None and wid not in out:
            out.append(wid)
    return out


# ---------------------------------------------------------------------------
# SQL clauses for discovery queries.
# ---------------------------------------------------------------------------

def _negative_exists(scope_type, id_column):
    from app.models import CurationJudgement as CJ
    return exists().where(and_(
        CJ.scope_type == scope_type,
        CJ.scope_id == id_column,
        CJ.polarity == "negative",
        CJ.revoked_at.is_(None),
    ))


def not_curated_against(user_id_col=None, wiki_id_col=None, page_id_col=None):
    """SQL filter: none of the given scopes has an active negative judgement."""
    clauses = []
    for scope_type, col in (("account", user_id_col), ("wiki", wiki_id_col), ("page", page_id_col)):
        if col is not None:
            clauses.append(~_negative_exists(scope_type, col))
    return and_(*clauses) if clauses else true()


def curated_against_sort_key(user_id_col=None, wiki_id_col=None, page_id_col=None):
    """ORDER BY expression: 0 for normal rows, 1 for curated-against rows."""
    from sqlalchemy import case, or_
    conds = [_negative_exists(st, col) for st, col in
             (("account", user_id_col), ("wiki", wiki_id_col), ("page", page_id_col)) if col is not None]
    return case((or_(*conds), 1), else_=0)


# ---------------------------------------------------------------------------
# Request hook: record the effective judgement for /@user/... URLs so
# base.html can render the banner + meta and noindex applies.
# ---------------------------------------------------------------------------

def mark_request(username, slug=None, url_path=None):
    from flask import request
    judgement = effective_by_names(username, slug, url_path)
    if judgement:
        request.environ["wikihub.curation"] = judgement
        if judgement["polarity"] == "negative":
            request.environ["wikihub.noindex"] = True
    return judgement


def request_negative_for(username, slug=None, page_path=None):
    """True when rendered content at (username, slug, page_path) is curated
    against; used by the renderer to decide rel="nofollow ugc"."""
    try:
        judgement = effective_by_names(username, slug, page_path)
    except RuntimeError:  # outside an app context
        return False
    return bool(judgement and judgement["polarity"] == "negative")
