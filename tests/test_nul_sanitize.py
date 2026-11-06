"""PostgreSQL text/jsonb reject NUL; indexing must drop it (img_8938-jpeg / admitsphere reindex failures)."""
from app.wiki_ops import _pg_text, _sanitize_for_json


def test_pg_text_strips_nul():
    assert _pg_text("a\x00b") == "ab"
    assert _pg_text(None) is None


def test_frontmatter_strings_are_sanitized():
    assert _sanitize_for_json({"title": "x\x00y", "tags": ["a\x00", 3]}) == {"title": "xy", "tags": ["a", 3]}
