"""Link extraction must ignore multi-line or oversized `[[...]]` captures.

`[[` inside code or prose used to be captured across newlines into a
kilobytes-long target_path, which exceeds the btree row limit of
ix_wikilinks_unresolved_target_path and aborted the whole write/reindex.
"""
from app.content_utils import MAX_WIKILINK_TARGET_BYTES, extract_wikilinks


def test_normal_links_are_extracted():
    content = "See [[notes/one]] and [[Two|label]] and [[three.md]]."
    assert extract_wikilinks(content) == ["notes/one", "Two", "three.md"]


def test_multiline_capture_is_ignored():
    content = "Use `[[note` in the composer.\n\nMore prose\nwith a later ]] closing, then [[real]]."
    assert extract_wikilinks(content) == ["real"]


def test_oversized_target_is_ignored():
    long_target = "x" * (MAX_WIKILINK_TARGET_BYTES + 1)
    assert extract_wikilinks(f"[[{long_target}]] [[ok]]") == ["ok"]
    at_limit = "y" * MAX_WIKILINK_TARGET_BYTES
    assert extract_wikilinks(f"[[{at_limit}]]") == [at_limit]


def test_limit_is_in_bytes_not_characters():
    # 4-byte characters: 300 of them is 1200 bytes, over the limit.
    assert extract_wikilinks("[[" + "\U0001F600" * 300 + "]]") == []


def test_nul_in_target_is_ignored():
    assert extract_wikilinks("[[bad\x00target]] [[good]]") == ["good"]
