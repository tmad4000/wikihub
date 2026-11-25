"""POST /api/v1/wikis/<owner>/<slug>/ask -- the reader assistant (wikihub-1bfx).

Anyone who can read a wiki can ask it questions; the answer is built only
from pages that same visitor can read. See app/reader_assistant.py.
"""

import ipaddress
import threading
import time
from urllib.parse import urlsplit

from flask import Blueprint, current_app, jsonify, request

from app import rate_limit
from app.auth_utils import client_ip, get_current_user_from_request, security_event
from app.models import Page
from app.page_utils import content_page_path_filter
from app.reader_assistant import (
    MAX_QUESTION_CHARS,
    AssistantUnavailable,
    answer_question,
    build_scope,
    is_available,
)
from app.routes.api_wikis import _get_wiki_or_404

reader_assistant_bp = Blueprint("reader_assistant", __name__)

# Per-worker cap on in-flight asks so slow model calls can never occupy every
# gunicorn thread (prod: 2 workers x 4 threads). Busy -> 429, never queue.
_inflight = None
_inflight_lock = threading.Lock()


def _slots():
    global _inflight
    with _inflight_lock:
        if _inflight is None:
            _inflight = threading.BoundedSemaphore(max(1, int(current_app.config.get("READER_ASSISTANT_MAX_CONCURRENT", 2))))
        return _inflight


def _visitor_key(viewer):
    if viewer:
        return f"u:{viewer.id}"
    ip = client_ip() or "unknown"
    try:
        addr = ipaddress.ip_address(ip)
        if addr.version == 6:  # one household/host rotates within its /64
            return "ip6:" + str(ipaddress.ip_network(f"{ip}/64", strict=False).network_address)
    except ValueError:
        pass
    return f"ip:{ip}"


def _cross_site():
    """Browsers always send Origin on POST. A different site may not spend
    this server's model budget through its visitors (CORS is open on /api)."""
    origin = request.headers.get("Origin")
    if not origin:
        return False  # non-browser clients (curl, agents)
    return urlsplit(origin).netloc.lower() != request.host.lower()


def _no_store(resp, status=200, headers=None):
    resp = jsonify(resp) if isinstance(resp, dict) else resp
    resp.status_code = status
    resp.headers["Cache-Control"] = "no-store"
    for k, v in (headers or {}).items():
        resp.headers[k] = v
    return resp


def _limited(viewer, owner, slug):
    """Per-visitor, per-wiki and site-wide budgets (Postgres-backed, shared by
    workers). Checked before any page scan so probing is throttled too."""
    per_hour = rate_limit.limit_value(
        "READER_ASSISTANT_MAX_PER_USER_PER_HOUR" if viewer else "READER_ASSISTANT_MAX_PER_IP_PER_HOUR"
    )
    rules = [
        (f"reader_ask:{_visitor_key(viewer)}:h", per_hour, 3600),
        (f"reader_ask:wiki:{owner.lower()}/{slug.lower()}:d", rate_limit.limit_value("READER_ASSISTANT_MAX_PER_WIKI_PER_DAY"), 86400),
        ("reader_ask:all:d", rate_limit.limit_value("READER_ASSISTANT_MAX_PER_DAY"), 86400),
    ]
    allowed, retry_after, desc = rate_limit.check_all(rules)
    if allowed:
        return None
    security_event("rate_limit", "denied", scope="reader_ask", limit=desc)
    return _no_store(
        {
            "error": "rate_limited",
            "message": f"The reader assistant is busy for you right now. Try again in {max(1, retry_after // 60)} min.",
            "retry_after": retry_after,
        },
        429,
        {"Retry-After": str(retry_after)},
    )


@reader_assistant_bp.route("/wikis/<owner>/<slug>/ask", methods=["POST"])
def ask_wiki(owner, slug):
    if not is_available():
        return _no_store({"error": "disabled", "message": "The reader assistant is not enabled on this server."}, 503)
    # JSON only, same-site browsers only: other sites cannot spend the budget.
    if not request.is_json:
        return _no_store({"error": "bad_request", "message": "Send JSON."}, 415)
    if _cross_site():
        return _no_store({"error": "forbidden", "message": "Ask from the wiki's own pages."}, 403)
    data = request.get_json(silent=True) or {}
    question = data.get("question")
    if not isinstance(question, str) or not question.strip():
        return _no_store({"error": "bad_request", "message": "Ask a question."}, 400)
    question = question.strip()
    if len(question) > MAX_QUESTION_CHARS:
        return _no_store({"error": "bad_request", "message": f"Questions are limited to {MAX_QUESTION_CHARS} characters."}, 400)

    viewer = get_current_user_from_request()
    limited = _limited(viewer, owner, slug)
    if limited is not None:
        return limited
    owner_user, wiki, err = _get_wiki_or_404(owner, slug)
    if err:
        return _no_store(err[0], err[1])
    if owner_user.suspended_at is not None:
        return _no_store({"error": "gone", "message": "This wiki is no longer available."}, 410)

    scope = build_scope(owner_user, wiki, viewer)
    if not scope.readable_pages():
        has_pages = Page.query.filter_by(wiki_id=wiki.id).filter(content_page_path_filter(Page.path)).first() is not None
        if not has_pages:
            return _no_store({"error": "not_found", "message": "This wiki has no pages yet."}, 404)
        # wikihub-dkp8: exists but restricted.
        if viewer:
            return _no_store({"error": "forbidden", "message": "This wiki is restricted — you don't have access."}, 403)
        return _no_store({"error": "authentication_required", "message": "This wiki is restricted — sign in to check your access."}, 401)

    page_path = data.get("page_path")
    current_page = scope.resolve(page_path) if isinstance(page_path, str) and page_path else None

    scope_factory = current_app.config.get("READER_SCOPE_FACTORY")
    if scope_factory:
        scope = scope_factory(scope, current_page, data.get("scope", "wiki"))

    slots = _slots()
    if not slots.acquire(blocking=False):
        return _no_store(
            {"error": "busy", "message": "The reader assistant is busy. Try again in a moment.", "retry_after": 10},
            429, {"Retry-After": "10"},
        )
    started = time.monotonic()
    try:
        result = answer_question(scope, question, current_page=current_page, history=data.get("history"))
    except AssistantUnavailable:
        return _no_store({"error": "disabled", "message": "The reader assistant is not configured on this server."}, 503)
    except Exception as exc:  # upstream/model errors: no internals to the client
        current_app.logger.warning("reader assistant failed for %s/%s: %s", owner_user.username, wiki.slug, type(exc).__name__)
        return _no_store({"error": "upstream_error", "message": "The assistant couldn't answer just now. Please try again."}, 502)
    finally:
        slots.release()

    # Usage log without question text or page content.
    current_app.logger.info(
        "reader_ask wiki=%s/%s viewer=%s tools=%d cites=%d ms=%d calls=%d in=%d out=%d cached=%d incomplete=%s",
        owner_user.username, wiki.slug, viewer.id if viewer else "anon",
        result["tool_calls"], len(result["citations"]), int((time.monotonic() - started) * 1000),
        result["usage"]["calls"], result["usage"]["input"], result["usage"]["output"],
        result["usage"]["cache_read"], result["incomplete"],
    )
    return _no_store({
        "answer": result["answer"],
        "citations": result["citations"],
        "wiki": f"{owner_user.username}/{wiki.slug}",
        "as": viewer.username if viewer else None,
    })
