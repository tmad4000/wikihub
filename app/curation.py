"""Editorial curation (wikihub-f3b7). Design: Noos semantic-web plan D4 as
amended by section 3c (Astra review), and docs/curation.md.

Two layers, kept apart:

JUDGEMENT. A row in curation_judgements: an attributed ("WikiHub editors"),
versioned (policy_version, affected_version) opinion about an account, wiki
or page, with reason, public evidence, a lifecycle (status active, retracted,
superseded or expired; review_at; expires_at; superseded_by) and an optional
reply_url for the owner's reply or appeal. WikiHub is the authoritative
writer; GET /api/v1/curation exports judgements, revocations included, so
other systems (Noos, crawlers) can decide how far to trust them.

ENFORCEMENT. What WikiHub itself does on its own surfaces while a negative
judgement is in force. Content is never deleted and stays readable by link:
  * a dismissible banner naming the curator, reason, scope and reply path;
  * server-rendered noindex (meta tag and X-Robots-Tag) and link-level
    rel="nofollow ugc" on external links;
  * exclusion from /explore, /activity (+RSS), /people, /llms.txt,
    /llms-full.txt, the landing featured list and GET /api/v1/wikis;
  * last place in site search.
Positive judgements (featured, official, editor_pick) add a wiki to
Explore's Editorial Picks. <meta name="wikihub:curation"> and the API
`curation` field carry the same data as the export.

Resolution. A page is judged through itself, its wiki and its owner. Only
judgements in force count (status 'active' and expires_at unset or in the
future). Negative and positive are resolved separately; within each, the
most specific scope wins (page > wiki > account), then the newest. A
positive judgement never cancels a negative one: both are shown, and
enforcement follows the negative. An account-scope negative also withdraws
trust (app/trust.py), so the trust gate's plumbing applies to it. Curation
applies whether or not TRUST_GATE_ENABLED is set.
"""

import posixpath

from sqlalchemy import and_, exists, func, or_, true

NEGATIVE_REASONS = ("promotional", "affiliate_farm", "automated_link_placement", "low_quality", "off_topic")
POSITIVE_REASONS = ("featured", "official", "editor_pick")
SCOPE_TYPES = ("account", "wiki", "page")
STATUSES = ("active", "retracted", "superseded", "expired")
CURATOR_NAME = "WikiHub editors"
CURATOR_HANDLE = "wikihub"
DEFAULT_POLICY_VERSION = "2026-11-09"
_SPECIFICITY = {"page": 0, "wiki": 1, "account": 2}
_INDEX_NAMES = ("index.md", "README.md")

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

