#!/usr/bin/env python3
"""Mail a translated AI summary of every new Schoology homeroom post.

One run: restore (or create) a session, read the course feed, and for each post
that has not been mailed before, summarise it, translate it, send it, and record
that it went out -- in that order, one post at a time, so an interrupted run
never re-sends and never silently drops a post.
"""

from __future__ import annotations

import argparse
from datetime import datetime
import logging
import sys
import traceback
from pathlib import Path

import archive
import materials
from auth import SchoologyAuth
from config import ConfigError, Settings
from mailer import notify_error, send_email
import faces
import slack_notify
from schoology import Post, SchoologyClient
from schoology_api import SchoologyAPI
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
    translated = [
        translate(summary, language, settings.translation_model)
        for language in settings.translation_languages
    ]
    summaries = [markdown_to_html(text) for text in [summary, *translated]]

    archive.write_post(
        settings.posts_dir, post, summary,
        dict(zip(settings.translation_languages, translated)),
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

    # Slack comes last: the post is recorded either way, so a Slack hiccup is
    # logged rather than allowed to cause a second mail.
    slack_notify.notify_post(
        settings, header, summary, dict(zip(settings.translation_languages, translated))
    )


def build_materials_html(
    settings: Settings,
    report: materials.SyncReport,
    albums: materials.SyncReport | None = None,
) -> str:
    parts = [f'<a href="{settings.homeroom_course_url}">View the course on Schoology</a>']

    if report.baseline:
        parts.append(
            f"<p>Mirrored {report.items_seen} items "
            f"({report.files_downloaded} files, "
            f"{report.bytes_downloaded / 1024 / 1024:.0f} MB) to establish a baseline. "
            "From now on only changes are mailed.</p>"
        )

    for heading, changes in (("New", report.new), ("Updated", report.updated)):
        if not changes:
            continue
        parts.append(f"<h3>{heading}</h3><ul>")
        for change in changes:
            size = f" &middot; {change.size / 1024:.0f} KB" if change.size else ""
            parts.append(f"<li><b>{change.folder}</b> &mdash; {change.title}{size}</li>")
        parts.append("</ul>")

    errors = list(report.errors) + list(albums.errors if albums else [])
    if errors:
        parts.append("<h3>Could not sync</h3><ul>")
        parts.extend(f"<li>{e}</li>" for e in errors)
        parts.append("</ul>")

    return "\n".join(parts)


SHEET_FROM = 4  # this many new photos or more go out as one contact sheet


def find_people(settings: Settings, album_report: materials.SyncReport) -> dict[str, int]:
    """Look for every learned person in the photos this sync brought in, and post
    a contact sheet of the hits to Slack. Returns {person: hits}."""
    index = faces.FaceIndex(settings.data_dir)
    people = index.people()
    new_photos = [
        c.path for c in album_report.changes
        if c.path and c.path.suffix.lower() in faces.IMAGE_SUFFIXES
    ]
    if not people or not new_photos:
        return {}
    index.scan(new_photos)
    hits = {}
    for person in people:
        found = index.match(person, settings.face_threshold, new_photos,
                            settings.face_min_size, settings.face_min_prominence)
        hits[person] = len(found)
        if not found:
            continue
        out = settings.data_dir / ".faces" / "sheets"
        stamp = f"{person}-{datetime.now():%Y%m%d-%H%M}"
        if len(found) <= SHEET_FROM:
            # A few photos are worth seeing full size, reframed where the child
            # was in the background; more than that reads better as one sheet.
            files = []
            for i, m in enumerate(found, 1):
                out.mkdir(parents=True, exist_ok=True)
                files.append(out / f"{stamp}-{i}.jpg")
                faces.render(settings.data_dir, m, 1600).save(files[-1], "JPEG", quality=85)
        else:
            files = [faces.contact_sheet(settings.data_dir, found, out / f"{stamp}.jpg",
                                         label=False, crop_face=False)]
        albums = sorted({Path(m.path).parent.name for m in found})
        slack_notify.notify_photos(settings, person, len(found), albums, files)
    logger.info("Faces: %s", ", ".join(f"{k}={v}" for k, v in hits.items()))
    return hits


def sync_materials(
    settings: Settings, state: State, dry_run: bool = False, no_bcc: bool = False
) -> None:
    """Mirror the course materials and mail whatever changed since last time."""
    api = SchoologyAPI(
        settings.api_consumer_key, settings.api_consumer_secret, settings.http_timeout
    )
    section_id = settings.course_id or state.course_id
    report = materials.sync(
        api,
        section_id,
        settings.materials_dir,
        state.materials,
        settings.materials_skip_folders,
        settings.max_material_mb,
        dry_run,
        settings.base_url,
    )
    album_report = materials.SyncReport()
    if settings.sync_albums:
        album_report = materials.sync_albums(
            api, section_id, settings.albums_dir, state.materials,
            settings.max_album_mb, dry_run, settings.album_originals,
        )
        logger.info(
            "Albums: %d items, %d new, %d downloaded (%.0f MB)",
            album_report.items_seen, len(album_report.new),
            album_report.files_downloaded, album_report.bytes_downloaded / 1024 / 1024,
        )
        report.items_seen += album_report.items_seen
        report.files_downloaded += album_report.files_downloaded
        report.bytes_downloaded += album_report.bytes_downloaded
        if not dry_run:
            try:
                find_people(settings, album_report)
            except Exception:
                logger.exception("Face matching failed")
    logger.info(
        "Materials: %d items, %d new, %d updated, %d downloaded (%.0f MB)",
        report.items_seen, len(report.new), len(report.updated),
        report.files_downloaded, report.bytes_downloaded / 1024 / 1024,
    )

    # Photos are archived to disk but never mailed, so they must still be recorded
    # here: without this the next run re-downloads every one of them.
    if album_report.changes and not dry_run:
        state.save()

    if not report.changes:
        logger.info("No material changes to report")
        return

    if dry_run:
        logger.info(
            "[dry run] Would mail %d material changes and %d photos",
            len(report.changes), len(album_report.changes),
        )
        return

    if report.baseline:
        # 310MB of coursework and 1,100 photos are a mirror, not a mail:
        # summarise them and attach nothing.
        subject = f"{settings.homeroom_class} Materials baseline".strip()
        attachments = []
    else:
        subject = (
            f"{settings.homeroom_class} Materials: "
            f"{len(report.new)} new, {len(report.updated)} updated"
        ).strip()
        attachments, _ = _materials_attachments(report, settings.max_email_attachment_mb)

    send_email(
        settings,
        subject,
        build_materials_html(settings, report, album_report),
        attachments,
        bcc=[] if no_bcc else None,
    )
    state.save()
    if not report.baseline:
        slack_notify.notify_materials(settings, report.new, report.updated)


def _materials_attachments(report: materials.SyncReport, budget_mb: float):
    paths, remaining = [], budget_mb * 1024 * 1024
    for change in report.changes:
        if change.path is None or not change.path.exists():
            continue
        size = change.path.stat().st_size
        if size > remaining:
            continue
        remaining -= size
        paths.append(change.path)
    return paths, remaining


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

    if not dry_run:
        # Backfill the archive for posts that were mailed before it existed. The
        # bodies are already in hand, so this costs nothing but a file write.
        for post in posts:
            if not archive.post_path(settings.posts_dir, post).exists():
                client.expand(post)
                archive.write_post(settings.posts_dir, post)

    failures = []
    for post in new_posts:
        try:
            handle_post(settings, client, post, state, dry_run, no_bcc)
        except Exception:
            logger.exception("Post %s failed", post.post_id)
            failures.append(f"Post {post.post_id} ({post.datetime_text}):\n{traceback.format_exc()}")

    if settings.materials_enabled:
        try:
            sync_materials(settings, state, dry_run, no_bcc)
        except Exception:
            logger.exception("Materials sync failed")
            failures.append(f"Materials sync:\n{traceback.format_exc()}")
    else:
        logger.info("Materials sync is off (no SCHOOLOGY_API_CONSUMER_KEY/SECRET)")

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
