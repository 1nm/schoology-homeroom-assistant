#!/usr/bin/env python3
"""An MCP server over the local homeroom archive.

Deliberately retrieval only: it lists, searches, reads and hands back files, and
leaves the reasoning to whatever agent is calling it. The corpus is small enough
that an agent can iterate -- search, read a whole document, search again -- which
beats answering from whatever a single top-k lookup happened to return.

    python app/mcp_server.py --data-dir ~/homeroom
"""

from __future__ import annotations

import argparse
import hashlib
import json
import logging
import math
import os
import re
import secrets
from collections import Counter
from dataclasses import dataclass
from pathlib import Path

import pymupdf
import uvicorn
from dotenv import find_dotenv, load_dotenv
from mcp.server.mcpserver import Image, MCPServer
from mcp.server.transport_security import TransportSecuritySettings

import semantic

logger = logging.getLogger(__name__)

DATA_DIR = Path(os.environ.get("DATA_DIR") or Path.cwd())
MAX_INLINE_PHOTOS = 8
PHOTO_MAX_EDGE = 900
IMAGE_SUFFIXES = {".jpg", ".jpeg", ".png", ".gif", ".webp"}

server = MCPServer(
    name="homeroom",
    instructions=(
        "A mirror of one school homeroom: the teacher's posts, the course materials "
        "(PDFs and pages), and the class photo albums. Use search() to find things, "
        "read_document() to read one in full, and get_file()/get_photos() to surface "
        "the originals."
    ),
)


# --- corpus -----------------------------------------------------------------


@dataclass
class Doc:
    id: str
    kind: str  # post | file | page
    title: str
    folder: str
    path: Path
    web_url: str = ""


def _materials_dir() -> Path:
    return DATA_DIR / "materials"


def _posts_dir() -> Path:
    return DATA_DIR / "posts"


def _index() -> dict:
    path = _materials_dir() / "index.json"
    if not path.exists():
        return {}
    return json.loads(path.read_text(encoding="utf-8"))


def _documents() -> list[Doc]:
    """Everything with readable text: posts, mirrored files, and pages."""
    docs = [
        Doc(id=p.stem, kind="post", title=p.stem, folder="posts", path=p)
        for p in sorted(_posts_dir().glob("*.md"))
    ]
    seen: set[Path] = set()
    for key, entry in _index().items():
        if entry.get("type") not in {"file", "page"} or not entry.get("path"):
            continue
        path = _materials_dir() / entry["path"]
        # The same page can be filed in two folders; it is still one document.
        if path.exists() and path not in seen:
            seen.add(path)
            docs.append(
                Doc(
                    id=key,
                    kind=entry["type"],
                    title=entry.get("title", path.name),
                    folder=entry.get("folder", ""),
                    path=path,
                    web_url=entry.get("web_url", ""),
                )
            )
    return docs


def _text_of(doc: Doc, max_chars: int = 0) -> str:
    """Extracted text, cached on disk because PDFs are slow to open."""
    suffix = doc.path.suffix.lower()
    if suffix in {".md", ".txt"}:
        text = doc.path.read_text(encoding="utf-8", errors="replace")
    elif suffix in {".html", ".htm"}:
        text = re.sub(r"<[^>]+>", " ", doc.path.read_text(encoding="utf-8", errors="replace"))
        text = re.sub(r"\s+", " ", text)
    elif suffix == ".pdf":
        text = _cached_pdf_text(doc.path)
    else:
        return ""
    return text[:max_chars] if max_chars else text


def _cached_pdf_text(path: Path) -> str:
    cache = DATA_DIR / ".textcache"
    cache.mkdir(exist_ok=True)
    stamp = f"{path}:{path.stat().st_mtime_ns}"
    cached = cache / (hashlib.sha1(stamp.encode()).hexdigest() + ".txt")
    if cached.exists():
        return cached.read_text(encoding="utf-8")
    try:
        with pymupdf.open(path) as document:
            text = "\n".join(page.get_text() for page in document)
    except Exception as exc:  # a broken or encrypted PDF must not break search
        logger.warning("Could not read %s: %s", path, exc)
        text = ""
    cached.write_text(text, encoding="utf-8")
    return text


