"""Git-backed CSV/TSV table rendering and published Google Sheets refresh."""

import csv
import io
import json
import re
from datetime import datetime, timezone
from urllib.parse import parse_qs, urlencode, urljoin, urlparse, urlunparse

import requests

from app.custom_domains import is_allowed_sheet_source
from app.git_sync import read_file_from_repo


DATA_SOURCES_PATH = ".wikihub/data-sources.json"
MAX_TABLE_ROWS = 5000
MAX_TABLE_COLUMNS = 100
MAX_SOURCE_BYTES = 2 * 1024 * 1024
MAX_REDIRECTS = 4
_SHEET_PATH_RE = re.compile(r"^/spreadsheets/d/(e/)?([^/]+)/")


class TableSourceError(ValueError):
    pass


def parse_delimited_bytes(data, extension):
    if len(data) > MAX_SOURCE_BYTES:
        raise TableSourceError("Table is larger than the 2 MB preview limit")
    try:
        text = data.decode("utf-8-sig")
    except UnicodeDecodeError as exc:
        raise TableSourceError("Table must be UTF-8 text") from exc
    delimiter = "\t" if extension.lower() == ".tsv" else ","
    try:
        rows = list(csv.reader(io.StringIO(text, newline=""), delimiter=delimiter))
    except csv.Error as exc:
        raise TableSourceError(f"Could not parse table: {exc}") from exc
    if not rows:
        return {"headers": [], "rows": [], "total_rows": 0, "truncated": False}
    # Published Sheets often include a title/link row before the actual header
    # (Body Masters is a real example). Pick the earliest of the fullest rows
    # in the first ten lines instead of blindly treating row one as headers.
    candidates = rows[:10]
    header_index = max(
        range(len(candidates)),
        key=lambda index: (sum(bool(value.strip()) for value in candidates[index]), -index),
    )
    width = min(max(len(row) for row in rows[header_index:]), MAX_TABLE_COLUMNS)
    raw_headers = rows[header_index][:width]
    headers = [value.strip() or f"Column {index + 1}" for index, value in enumerate(raw_headers)]
    if len(headers) < width:
        headers.extend(f"Column {index + 1}" for index in range(len(headers), width))
    data_rows = rows[header_index + 1:]
    preview_rows = [row[:width] + [""] * max(0, width - len(row)) for row in data_rows[:MAX_TABLE_ROWS]]
    return {
        "headers": headers,
        "rows": preview_rows,
        "total_rows": len(data_rows),
        "truncated": len(data_rows) > MAX_TABLE_ROWS,
        "header_row": header_index + 1,
    }


def load_data_sources(username, slug):
    raw = read_file_from_repo(username, slug, DATA_SOURCES_PATH, public=False)
    if not raw:
        return {}
    try:
        payload = json.loads(raw)
    except (TypeError, ValueError):
        return {}
    return payload if isinstance(payload, dict) else {}


def dump_data_sources(sources):
    return json.dumps(sources, indent=2, sort_keys=True, ensure_ascii=False) + "\n"


def normalize_sheet_csv_url(value):
    url = str(value or "").strip()
    if not is_allowed_sheet_source(url):
        raise TableSourceError("Use a public Google Sheets URL from docs.google.com")
    parsed = urlparse(url)
    match = _SHEET_PATH_RE.match(parsed.path)
    if not match:
        raise TableSourceError("That Google Sheets URL is not recognized")
    published, sheet_id = match.groups()
    query = parse_qs(parsed.query)
    fragment = parse_qs(parsed.fragment)
    gid = (query.get("gid") or fragment.get("gid") or [None])[0]
    if published:
        path = f"/spreadsheets/d/e/{sheet_id}/pub"
        output_query = {"output": "csv"}
        if gid:
            output_query["gid"] = gid
    else:
        path = f"/spreadsheets/d/{sheet_id}/export"
        output_query = {"format": "csv"}
        if gid:
            output_query["gid"] = gid
    return urlunparse(("https", "docs.google.com", path, "", urlencode(output_query), ""))


def _allowed_google_response_host(hostname):
    host = (hostname or "").lower().rstrip(".")
    return host == "docs.google.com" or host == "docs.googleusercontent.com" or host.endswith(".googleusercontent.com")


def fetch_sheet_csv(source_url, timeout=12):
    """Fetch a public sheet without allowing redirects outside Google hosts."""
    current = normalize_sheet_csv_url(source_url)
    for _ in range(MAX_REDIRECTS + 1):
        response = requests.get(
            current,
            timeout=timeout,
            allow_redirects=False,
            headers={"User-Agent": "WikiHub-Table-Refresh/1.0", "Accept": "text/csv,text/plain;q=0.9,*/*;q=0.1"},
        )
        if response.status_code in {301, 302, 303, 307, 308}:
            location = response.headers.get("Location")
            if not location:
                raise TableSourceError("Google returned an incomplete redirect")
            current = urljoin(current, location)
            if not _allowed_google_response_host(urlparse(current).hostname):
                raise TableSourceError("Google redirected to an unexpected host")
            continue
        try:
            response.raise_for_status()
        except requests.RequestException as exc:
            raise TableSourceError("The sheet could not be downloaded; make sure it is public") from exc
        data = response.content
        if len(data) > MAX_SOURCE_BYTES:
            raise TableSourceError("The sheet is larger than the 2 MB refresh limit")
        content_type = response.headers.get("Content-Type", "").lower()
        if "html" in content_type or data.lstrip().lower().startswith(b"<!doctype html"):
            raise TableSourceError("Google returned a sign-in page; publish the sheet or enable link access")
        # Parse once before persisting so a broken export cannot replace a good table.
        parse_delimited_bytes(data, ".csv")
        return data
    raise TableSourceError("Google redirected too many times")


def source_record(source_url, previous=None):
    record = dict(previous or {})
    record.update({
        "kind": "google-sheets",
        "source_url": str(source_url).strip(),
        "csv_url": normalize_sheet_csv_url(source_url),
        "updated_at": datetime.now(timezone.utc).isoformat(),
    })
    return record


def mark_source_synced(record):
    updated = dict(record)
    now = datetime.now(timezone.utc).isoformat()
    updated["last_synced_at"] = now
    updated["updated_at"] = now
    updated.pop("last_error", None)
    return updated
