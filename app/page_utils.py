def is_wikihub_plumbing_path(path):
    normalized = (path or "").strip().replace("\\", "/")
    return normalized == ".wikihub" or normalized.startswith(".wikihub/")


def is_content_page_path(path):
    return not is_wikihub_plumbing_path(path)