def _tokens(text: str) -> list[str]:
    return re.findall(r"[a-z0-9']+", text.lower())


def _resolve(path: str) -> Path:
    """Resolve a caller-supplied path inside the archive, and nowhere else."""
    candidate = (DATA_DIR / path).resolve() if not Path(path).is_absolute() else Path(path).resolve()
    if not str(candidate).startswith(str(DATA_DIR.resolve())):
        raise ValueError(f"{path} is outside the archive")
    if not candidate.exists():
        # Callers often pass a path relative to materials/ instead of the root.
        alternative = (_materials_dir() / path).resolve()
        if str(alternative).startswith(str(DATA_DIR.resolve())) and alternative.exists():
            return alternative
        raise FileNotFoundError(path)
    return candidate


# --- tools ------------------------------------------------------------------


@server.tool(description="List the folders in the archive and how much is in each.")
def list_folders() -> list[dict]:
    counts: Counter = Counter()
    sizes: Counter = Counter()
    for entry in _index().values():
        folder = entry.get("folder", "(root)")
        counts[folder] += 1
        sizes[folder] += int(entry.get("size") or 0)
    folders = [
        {"folder": f, "items": counts[f], "megabytes": round(sizes[f] / 1024 / 1024, 1)}
        for f in sorted(counts)
    ]
    posts = list(_posts_dir().glob("*.md"))
    if posts:
        folders.insert(0, {"folder": "posts", "items": len(posts), "megabytes": 0.0})
    albums = _materials_dir() / "Photos"
    if albums.exists():
        folders.append(
            {"folder": "Photos", "items": sum(1 for _ in albums.rglob("*") if _.is_file()),
             "megabytes": round(sum(f.stat().st_size for f in albums.rglob("*") if f.is_file())
                                / 1024 / 1024, 1)}
        )
    return folders


@server.tool(
    description="List materials, optionally filtered by folder, kind (file/page/video/"
    "embed/link) or a substring of the title."
)
def list_materials(folder: str = "", kind: str = "", contains: str = "") -> list[dict]:
    found = []
    for entry in _index().values():
        if folder and folder.lower() not in entry.get("folder", "").lower():
            continue
        if kind and entry.get("type") != kind:
            continue
        if contains and contains.lower() not in entry.get("title", "").lower():
            continue
        found.append(
            {
                "title": entry.get("title"),
                "folder": entry.get("folder"),
                "kind": entry.get("type"),
                "path": entry.get("path"),
                "size": entry.get("size"),
                "url": entry.get("url") or entry.get("web_url"),
            }
        )
    return sorted(found, key=lambda e: (e["folder"] or "", e["title"] or ""))


def _semantic_index() -> semantic.SemanticIndex:
    return semantic.SemanticIndex(DATA_DIR / ".semantic")


@server.tool(
    description="Rebuild the semantic search index over the archive. Run this after a "
    "sync brings in new materials; unchanged text is not re-embedded."
)
def reindex() -> dict:
    documents = [
        (d.id, _text_of(d), {"path": str(d.path.relative_to(DATA_DIR)), "title": d.title,
                             "folder": d.folder, "kind": d.kind})
        for d in _documents()
    ]
    return _semantic_index().build([d for d in documents if d[1].strip()])


