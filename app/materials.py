"""Keeping a local mirror of the course materials, and reporting what changed.

The API gives an md5 for every attachment, so "changed" is exact rather than a
guess from titles and sizes. Pages have no file behind them, so they are hashed
from their rendered body instead.
"""

from __future__ import annotations

import ast
import hashlib
import json
import logging
from dataclasses import dataclass, field
from pathlib import Path

from schoology import _safe_filename
from schoology_api import SchoologyAPI, SchoologyAPIError

logger = logging.getLogger(__name__)


@dataclass
class Change:
    kind: str  # "new" or "updated"
    folder: str
    title: str
    filename: str
    size: int = 0
    path: Path | None = None


@dataclass
class SyncReport:
    baseline: bool = False
    changes: list[Change] = field(default_factory=list)
    items_seen: int = 0
    files_downloaded: int = 0
    bytes_downloaded: int = 0
    errors: list[str] = field(default_factory=list)

    @property
    def new(self) -> list[Change]:
        return [c for c in self.changes if c.kind == "new"]

    @property
    def updated(self) -> list[Change]:
        return [c for c in self.changes if c.kind == "updated"]

    def __bool__(self) -> bool:
        return bool(self.changes)


def sync(
    api: SchoologyAPI,
    section_id: str,
    destination: Path,
    known: dict[str, dict],
    skip_folders: frozenset[str] = frozenset(),
    max_file_mb: float = 50,
    dry_run: bool = False,
    web_base: str = "",
) -> SyncReport:
    """Mirror the course materials into `destination`, updating `known` in place.

    A dry run reports what would change without downloading anything, writing the
    index, or touching `known`.
    """
    report = SyncReport(baseline=not known)
    if report.baseline:
        logger.info("No materials recorded yet: this run establishes the baseline")

    items = api.walk(section_id, skip=skip_folders)
    documents = api.documents(section_id)
    pages = api.pages(section_id)
    index: dict[str, dict] = {}

    for item in items:
        if item.is_folder:
            continue
        report.items_seen += 1
        try:
            if item.type == "document":
                _sync_document(
                    api, documents.get(item.id), item, destination, known, index,
                    report, max_file_mb, dry_run, _web_url(web_base, section_id, item),
                )
            elif item.type == "page":
                _sync_page(
                    pages.get(item.id), item, destination, known, index, report, dry_run,
                    _web_url(web_base, section_id, item),
                )
        except (SchoologyAPIError, OSError) as exc:
            message = f"{'/'.join(item.path)}/{item.title}: {exc}"
            logger.warning("Could not sync %s", message)
            report.errors.append(message)

    if not dry_run:
        _write_index(destination, index)
    return report


def _sync_document(
    api, document, item, destination, known, index, report, max_file_mb, dry_run=False,
    web_url="",
) -> None:
    if document is None:
        logger.debug("Document %s (%s) was listed in a folder but not in /documents",
                     item.id, item.title)
        return

    for kind, url in api.links_of(document):
        # Nothing to download, but the MCP corpus wants to know these exist.
        index[f"{item.id}:{kind}:{url[:40]}"] = {
            "type": kind, "folder": _folder(item), "title": item.title, "url": url,
        }

    for material in api.files_of(document, item):
        key = f"{item.id}:{material.filename}"
        target = destination.joinpath(*item.path, _safe_filename(material.filename))
        index[key] = {
            "type": "file", "folder": _folder(item), "title": material.title,
            "filename": material.filename, "path": str(target.relative_to(destination)),
            "md5": material.md5, "size": material.filesize, "web_url": web_url,
        }

        previous = known.get(key)
        unchanged = previous and previous.get("fingerprint") == material.fingerprint
        if unchanged and target.exists():
            continue

        # The state may be behind the disk -- a previous run downloaded the file but
        # failed before its mail was accepted. Re-report it, but do not re-fetch it.
        already_correct = (
            material.md5 and target.exists() and _md5(target) == material.md5
        )

        if material.filesize > max_file_mb * 1024 * 1024:
            logger.info("Skipping %s (%.0f MB exceeds the cap)",
                        material.filename, material.filesize / 1024 / 1024)
            continue

        if dry_run:
            report.changes.append(
                Change(
                    kind="new" if previous is None else "updated",
                    folder=_folder(item),
                    title=material.title,
                    filename=material.filename,
                    size=material.filesize,
                )
            )
            continue

        if already_correct:
            logger.info("%s is already on disk and matches its md5", target)
        else:
            logger.info("Downloading %s -> %s", material.filename, target)
            api.download(material.download_url, target, material.filesize)
            report.files_downloaded += 1
            report.bytes_downloaded += material.filesize
        report.changes.append(
            Change(
                kind="new" if previous is None else "updated",
                folder=_folder(item),
                title=material.title,
                filename=material.filename,
                size=material.filesize,
                path=target,
            )
        )
        known[key] = {
            "fingerprint": material.fingerprint,
            "title": material.title,
            "folder": _folder(item),
            "path": str(target.relative_to(destination)),
        }


