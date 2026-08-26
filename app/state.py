"""Which posts have already been mailed out, persisted between runs.

The file is written atomically and after *every* successful send, so a failure
half-way through a run can never cause an already-delivered post to be mailed
again on the next run.
"""

from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

logger = logging.getLogger(__name__)

# Feeds only ever show a couple of weeks of posts, so anything beyond this is
# dead weight -- the old format grew to 200KB by keeping every post's full HTML.
MAX_ENTRIES = 500


@dataclass
class State:
    path: Path
    course_id: str = ""
    sent: dict[str, dict] = field(default_factory=dict)
    materials: dict[str, dict] = field(default_factory=dict)

    @classmethod
    def load(cls, path: Path) -> "State":
        if not path.exists():
            logger.info("No state file at %s, starting fresh", path)
            return cls(path=path)

        logger.info("Loading state from %s", path)
        with open(path, "r", encoding="utf-8") as f:
            raw = json.load(f)

        sent = raw.get("sent")
        if sent is None:
            # Legacy format: {"course_id", "downloaded", "updates": {id: <full post>}}.
            legacy = raw.get("updates", {})
            logger.info("Migrating %d entries from the legacy state format", len(legacy))
            sent = {
                post_id: {"posted_at": post.get("datetime", ""), "sent_at": ""}
                for post_id, post in legacy.items()
            }

        return cls(
            path=path,
            course_id=raw.get("course_id", ""),
            sent=sent,
            materials=raw.get("materials", {}),
        )

    def is_sent(self, post_id: str) -> bool:
        return post_id in self.sent

    def mark_sent(self, post_id: str, subject: str, posted_at: str) -> None:
        self.sent[post_id] = {
            "subject": subject,
            "posted_at": posted_at,
            "sent_at": datetime.now().astimezone().isoformat(timespec="seconds"),
        }

    def save(self) -> None:
        self._prune()
        payload = {
            "course_id": self.course_id,
            "sent": self.sent,
            "materials": self.materials,
        }
        tmp = self.path.with_suffix(self.path.suffix + ".tmp")
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(payload, f, indent=2, ensure_ascii=False)
            f.flush()
            os.fsync(f.fileno())
        tmp.replace(self.path)
        logger.info("Saved state to %s (%d posts)", self.path, len(self.sent))

    def _prune(self) -> None:
        overflow = len(self.sent) - MAX_ENTRIES
        if overflow > 0:
            for post_id in list(self.sent)[:overflow]:
                del self.sent[post_id]
