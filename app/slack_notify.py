"""Post homeroom updates to a Slack channel, next to the mail.

Off unless SLACK_BOT_TOKEN and SLACK_CHANNEL are set. The message is a real
Markdown block, so the summary the model wrote renders as written; a failure
here is logged, never raised: the mail has already gone out and the post is
already recorded, so there is nothing to retry.
"""

from __future__ import annotations

import logging
from pathlib import Path

import requests

from config import Settings

logger = logging.getLogger(__name__)

SLACK_API = "https://slack.com/api/chat.postMessage"
MAX_BLOCK_CHARS = 12000


def enabled(settings: Settings) -> bool:
    return bool(settings.slack_bot_token and settings.slack_channel)


def mention_line(settings: Settings) -> str:
    return " ".join(f"<@{uid}>" for uid in settings.slack_mentions)


def post_markdown(settings: Settings, markdown: str, fallback: str) -> None:
    """One message: `markdown` rendered as a Markdown block, `fallback` for notifications."""
    if not enabled(settings):
        return
    response = requests.post(
        SLACK_API,
        headers={"Authorization": f"Bearer {settings.slack_bot_token}"},
        json={
            "channel": settings.slack_channel,
            "text": fallback,
            "blocks": [{"type": "markdown", "text": markdown[:MAX_BLOCK_CHARS]}],
            "unfurl_links": False,
        },
        timeout=settings.http_timeout,
    )
    payload = response.json()
    if not payload.get("ok"):
        raise RuntimeError(f"Slack refused the message: {payload.get('error', response.text)}")


def notify_post(settings: Settings, header: str, summary: str, translations: dict[str, str]) -> None:
    """A new homeroom post: mention the parents, then the summary in their language."""
    # Parents read Chinese first; fall back to whatever translation exists, then English.
    body = translations.get("Chinese") or next(iter(translations.values()), summary)
    text = "\n\n".join(
        part for part in (
            f"{mention_line(settings)} 📢 **New homeroom post**".strip(),
            f"_{header}_",
            body,
            f"[View on Schoology]({settings.homeroom_course_url})" if settings.homeroom_course_url else "",
        ) if part
    )
    _safe(settings, text, f"New homeroom post — {header}")


def notify_materials(settings: Settings, new: list, updated: list) -> None:
    """Changed course materials: titles only, the files are in the mail and the archive."""
    if not new and not updated:
        return
    lines = [f"{mention_line(settings)} 📚 **Course materials updated**".strip()]
    for heading, changes in (("New", new), ("Updated", updated)):
        if changes:
            lines.append(f"\n**{heading}**")
            lines.extend(f"- {c.folder} — {c.title}" for c in changes)
    if settings.homeroom_course_url:
        lines.append(f"\n[View on Schoology]({settings.homeroom_course_url})")
    _safe(settings, "\n".join(lines), f"Course materials: {len(new)} new, {len(updated)} updated")


FILES_PER_MESSAGE = 10


def notify_photos(settings: Settings, person: str, count: int, albums: list[str],
                  files: list[Path]) -> None:
    """New photos of one child, the photos themselves, with the parents mentioned.
    A big batch goes out as several messages; only the first carries the mention."""
    comment = (
        f"{mention_line(settings)} 📷 {count} new photo{'s' if count != 1 else ''} of "
        f"{person.title()} in {', '.join(albums)}"
    ).strip()
    title = f"{person.title()} — {', '.join(albums)}"
    try:
        for start in range(0, len(files), FILES_PER_MESSAGE):
            batch = files[start:start + FILES_PER_MESSAGE]
            upload_files(settings, [(f, title) for f in batch],
                         comment if start == 0 else f"({start + 1}–{start + len(batch)} / {count})")
    except Exception:  # noqa: BLE001 - see the module docstring
        logger.exception("Could not upload the photos to Slack")


def upload_file(settings: Settings, path: Path, title: str, comment: str) -> None:
    upload_files(settings, [(path, title)], comment)


def upload_files(settings: Settings, files: list[tuple[Path, str]], comment: str) -> None:
    """The three-step external upload, for one message holding all the files:
    get a URL per file, PUT the bytes, then complete them together."""
    if not enabled(settings):
        return
    headers = {"Authorization": f"Bearer {settings.slack_bot_token}"}
    uploaded = []
    for path, title in files:
        data = path.read_bytes()
        ticket = requests.post(
            "https://slack.com/api/files.getUploadURLExternal", headers=headers,
            data={"filename": path.name, "length": len(data)}, timeout=settings.http_timeout,
        ).json()
        if not ticket.get("ok"):
            raise RuntimeError(f"Slack refused the upload: {ticket.get('error')}")
        requests.post(ticket["upload_url"], data=data, timeout=settings.http_timeout * 4
                      ).raise_for_status()
        uploaded.append({"id": ticket["file_id"], "title": title})
    done = requests.post(
        "https://slack.com/api/files.completeUploadExternal", headers=headers,
        json={"files": uploaded, "channel_id": settings.slack_channel,
              "initial_comment": comment},
        timeout=settings.http_timeout,
    ).json()
    if not done.get("ok"):
        raise RuntimeError(f"Slack refused to complete the upload: {done.get('error')}")


def _safe(settings: Settings, markdown: str, fallback: str) -> None:
    try:
        post_markdown(settings, markdown, fallback)
    except Exception:  # noqa: BLE001 - see the module docstring
        logger.exception("Could not post to Slack")
