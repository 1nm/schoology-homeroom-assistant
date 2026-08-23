"""Reading and parsing the course feed, and fetching what is attached to it."""

from __future__ import annotations

import hashlib
import logging
import mimetypes
import re
import urllib.parse
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path

import pymupdf
import requests
from bs4 import BeautifulSoup

logger = logging.getLogger(__name__)

_UNICODE_ESCAPE = re.compile(r"\\u([0-9a-fA-F]{4})")
_MERIDIEM = re.compile(r"\b([ap])\.?m\.?\b", re.IGNORECASE)
_UNSAFE_FILENAME = re.compile(r"[^\w.\-() ]+")
_HTML_TYPES = {"text/html", "application/xhtml+xml"}


@dataclass
class Attachment:
    url: str
    filename: str
    path: Path | None = None
    text: str = ""


@dataclass
class Post:
    post_id: str
    datetime_text: str = ""
    author: str = ""
    content: str = ""
    html_content: str = ""
    attachments_html: str = ""
    show_more_href: str = ""
    images: list[str] = field(default_factory=list)
    attachments: list[Attachment] = field(default_factory=list)

    @property
    def posted_at(self) -> datetime:
        parsed = parse_post_datetime(self.datetime_text)
        if parsed is None:
            logger.warning(
                "Unrecognised post timestamp %r on post %s, falling back to now",
                self.datetime_text,
                self.post_id,
            )
            return datetime.now()
        return parsed


def parse_post_datetime(text: str, now: datetime | None = None) -> datetime | None:
    """Schoology renders relative dates for recent posts and absolute ones for older."""
    text = (text or "").strip()
    if not text:
        return None

    now = now or datetime.now()
    normalised = _MERIDIEM.sub(lambda m: m.group(1).upper() + "M", text)

    for prefix, day in (("Today", now), ("Yesterday", now - timedelta(days=1))):
        if normalised.startswith(prefix):
            normalised = day.strftime("%b %d, %Y") + normalised[len(prefix):]
            break

    for fmt in ("%a %b %d, %Y at %I:%M %p", "%b %d, %Y at %I:%M %p"):
        try:
            return datetime.strptime(normalised, fmt)
        except ValueError:
            continue
    return None


