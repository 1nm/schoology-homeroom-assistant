"""A plain-text archive of everything the mailer has seen.

The state file deliberately keeps only ids, so this is what gives the MCP server
something to read: one markdown file per post, with the teacher's original words
and, once it exists, the summary that was mailed out.
"""

from __future__ import annotations

import logging
from datetime import datetime
from pathlib import Path

from schoology import Post, _safe_filename

logger = logging.getLogger(__name__)


def post_path(root: Path, post: Post) -> Path:
    return root / _safe_filename(f"{post.posted_at:%Y-%m-%d}-{post.post_id}.md")


def write_post(
    root: Path,
    post: Post,
    summary: str = "",
    translations: dict[str, str] | None = None,
) -> Path:
    """Write (or enrich) one post's archive entry.

    Called for every post in the feed, not just the new ones, so that posts sent
    before the archive existed are backfilled on the next run. A later call with a
    summary replaces an earlier body-only file.
    """
    path = post_path(root, post)
    body = [
        "---",
        f"post_id: {post.post_id}",
        f"author: {post.author}",
        f"posted_at: {post.posted_at.isoformat(timespec='minutes')}",
        f"archived_at: {datetime.now().astimezone().isoformat(timespec='seconds')}",
        "---",
        "",
        f"# {post.author} — {post.posted_at:%b %d, %Y at %I:%M %p}",
        "",
        post.content,
    ]

    if post.attachments:
        body += ["", "## Attachments", ""]
        body += [f"- {a.filename or a.url}" for a in post.attachments]

    if summary:
        body += ["", "## AI summary", "", summary]
    for language, text in (translations or {}).items():
        body += ["", f"## AI summary ({language})", "", text]

    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(body), encoding="utf-8")
    return path


def has_summary(root: Path, post: Post) -> bool:
    path = post_path(root, post)
    return path.exists() and "## AI summary" in path.read_text(encoding="utf-8")
