"""Schoology REST API v1 client.

Two-legged OAuth 1.0 with a PLAINTEXT signature. The header format is fussy in
two ways that produce a misleading "Duplicate timestamp/nonce combination"
error when you get them wrong: the realm must be URL-encoded ("Schoology%20API",
not a literal space), and the signature ends in a literal "&", not "%26".

A parent's key can read course materials but not the course Edge feed:
`OPTIONS /sections/<id>/updates` answers with an empty `Allow` header, while
`/documents` answers `Allow: GET`. Homeroom posts therefore still come from the
scraped feed in schoology.py.
"""

from __future__ import annotations

import logging
import random
import re
import string
import time
from dataclasses import dataclass
from pathlib import Path

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

logger = logging.getLogger(__name__)

BASE_URL = "https://api.schoology.com/v1"


class SchoologyAPIError(RuntimeError):
    """The API refused a request."""


@dataclass
class FolderItem:
    """One row of a course folder: a subfolder, a document, or a page."""

    id: str
    type: str
    title: str
    path: tuple[str, ...] = ()

    @property
    def is_folder(self) -> bool:
        return self.type == "folder"


@dataclass
class MaterialFile:
    """An attachment hanging off a document, with everything needed to sync it."""

    item_id: str
    title: str
    filename: str
    filesize: int
    md5: str
    timestamp: int
    mimetype: str
    download_url: str
    folder_path: tuple[str, ...] = ()

    @property
    def fingerprint(self) -> str:
        """What has to change for the file to count as changed."""
        return self.md5 or f"{self.filesize}:{self.timestamp}"


@dataclass
class MaterialPage:
    """A Schoology page -- rich text with no file behind it."""

    item_id: str
    title: str
    body: str
    folder_path: tuple[str, ...] = ()
    fingerprint: str = ""


@dataclass
class Section:
    id: str
    course_title: str
    section_title: str

    @property
    def name(self) -> str:
        return f"{self.course_title}: {self.section_title}".strip(": ")


