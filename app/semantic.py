"""Semantic search over the archive, backed by FAISS.

Embeddings are cached by the hash of the chunk text, so re-indexing after a sync
only pays for whatever actually changed -- which, for a course whose bulk is
static maths workbooks, is almost nothing.

Two embedding backends, picked by EMBEDDING_BACKEND:
- qwen3 (default): Qwen3-Embedding-0.6B, int8 ONNX, run locally on the CPU. The
  model (~600 MB) is downloaded once into DATA_DIR/.models. Multilingual, so a
  Chinese or Japanese question finds the English post.
- openai: text-embedding-3-small over the API (needs OPENAI_API_KEY).
Each backend keeps its own index directory, so switching never mixes vectors.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import threading
from dataclasses import dataclass
from pathlib import Path

import faiss
import numpy as np

logger = logging.getLogger(__name__)

CHUNK_CHARS = 1200
CHUNK_OVERLAP = 200
EMBED_BATCH = 100
DEFAULT_MODEL = "text-embedding-3-small"

QWEN3_REPO = "onnx-community/Qwen3-Embedding-0.6B-ONNX"
QWEN3_FILES = ("onnx/model_int8.onnx", "tokenizer.json")
QWEN3_SLUG = "qwen3-0.6b"
QWEN3_MAX_TOKENS = 1024   # a 1200-character chunk is ~300-600 tokens
QWEN3_BATCH = 8
QWEN3_QUERY_PREFIX = (
    "Instruct: Given a parent's question, retrieve passages from a school homeroom "
    "archive that answer it\nQuery: "
)


def backend_name() -> str:
    return os.environ.get("EMBEDDING_BACKEND", "qwen3").strip().lower() or "qwen3"


class OpenAIEmbedder:
    def __init__(self, model: str = DEFAULT_MODEL) -> None:
        from langchain_openai import OpenAIEmbeddings

        self._client = OpenAIEmbeddings(model=model)

    def embed_documents(self, texts: list[str]) -> list[np.ndarray]:
        out = []
        for start in range(0, len(texts), EMBED_BATCH):
            out.extend(self._client.embed_documents(texts[start:start + EMBED_BATCH]))
        return [np.asarray(v, dtype="float32") for v in out]

    def embed_query(self, text: str) -> np.ndarray:
        return np.asarray(self._client.embed_query(text), dtype="float32")


class Qwen3Embedder:
    """Qwen3-Embedding-0.6B on onnxruntime: last-token pooling, left padding.

    The ONNX export is the decoder with a KV cache, so every layer's past key and
    value is fed empty (past length 0); only last_hidden_state is used.
    """

    def __init__(self, model_dir: Path) -> None:
        import onnxruntime as ort
        from tokenizers import Tokenizer

        model_dir = Path(model_dir)
        if not all((model_dir / f).exists() for f in QWEN3_FILES):
            from huggingface_hub import hf_hub_download

            logger.info("Downloading %s into %s (one time, ~600 MB)", QWEN3_REPO, model_dir)
            for f in QWEN3_FILES:
                hf_hub_download(QWEN3_REPO, f, local_dir=str(model_dir))

        options = ort.SessionOptions()
        options.intra_op_num_threads = int(os.environ.get("EMBED_THREADS", "3"))
        self._session = ort.InferenceSession(
            str(model_dir / QWEN3_FILES[0]), options, providers=["CPUExecutionProvider"]
        )
        self._tokenizer = Tokenizer.from_file(str(model_dir / QWEN3_FILES[1]))
        self._eos = self._tokenizer.token_to_id("<|endoftext|>")
        self._past = [
            (i.name, [d if isinstance(d, int) else 0 for d in i.shape][1:])
            for i in self._session.get_inputs()
            if i.name.startswith("past_key_values.")
        ]
        self._lock = threading.Lock()  # one inference at a time on a 4-core box
        logger.info("Loaded Qwen3 embedding model from %s", model_dir)

    def _ids(self, text: str) -> list[int]:
        # The tokenizer appends <|endoftext|> itself; keep it when cutting a long
        # text short, because the pooled vector is the one at that last token.
        ids = self._tokenizer.encode(text).ids
        if len(ids) > QWEN3_MAX_TOKENS:
            ids = ids[:QWEN3_MAX_TOKENS - 1] + [self._eos]
        return ids

    def _embed(self, texts: list[str]) -> list[np.ndarray]:
        out: list[np.ndarray] = []
        for start in range(0, len(texts), QWEN3_BATCH):
            batch = [self._ids(t) for t in texts[start:start + QWEN3_BATCH]]
            width = max(len(ids) for ids in batch)
            input_ids = np.zeros((len(batch), width), dtype="int64")
            mask = np.zeros((len(batch), width), dtype="int64")
            for row, ids in enumerate(batch):  # left padding: real tokens end at the right edge
                input_ids[row, width - len(ids):] = ids
                mask[row, width - len(ids):] = 1
            feeds = {
                "input_ids": input_ids,
                "attention_mask": mask,
                "position_ids": np.clip(np.cumsum(mask, axis=1) - 1, 0, None),
            }
            for name, (heads, _, dim) in self._past:
                feeds[name] = np.zeros((len(batch), heads, 0, dim), dtype="float32")
            with self._lock:
                hidden = self._session.run(["last_hidden_state"], feeds)[0]
            vectors = hidden[:, -1, :].astype("float32")
            vectors /= np.linalg.norm(vectors, axis=1, keepdims=True)
            out.extend(vectors)
        return out

    def embed_documents(self, texts: list[str]) -> list[np.ndarray]:
        return self._embed(texts)

    def embed_query(self, text: str) -> np.ndarray:
        return self._embed([QWEN3_QUERY_PREFIX + text])[0]


_embedders: dict[tuple, object] = {}
_embedders_lock = threading.Lock()


def get_embedder(backend: str, data_dir: Path, model: str = DEFAULT_MODEL):
    """One embedder per process: a long-running server loads the model once."""
    key = (backend, str(data_dir), model)
    with _embedders_lock:
        if key not in _embedders:
            if backend == "qwen3":
                model_dir = Path(
                    os.environ.get("EMBEDDING_MODEL_DIR")
                    or Path(data_dir) / ".models" / "qwen3-embedding-0.6b"
                )
                _embedders[key] = Qwen3Embedder(model_dir)
            elif backend == "openai":
                _embedders[key] = OpenAIEmbedder(model)
            else:
                raise ValueError(f"EMBEDDING_BACKEND must be qwen3 or openai, not {backend!r}")
        return _embedders[key]


def _replace_atomically(path: Path, write) -> None:
    """Write next to the target, then rename, so a reader never sees half a file."""
    tmp = path.with_name(f".{path.stem}.tmp{path.suffix}")
    write(tmp)
    os.replace(tmp, path)


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
    def __init__(self, root: Path, model: str = DEFAULT_MODEL, backend: str | None = None) -> None:
        """root is DATA_DIR/.semantic. The OpenAI index lives there directly (where it
        always has); every other backend gets a subdirectory of its own."""
        self.backend = backend or backend_name()
        self.data_dir = Path(root).parent
        self.root = Path(root) if self.backend == "openai" else Path(root) / QWEN3_SLUG
        self.model = model
        self._index: faiss.Index | None = None
        self._chunks: list[Chunk] = []

    @property
    def embedder(self):
        return get_embedder(self.backend, self.data_dir, self.model)

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
        _replace_atomically(self._cache_path, lambda tmp: np.savez(tmp, **cache))

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
            embedder = self.embedder
            # Saved every batch, so an interrupted first build resumes where it stopped.
            for start in range(0, len(missing), EMBED_BATCH):
                batch = missing[start:start + EMBED_BATCH]
                vectors = embedder.embed_documents([c.text for c in batch])
                for chunk, vector in zip(batch, vectors):
                    cache[chunk.digest] = np.asarray(vector, dtype="float32")
                self._save_cache(cache)
                logger.info("Embedded %d/%d", min(start + EMBED_BATCH, len(missing)), len(missing))

        matrix = np.vstack([cache[c.digest] for c in chunks]).astype("float32")
        faiss.normalize_L2(matrix)  # inner product on unit vectors == cosine
        index = faiss.IndexFlatIP(matrix.shape[1])
        index.add(matrix)

        self.root.mkdir(parents=True, exist_ok=True)
        _replace_atomically(self._index_path, lambda tmp: faiss.write_index(index, str(tmp)))
        _replace_atomically(self._chunks_path, lambda tmp: tmp.write_text(
            json.dumps([c.__dict__ for c in chunks], ensure_ascii=False), encoding="utf-8"
        ))
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
        vector = np.asarray(self.embedder.embed_query(query), dtype="float32").reshape(1, -1)
        faiss.normalize_L2(vector)
        scores, positions = self._index.search(vector, min(k, len(self._chunks)))
        return [
            Hit(score=float(score), chunk=self._chunks[position])
            for score, position in zip(scores[0], positions[0])
            if position >= 0
        ]
