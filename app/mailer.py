"""Sending the summary mails, and telling the operator when a run breaks."""

from __future__ import annotations

import hashlib
import json
import logging
import os
import smtplib
import time
from email import encoders
from email.mime.base import MIMEBase
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from pathlib import Path
from typing import Iterable, Sequence

from config import Settings

logger = logging.getLogger(__name__)


class EmailError(RuntimeError):
    """The message could not be handed to the SMTP server."""


def send_email(
    settings: Settings,
    subject: str,
    html_content: str,
    attachments: Sequence[Path] = (),
    bcc: Iterable[str] | None = None,
) -> None:
    """Send one HTML mail. Raises on failure: a post must not be recorded as
    delivered when it never left the machine."""
    bcc_emails = list(settings.bcc_emails if bcc is None else bcc)

    message = MIMEMultipart()
    message["From"] = settings.sender_email
    message["To"] = settings.receiver_email
    message["Subject"] = subject
    if bcc_emails:
        message["Bcc"] = ", ".join(bcc_emails)
    message.attach(MIMEText(html_content, "html"))

    for path in attachments:
        message.attach(_attachment_part(Path(path)))

    recipients = [settings.receiver_email] + bcc_emails
    logger.info("Sending %r to %s (bcc %d)", subject, settings.receiver_email, len(bcc_emails))
    try:
        with smtplib.SMTP_SSL("smtp.gmail.com", 465, timeout=60) as server:
            server.login(settings.sender_email, settings.app_password)
            server.sendmail(settings.sender_email, recipients, message.as_string())
    except (smtplib.SMTPException, OSError) as exc:
        raise EmailError(f"Failed to send {subject!r}: {exc}") from exc
    logger.info("Sent %r", subject)


def _attachment_part(path: Path) -> MIMEBase:
    part = MIMEBase("application", "octet-stream")
    with open(path, "rb") as f:
        part.set_payload(f.read())
    encoders.encode_base64(part)
    part.add_header("Content-Disposition", f'attachment; filename="{os.path.basename(path)}"')
    return part


def notify_error(settings: Settings, error_message: str, attachments: Sequence[Path] = ()) -> None:
    """Mail the operator, but not once every cron tick for the same failure."""
    last_line = (error_message.strip().splitlines() or [""])[-1]
    signature = hashlib.sha1(last_line.encode()).hexdigest()
    suppressed = _throttle(settings, signature)
    if suppressed is None:
        logger.info("Error notification suppressed (same failure as the last one)")
        return

    note = ""
    if suppressed:
        note = f"<p>{suppressed} identical failures were suppressed since the last notification.</p>"
    html = (
        "<h3>The Schoology update script failed:</h3>"
        f"{note}<pre>{error_message}</pre>"
    )
    try:
        send_email(settings, "Schoology Script Failed", html, attachments, bcc=[])
    except EmailError as exc:
        logger.error("Could not send the error notification: %s", exc)


def _throttle(settings: Settings, signature: str) -> int | None:
    """Returns how many notifications were suppressed, or None to stay quiet."""
    path = settings.error_state_file
    now = time.time()
    window = settings.error_notify_interval_hours * 3600

    record = {}
    if path.exists():
        try:
            with open(path, "r", encoding="utf-8") as f:
                record = json.load(f)
        except (OSError, ValueError):
            record = {}

    same = record.get("signature") == signature
    recent = now - record.get("last_sent", 0) < window
    if same and recent:
        record["suppressed"] = record.get("suppressed", 0) + 1
        _write(path, record)
        return None

    suppressed = record.get("suppressed", 0) if same else 0
    _write(path, {"signature": signature, "last_sent": now, "suppressed": 0})
    return suppressed


def _write(path: Path, record: dict) -> None:
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            json.dump(record, f)
    except OSError as exc:
        logger.warning("Could not update %s: %s", path, exc)
