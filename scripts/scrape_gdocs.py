#!/usr/bin/env python3 -u
"""
Scrape Google Docs linked from Jeremy Nixon's wikihub wiki and inline their content.

For each page that contains a Google Docs link:
1. Export the doc as plain text via /export?format=txt
2. Update the wiki page to include the full doc content below the stub
3. Keep the original link as a source reference

Idempotent: skips pages that already have inlined content (marker comment present).
"""

import os
import re
import sys
import time
import requests
from urllib.parse import unquote

WIKIHUB_BASE = "https://wikihub.md"
API_KEY = "wh_PSINkt8oJZuM9CbuRlXjRho6vJZ-PFIyTTsHy8ji2eY"
WIKI_OWNER = "jeremynixon"
WIKI_SLUG = "thinking"

HEADERS = {
    "Authorization": f"Bearer {API_KEY}",
    "Content-Type": "application/json",
}

INLINE_MARKER = "<!-- gdoc-inlined -->"
DRY_RUN = False

GDOC_URL_RE = re.compile(
    r'https://docs\.google\.com/document/d/([a-zA-Z0-9_-]+)'
)


def log(msg):
    print(msg, flush=True)


def get_all_pages():
    """Fetch all page paths from the wiki."""
    resp = requests.get(
        f"{WIKIHUB_BASE}/api/v1/wikis/{WIKI_OWNER}/{WIKI_SLUG}/pages",
        headers=HEADERS,
    )
    resp.raise_for_status()
    return resp.json()["pages"]


def get_page_content(page_path):
    """Fetch markdown content for a single page, with retry."""
    for attempt in range(3):
        try:
            resp = requests.get(
                f"{WIKIHUB_BASE}/api/v1/wikis/{WIKI_OWNER}/{WIKI_SLUG}/pages/{page_path}",
                headers=HEADERS,
                timeout=15,
            )
        except requests.RequestException:
            time.sleep(3)
            continue
        if resp.status_code == 404:
            return None
        if resp.status_code >= 500:
            time.sleep(3)
            continue
        resp.raise_for_status()
        return resp.json()
    return None


def update_page(page_path, content):
    """Update a page's content via PUT, with retry on rate limit and errors."""
    for attempt in range(5):
        try:
            resp = requests.put(
                f"{WIKIHUB_BASE}/api/v1/wikis/{WIKI_OWNER}/{WIKI_SLUG}/pages/{page_path}",
                headers=HEADERS,
                json={"content": content},
                timeout=30,
            )
        except requests.RequestException as e:
            wait = 5 * (attempt + 1)
            log(f"  connection error, retrying in {wait}s...")
            time.sleep(wait)
            continue
        if resp.status_code == 429:
            wait = int(resp.headers.get("Retry-After", 8))
            log(f"  rate limited, waiting {wait}s...")
            time.sleep(wait)
            continue
        resp.raise_for_status()
        return resp.json()
    raise RuntimeError(f"failed after 5 retries for {page_path}")


def export_gdoc_text(doc_id):
    """Try to export a Google Doc as plain text. Returns (text, error)."""
    url = f"https://docs.google.com/document/d/{doc_id}/export?format=txt"
    try:
        resp = requests.get(url, timeout=15)
        if resp.status_code == 200:
            text = resp.text.strip()
            # Google sometimes returns HTML error pages even with 200
            if text.startswith("<!DOCTYPE") or text.startswith("<html"):
                return None, "got HTML instead of text (likely private)"
            return text, None
        elif resp.status_code in (401, 403):
            return None, "private document"
        else:
            return None, f"HTTP {resp.status_code}"
    except requests.RequestException as e:
        return None, str(e)


def extract_gdoc_urls(markdown):
    """Extract all Google Doc URLs and their doc IDs from markdown."""
    results = []
    for match in GDOC_URL_RE.finditer(markdown):
        doc_id = match.group(1)
        full_url = match.group(0)
        # Find the full URL (may have /edit or /view suffix)
        end = match.end()
        rest = markdown[end:]
        suffix_match = re.match(r'[^\s\)]*', rest)
        if suffix_match:
            full_url += suffix_match.group(0)
        results.append((doc_id, full_url))
    return results


def build_updated_content(original_content, doc_text, doc_url):
    """Build the updated page content with inlined Google Doc text."""
    lines = []
    lines.append(original_content.rstrip())
    lines.append("")
    lines.append(INLINE_MARKER)
    lines.append("")
    lines.append("---")
    lines.append("")
    lines.append(doc_text)
    lines.append("")
    lines.append("---")
    lines.append("")
    lines.append(f"*Source: [Original Google Doc]({doc_url})*")
    lines.append("")
    return "\n".join(lines)