class SchoologyClient:
    def __init__(self, session: requests.Session, base_url: str, timeout: int = 30) -> None:
        self._session = session
        self._base_url = base_url.rstrip("/")
        self._timeout = timeout

    def enter_course_as_parent(self, course_id: str, child_uid: str = "") -> str:
        """Put the session into the course's realm, and return the child uid used.

        A parent account gets an empty feed (`<ul class="s-edge-feed feed-no-realm">
        ... There are no posts`) until it has opened the child's course through the
        parent preview URL that the parent home links to. The old script did this by
        accident -- it clicked the "Homeroom" link on the parent home at the end of
        every sign-in, and that link *is* the preview URL. Doing it here instead means
        it still happens on the runs that reuse a stored session.
        """
        child_uid = child_uid or self.find_child_uid(course_id)
        if not child_uid:
            logger.info(
                "No parent preview link for course %s; reading the feed directly", course_id
            )
            return ""

        url = f"{self._base_url}/course/{course_id}/preview/{child_uid}/parent"
        logger.info("Entering the course as a parent via %s", url)
        self._session.get(url, timeout=self._timeout).raise_for_status()
        return child_uid

    def find_child_uid(self, course_id: str) -> str:
        """The parent home lists each child's courses as /course/<id>/preview/<uid>/parent."""
        response = self._session.get(f"{self._base_url}/parent/home", timeout=self._timeout)
        response.raise_for_status()
        match = re.search(
            rf"/course/{re.escape(course_id)}/preview/(\d+)/parent", response.text
        )
        if not match:
            return ""
        logger.info("Found child uid %s for course %s", match.group(1), course_id)
        return match.group(1)

    def fetch_feed(self, course_id: str) -> str:
        url = f"{self._base_url}/course/{course_id}/feed?filter=1"
        logger.info("Fetching the course feed from %s", url)
        response = self._session.get(url, timeout=self._timeout)
        response.raise_for_status()
        return _feed_markup(response)

    def parse_posts(self, html: str) -> list[Post]:
        """Parse the feed. Deliberately does no network I/O: callers filter out
        posts they have already handled before paying for expansion or downloads."""
        soup = BeautifulSoup(html, "html.parser")
        posts = []
        # Selecting on the id rather than on class="first"/class="": every post
        # carries the id, only the top one carries the class.
        for element in soup.select('li[id^="edge-assoc-"]'):
            post_id = element["id"][len("edge-assoc-"):]
            if not post_id:
                continue
            posts.append(self._parse_post(element, post_id))
        return posts

    def _parse_post(self, element, post_id: str) -> Post:
        timestamp = element.find("span", {"class": "small gray"})
        author = element.find("a", {"title": "View user profile."})
        body = element.find("span", {"class": "update-body s-rte"})
        show_more = element.find("a", {"class": "show-more-link"})
        attachments_div = element.find("div", {"class": "attachments clearfix"})

        return Post(
            post_id=post_id,
            datetime_text=timestamp.text if timestamp else "",
            author=author.text if author else "",
            content=body.get_text().strip() if body else "",
            html_content=body.prettify() if body else "",
            attachments_html=attachments_div.prettify() if attachments_div else "",
            show_more_href=show_more.get("href", "") if show_more else "",
            images=[img.get("src", "") for img in body.find_all("img")] if body else [],
            attachments=self._parse_attachments(attachments_div),
        )

    def _parse_attachments(self, attachments_div) -> list[Attachment]:
        if not attachments_div:
            return []
        attachments = []
        for anchor in attachments_div.find_all("a"):
            href = anchor.get("href")
            span = anchor.find("span")
            if not href or not span:
                continue
            attachments.append(
                Attachment(
                    url=urllib.parse.urljoin(self._base_url, href),
                    filename=span.get("aria-label") or "",
                )
            )
        return attachments

    def expand(self, post: Post) -> None:
        """Follow the post's "Show more" link to get the untruncated body."""
        if not post.show_more_href:
            return

        url = urllib.parse.urljoin(self._base_url, post.show_more_href)
        logger.info("Loading the full body of post %s", post.post_id)
        response = self._session.post(url, timeout=self._timeout)
        if not response.ok:
            logger.warning("Show-more request for post %s returned %s, keeping the truncated body",
                           post.post_id, response.status_code)
            return

        try:
            body = response.json()["update"]
        except (ValueError, KeyError) as exc:
            logger.warning("Unexpected show-more payload for post %s (%s)", post.post_id, exc)
            return

        soup = BeautifulSoup(body, "html.parser")
        post.html_content = soup.prettify()
        post.content = soup.get_text().strip()
        post.images = [img.get("src", "") for img in soup.find_all("img")]

    def download_attachments(
        self,
        post: Post,
        dest_dir: Path,
        max_attachment_mb: float = 50,
        max_pdf_mb: float = 20,
    ) -> None:
        for attachment in post.attachments:
            try:
                attachment.path = self._download(attachment.url, dest_dir, max_attachment_mb)
            except (requests.RequestException, OSError) as exc:
                # A dead link or an unwritable directory must not sink the post.
                logger.warning("Could not download %s: %s", attachment.url, exc)
                continue

            if attachment.path is None:
                continue
            if attachment.path.suffix.lower() != ".pdf":
                continue
            size_mb = attachment.path.stat().st_size / (1024 * 1024)
            if size_mb > max_pdf_mb:
                logger.info("Skipping text extraction for %s (%.1f MB)", attachment.path, size_mb)
                continue
            attachment.text = extract_text_from_pdf(attachment.path)

    def _download(self, url: str, dest_dir: Path, max_mb: float) -> Path | None:
        max_bytes = int(max_mb * 1024 * 1024)
        with self._session.get(
            url, stream=True, allow_redirects=True, timeout=self._timeout
        ) as response:
            response.raise_for_status()

            content_type = response.headers.get("Content-Type", "").split(";")[0].strip().lower()
            name = _filename_from_disposition(response.headers.get("Content-Disposition", ""))

            if not name:
                # An unnamed HTML body means Schoology served us a page (usually the
                # signed-out home page) rather than the file. Writing it out is how
                # this script used to accumulate thousands of junk files.
                if content_type in _HTML_TYPES:
                    logger.warning(
                        "%s returned an HTML page instead of a file; skipping it", url
                    )
                    return None
                digest = hashlib.sha1(url.encode()).hexdigest()[:16]
                name = digest + (mimetypes.guess_extension(content_type) or ".bin")

            dest_dir.mkdir(parents=True, exist_ok=True)
            destination = dest_dir / _safe_filename(name)
            if destination.exists() and destination.stat().st_size > 0:
                logger.info("%s is already downloaded", destination)
                return destination

            declared = int(response.headers.get("Content-Length") or 0)
            if declared > max_bytes:
                logger.warning("Skipping %s: %d bytes exceeds the %.0f MB cap", url, declared, max_mb)
                return None

            logger.info("Downloading %s to %s", url, destination)
            tmp = destination.with_suffix(destination.suffix + ".part")
            written = 0
            with open(tmp, "wb") as f:
                for chunk in response.iter_content(chunk_size=64 * 1024):
                    written += len(chunk)
                    if written > max_bytes:
                        f.close()
                        tmp.unlink(missing_ok=True)
                        logger.warning("Aborted %s: exceeded the %.0f MB cap", url, max_mb)
                        return None
                    f.write(chunk)
            tmp.replace(destination)
            return destination


