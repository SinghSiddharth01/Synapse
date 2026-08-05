"""Append-only record of every segment triage skipped — the full Segment.

Why the whole segment rather than a byte range: the segment is already in
memory at skip time, serializing it makes replay EXACT rather than a re-parse
approximation, and the cost is a few KB per skip on the same disk that holds
the transcript itself. A wrong skip is therefore never permanent loss —
`synapse-worker replay --skipped` re-distils from here.

Same crash posture as the producer's write-ahead log: append-only, corrupt
lines skipped loudly, archive-by-rename so nothing is ever overwritten.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from pathlib import Path

from synapse_contracts import Segment

logger = logging.getLogger(__name__)

SKIPS_FILENAME = "triage-skips.jsonl"


class TriageLog:
    def __init__(self, state_dir: Path) -> None:
        self.state_dir = Path(state_dir)
        self.state_dir.mkdir(parents=True, exist_ok=True)
        self.path = self.state_dir / SKIPS_FILENAME

    def record_skip(self, segment: Segment, reason: str) -> None:
        entry = {
            "ts": datetime.now(timezone.utc).isoformat(),
            "reason": reason,
            "segment": segment.model_dump(mode="json"),
        }
        with self.path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(entry) + "\n")

    def load_skipped(self) -> list[tuple[Segment, str]]:
        if not self.path.is_file():
            return []
        out: list[tuple[Segment, str]] = []
        for line in self.path.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            try:
                entry = json.loads(line)
                out.append((Segment.model_validate(entry["segment"]), entry["reason"]))
            except (json.JSONDecodeError, KeyError, ValueError) as exc:
                logger.warning("Skipping corrupt triage-log line (%s)", exc)
        return out

    def archive(self) -> Path | None:
        if not self.path.is_file():
            return None
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S")
        target = self.path.with_name(f"triage-skips.replayed-{stamp}.jsonl")
        self.path.rename(target)
        return target