@server.tool(
    description="Search the homeroom posts, pages and PDFs. mode='hybrid' (default) "
    "blends exact keyword matching with meaning-based search, 'keyword' is exact only, "
    "'semantic' is meaning only. Returns passages plus the document each came from, so "
    "you can then read that document in full."
)
def search(query: str, limit: int = 8, mode: str = "hybrid") -> list[dict]:
    keyword = _keyword_search(query, limit * 2) if mode in {"hybrid", "keyword"} else []
    meaning = []
    if mode in {"hybrid", "semantic"}:
        try:
            # One document can own several matching chunks; keep its best one so a
            # single long PDF cannot crowd everything else out of the results.
            best_per_document: dict[str, dict] = {}
            for hit in _semantic_index().search(query, limit * 4):
                if hit.chunk.path in best_per_document:
                    continue
                best_per_document[hit.chunk.path] = {
                    "title": hit.chunk.title, "kind": hit.chunk.kind,
                    "folder": hit.chunk.folder, "path": hit.chunk.path,
                    "url": "", "score": round(hit.score, 4),
                    "passage": re.sub(r"\s+", " ", hit.chunk.text)[:320].strip(),
                }
            meaning = list(best_per_document.values())
        except FileNotFoundError:
            if mode == "semantic":
                return [{"error": "Run reindex() first: the semantic index is not built."}]
            logger.info("No semantic index yet, answering from keywords alone")
        except Exception as exc:  # noqa: BLE001 - a missing key must not kill the tool
            # Typically the embedding backend has no credentials. Keyword search
            # still works, so degrade rather than fail the whole call.
            if mode == "semantic":
                return [{"error": f"Semantic search is unavailable: {exc}"}]
            logger.warning("Semantic search unavailable (%s), answering from keywords alone", exc)

    if not meaning:
        return keyword[:limit]
    if not keyword:
        return meaning[:limit]
    return _fuse(keyword, meaning)[:limit]


def _fuse(*rankings: list[dict], k: int = 60) -> list[dict]:
    """Reciprocal rank fusion: a document ranked well by either method wins, without
    having to make two incomparable scores comparable."""
    scores: dict[str, float] = {}
    best: dict[str, dict] = {}
    best_rank: dict[str, int] = {}
    for ranking in rankings:
        for position, hit in enumerate(ranking):
            key = hit["path"]
            scores[key] = scores.get(key, 0.0) + 1.0 / (k + position + 1)
            # Keep the passage from whichever method ranked it highest, not the
            # longest one: the best-ranked passage is the one that answered.
            if key not in best_rank or position < best_rank[key]:
                best_rank[key], best[key] = position, hit
    ordered = sorted(scores, key=lambda key: -scores[key])
    return [{**best[key], "score": round(scores[key], 5)} for key in ordered]


def _keyword_search(query: str, limit: int = 8) -> list[dict]:
    terms = _tokens(query)
    if not terms:
        return []

    docs = _documents()
    texts = {d.id: _text_of(d) for d in docs}
    frequencies = Counter()
    for text in texts.values():
        frequencies.update(set(_tokens(text)))
    total = max(len(texts), 1)

    scored = []
    for doc in docs:
        tokens = _tokens(texts[doc.id])
        counts = Counter(tokens)
        # Plain tf-idf: the corpus is small enough that this beats nothing and is
        # exact on the things people actually look up, like a class code.
        score = sum(
            counts[t] / len(tokens) * math.log(total / (1 + frequencies[t]))
            for t in terms if counts[t]
        ) if tokens else 0.0
        # A title match is worth far more than a passing mention in a body: asking
        # for "Epic class code" should surface the page called "EPIC! Class Code".
        title_tokens = set(_tokens(doc.title))
        if title_tokens:
            score += 0.5 * sum(1 for t in terms if t in title_tokens) / len(terms)
        if score > 0:
            scored.append((score, doc, texts[doc.id]))

    scored.sort(key=lambda row: -row[0])
    results = []
    for score, doc, text in scored[:limit]:
        results.append(
            {
                "title": doc.title,
                "kind": doc.kind,
                "folder": doc.folder,
                "path": str(doc.path.relative_to(DATA_DIR)),
                "url": doc.web_url,
                "score": round(score, 4),
                "passage": _passage(text, terms),
            }
        )
    return results


