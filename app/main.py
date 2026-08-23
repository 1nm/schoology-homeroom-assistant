#!/usr/bin/env python3
"""Mail a translated AI summary of every new Schoology homeroom post.

One run: restore (or create) a session, read the course feed, and for each post
that has not been mailed before, summarise it, translate it, send it, and record
that it went out -- in that order, one post at a time, so an interrupted run
never re-sends and never silently drops a post.
"""

from __future__ import annotations

import argparse
import logging
import sys
import traceback
from pathlib import Path

from auth import SchoologyAuth
from config import ConfigError, Settings
from mailer import notify_error, send_email
from schoology import Post, SchoologyClient
from state import State
from summarize import markdown_to_html, summarize, translate

logger = logging.getLogger("schoology")


def collect_attachments(post: Post, budget_mb: float) -> tuple[list[Path], str]:
    """Files to attach to the mail, and the text pulled out of them for the summary."""
    paths: list[Path] = []
    texts: list[str] = []
    remaining = budget_mb * 1024 * 1024

    for attachment in post.attachments:
        if attachment.text:
            texts.append(attachment.text)
        if attachment.path is None:
            continue
        size = attachment.path.stat().st_size
        if size > remaining:
            logger.info("Not attaching %s, the mail's size budget is used up", attachment.path)
            continue
        remaining -= size
        paths.append(attachment.path)

    return paths, "\n".join(texts)


def build_email_html(settings: Settings, post: Post, header: str, summaries: list[str]) -> str:
    parts = [
        f'<a href="{settings.homeroom_course_url}">View updates on Schoology</a>',
        "<br/><br/>",
        header,
        "<br/><br/>",
        post.html_content,
    ]
    for summary_html in summaries:
        parts.extend(["<hr/>", summary_html])
    return "\n".join(parts)


def handle_post(
    settings: Settings,
    client: SchoologyClient,
    post: Post,
    state: State,
    dry_run: bool = False,
    no_bcc: bool = False,
) -> None:
    client.expand(post)
    client.download_attachments(
        post,
        settings.attachments_dir / post.post_id,
        settings.max_attachment_mb,
        settings.max_pdf_mb,
    )

    posted_at = post.posted_at
    header = f"On {posted_at:%b %d, %Y at %I:%M %p}, {post.author} posted:"
    attachment_paths, attachment_text = collect_attachments(post, settings.max_email_attachment_mb)

    if dry_run:
        logger.info(
            "[dry run] Would summarise and send post %s: %s | %d chars | %d attachments",
            post.post_id, header, len(post.content), len(attachment_paths),
        )
        return

    summary = summarize(
        f"{header}\n\n{post.content}\n\n{attachment_text}",
        settings.summary_model,
        posted_on=f"{posted_at:%A, %B %d, %Y}",
    )
    summaries = [markdown_to_html(summary)]
    for language in settings.translation_languages:
        summaries.append(
            markdown_to_html(translate(summary, language, settings.translation_model))
        )

    subject = f"{settings.homeroom_class} Homeroom Updates {posted_at:%Y%m%d}".strip()
    send_email(
        settings,
        subject,
        build_email_html(settings, post, header, summaries),
        attachment_paths,
        bcc=[] if no_bcc else None,
    )

    # Recorded only after the mail is actually accepted by the SMTP server, and
    # flushed immediately so a later failure cannot cause a re-send.
    state.mark_sent(post.post_id, subject, posted_at.isoformat(timespec="minutes"))
    state.save()


def run(
    settings: Settings, auth: SchoologyAuth, dry_run: bool = False, no_bcc: bool = False
) -> int:
    state = State.load(settings.state_file)

    auth.course_id = settings.course_id or state.course_id
    session = auth.session()

    course_id = auth.course_id
    if not course_id:
        raise RuntimeError("No course id: set SCHOOLOGY_COURSE_ID or HOMEROOM_COURSE_URL")
    if state.course_id != course_id:
        state.course_id = course_id
        state.save()

    client = SchoologyClient(session, settings.base_url, settings.http_timeout)
    client.enter_course_as_parent(course_id, settings.child_uid)
    posts = client.parse_posts(client.fetch_feed(course_id))
    new_posts = [p for p in reversed(posts) if not state.is_sent(p.post_id)]
    logger.info("%d posts in the feed, %d not sent yet", len(posts), len(new_posts))

    failures = []
    for post in new_posts:
        try:
            handle_post(settings, client, post, state, dry_run, no_bcc)
        except Exception:
            logger.exception("Post %s failed", post.post_id)
            failures.append(f"Post {post.post_id} ({post.datetime_text}):\n{traceback.format_exc()}")

    if failures:
        # A dry run must stay silent: no mail of any kind, error mail included.
        if not dry_run:
            notify_error(settings, "\n\n".join(failures))
        return 1
    return 0


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Sign in, read the feed and fetch attachments, but send nothing, "
             "call no models, and record nothing. Safe to run at any time.",
    )
    parser.add_argument(
        "--no-bcc",
        action="store_true",
        help="Mail only SUMMARY_RECEIVER_EMAIL, ignoring BCC_EMAILS entirely.",
    )
    parser.add_argument("-v", "--verbose", action="store_true", help="Log at DEBUG level")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    logging.basicConfig(
        format="%(asctime)s - %(levelname)s - %(name)s: %(message)s",
        level=logging.DEBUG if args.verbose else logging.INFO,
    )

    try:
        settings = Settings.load()
    except ConfigError as exc:
        logger.error("Configuration error: %s", exc)
        return 2

    auth = SchoologyAuth(settings)
    try:
        if settings.bcc_emails and not args.no_bcc:
            logger.info("BCC is on: %d addresses", len(settings.bcc_emails))
        return run(settings, auth, dry_run=args.dry_run, no_bcc=args.no_bcc)
    except Exception:
        message = traceback.format_exc()
        logger.error("Run failed:\n%s", message)
        screenshots = [auth.screenshot] if auth.screenshot else []
        notify_error(settings, message, screenshots)
        return 1


if __name__ == "__main__":
    sys.exit(main())