# What WikiHub enforces for a judgement in force, published with each
# judgement so consumers can tell the opinion from WikiHub's own response.
ENFORCEMENT = {
    "negative": ["banner", "noindex", "nofollow_ugc", "excluded_from_discovery", "search_last"],
    "positive": ["editorial_picks"],
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


def policy_version():
    from flask import current_app
    try:
        return current_app.config.get("CURATION_POLICY_VERSION") or DEFAULT_POLICY_VERSION
    except RuntimeError:
        return DEFAULT_POLICY_VERSION


def effective_status(row, now=None):
    """Stored status, except an 'active' row past expires_at reads 'expired'."""
    if row.status == "active" and row.expires_at is not None:
        from app.models import utcnow
        if row.expires_at <= (now or utcnow()):
            return "expired"
    return row.status


def in_force_clause(model=None):
    """SQL: the judgement currently has effects."""
    if model is None:
        from app.models import CurationJudgement as model
    return and_(model.status == "active",
                or_(model.expires_at.is_(None), model.expires_at > func.now()))


def _norm_url_path(path):
    path = (path or "").strip("/")
    if path.endswith(".md"):
        path = path[:-3]
    return path


def _page_keys(path):
    """URL keys under which a page is served: its own URL forms and, for an
    index page, the folder (or wiki root, key '') it renders for."""
    from app.url_utils import url_param_from_page_path
    keys = {_norm_url_path(url_param_from_page_path(path)), _norm_url_path(path)}
    if posixpath.basename(path) in _INDEX_NAMES:
        folder = posixpath.dirname(path)
        keys |= {_norm_url_path(folder), _norm_url_path(url_param_from_page_path(folder))}
    return keys


# ---------------------------------------------------------------------------
# Snapshot of judgements in force. The table is small (tens to hundreds of
# rows), so one query per request beats per-object lookups.
# ---------------------------------------------------------------------------

def _load_snapshot():
    from app import db
    from app.models import CurationJudgement, Page, User, Wiki

    rows = CurationJudgement.query.filter(in_force_clause(CurationJudgement)).all()
    snap = {"rows": rows, "by_scope": {}, "page_names": {}, "wiki_names": {}, "account_names": {}, "names": {}}
    for row in rows:
        snap["by_scope"].setdefault((row.scope_type, row.scope_id), []).append(row)

    wiki_ids = [r.scope_id for r in rows if r.scope_type == "wiki"]
    page_ids = [r.scope_id for r in rows if r.scope_type == "page"]
    account_ids = [r.scope_id for r in rows if r.scope_type == "account"]
    if account_ids:
        for uid, uname in db.session.query(User.id, User.username).filter(User.id.in_(account_ids)):
            snap["account_names"][uname] = uid
            snap["names"][("account", uid)] = (uname, None, None)
    if wiki_ids:
        for wid, slug, uname in (db.session.query(Wiki.id, Wiki.slug, User.username)
                                 .join(User, Wiki.owner_id == User.id).filter(Wiki.id.in_(wiki_ids))):
            snap["wiki_names"][(uname, slug)] = wid
            snap["names"][("wiki", wid)] = (uname, slug, None)
    if page_ids:
        for pid, path, slug, uname in (db.session.query(Page.id, Page.path, Wiki.slug, User.username)
                                       .join(Wiki, Page.wiki_id == Wiki.id)
                                       .join(User, Wiki.owner_id == User.id)
                                       .filter(Page.id.in_(page_ids))):
            for key in _page_keys(path):
                snap["page_names"][(uname, slug, key)] = pid
            snap["names"][("page", pid)] = (uname, slug, path)
    return snap


def snapshot():
    """Judgements in force, cached for the current request (fresh outside one)."""
    from flask import has_request_context, request
    if has_request_context():
        snap = request.environ.get("wikihub.curation_snapshot")
        if snap is None:
            snap = _load_snapshot()
            request.environ["wikihub.curation_snapshot"] = snap
        return snap
    return _load_snapshot()


def scope_phrase(scope_type, names):
    """Human description of what a judgement covers, for the banner."""
    username, slug, _path = names or (None, None, None)
    if scope_type == "page":
        return "this page"
    if scope_type == "wiki":
        return f"this wiki (@{username}/{slug})" if username and slug else "this wiki"
    return f"everything published by @{username}" if username else "this account"


def serialize(row, names=None):
    """Public view of one judgement (API field, meta, banner). Never the note."""
    return {
        "id": row.id,
        "polarity": row.polarity,
        "reason": row.reason,
        "label": REASON_LABELS.get(row.reason, row.reason),
        "scope": row.scope_type,
        "scope_description": scope_phrase(row.scope_type, names),
        "status": effective_status(row),
        "curator": CURATOR_NAME,
        "policy_version": row.policy_version,
        "at": row.created_at.isoformat() if row.created_at else None,
        "reply_url": row.reply_url,
        "info_url": f"/curation#{row.reason}",
        "enforcement": list(ENFORCEMENT[row.polarity]),
    }


def _most_specific(rows):
    if not rows:
        return None
    return sorted(rows, key=lambda r: (_SPECIFICITY[r.scope_type],
                                       -(r.created_at.timestamp() if r.created_at else 0)))[0]


def resolve_rows(owner_id=None, wiki_id=None, page_id=None, snap=None):
    """(negative_row, positive_row) in force for the object; either may be None."""
    snap = snap if snap is not None else snapshot()
    if not snap["rows"]:
        return None, None
    rows = []
    for scope_type, scope_id in (("account", owner_id), ("wiki", wiki_id), ("page", page_id)):
        if scope_id is not None:
            rows.extend(snap["by_scope"].get((scope_type, scope_id), ()))
    return (_most_specific([r for r in rows if r.polarity == "negative"]),
            _most_specific([r for r in rows if r.polarity == "positive"]))


def _bundle(neg, pos, snap):
    if neg is None and pos is None:
        return None
    return {
        "negative": serialize(neg, snap["names"].get((neg.scope_type, neg.scope_id))) if neg else None,
        "positive": serialize(pos, snap["names"].get((pos.scope_type, pos.scope_id))) if pos else None,
    }


def effective(owner_id=None, wiki_id=None, page_id=None):
    """The `curation` API field: None, or {"negative": J|None, "positive": J|None}."""
    snap = snapshot()
    neg, pos = resolve_rows(owner_id, wiki_id, page_id, snap)
    return _bundle(neg, pos, snap)


def effective_by_names(username, slug=None, url_path=None):
    """Same as effective(), resolved from URL components (/@username/slug/url_path).
    An empty url_path is the wiki root, which renders its index page."""
    snap = snapshot()
    if not snap["rows"] or not username:
        return None
    owner_id = snap["account_names"].get(username)
    wiki_id = snap["wiki_names"].get((username, slug)) if slug else None
    page_id = snap["page_names"].get((username, slug, _norm_url_path(url_path))) if slug else None
    if owner_id is None and wiki_id is None and page_id is None:
        return None
    neg, pos = resolve_rows(owner_id, wiki_id, page_id, snap)
    return _bundle(neg, pos, snap)


def meta_content(bundle):
    """<meta name="wikihub:curation"> value: space-separated polarity:reason
    tokens, negative first, e.g. 'negative:promotional positive:editor_pick'."""
    if not bundle:
        return ""
    return " ".join(f"{j['polarity']}:{j['reason']}" for j in (bundle["negative"], bundle["positive"]) if j)


def negative_ids(scope_type):
    return {sid for (st, sid), rows in snapshot()["by_scope"].items()
            if st == scope_type and any(r.polarity == "negative" for r in rows)}


def is_account_negative(user_id):
    return user_id is not None and user_id in negative_ids("account")


def positive_wiki_ids():
    """Wikis with a positive wiki- or page-scope judgement in force, newest first."""
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


def rekey_page_judgements(wiki_id):
    """Point page-scope judgements of this wiki at the current Page row for
    their stored path. Called after index_repo_pages(), which recreates page
    rows with new ids on a reset reindex. Caller commits. Returns the count
    re-keyed. A judgement whose page no longer exists keeps its old id (it has
    no effect) and is re-keyed if the page comes back."""
    from app.models import CurationJudgement, Page
    rows = (CurationJudgement.query
            .filter(CurationJudgement.scope_type == "page",
                    CurationJudgement.scope_wiki_id == wiki_id,
                    CurationJudgement.scope_path.isnot(None))
            .all())
    if not rows:
        return 0
    ids = dict(Page.query.with_entities(Page.path, Page.id)
               .filter(Page.wiki_id == wiki_id, Page.path.in_({r.scope_path for r in rows})).all())
    taken = {(r.scope_id, r.polarity, r.reason) for r in rows if r.status == "active" and ids.get(r.scope_path) == r.scope_id}
    moved = 0
    for r in rows:
        new_id = ids.get(r.scope_path)
        if new_id is None or new_id == r.scope_id:
            continue
        key = (new_id, r.polarity, r.reason)
        if r.status == "active" and key in taken:
            continue  # a newer judgement already covers the recreated page
        r.scope_id = new_id
        if r.status == "active":
            taken.add(key)
        moved += 1
    return moved


# ---------------------------------------------------------------------------
# Enforcement: SQL clauses for discovery queries.
# ---------------------------------------------------------------------------

def _negative_exists(scope_type, id_column):
    from app.models import CurationJudgement as CJ
    return exists().where(and_(
        CJ.scope_type == scope_type,
        CJ.scope_id == id_column,
        CJ.polarity == "negative",
        in_force_clause(CJ),
    ))


def not_curated_against(user_id_col=None, wiki_id_col=None, page_id_col=None):
    """SQL filter: no negative judgement in force on any of the given scopes."""
    clauses = []
    for scope_type, col in (("account", user_id_col), ("wiki", wiki_id_col), ("page", page_id_col)):
        if col is not None:
            clauses.append(~_negative_exists(scope_type, col))
    return and_(*clauses) if clauses else true()


def curated_against_sort_key(user_id_col=None, wiki_id_col=None, page_id_col=None):
    """ORDER BY expression: 0 for normal rows, 1 for curated-against rows."""
    from sqlalchemy import case
    conds = [_negative_exists(st, col) for st, col in
             (("account", user_id_col), ("wiki", wiki_id_col), ("page", page_id_col)) if col is not None]
    return case((or_(*conds), 1), else_=0)


# ---------------------------------------------------------------------------
# Enforcement: request hook for /@user/... URLs. base.html renders the
# banner and meta from request.environ["wikihub.curation"].
# ---------------------------------------------------------------------------

CURATION_MARK_START = "<!--wikihub-curation-->"
CURATION_MARK_END = "<!--/wikihub-curation-->"


def mark_request(username, slug=None, url_path=None):
    """Record the curation bundle for this URL. /@user (no slug) is the
    profile, which renders the personal wiki @user/user's index.

    This runs before any access check, so nothing here may reach a denied
    response: base.html wraps everything it renders from the bundle in
    CURATION_MARK_START/END, and after_response() strips that region and
    skips the curation noindex header on any status >= 400. A judgement is
    substance of the thing judged; a viewer who cannot read the page must not
    learn it (plan 3d)."""
    from flask import request
    bundle = effective_by_names(username, slug or username, url_path or "")
    if bundle:
        request.environ["wikihub.curation"] = bundle
        request.environ["wikihub.curation_meta"] = meta_content(bundle)
        request.environ["wikihub.curation_owner"] = username
        if bundle["negative"]:
            request.environ["wikihub.curation_noindex"] = True
    return bundle


def after_response(response):
    """Enforce only on granted responses: add the curation noindex header on
    status < 400; strip every trace of the bundle from denied/error pages."""
    import re
    from flask import request
    if not request.environ.get("wikihub.curation"):
        return response
    if response.status_code < 400:
        if request.environ.get("wikihub.curation_noindex") and "X-Robots-Tag" not in response.headers:
            response.headers["X-Robots-Tag"] = "noindex"
        return response
    if response.direct_passthrough or response.is_streamed:
        return response
    if (response.mimetype or "").startswith("text/html"):
        body = response.get_data(as_text=True)
        if CURATION_MARK_START in body:
            response.set_data(re.sub(re.escape(CURATION_MARK_START) + r".*?" + re.escape(CURATION_MARK_END),
                                     "", body, flags=re.S))
    return response


def request_negative_for(username, slug=None, page_path=None):
    """True when rendered content at (username, slug, page_path) is curated
    against; the renderer then marks external links rel="nofollow ugc"."""
    try:
        bundle = effective_by_names(username, slug, page_path or "")
    except RuntimeError:  # outside an app context
        return False
    return bool(bundle and bundle["negative"])