class SchoologyAPI:
    def __init__(self, consumer_key: str, consumer_secret: str, timeout: int = 30) -> None:
        self._key = consumer_key
        self._secret = consumer_secret
        self._timeout = timeout
        self._session = requests.Session()
        retry = Retry(
            total=3,
            backoff_factor=1,
            status_forcelist=(429, 500, 502, 503, 504),
            allowed_methods=frozenset({"GET"}),
        )
        self._session.mount("https://", HTTPAdapter(max_retries=retry))

    # --- transport --------------------------------------------------------

    def _auth_header(self) -> str:
        nonce = "".join(random.choice(string.ascii_letters + string.digits) for _ in range(16))
        return (
            'OAuth realm="Schoology%20API",'
            f'oauth_consumer_key="{self._key}",'
            'oauth_signature_method="PLAINTEXT",'
            f'oauth_timestamp="{int(time.time())}",'
            f'oauth_nonce="{nonce}",'
            'oauth_version="1.0",'
            f'oauth_signature="{self._secret}&"'
        )

    def get(self, path: str, **params) -> dict:
        url = path if path.startswith("http") else f"{BASE_URL}{path}"
        response = self._session.get(
            url,
            headers={"Authorization": self._auth_header(), "Accept": "application/json"},
            params=params,
            timeout=self._timeout,
        )
        if response.status_code == 403:
            raise SchoologyAPIError(f"{path} is not readable with this API key (403)")
        response.raise_for_status()
        return response.json()

    def download(self, url: str, destination: Path, expected_size: int = 0) -> Path:
        destination.parent.mkdir(parents=True, exist_ok=True)
        tmp = destination.with_suffix(destination.suffix + ".part")
        with self._session.get(
            url,
            headers={"Authorization": self._auth_header()},
            stream=True,
            timeout=self._timeout,
        ) as response:
            response.raise_for_status()
            with open(tmp, "wb") as f:
                for chunk in response.iter_content(chunk_size=64 * 1024):
                    f.write(chunk)
        written = tmp.stat().st_size
        if expected_size and written != expected_size:
            tmp.unlink(missing_ok=True)
            raise SchoologyAPIError(
                f"{destination.name}: expected {expected_size} bytes, got {written}"
            )
        tmp.replace(destination)
        return destination

    # --- identity ---------------------------------------------------------

    def current_user_id(self) -> str:
        return str(self.get("/app-user-info")["api_uid"])

    def user(self, user_id: str) -> dict:
        return self.get(f"/users/{user_id}")

    def child_ids(self, user_id: str) -> list[str]:
        raw = self.user(user_id).get("child_uids") or ""
        return [c for c in str(raw).split(",") if c]

    def sections(self, user_id: str) -> list[Section]:
        payload = self.get(f"/users/{user_id}/sections")
        return [
            Section(
                id=str(s["id"]),
                course_title=s.get("course_title", ""),
                section_title=s.get("section_title", ""),
            )
            for s in payload.get("section", [])
        ]

    # --- materials --------------------------------------------------------

    def folder(self, section_id: str, folder_id: str = "0") -> list[FolderItem]:
        payload = self.get(f"/courses/{section_id}/folder/{folder_id}")
        return [
            FolderItem(
                id=str(item["id"]),
                type=item.get("type", ""),
                title=item.get("title", "").strip(),
            )
            for item in payload.get("folder-item", [])
        ]

    def walk(
        self,
        section_id: str,
        folder_id: str = "0",
        path: tuple[str, ...] = (),
        skip: frozenset[str] = frozenset(),
        depth: int = 0,
    ) -> list[FolderItem]:
        """Every item in the course, depth first, each tagged with its folder path."""
        if depth > 8:  # a cycle would otherwise recurse until the API rate-limits us
            logger.warning("Stopping at depth %d under %s", depth, "/".join(path))
            return []

        found = []
        for item in self.folder(section_id, folder_id):
            item.path = path
            if not item.is_folder:
                found.append(item)
                continue
            if item.title in skip:
                logger.info("Skipping folder %r", item.title)
                continue
            found.append(item)
            found.extend(
                self.walk(section_id, item.id, path + (item.title,), skip, depth + 1)
            )
        return found

    def _paginated(self, path: str, key: str, limit: int = 200) -> list[dict]:
        """Collect every page of a list endpoint. 287 documents come back in two
        calls this way; asking for them one id at a time would be 287."""
        found: list[dict] = []
        url = f"{path}?limit={limit}"
        while url:
            payload = self.get(url)
            found.extend(payload.get(key, []))
            url = (payload.get("links") or {}).get("next", "")
        return found

    def documents(self, section_id: str) -> dict[str, dict]:
        """Every document in the section by id, attachments included."""
        docs = self._paginated(f"/sections/{section_id}/documents", "document")
        logger.info("Fetched %d documents from section %s", len(docs), section_id)
        return {str(d["id"]): d for d in docs}

    def pages(self, section_id: str) -> dict[str, dict]:
        pages = self._paginated(f"/sections/{section_id}/pages", "page")
        logger.info("Fetched %d pages from section %s", len(pages), section_id)
        return {str(p["id"]): p for p in pages}

    @staticmethod
    def files_of(document: dict, item: FolderItem) -> list[MaterialFile]:
        files = ((document.get("attachments") or {}).get("files") or {}).get("file") or []
        return [
            MaterialFile(
                item_id=item.id,
                title=item.title,
                filename=f.get("filename") or f"{item.title}.{f.get('extension', 'bin')}",
                filesize=int(f.get("filesize") or 0),
                md5=f.get("md5_checksum") or "",
                timestamp=int(f.get("timestamp") or 0),
                mimetype=f.get("filemime") or "",
                download_url=f.get("download_path") or "",
                folder_path=item.path,
            )
            for f in files
            if f.get("download_path")
        ]

    # --- albums ----------------------------------------------------------

    def albums(self, section_id: str) -> list[dict]:
        albums = self._paginated(f"/sections/{section_id}/albums", "album")
        logger.info("Fetched %d albums from section %s", len(albums), section_id)
        return albums

    def album_content(self, section_id: str, album_id: str) -> list[dict]:
        payload = self.get(f"/sections/{section_id}/albums/{album_id}?withcontent=1")
        content = payload.get("content") or []
        # Schoology returns this as a bare list, unlike every other list endpoint.
        return content if isinstance(content, list) else content.get("content-item", [])

    @staticmethod
    def links_of(document: dict) -> list[tuple[str, str]]:
        """Videos, embeds and plain links: nothing to download, but worth recording."""
        attachments = document.get("attachments") or {}
        found = []
        for kind, wrapper, inner in (
            ("video", "videos", "video"),
            ("embed", "embeds", "embed"),
            ("link", "links", "link"),
        ):
            for entry in (attachments.get(wrapper) or {}).get(inner) or []:
                url = entry.get("url") or ""
                if not url:
                    # Embeds arrive as a whole <iframe> tag; the src is the useful part.
                    match = re.search(r'src="([^"]+)"', entry.get("embed_code") or "")
                    url = match.group(1) if match else ""
                if url:
                    found.append((kind, url))
        return found
