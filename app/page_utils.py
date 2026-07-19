import posixpath

from sqlalchemy import and_


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


def content_page_path_filter(path_column):
    return and_(
        path_column != ".wikihub",
        ~path_column.startswith(".wikihub/"),
        path_column != "./.wikihub",
        ~path_column.startswith("./.wikihub/"),
    )
