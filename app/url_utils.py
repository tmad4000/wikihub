from urllib.parse import quote


def url_param_from_page_path(page_path, strip_md=True):
    """Convert a repo/page path into a URL param (no quoting)."""
    if not page_path:
        return ""
    path = page_path.replace("\\", "/")
    if strip_md and path.endswith(".md"):
        path = path[:-3]
    return path.replace(" ", "_")


def url_path_from_page_path(page_path, strip_md=True):
    """Convert a repo/page path into a URL-safe path segment.

    - spaces -> underscores
    - optional .md stripping
    - percent-encode other unsafe characters
    """
    path = url_param_from_page_path(page_path, strip_md=strip_md)
    return quote(path, safe="/")


def page_path_from_url_path(url_path):
    """Convert a URL path segment back to a repo/page path."""
    if not url_path:
        return ""
    return url_path.replace("_", " ")
