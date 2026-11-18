"""Bound client-cookie OAuth state without changing Authlib's verification.

Only Google/Ideaflow pending flows are touched. Logged-in identity, CSRF and
unrelated session data survive pruning. Legacy undated pending flows fail closed
and can be restarted; completed login sessions are unaffected.
"""
import math
from functools import wraps
from time import time

from flask import current_app, request, session

MAX_PENDING = 3
TTL_SECONDS = 10 * 60
MAX_COOKIE_VALUE_BYTES = 3072  # leaves room for attributes and other response headers
PROVIDERS = ("google", "ideaflow")
_STARTED = "_oauth_started"
_ORDER = "_oauth_order"


def _contexts(provider):
    value = session.get(provider + "_oauth_contexts", {})
    return dict(value) if isinstance(value, dict) else {}


def _write(provider, contexts):
    key = provider + "_oauth_contexts"
    if contexts:
        session[key] = contexts
    else:
        session.pop(key, None)


def discard(provider, state):
    if not isinstance(state, str) or not state:
        return
    contexts = _contexts(provider)
    contexts.pop(state, None)
    _write(provider, contexts)
    session.pop(f"_state_{provider}_{state}", None)


def _cookie_bytes():
    getter = getattr(current_app.session_interface, "get_signing_serializer", None)
    serializer = getter(current_app) if getter else None
    # This app uses Flask's signed-cookie session. Refuse to start a flow if its
    # configured serializer cannot enforce the budget rather than bypass it.
    return len(serializer.dumps(dict(session)).encode()) if serializer else math.inf


def prune():
    now = time()
    entries = []
    for provider in PROVIDERS:
        for state, context in _contexts(provider).items():
            if not isinstance(state, str) or not isinstance(context, dict):
                continue
            started, order = context.get(_STARTED), context.get(_ORDER)
            if (type(started) not in (int, float) or not math.isfinite(started)
                    or not 0 <= now - started < TTL_SECONDS or type(order) is not int):
                continue
            entries.append((order, started, provider, state, context))
    entries.sort(key=lambda item: item[:4])
    entries = entries[-MAX_PENDING:]

    def retain(kept):
        contexts = {provider: {} for provider in PROVIDERS}
        for rank, (_, _, provider, state, context) in enumerate(kept):
            context = dict(context)
            context[_ORDER] = rank
            contexts[provider][state] = context
        for provider in PROVIDERS:
            _write(provider, contexts[provider])
            prefix = f"_state_{provider}_"
            for key in list(session):
                if key.startswith(prefix) and key[len(prefix):] not in contexts[provider]:
                    session.pop(key, None)

    retain(entries)
    while entries and _cookie_bytes() > MAX_COOKIE_VALUE_BYTES:
        entries.pop(0)
        retain(entries)


def stash(provider, state, context):
    if not isinstance(state, str) or not state:
        return False
    order = max((c.get(_ORDER, -1) for p in PROVIDERS for c in _contexts(p).values()
                 if isinstance(c, dict) and type(c.get(_ORDER)) is int), default=-1) + 1
    pending = _contexts(provider)
    pending[state] = {**context, _STARTED: time(), _ORDER: order}
    _write(provider, pending)
    # Authlib has already stored nonce/PKCE data for this new state. Prune both
    # maps together only after adding its context, so the new record isn't orphaned.
    prune()
    return state in _contexts(provider)


def pop_context(provider, state):
    prune()
    pending = _contexts(provider)
    context = pending.pop(state, {}) if isinstance(state, str) else {}
    _write(provider, pending)
    # Leave Authlib's record until authorize_access_token has verified/consumed it.
    context = dict(context)
    context.pop(_STARTED, None)
    context.pop(_ORDER, None)
    return context


def callback_cleanup(provider):
    def decorate(view):
        @wraps(view)
        def wrapped(*args, **kwargs):
            prune()
            try:
                return view(*args, **kwargs)
            finally:
                # Success, provider denial, failed token exchange and exceptions
                # all consume only this attempt; other valid tabs remain pending.
                discard(provider, request.args.get("state", "").strip())
                prune()
        return wrapped
    return decorate
