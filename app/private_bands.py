"""Server-side support for owner-only private bands on WRITE paths.

Read paths go through ``app.content_utils.content_for_viewer``. This module
covers the other half: a non-owner (ACL editor, public-edit contributor,
suggester, curator agent) only ever sees the band-stripped text, so their
submission must be 3-way merged back into the owner's full content. That way
an editor can neither see nor delete the owner's bands.

    base   = strip_private_bands(current_full)   # what the editor was shown
    ours   = current_full                        # owner's content incl. bands
    theirs = submitted                           # editor's new text

``git merge-file -p`` produces the merged text (exit 0) or reports a conflict
(exit > 0). On conflict callers must NOT write; the web/API paths save the
submission as a suggestion (Proposal) instead and answer 409.
"""

import os
import subprocess
import tempfile

from app.content_utils import count_private_bands, has_private_bands, strip_private_bands


class PrivateBandConflict(Exception):
    """A non-owner edit overlaps the owner's private bands and cannot be
    merged automatically. ``diff3`` holds the conflicted text (owner-only)."""

    def __init__(self, diff3=None):
        super().__init__("conflict with owner-only private sections")
        self.diff3 = diff3


def _git_merge_file(ours, base, theirs, diff3=False):
    with tempfile.TemporaryDirectory(prefix="wikihub-band-merge-") as tmp:
        paths = {}
        for name, text in (("ours", ours), ("base", base), ("theirs", theirs)):
            path = os.path.join(tmp, name)
            with open(path, "w", encoding="utf-8", newline="") as handle:
                handle.write(text)
            paths[name] = path
        cmd = ["git", "merge-file", "-p"]
        if diff3:
            cmd.append("--diff3")
        cmd += ["-L", "owner", "-L", "base", "-L", "edit", paths["ours"], paths["base"], paths["theirs"]]
        result = subprocess.run(cmd, capture_output=True, check=False)
    if result.returncode < 0 or result.returncode > 127:
        raise RuntimeError(f"git merge-file failed: {result.stderr.decode(errors='replace')}")
    return result.returncode == 0, result.stdout.decode("utf-8")


def _visible_signature(text):
    """Whitespace-insensitive form of the band-stripped text."""
    return "".join(strip_private_bands(text or "").split())


def merge_into_bands(current_full, submitted, base=None, normalize=None):
    """Merge a non-owner submission into the owner's full content.

    ``base`` is what the submitter was shown (defaults to the band-stripped
    current content). ``normalize`` is the transformation the caller already
    applied to ``submitted`` (e.g. ``set_visibility_in_content``, which
    re-serializes frontmatter and trims the body); it is applied to the base
    and to the owner's content too so the merge only sees real edits.
    Returns the content to store. Raises ``PrivateBandConflict`` when the edit
    cannot be merged cleanly OR when the merged result would expose band text
    (e.g. a contributor adds an unclosed code fence above the owner's markers,
    which would turn the markers into literal code and publish the bands).

    Submissions that themselves contain new private markers are kept as-is
    (a contributor hiding their own text from the public is harmless; the
    owner sees it badged). Markers are never stripped from submissions.
    """
    if current_full is None or not has_private_bands(current_full):
        return submitted
    if base is None:
        base = strip_private_bands(current_full)
    ours = current_full
    if normalize is not None:
        base = normalize(base)
        ours = normalize(current_full)
    if submitted == base:
        return ours
    clean, merged = _git_merge_file(ours, base, submitted)
    if not clean:
        _, conflicted = _git_merge_file(ours, base, submitted, diff3=True)
        raise PrivateBandConflict(conflicted)
    # Integrity: everything that was private must still be private. The
    # visible text of the merge must equal the visible text the contributor
    # submitted, and no owner band may have been deactivated.
    if (
        _visible_signature(merged) != _visible_signature(submitted)
        or count_private_bands(merged) < count_private_bands(ours)
    ):
        _, conflicted = _git_merge_file(ours, base, submitted, diff3=True)
        raise PrivateBandConflict(conflicted)
    return merged


def create_conflict_proposal(*, wiki, page, file_path, base_content, proposed_content,
                             author_id=None, author_name=None, note=None):
    """Save a non-owner's unmergeable edit as a pending suggestion so nothing
    is lost. ``base_content`` must be the band-stripped text the author saw.
    Caller commits."""
    from app import db
    from app.models import Proposal, ProposalComment, ProposalPagePatch, ProposalRevision

    proposal = Proposal(
        wiki_id=wiki.id,
        page_id=page.id if page else None,
        page_path=file_path,
        author_id=author_id,
        author_name=author_name or "anonymous",
        title=f"Edit to {getattr(page, 'title', None) or file_path} (needs owner merge)",
        status="pending",
        base_content_hash=getattr(page, "content_hash", None),
    )
    db.session.add(proposal)
    db.session.flush()
    revision = ProposalRevision(proposal_id=proposal.id, revision_number=1, note=note)
    db.session.add(revision)
    db.session.flush()
    db.session.add(ProposalPagePatch(
        revision_id=revision.id,
        page_path=file_path,
        base_content_hash=getattr(page, "content_hash", None),
        base_content=base_content,
        proposed_content=proposed_content,
    ))
    db.session.add(ProposalComment(
        proposal_id=proposal.id,
        author_id=author_id,
        author_name=author_name or "anonymous",
        body=note or "This page has private sections the owner must merge; the edit was saved as a suggestion.",
        event="submitted",
    ))
    return proposal


CONFLICT_MESSAGE = (
    "This page has private sections the owner must merge. "
    "Your change was saved as a suggestion."
)