def build_private_note(original_content, doc_url):
    """Add a note that the doc is private."""
    lines = []
    lines.append(original_content.rstrip())
    lines.append("")
    lines.append(INLINE_MARKER)
    lines.append("")
    lines.append(f"> This document is private. [View on Google Docs]({doc_url})")
    lines.append("")
    return "\n".join(lines)


def main():
    global WIKIHUB_BASE, DRY_RUN, API_KEY, HEADERS
    args = sys.argv[1:]
    if "--local" in args:
        WIKIHUB_BASE = "http://localhost:5100"
        log("Running against LOCAL dev server")
        # Use API key from env if provided (prod key won't work locally)
        env_key = os.environ.get("WIKIHUB_API_KEY")
        if env_key:
            API_KEY = env_key
            HEADERS["Authorization"] = f"Bearer {API_KEY}"
            log(f"Using API key from env: {API_KEY[:11]}...")
    if "--dry-run" in args:
        DRY_RUN = True
        log("DRY RUN — no pages will be updated")

    log(f"Fetching pages from {WIKIHUB_BASE}...")
    pages = get_all_pages()
    log(f"Found {len(pages)} pages")

    stats = {
        "total": len(pages),
        "with_gdoc": 0,
        "already_inlined": 0,
        "fetched": 0,
        "private": 0,
        "errors": 0,
        "no_gdoc": 0,
        "updated": 0,
    }
    results = []

    pages_processed = 0  # count of pages with gdocs (for batch pausing)

    for i, page_info in enumerate(pages):
        path = page_info["path"]

        # Skip index pages
        if path in ("index.md",):
            continue

        log(f"Page {i+1}/{len(pages)}: checking {path}")
        time.sleep(2)  # rate limit wikihub API

        page = get_page_content(path)
        if not page:
            continue

        content = page.get("content", "")

        # Check if already inlined
        if INLINE_MARKER in content:
            stats["already_inlined"] += 1
            log(f"  → already inlined, skipping")
            continue

        # Extract Google Docs URLs
        gdoc_urls = extract_gdoc_urls(content)
        if not gdoc_urls:
            stats["no_gdoc"] += 1
            continue

        stats["with_gdoc"] += 1
        pages_processed += 1
        doc_id, doc_url = gdoc_urls[0]  # Use first Google Doc link

        # Batch pause every 20 pages to avoid sustained load
        if pages_processed > 1 and (pages_processed - 1) % 20 == 0:
            log(f"  ⏸ batch pause (processed {pages_processed - 1} gdoc pages), waiting 10s...")
            time.sleep(10)

        log(f"  fetching doc {doc_id[:16]}...")

        time.sleep(2)  # rate limit Google Docs API

        doc_text, error = export_gdoc_text(doc_id)

        if doc_text:
            new_content = build_updated_content(content, doc_text, doc_url)
            if DRY_RUN:
                stats["fetched"] += 1
                log(f"  ✓ would inline ({len(doc_text)} chars)")
            else:
                try:
                    update_page(path, new_content)
                    time.sleep(2)  # rate limit wikihub API
                    stats["fetched"] += 1
                    stats["updated"] += 1
                    results.append({"path": path, "status": "inlined", "doc_id": doc_id})
                    log(f"  ✓ inlined ({len(doc_text)} chars)")
                except Exception as e:
                    stats["errors"] += 1
                    results.append({"path": path, "status": "update_error", "error": str(e)})
                    log(f"  ✗ update failed: {e}")
        elif error and "private" in error.lower():
            new_content = build_private_note(content, doc_url)
            if DRY_RUN:
                stats["private"] += 1
                log(f"  ⊘ private doc (would mark)")
            else:
                try:
                    update_page(path, new_content)
                    time.sleep(2)  # rate limit wikihub API
                    stats["private"] += 1
                    stats["updated"] += 1
                    results.append({"path": path, "status": "private", "doc_id": doc_id})
                    log(f"  ⊘ private doc")
                except Exception as e:
                    stats["errors"] += 1
                    results.append({"path": path, "status": "update_error", "error": str(e)})
                    log(f"  ✗ update failed: {e}")
        else:
            stats["errors"] += 1
            results.append({"path": path, "status": "error", "error": error, "doc_id": doc_id})
            log(f"  ✗ {error}")

    log("")
    log("=" * 50)
    log("RESULTS")
    log("=" * 50)
    log(f"Total pages:      {stats['total']}")
    log(f"No Google Doc:    {stats['no_gdoc']}")
    log(f"Already inlined:  {stats['already_inlined']}")
    log(f"With Google Doc:  {stats['with_gdoc']}")
    log(f"  Fetched OK:     {stats['fetched']}")
    log(f"  Private:        {stats['private']}")
    log(f"  Errors:         {stats['errors']}")
    log(f"Pages updated:    {stats['updated']}")


if __name__ == "__main__":
    main()