def _sync_page(
    page, item, destination, known, index, report, dry_run=False, web_url=""
) -> None:
    if page is None:
        return

    body = page.get("body") or ""
    fingerprint = hashlib.sha256(body.encode("utf-8")).hexdigest()
    target = destination.joinpath(*item.path, _safe_filename(f"{item.title}.html"))
    key = f"{item.id}:page"
    index[key] = {
        "type": "page", "folder": _folder(item), "title": item.title,
        "path": str(target.relative_to(destination)), "sha256": fingerprint,
        "web_url": web_url,
    }

    previous = known.get(key)
    if previous and previous.get("fingerprint") == fingerprint and target.exists():
        return

    if not dry_run:
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(body, encoding="utf-8")
    report.changes.append(
        Change(
            kind="new" if previous is None else "updated",
            folder=_folder(item),
            title=item.title,
            filename=target.name,
            size=len(body),
            path=target,
        )
    )
    if not dry_run:
        known[key] = {
            "fingerprint": fingerprint,
            "title": item.title,
            "folder": _folder(item),
            "path": str(target.relative_to(destination)),
        }


def _web_url(base: str, section_id: str, item) -> str:
    """A link that opens in the browser.

    The API's own download_path needs an OAuth header, so it is useless as a link;
    these are the pages the parent is already signed in to.
    """
    if not base:
        return ""
    if item.type == "page":
        return f"{base}/page/{item.id}"
    return f"{base}/course/{section_id}/materials/gp/{item.id}"


def _md5(path: Path) -> str:
    digest = hashlib.md5()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _folder(item) -> str:
    return "/".join(item.path) or "(root)"


def _write_index(destination: Path, index: dict) -> None:
    """A manifest of everything mirrored, for later retrieval over the corpus."""
    destination.mkdir(parents=True, exist_ok=True)
    path = destination / "index.json"
    tmp = path.with_suffix(".tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(index, f, indent=2, ensure_ascii=False)
    tmp.replace(path)
    logger.info("Wrote %s (%d entries)", path, len(index))


def sync_albums(
    api: SchoologyAPI,
    section_id: str,
    destination: Path,
    known: dict[str, dict],
    max_file_mb: float = 50,
    dry_run: bool = False,
    originals: bool = True,
) -> SyncReport:
    """Mirror the class photo albums, one directory per album.

    Kept apart from `sync` because the albums are the bulk of the data by far and
    are worth turning off on their own.
    """
    report = SyncReport(baseline=not any(k.startswith("album:") for k in known))

    for album in api.albums(section_id):
        album_id = str(album["id"])
        title = album.get("title") or f"Album {album_id}"
        folder = destination / _safe_filename(title)

        for entry in api.album_content(section_id, album_id):
            report.items_seen += 1
            key = f"album:{album_id}:{entry.get('id')}"
            md5 = entry.get("content_md5_checksum") or ""
            original = _original_of(entry) if originals else None

            if original:
                url = original.get("download_path") or ""
                size = int(original.get("filesize") or 0)
                name = _safe_filename(original.get("filename") or "")
            else:
                # content_url serves the 600x600 album_large variant, not the photo.
                url = entry.get("content_url") or ""
                try:
                    size = int(entry.get("content_filesize") or 0)
                except (TypeError, ValueError):
                    size = 0
                name = ""

            if not url or url == "None":
                continue
            name = name or _album_filename(entry, title)
            target = folder / name
            previous = known.get(key)
            if previous and previous.get("fingerprint") == md5 and target.exists():
                continue

            if size and size > max_file_mb * 1024 * 1024:
                logger.info("Skipping %s (%.0f MB exceeds the cap)", name, size / 1024 / 1024)
                continue

            change = Change(
                kind="new" if previous is None else "updated",
                folder=title,
                title=entry.get("caption") or name,
                filename=name,
                size=size,
                path=None if dry_run else target,
            )
            if dry_run:
                report.changes.append(change)
                continue

            try:
                api.download(url, target, 0)
            except (SchoologyAPIError, OSError) as exc:
                logger.warning("Could not download %s: %s", name, exc)
                report.errors.append(f"{title}/{name}: {exc}")
                continue

            report.files_downloaded += 1
            report.bytes_downloaded += target.stat().st_size
            report.changes.append(change)
            known[key] = {
                "fingerprint": md5,
                "title": change.title,
                "folder": title,
                "path": str(target.relative_to(destination.parent)),
            }

    return report


def _original_of(entry: dict) -> dict | None:
    """The full-resolution photo behind an album entry.

    `content_url` points at a 600x600 thumbnail; the original hides in a nested
    attachments blob, which the API sometimes hands back as a Python repr string
    rather than JSON.
    """
    raw = entry.get("attachments")
    if isinstance(raw, str):
        try:
            raw = ast.literal_eval(raw)
        except (ValueError, SyntaxError):
            return None
    if not isinstance(raw, dict):
        return None
    files = ((raw.get("files") or {}).get("file")) or []
    return files[0] if files and files[0].get("download_path") else None


def _album_filename(entry: dict, album_title: str) -> str:
    """Name photos by capture order so a directory listing reads chronologically."""
    order = str(entry.get("display_order") or "0").zfill(4)
    extension = ".jpg" if entry.get("type") == "image" else ".mp4"
    return _safe_filename(f"{order}-{entry.get('id')}{extension}")
