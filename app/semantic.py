"""Semantic search over the archive, backed by FAISS.

Embeddings are cached by the hash of the chunk text, so re-indexing after a sync
only pays for whatever actually changed -- which, for a course whose bulk is
static maths workbooks, is almost nothing.
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
from dataclasses import dataclass
from pathlib import Path

import faiss
import numpy as np
from langchain_openai import OpenAIEmbeddings

logger = logging.getLogger(__name__)

CHUNK_CHARS = 1200
CHUNK_OVERLAP = 200
EMBED_BATCH = 100
DEFAULT_MODEL = "text-embedding-3-small"


@dataclass
class Chunk:
    key: str
    path: str
    title: str
    folder: str
    kind: str
    text: str

    @property
    def digest(self) -> str:
        return hashlib.sha1(self.text.encode("utf-8")).hexdigest()


@dataclass
class Hit:
    score: float
    chunk: Chunk


def split(text: str) -> list[str]:
    """Paragraph-aware chunks, so a passage rarely starts mid-sentence."""
    text = re.sub(r"\n{3,}", "\n\n", text.strip())
    if not text:
        return []

    chunks, current = [], ""
    for paragraph in text.split("\n\n"):
        if len(current) + len(paragraph) + 2 <= CHUNK_CHARS:
            current = f"{current}\n\n{paragraph}" if current else paragraph
            continue
        if current:
            chunks.append(current)
        while len(paragraph) > CHUNK_CHARS:
            chunks.append(paragraph[:CHUNK_CHARS])
            paragraph = paragraph[CHUNK_CHARS - CHUNK_OVERLAP:]
        current = paragraph
    if current:
        chunks.append(current)
    return [c for c in chunks if c.strip()]


class SemanticIndex:
    def __init__(self, root: Path, model: str = DEFAULT_MODEL) -> None:
        self.root = root
        self.model = model
        self._index: faiss.Index | None = None
        self._chunks: list[Chunk] = []

    # --- storage ----------------------------------------------------------

    @property
    def _index_path(self) -> Path:
        return self.root / "index.faiss"

    @property
    def _chunks_path(self) -> Path:
        return self.root / "chunks.json"

    @property
    def _cache_path(self) -> Path:
        return self.root / "embeddings.npz"

    @property
    def exists(self) -> bool:
        return self._index_path.exists() and self._chunks_path.exists()

    def _load_cache(self) -> dict[str, np.ndarray]:
        if not self._cache_path.exists():
            return {}
        with np.load(self._cache_path) as data:
            return {k: data[k] for k in data.files}

    def _save_cache(self, cache: dict[str, np.ndarray]) -> None:
        self.root.mkdir(parents=True, exist_ok=True)
        np.savez(self._cache_path, **cache)

    # --- building ---------------------------------------------------------

    def build(self, documents: list[tuple[str, str, dict]]) -> dict:
        """documents: (key, text, metadata). Returns a small build report."""
        chunks: list[Chunk] = []
        for key, text, meta in documents:
            for piece in split(text):
                chunks.append(
                    Chunk(key=key, path=meta.get("path", ""), title=meta.get("title", ""),
                          folder=meta.get("folder", ""), kind=meta.get("kind", ""), text=piece)
                )

        cache = self._load_cache()
        missing = [c for c in chunks if c.digest not in cache]
        logger.info("%d chunks, %d already embedded, %d to embed",
                    len(chunks), len(chunks) - len(missing), len(missing))

        if missing:
            embeddings = OpenAIEmbeddings(model=self.model)
            for start in range(0, len(missing), EMBED_BATCH):
                batch = missing[start:start + EMBED_BATCH]
                vectors = embeddings.embed_documents([c.text for c in batch])
                for chunk, vector in zip(batch, vectors):
                    cache[chunk.digest] = np.asarray(vector, dtype="float32")
                logger.info("Embedded %d/%d", min(start + EMBED_BATCH, len(missing)), len(missing))
            self._save_cache(cache)

        matrix = np.vstack([cache[c.digest] for c in chunks]).astype("float32")
        faiss.normalize_L2(matrix)  # inner product on unit vectors == cosine
        index = faiss.IndexFlatIP(matrix.shape[1])
        index.add(matrix)

        self.root.mkdir(parents=True, exist_ok=True)
        faiss.write_index(index, str(self._index_path))
        self._chunks_path.write_text(
            json.dumps([c.__dict__ for c in chunks], ensure_ascii=False), encoding="utf-8"
        )
        self._index, self._chunks = index, chunks
        return {
            "documents": len(documents),
            "chunks": len(chunks),
            "embedded_now": len(missing),
            "dimensions": int(matrix.shape[1]),
        }

    # --- querying ---------------------------------------------------------

    def load(self) -> None:
        if self._index is not None:
            return
        if not self.exists:
            raise FileNotFoundError("the semantic index has not been built yet")
        self._index = faiss.read_index(str(self._index_path))
        self._chunks = [Chunk(**row) for row in json.loads(self._chunks_path.read_text())]

    def search(self, query: str, k: int = 8) -> list[Hit]:
        self.load()
        vector = np.asarray(
            OpenAIEmbeddings(model=self.model).embed_query(query), dtype="float32"
        ).reshape(1, -1)
        faiss.normalize_L2(vector)
        scores, positions = self._index.search(vector, min(k, len(self._chunks)))
        return [
            Hit(score=float(score), chunk=self._chunks[position])
            for score, position in zip(scores[0], positions[0])
            if position >= 0
        ]