def extract_text_from_pdf(pdf_path: Path) -> str:
    try:
        with pymupdf.open(pdf_path) as document:
            return "\n".join(page.get_text() for page in document)
    except Exception as exc:  # PyMuPDF raises a grab-bag of errors on bad files
        logger.warning("Could not extract text from %s: %s", pdf_path, exc)
        return ""


def _feed_markup(response: requests.Response) -> str:
    """The feed endpoint wraps rendered markup in a JSON envelope."""
    try:
        payload = response.json()
    except ValueError:
        return _unescape(response.text)

    if isinstance(payload, dict):
        for key in ("content", "update", "html", "output"):
            value = payload.get(key)
            if isinstance(value, str) and value:
                return value
        strings = [v for v in payload.values() if isinstance(v, str)]
        if strings:
            return max(strings, key=len)
    return _unescape(response.text)


def _unescape(text: str) -> str:
    """Undo the JSON-style escaping without mangling non-ASCII text.

    The previous implementation used `bytes.decode("unicode-escape")`, which
    treats the payload as latin-1 and turns every Japanese character into mojibake.
    """
    text = text.replace("\\/", "/")
    text = _UNICODE_ESCAPE.sub(lambda m: chr(int(m.group(1), 16)), text)
    try:
        # Re-join any surrogate pairs the substitution above produced.
        return text.encode("utf-16", "surrogatepass").decode("utf-16")
    except UnicodeError:
        return text


def _filename_from_disposition(disposition: str) -> str:
    if not disposition:
        return ""
    extended = re.search(r"filename\*=(?:UTF-8'')?([^;]+)", disposition, re.IGNORECASE)
    if extended:
        return urllib.parse.unquote(extended.group(1).strip().strip('"'))
    plain = re.search(r'filename="?([^";]+)"?', disposition, re.IGNORECASE)
    return plain.group(1).strip() if plain else ""


def _safe_filename(name: str) -> str:
    name = name.replace("/", ".").strip().lstrip(".")
    name = _UNSAFE_FILENAME.sub("_", name)
    return (name or "attachment")[:120]
