import posixpath


def normalize_repo_path(path):
    normalized = (path or "").strip().replace("\\", "/")
    if not normalized:
        return ""
    normalized = posixpath.normpath(normalized)
    if normalized == ".":
        return ""
    return normalized.lstrip("/")


def is_wikihub_plumbing_path(path):
    normalized = normalize_repo_path(path)
    return normalized == ".wikihub" or normalized.startswith(".wikihub/")


def is_content_page_path(path):
    return not is_wikihub_plumbing_path(path)
