"""Activity feed helpers — shared by the global /activity page, /activity.rss,
and per-wiki /@owner/slug/activity.rss.

Visibility is enforced by the CALLERS at the query level (see routes). This
module is pure formatting: turning Page rows into activity entries and RSS XML.
"""
from datetime import datetime, timezone
from email.utils import format_datetime
from xml.sax.saxutils import escape

from app.url_utils import url_path_from_page_path


def relative_time(dt):
    """Human 'x ago' string. Returns '' for None."""
    if dt is None:
        return ""
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    seconds = (datetime.now(timezone.utc) - dt).total_seconds()
    if seconds < 0:
        seconds = 0
    if seconds < 60:
        return "just now"
    minute, hour, day, week, month, year = 60, 3600, 86400, 604800, 2592000, 31536000
    if seconds < hour:
        n = int(seconds // minute); return f"{n} minute{'s' if n != 1 else ''} ago"
    if seconds < day:
        n = int(seconds // hour); return f"{n} hour{'s' if n != 1 else ''} ago"
    if seconds < week:
        n = int(seconds // day); return f"{n} day{'s' if n != 1 else ''} ago"
    if seconds < month:
        n = int(seconds // week); return f"{n} week{'s' if n != 1 else ''} ago"
    if seconds < year:
        n = int(seconds // month); return f"{n} month{'s' if n != 1 else ''} ago"
    n = int(seconds // year)
    return f"{n} year{'s' if n != 1 else ''} ago"

# How much clock skew (seconds) between created_at and updated_at still counts
# as a "created" event rather than an "updated" one. Page create + initial
# index write can land a few ms apart.
_CREATE_WINDOW_SECONDS = 5


def event_type_for_page(page):
    """'created' if the page has never been meaningfully updated, else 'updated'."""
    if page.created_at is None or page.updated_at is None:
        return "updated"
    delta = (page.updated_at - page.created_at).total_seconds()
    return "created" if abs(delta) <= _CREATE_WINDOW_SECONDS else "updated"


def author_for_page(page):
    """Displayable author, or None. Anonymous pages never expose their author."""
    if getattr(page, "anonymous", False):
        return None
    author = (page.author or "").strip()
    return author or None


def page_relative_url(owner_username, wiki_slug, page_path):
    """Site-relative URL to a page (no host)."""
    return f"/@{owner_username}/{wiki_slug}/{url_path_from_page_path(page_path, strip_md=True)}"


def activity_entry(page, wiki, owner_username, host_url):
    """Build a dict describing one activity event for templates + RSS.

    host_url should be request.host_url (trailing slash ok).
    """
    rel = page_relative_url(owner_username, wiki_slug=wiki.slug, page_path=page.path)
    title = (page.title or "").strip() or page.path
    return {
        "page_title": title,
        "wiki_title": (wiki.title or wiki.slug),
        "wiki_slug": wiki.slug,
        "owner": owner_username,
        "event": event_type_for_page(page),
        "author": author_for_page(page),
        "timestamp": page.updated_at,
        "relative": relative_time(page.updated_at),
        "rel_url": rel,
        "abs_url": host_url.rstrip("/") + rel,
    }


def _rss_date(dt):
    if dt is None:
        return ""
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return format_datetime(dt.astimezone(timezone.utc))


def render_rss(feed_title, feed_link, self_url, description, entries):
    """Render a well-formed RSS 2.0 document.

    entries: list of dicts from activity_entry(). Uses abs_url as guid+link.
    """
    parts = [
        '<?xml version="1.0" encoding="UTF-8"?>',
        '<rss version="2.0" xmlns:atom="http://www.w3.org/2005/Atom">',
        "<channel>",
        f"<title>{escape(feed_title)}</title>",
        f"<link>{escape(feed_link)}</link>",
        f"<description>{escape(description)}</description>",
        f'<atom:link href="{escape(self_url)}" rel="self" type="application/rss+xml" />',
    ]
    for e in entries:
        verb = "created" if e["event"] == "created" else "updated"
        item_title = f"{e['page_title']} ({verb} in {e['wiki_title']})"
        desc_bits = [f"{verb.capitalize()} in {e['wiki_title']}"]
        if e.get("author"):
            desc_bits.append(f"by {e['author']}")
        parts.extend(
            [
                "<item>",
                f"<title>{escape(item_title)}</title>",
                f"<link>{escape(e['abs_url'])}</link>",
                f'<guid isPermaLink="false">{escape(e["abs_url"])}#{verb}-{_rss_date(e["timestamp"])}</guid>',
                f"<pubDate>{_rss_date(e['timestamp'])}</pubDate>",
                f"<description>{escape(' '.join(desc_bits))}</description>",
                "</item>",
            ]
        )
    parts.extend(["</channel>", "</rss>"])
    return "\n".join(parts)
