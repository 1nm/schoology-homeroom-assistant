"""Runtime configuration, resolved once from the environment."""

from __future__ import annotations

import os
import re
from dataclasses import dataclass
from pathlib import Path

from dotenv import find_dotenv, load_dotenv


class ConfigError(RuntimeError):
    """The environment is missing something the run needs."""


def _get(name: str, default: str = "") -> str:
    return os.environ.get(name, default).strip()


def _require(name: str) -> str:
    value = _get(name)
    if not value:
        raise ConfigError(f"Required environment variable {name} is not set")
    return value


def _get_bool(name: str, default: bool) -> bool:
    value = _get(name).lower()
    if not value:
        return default
    return value in {"1", "true", "yes", "on"}


def _get_number(name: str, default: float) -> float:
    value = _get(name)
    if not value:
        return default
    try:
        return float(value)
    except ValueError as exc:
        raise ConfigError(f"{name} must be a number, got {value!r}") from exc


def course_id_from_url(url: str) -> str:
    """Pull the numeric course id out of any Schoology course URL."""
    match = re.search(r"/course/(\d+)", url)
    return match.group(1) if match else ""


@dataclass(frozen=True)
class Settings:
    # Schoology account
    email: str
    password: str
    subdomain: str

    # The course we watch
    homeroom_class: str
    homeroom_course_url: str
    homeroom_course_name: str
    course_id: str
    child_uid: str

    # Materials mirror (REST API)
    api_consumer_key: str
    api_consumer_secret: str
    materials_skip_folders: frozenset
    max_material_mb: float
    sync_albums: bool
    album_originals: bool
    max_album_mb: float

    # Mail
    sender_email: str
    receiver_email: str
    bcc_emails: list[str]
    app_password: str

    # Models
    summary_model: str
    translation_model: str
    translation_languages: list[str]

    # Behaviour
    data_dir: Path
    state_filename: str
    headless: bool
    selenium_timeout: int
    http_timeout: int
    max_pdf_mb: float
    max_attachment_mb: float
    max_email_attachment_mb: float
    error_notify_interval_hours: float

    # Slack: optional mirror of the mail into one channel.
    slack_bot_token: str
    slack_channel: str
    slack_mentions: list[str]

    # Face matching over the class albums (see faces.py); people are learned by CLI.
    face_threshold: float

    @classmethod
    def load(cls) -> "Settings":
        load_dotenv(find_dotenv(usecwd=True))

        # Everything the run cannot proceed without, checked up front.
        email = _require("SCHOOLOGY_EMAIL")
        password = _require("SCHOOLOGY_PASSWORD")
        subdomain = _require("SCHOOLOGY_SUBDOMAIN")
        sender_email = _require("SUMMARY_SENDER_EMAIL")
        app_password = _require("GOOGLE_APP_PASSWORD")

        homeroom_course_url = _get("HOMEROOM_COURSE_URL")
        bcc_raw = _get("BCC_EMAILS")
        languages = [
            lang.strip()
            for lang in _get("TRANSLATION_LANGUAGES", "Japanese,Chinese").split(",")
            if lang.strip()
        ]

        return cls(
            email=email,
            password=password,
            subdomain=subdomain,
            homeroom_class=_get("HOMEROOM_CLASS"),
            homeroom_course_url=homeroom_course_url,
            homeroom_course_name=_get("HOMEROOM_COURSE_NAME", "Homeroom"),
            # An explicit id wins; otherwise reuse the one already embedded in the
            # course URL so a normal run never has to drive the browser to find it.
            course_id=_get("SCHOOLOGY_COURSE_ID")
            or course_id_from_url(homeroom_course_url),
            child_uid=_get("SCHOOLOGY_CHILD_UID"),
            api_consumer_key=_get("SCHOOLOGY_API_CONSUMER_KEY"),
            api_consumer_secret=_get("SCHOOLOGY_API_CONSUMER_SECRET"),
            materials_skip_folders=frozenset(
                f.strip() for f in _get("MATERIALS_SKIP_FOLDERS").split(",") if f.strip()
            ),
            max_material_mb=_get_number("MAX_MATERIAL_MB", 50),
            # Off by default: the class albums run to several GB.
            sync_albums=_get_bool("SYNC_ALBUMS", False),
            # Originals are ~7MB each; the alternative is a 600x600 thumbnail.
            album_originals=_get_bool("ALBUM_ORIGINALS", True),
            max_album_mb=_get_number("MAX_ALBUM_MB", 50),
            sender_email=sender_email,
            receiver_email=_get("SUMMARY_RECEIVER_EMAIL") or sender_email,
            bcc_emails=[e.strip() for e in bcc_raw.split(",") if e.strip()],
            app_password=app_password,
            summary_model=_get("SUMMARY_MODEL", "gpt-5.4"),
            translation_model=_get("TRANSLATION_MODEL", "gpt-4o"),
            translation_languages=languages,
            data_dir=Path(_get("DATA_DIR") or Path.cwd()).resolve(),
            state_filename=_get("STATE_FILE", ".sadc.conf"),
            headless=_get_bool("HEADLESS", True),
            selenium_timeout=int(_get_number("SELENIUM_TIMEOUT", 30)),
            http_timeout=int(_get_number("HTTP_TIMEOUT", 30)),
            max_pdf_mb=_get_number("MAX_PDF_MB", 20),
            max_attachment_mb=_get_number("MAX_ATTACHMENT_MB", 50),
            # Gmail rejects anything over 25MB, so leave headroom for encoding.
            max_email_attachment_mb=_get_number("MAX_EMAIL_ATTACHMENT_MB", 15),
            error_notify_interval_hours=_get_number("ERROR_NOTIFY_INTERVAL_HOURS", 6),
            slack_bot_token=_get("SLACK_BOT_TOKEN"),
            slack_channel=_get("SLACK_CHANNEL"),
            slack_mentions=[u.strip() for u in _get("SLACK_MENTIONS").split(",") if u.strip()],
            face_threshold=_get_number("FACE_THRESHOLD", 0.45),
        )

    @property
    def base_url(self) -> str:
        return f"https://{self.subdomain}.schoology.com"

    @property
    def state_file(self) -> Path:
        return self.data_dir / self.state_filename

    @property
    def cookie_file(self) -> Path:
        return self.data_dir / ".schoology_cookies.json"

    @property
    def error_state_file(self) -> Path:
        return self.data_dir / ".error_notify.json"

    @property
    def materials_enabled(self) -> bool:
        """Materials come from the REST API, which needs its own credentials."""
        return bool(self.api_consumer_key and self.api_consumer_secret)

    @property
    def materials_dir(self) -> Path:
        return self.data_dir / "materials"

    @property
    def posts_dir(self) -> Path:
        return self.data_dir / "posts"

    @property
    def albums_dir(self) -> Path:
        return self.materials_dir / "Photos"

    @property
    def attachments_dir(self) -> Path:
        return self.data_dir / "attachments"

    @property
    def screenshot_file(self) -> Path:
        return self.data_dir / "error_screenshot.png"