def _passage(text: str, terms: list[str], width: int = 320) -> str:
    lowered = text.lower()
    position = min(
        (lowered.find(t) for t in terms if lowered.find(t) >= 0), default=0
    )
    start = max(0, position - width // 3)
    return re.sub(r"\s+", " ", text[start:start + width]).strip()


@server.tool(description="Read the full text of one archived document, post or page.")
def read_document(path: str, max_chars: int = 20000) -> dict:
    resolved = _resolve(path)
    doc = Doc(id=path, kind="file", title=resolved.name, folder=resolved.parent.name,
              path=resolved)
    text = _text_of(doc)
    return {
        "path": str(resolved.relative_to(DATA_DIR)),
        "characters": len(text),
        "truncated": len(text) > max_chars,
        "text": text[:max_chars],
    }


@server.tool(
    description="Locate one archived file: its absolute path on this machine, its size, "
    "and a Schoology URL that opens it in a signed-in browser. Images come back inline."
)
def get_file(path: str) -> list:
    resolved = _resolve(path)
    entry = next(
        (e for e in _index().values()
         if e.get("path") and (_materials_dir() / e["path"]).resolve() == resolved),
        {},
    )
    details = {
        "absolute_path": str(resolved),
        "size_bytes": resolved.stat().st_size,
        "title": entry.get("title", resolved.stem),
        "folder": entry.get("folder", ""),
        # The API's own download_path needs an OAuth header and is useless as a link;
        # this one opens for anyone already signed in to Schoology.
        "browser_url": entry.get("web_url", ""),
    }
    parts: list = [json.dumps(details, ensure_ascii=False, indent=2)]
    if resolved.suffix.lower() in IMAGE_SUFFIXES:
        parts.append(_inline_image(resolved))
    return parts


@server.tool(description="List the class photo albums, newest first.")
def list_albums() -> list[dict]:
    root = _materials_dir() / "Photos"
    if not root.exists():
        return []
    albums = []
    for folder in sorted(root.iterdir(), reverse=True):
        if not folder.is_dir():
            continue
        photos = [f for f in folder.iterdir() if f.suffix.lower() in IMAGE_SUFFIXES]
        albums.append(
            {
                "album": folder.name,
                "photos": len(photos),
                "megabytes": round(sum(f.stat().st_size for f in photos) / 1024 / 1024, 1),
            }
        )
    return albums


@server.tool(
    description="Show photos from one album, inline. Ask for an album by name (a "
    "substring is enough, e.g. 'Week 1'); use offset to page through a big album."
)
def get_photos(album: str, limit: int = 4, offset: int = 0) -> list:
    root = _materials_dir() / "Photos"
    folders = [f for f in root.iterdir() if f.is_dir() and album.lower() in f.name.lower()] \
        if root.exists() else []
    if not folders:
        return [f"No album matching {album!r}. Call list_albums() to see what is here."]

    folder = folders[0]
    photos = sorted(f for f in folder.iterdir() if f.suffix.lower() in IMAGE_SUFFIXES)
    window = photos[offset:offset + min(limit, MAX_INLINE_PHOTOS)]
    parts: list = [
        json.dumps(
            {"album": folder.name, "photos": len(photos), "showing": [p.name for p in window],
             "offset": offset, "directory": str(folder)},
            ensure_ascii=False, indent=2,
        )
    ]
    parts.extend(_inline_image(p) for p in window)
    return parts


def _inline_image(path: Path) -> Image:
    """Downscale before returning: the originals are 4032x3024 and ~7MB each."""
    with pymupdf.open(path) as document:
        page = document[0]
        scale = min(1.0, PHOTO_MAX_EDGE / max(page.rect.width, page.rect.height))
        pixmap = page.get_pixmap(matrix=pymupdf.Matrix(scale, scale))
        return Image(data=pixmap.tobytes("jpeg", jpg_quality=70), format="jpeg")


@server.tool(description="List archived homeroom posts, newest first.")
def list_posts(limit: int = 20) -> list[dict]:
    posts = sorted(_posts_dir().glob("*.md"), reverse=True)[:limit]
    found = []
    for path in posts:
        header = path.read_text(encoding="utf-8", errors="replace")[:400]
        author = re.search(r"^author: (.+)$", header, re.M)
        posted = re.search(r"^posted_at: (.+)$", header, re.M)
        found.append(
            {
                "path": str(path.relative_to(DATA_DIR)),
                "posted_at": posted.group(1) if posted else "",
                "author": author.group(1) if author else "",
                "has_summary": "## AI summary" in path.read_text(encoding="utf-8", errors="replace"),
            }
        )
    return found


class RequireBearerToken:
    """Reject anything without the shared token.

    Raw ASGI rather than BaseHTTPMiddleware, which buffers responses and would
    break the streaming transport.
    """

    def __init__(self, app, token: str) -> None:
        self.app = app
        self.expected = f"Bearer {token}"

    async def __call__(self, scope, receive, send) -> None:
        if scope["type"] != "http":
            return await self.app(scope, receive, send)

        offered = dict(scope.get("headers") or {}).get(b"authorization", b"").decode()
        if not secrets.compare_digest(offered, self.expected):
            body = b'{"error":"unauthorized"}'
            await send({"type": "http.response.start", "status": 401,
                        "headers": [(b"content-type", b"application/json"),
                                    (b"content-length", str(len(body)).encode())]})
            await send({"type": "http.response.body", "body": body})
            return
        await self.app(scope, receive, send)


def main() -> None:
    global DATA_DIR
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data-dir", default=str(DATA_DIR),
                        help="the directory holding posts/ and materials/")
    parser.add_argument("--http", action="store_true",
                        help="serve over streamable HTTP instead of stdio")
    parser.add_argument("--host", default="127.0.0.1",
                        help="address to bind when serving over HTTP, e.g. 10.0.0.4 or a "
                             "Tailscale address. Never 0.0.0.0: this archive is private.")
    parser.add_argument("--port", type=int, default=8848)
    parser.add_argument("--token", default=os.environ.get("MCP_TOKEN", ""),
                        help="require this bearer token over HTTP (or set MCP_TOKEN). "
                             "Without one the archive is readable by anyone who can "
                             "reach the port.")
    parser.add_argument("--build-index", action="store_true",
                        help="build the semantic index and exit")
    args = parser.parse_args()

    DATA_DIR = Path(args.data_dir).expanduser().resolve()
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    # The embedding backend reads OPENAI_API_KEY from the environment; the sync
    # pipeline gets it through config.py, so load the same .env here.
    load_dotenv(DATA_DIR / ".env")
    load_dotenv(find_dotenv(usecwd=True))

    if args.build_index:
        logger.info("Indexing %s", DATA_DIR)
        print(json.dumps(reindex(), indent=2))
        return

    logger.info("Serving the homeroom archive at %s", DATA_DIR)
    if not args.http:
        server.run()
        return

    if args.host == "0.0.0.0":  # noqa: S104 - refused on purpose
        raise SystemExit("Refusing to bind 0.0.0.0: name the interface explicitly.")

    security = TransportSecuritySettings(
        allowed_hosts=[args.host, f"{args.host}:{args.port}", "localhost",
                       f"localhost:{args.port}"],
        allowed_origins=["*"],
    )
    app = server.streamable_http_app(transport_security=security, host=args.host)
    if args.token:
        app = RequireBearerToken(app, args.token)
        logger.info("Requiring a bearer token")
    else:
        logger.warning("No --token given: anyone who can reach %s:%d can read the "
                       "archive", args.host, args.port)
    logger.info("Listening on http://%s:%d/mcp", args.host, args.port)
    uvicorn.run(app, host=args.host, port=args.port, log_level="info")


if __name__ == "__main__":
    main()
