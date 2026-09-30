"""Find one child in the class photo albums, locally.

Every album photo is run once through a face detector/embedder (insightface,
CPU) and the face embeddings are cached under DATA_DIR/.faces. A person is
"learned" from a handful of photos a parent points at: the face that recurs
across those photos is taken as the reference. Matching is cosine similarity
against the references; the results are recorded so the same photo is never
judged twice and later syncs only look at new photos.

    python app/faces.py --data-dir ~/homeroom scan
    python app/faces.py --data-dir ~/homeroom learn alex "Week 6:3,7,12" "Week 5:2"
    python app/faces.py --data-dir ~/homeroom match alex --sheet review.jpg

Nothing leaves the machine: the photographs are other people's children.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
from dataclasses import dataclass, field
from pathlib import Path

# onnxruntime spawns a thread per core per op and thrashes a small box; set this
# before it is imported. The digest cron shares the machine, so stay modest.
os.environ.setdefault("OMP_NUM_THREADS", os.environ.get("FACE_THREADS", "3"))

import numpy as np  # noqa: E402

logger = logging.getLogger(__name__)

IMAGE_SUFFIXES = {".jpg", ".jpeg", ".png"}
DETECT_MAX_EDGE = 1600      # full 4032px originals take 10x longer for no gain
MIN_DET_SCORE = 0.6
DEFAULT_THRESHOLD = 0.45    # cosine on normed buffalo_l embeddings; ~0.4-0.5 is usual
THUMB, COLS = 320, 6


def default_min_face() -> float:
    """Smallest face that still counts, as a share of the image's short edge. Off by
    default: a class photo is a photo of every child in it, however small."""
    return float(os.environ.get("FACE_MIN_SIZE", "0"))


def default_min_prominence() -> float:
    """Smallest face relative to the largest face in the same photo, below which a
    photo is not reported at all. Off by default: see default_frame_below."""
    return float(os.environ.get("FACE_MIN_PROMINENCE", "0"))


def default_frame_below() -> float:
    """When the child's face is this much smaller than the largest face in the
    photo, they were in the background: show the photo reframed around them
    instead of the whole frame (in sheets and in what the bot hands back)."""
    return float(os.environ.get("FACE_FRAME_BELOW", "0.5"))


def frame_around(image, box: list[int], context: float = 6.0, min_share: float = 0.45):
    """Crop `image` (PIL) to a window centred on the face box: `context` face-heights
    tall, never less than `min_share` of the short edge, same aspect as the photo,
    clamped to the picture."""
    w, h = image.size
    x0, y0, x1, y1 = box
    cx, cy = (x0 + x1) / 2, (y0 + y1) / 2
    win_h = min(h, max(context * (y1 - y0), min_share * min(w, h)))
    win_w = min(w, win_h * w / h)
    left = int(min(max(0, cx - win_w / 2), w - win_w))
    top = int(min(max(0, cy - win_h / 2), h - win_h))
    return image.crop((left, top, int(left + win_w), int(top + win_h)))


def render(data_dir: Path, match: "Match", max_edge: int = 900):
    """The photo as a parent should see it: whole when the child is a proper part
    of it, reframed around them when they were in the background. Downscaled."""
    from PIL import Image, ImageOps

    image = ImageOps.exif_transpose(Image.open(data_dir / match.path)).convert("RGB")
    if match.box and match.prominence < default_frame_below():
        image = frame_around(image, match.box)
    image.thumbnail((max_edge, max_edge))
    return image


@dataclass
class Face:
    box: list[int]
    score: float
    embedding: np.ndarray


@dataclass
class Match:
    path: str
    score: float
    box: list[int] = field(default_factory=list)
    face: float = 0.0  # face height as a share of the image's short edge
    prominence: float = 1.0  # face height relative to the largest face in the photo


class FaceIndex:
    def __init__(self, data_dir: Path) -> None:
        self.data_dir = data_dir
        self.root = data_dir / ".faces"
        self.photos_dir = data_dir / "materials" / "Photos"
        self._cache_path = self.root / "faces.npz"
        self._people_path = self.root / "people.json"
        self._matches_path = self.root / "matches.json"
        self._cache: dict[str, dict] | None = None
        self._app = None

    # --- storage -------------------------------------------------------------

    def _load_cache(self) -> dict[str, dict]:
        if self._cache is None:
            self._cache = {}
            if self._cache_path.exists():
                with np.load(self._cache_path, allow_pickle=True) as data:
                    self._cache = data["cache"].item()
        return self._cache

    def _save_cache(self) -> None:
        self.root.mkdir(parents=True, exist_ok=True)
        np.savez_compressed(self._cache_path, cache=np.array(self._cache, dtype=object))

    def people(self) -> dict[str, dict]:
        return json.loads(self._people_path.read_text()) if self._people_path.exists() else {}

    def matches(self) -> dict[str, dict[str, dict]]:
        """{person: {photo_path: {"score": s, "box": [...]}}}."""
        return json.loads(self._matches_path.read_text()) if self._matches_path.exists() else {}

    def _write(self, path: Path, payload) -> None:
        self.root.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(payload, ensure_ascii=False, indent=1), encoding="utf-8")

    # --- detection -----------------------------------------------------------

    def _model(self):
        if self._app is None:
            from insightface.app import FaceAnalysis

            self._app = FaceAnalysis(name="buffalo_l", providers=["CPUExecutionProvider"])
            self._app.prepare(ctx_id=-1, det_size=(640, 640))
        return self._app

    @staticmethod
    def _image_size(path: Path) -> tuple[int, int]:
        from PIL import Image, ImageOps

        with Image.open(path) as image:
            return ImageOps.exif_transpose(image).size

    def _detect(self, path: Path) -> list[Face]:
        import cv2
        from PIL import Image, ImageOps

        # EXIF orientation matters: phones store landscape pixels for portrait shots.
        image = ImageOps.exif_transpose(Image.open(path)).convert("RGB")
        scale = min(1.0, DETECT_MAX_EDGE / max(image.size))
        if scale < 1.0:
            image = image.resize((round(image.width * scale), round(image.height * scale)))
        bgr = cv2.cvtColor(np.asarray(image), cv2.COLOR_RGB2BGR)
        faces = []
        for f in self._model().get(bgr):
            if float(f.det_score) < MIN_DET_SCORE:
                continue
            box = [int(v / scale) for v in f.bbox]
            faces.append(Face(box=box, score=float(f.det_score), embedding=f.normed_embedding))
        return faces

    def photos(self) -> list[Path]:
        if not self.photos_dir.exists():
            return []
        return sorted(
            p for p in self.photos_dir.rglob("*") if p.suffix.lower() in IMAGE_SUFFIXES
        )

    def relative(self, path: Path) -> str:
        return str(path.relative_to(self.data_dir))

    def scan(self, paths: list[Path] | None = None) -> int:
        """Embed every photo not yet in the cache. Returns how many were new."""
        cache = self._load_cache()
        todo = [p for p in (paths or self.photos()) if self.relative(p) not in cache]
        for i, path in enumerate(todo, 1):
            try:
                faces = self._detect(path)
            except Exception as exc:  # noqa: BLE001 - one bad file must not stop the run
                logger.warning("Could not read %s: %s", path, exc)
                faces = []
            try:
                size = list(self._image_size(path))
            except Exception:  # noqa: BLE001
                size = [0, 0]
            cache[self.relative(path)] = {
                "boxes": [f.box for f in faces],
                "scores": [f.score for f in faces],
                "embeddings": np.array([f.embedding for f in faces], dtype="float32"),
                "size": size,
            }
            if i % 25 == 0 or i == len(todo):
                logger.info("Scanned %d/%d photos", i, len(todo))
                self._save_cache()
        return len(todo)

    # --- people --------------------------------------------------------------

    def learn(self, person: str, photo_paths: list[str], min_similarity: float = 0.4) -> dict:
        """Take the face that recurs across the given photos as `person`.

        Class photos hold several children; the parent only says "this photo has
        my child in it". The child is the one face that appears in most of them.
        """
        cache = self._load_cache()
        missing = [p for p in photo_paths if p not in cache]
        if missing:
            self.scan([self.data_dir / p for p in missing])
        candidates = []  # (photo, index, embedding)
        for p in photo_paths:
            for i, emb in enumerate(cache[p]["embeddings"]):
                candidates.append((p, i, emb))
        if not candidates:
            raise ValueError("No faces found in the given photos")

        # For each candidate face, count in how many *other* photos a similar face
        # exists; the best-supported face is the child, its supporters the references.
        best, best_support = None, []
        for photo, i, emb in candidates:
            support = {}
            for other, j, other_emb in candidates:
                if other == photo:
                    continue
                sim = float(emb @ other_emb)
                if sim >= min_similarity and sim > support.get(other, (0, None))[0]:
                    support[other] = (sim, j)
            if len(support) > len(best_support):
                best, best_support = (photo, i, emb), [(o, j) for o, (s, j) in support.items()]

        photo, i, emb = best
        refs = [(photo, i)] + best_support
        embeddings = [cache[p]["embeddings"][j].tolist() for p, j in refs]
        people = self.people()
        people[person] = {
            "references": [{"path": p, "face": j} for p, j in refs],
            "embeddings": embeddings,
        }
        self._write(self._people_path, people)
        return {"person": person, "references": len(refs), "photos_given": len(photo_paths),
                "unsupported": sorted(set(photo_paths) - {p for p, _ in refs})}

    def review(self, person: str, accepted: list[str], rejected: list[str],
               min_reference_quality: float = 0.5) -> dict:
        """Fold a parent's verdicts back in: the child's face in each accepted photo
        becomes a reference (if it resembles the existing ones well enough -- a
        blurred profile makes a reference that matches everybody), and the face that
        fooled us in each rejected photo becomes a counter-example. Every recorded
        verdict is then recomputed."""
        people = self.people()
        entry = people[person]
        refs = np.array(entry["embeddings"], dtype="float32")
        cache = self._load_cache()

        def best_face(rel: str) -> tuple[np.ndarray, float, int]:
            embeddings = cache[rel]["embeddings"]
            sims = (embeddings @ refs.T).max(axis=1)
            j = int(np.argmax(sims))
            return embeddings[j], float(sims[j]), j

        added = skipped = 0
        known = {(r["path"], r["face"]) for r in entry["references"]}
        for rel in accepted:
            emb, sim, j = best_face(rel)
            if (rel, j) in known:
                continue
            if sim < min_reference_quality:
                skipped += 1
                continue
            entry["references"].append({"path": rel, "face": j})
            entry["embeddings"].append(emb.tolist())
            added += 1
        negatives = entry.setdefault("rejected", [])
        for rel in rejected:
            emb, _, j = best_face(rel)
            negatives.append({"path": rel, "face": j, "embedding": emb.tolist()})
        self._write(self._people_path, people)

        rescored = self.match(person, paths=[self.data_dir / p for p in self.matches().get(person, {})])
        return {"person": person, "references": len(entry["references"]), "added": added,
                "skipped_low_quality": skipped, "rejected": len(negatives),
                "matches_now": len(rescored)}

    def _score(self, person_entry: dict, embeddings: np.ndarray) -> tuple[float, int]:
        """Best (similarity, face index) for one photo. A face that resembles a known
        counter-example more than it resembles the references does not count."""
        refs = np.array(person_entry["embeddings"], dtype="float32")
        positive = (embeddings @ refs.T).max(axis=1)              # per face
        rejected = person_entry.get("rejected") or []
        if rejected:
            negs = np.array([r["embedding"] for r in rejected], dtype="float32")
            negative = (embeddings @ negs.T).max(axis=1)
            positive = np.where(negative > positive, 0.0, positive)
        best = int(np.argmax(positive))
        return float(positive[best]), best

    def _face_share(self, rel: str, entry: dict, box: list[int]) -> float:
        size = entry.get("size") or [0, 0]
        if not size[0]:
            try:
                size = list(self._image_size(self.data_dir / rel))
            except Exception:  # noqa: BLE001
                return 0.0
            entry["size"] = size
        return round((box[3] - box[1]) / max(1, min(size)), 4)

    def match(self, person: str, threshold: float = DEFAULT_THRESHOLD,
              paths: list[Path] | None = None, min_face: float | None = None,
              min_prominence: float | None = None) -> list[Match]:
        """Photos where `person` appears (score = best similarity to any reference).

        Records every judged photo (matched or not) so later runs only see new ones;
        pass `paths` to restrict, e.g. to what a sync just downloaded. The record
        keeps the face's size too, so `min_face` can be changed without re-judging.
        """
        people = self.people()
        if person not in people:
            raise KeyError(f"Unknown person {person!r}; run learn first")
        min_face = default_min_face() if min_face is None else min_face
        min_prominence = default_min_prominence() if min_prominence is None else min_prominence
        cache = self._load_cache()
        judged = self.matches()
        seen = judged.setdefault(person, {})
        found = []
        for path in (paths or self.photos()):
            rel = self.relative(path)
            if rel not in cache:
                self.scan([path])
            entry = cache[rel]
            if len(entry["embeddings"]) == 0:
                seen[rel] = {"score": 0.0}
                continue
            score, best_face = self._score(people[person], entry["embeddings"])
            box = entry["boxes"][best_face]
            share = self._face_share(rel, entry, box)
            tallest = max(b[3] - b[1] for b in entry["boxes"])
            prominence = round((box[3] - box[1]) / max(1, tallest), 4)
            seen[rel] = {"score": round(score, 4), "box": box, "face": share,
                         "prominence": prominence}
            if score >= threshold and share >= min_face and prominence >= min_prominence:
                found.append(Match(rel, round(score, 4), box, share, prominence))
        self._write(self._matches_path, judged)
        return sorted(found, key=lambda m: m.path)

    def photos_of(self, person: str, threshold: float = DEFAULT_THRESHOLD,
                  min_face: float | None = None,
                  min_prominence: float | None = None) -> list[Match]:
        """Recorded matches, no detection: what the bot and the mail read."""
        min_face = default_min_face() if min_face is None else min_face
        min_prominence = default_min_prominence() if min_prominence is None else min_prominence
        recorded = self.matches().get(person, {})
        return sorted(
            (Match(p, v["score"], v.get("box", []), v.get("face", 0.0), v.get("prominence", 1.0))
             for p, v in recorded.items()
             if v["score"] >= threshold and v.get("face", 0.0) >= min_face
             and v.get("prominence", 1.0) >= min_prominence),
            key=lambda m: m.path,
        )


# --- contact sheets -------------------------------------------------------------


def _font(size: int):
    from PIL import ImageFont

    for candidate in (
        "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",   # Debian
        "/System/Library/Fonts/Supplemental/Arial Bold.ttf",       # macOS
        "/System/Library/Fonts/Helvetica.ttc",
    ):
        try:
            return ImageFont.truetype(candidate, size)
        except OSError:
            continue
    return ImageFont.load_default(size=size)


def contact_sheet(data_dir: Path, matches: list[Match], out: Path, label: bool = True,
                  crop_face: bool = True, labels: list[str] | None = None) -> Path:
    """Numbered thumbnails of the matched photos: cropped to the face for a review,
    or the whole photo (crop_face=False) for a sheet meant to be looked at."""
    from PIL import Image, ImageDraw, ImageOps

    cols = max(1, min(COLS, len(matches)))
    rows = max(1, (len(matches) + cols - 1) // cols)
    sheet = Image.new("RGB", (cols * THUMB, rows * THUMB), "white")
    draw = ImageDraw.Draw(sheet)
    font = _font(30)
    for i, m in enumerate(matches):
        image = ImageOps.exif_transpose(Image.open(data_dir / m.path)).convert("RGB")
        if crop_face and m.box:
            x0, y0, x1, y1 = m.box
            pad = int(max(x1 - x0, y1 - y0) * 0.35)
            crop = image.crop((max(0, x0 - pad), max(0, y0 - pad), x1 + pad, y1 + pad))
        elif m.box and m.prominence < default_frame_below():
            crop = frame_around(image, m.box)
        else:
            crop = image
        thumb = ImageOps.fit(crop, (THUMB, THUMB))
        x, y = (i % cols) * THUMB, (i // cols) * THUMB
        sheet.paste(thumb, (x, y))
        if label:
            text = labels[i] if labels else f"{i + 1}  {m.score:.2f}"
            draw.rectangle([x, y, x + 12 + 16 * len(text), y + 36], fill="black")
            draw.text((x + 6, y + 2), text, fill="yellow", font=font)
    out.parent.mkdir(parents=True, exist_ok=True)
    sheet.save(out, quality=82)
    return out


# --- CLI ---------------------------------------------------------------------


def _resolve_marks(index: FaceIndex, marks: list[str]) -> list[str]:
    """'Week 6:3,7' -> photo paths, numbering photos in an album the way the
    review sheets do (sorted by filename, 1-based)."""
    paths = []
    for mark in marks:
        album, _, numbers = mark.partition(":")
        folders = [f for f in index.photos_dir.iterdir()
                   if f.is_dir() and album.strip().lower() in f.name.lower()]
        if len(folders) != 1:
            raise SystemExit(f"{album!r} matches {len(folders)} albums")
        photos = sorted(p for p in folders[0].iterdir() if p.suffix.lower() in IMAGE_SUFFIXES)
        for n in numbers.split(","):
            paths.append(index.relative(photos[int(n) - 1]))
    return paths


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data-dir", default=".")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("scan", help="embed every photo not yet in the cache")
    learn = sub.add_parser("learn", help="learn a person from photos they appear in")
    learn.add_argument("person")
    learn.add_argument("marks", nargs="+", help='"Album name:3,7,12" (numbers from the review sheet)')
    match = sub.add_parser("match", help="find every photo of a person")
    match.add_argument("person")
    match.add_argument("--threshold", type=float, default=DEFAULT_THRESHOLD)
    match.add_argument("--sheet", help="write a review contact sheet here")
    review = sub.add_parser(
        "review", help="fold verdicts on a review sheet back in: the numbers given are "
        "NOT the person, every other photo on that sheet is",
    )
    review.add_argument("person")
    review.add_argument("--threshold", type=float, default=DEFAULT_THRESHOLD,
                        help="the threshold the sheet was made with")
    review.add_argument("--reject", default="", help='sheet numbers, e.g. "13,14,19-27"')
    args = parser.parse_args(argv)

    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    index = FaceIndex(Path(args.data_dir).expanduser().resolve())
    if args.command == "scan":
        print(json.dumps({"scanned": index.scan(), "photos": len(index.photos())}))
    elif args.command == "learn":
        print(json.dumps(index.learn(args.person, _resolve_marks(index, args.marks)), indent=1))
    elif args.command == "match":
        found = index.match(args.person, args.threshold)
        print(json.dumps({"person": args.person, "matches": len(found),
                          "photos": len(index.photos()),
                          "files": [f"{m.path} ({m.score:.2f})" for m in found]}, indent=1,
                         ensure_ascii=False))
        if args.sheet and found:
            print("sheet:", contact_sheet(index.data_dir, found, Path(args.sheet)))
    elif args.command == "review":
        on_sheet = index.photos_of(args.person, args.threshold)   # same order as the sheet
        rejected = set()
        for part in filter(None, args.reject.replace(" ", "").split(",")):
            lo, _, hi = part.partition("-")
            rejected.update(range(int(lo), int(hi or lo) + 1))
        accepted = [m.path for i, m in enumerate(on_sheet, 1) if i not in rejected]
        wrong = [m.path for i, m in enumerate(on_sheet, 1) if i in rejected]
        print(json.dumps(index.review(args.person, accepted, wrong), indent=1))


if __name__ == "__main__":
    main()
