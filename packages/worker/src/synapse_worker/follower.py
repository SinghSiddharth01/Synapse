"""Durable-offset follower: read only what was appended since last time.

This is the "delta" in "condense the conversation delta each period". It exists
because of Plan A.6's constraint: **the worker never re-reads a transcript
position**. Distillation is the most expensive step in the system and it is
unrepeatable, so re-reading means paying NPU time twice, and losing the offset
means either duplicate findings or a permanently lost stretch of conversation.

Three details that are easy to get wrong and expensive to get wrong:

1. **Partial trailing lines.** The transcript is being appended to by another
   process. A read can land mid-line. Only complete newline-terminated lines are
   returned; the remainder stays unread and the offset stops before it, so the
   line is picked up whole on the next tick.

2. **Stat-gating.** An idle tick costs one `stat` call rather than an open and a
   seek. This is what makes a short poll interval cheap enough that polling
   beats a filesystem watch here.

3. **Atomic state writes.** The offset file is written to a temporary path and
   moved into place, so a crash mid-write leaves the previous offset rather than
   a truncated file. A corrupt offset file is worse than a stale one: stale
   costs duplicate work, corrupt loses the position entirely.
"""

from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass, field
from pathlib import Path

logger = logging.getLogger(__name__)


@dataclass
class FollowState:
    """Where we got to in each followed file. Persisted between runs."""

    offsets: dict[str, int] = field(default_factory=dict)
    sizes: dict[str, int] = field(default_factory=dict)

    def to_json(self) -> str:
        return json.dumps({"offsets": self.offsets, "sizes": self.sizes}, indent=2)

    @classmethod
    def from_json(cls, raw: str) -> FollowState:
        data = json.loads(raw)
        return cls(
            offsets={str(k): int(v) for k, v in data.get("offsets", {}).items()},
            sizes={str(k): int(v) for k, v in data.get("sizes", {}).items()},
        )


class TranscriptFollower:
    """Yields newly-appended complete lines from a set of files."""

    def __init__(self, state_path: Path) -> None:
        self.state_path = Path(state_path)
        self.state = self._load_state()

    def _load_state(self) -> FollowState:
        if not self.state_path.is_file():
            return FollowState()
        try:
            return FollowState.from_json(self.state_path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, ValueError, OSError) as exc:
            # Refuse to guess. Starting from zero would re-distil the whole
            # transcript at ~13 tok/s; starting from the end would silently drop
            # everything written so far. Make the operator decide.
            raise RuntimeError(
                f"Follow state at {self.state_path} is unreadable ({exc}). "
                f"Delete it to restart from the beginning of every transcript "
                f"(this re-distils everything), or restore it from a backup."
            ) from exc

    def save(self) -> None:
        """Atomic: write to a temp file, then replace."""
        self.state_path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.state_path.with_suffix(self.state_path.suffix + ".tmp")
        tmp.write_text(self.state.to_json(), encoding="utf-8")
        os.replace(tmp, self.state_path)

    def has_new_data(self, path: Path) -> bool:
        """One stat call. False means there is nothing to do this tick."""
        try:
            size = path.stat().st_size
        except OSError:
            return False
        return size != self.state.sizes.get(str(path), -1)

    def start_at_end(self, path: Path) -> None:
        """Mark everything currently in the file as already seen.

        For attaching to a conversation already in progress without distilling
        its entire history — which, on a 2.9 MB transcript, would be hours of
        NPU time for context nobody asked for.
        """
        key = str(path)
        try:
            size = path.stat().st_size
        except OSError:
            return
        self.state.offsets[key] = size
        self.state.sizes[key] = size

    def read_new_lines(self, path: Path) -> list[str]:
        """Complete lines appended since the last call. Advances the offset."""
        key = str(path)
        try:
            size = path.stat().st_size
        except OSError:
            logger.debug("Transcript %s is not readable this tick", path)
            return []

        offset = self.state.offsets.get(key, 0)

        # Rotation or truncation: the file is smaller than where we were. The
        # old content is gone, so the only safe position is the new start.
        if size < offset:
            logger.info(
                "Transcript %s shrank (%d -> %d); it was rotated or truncated. "
                "Resuming from the start of the new file.",
                path,
                offset,
                size,
            )
            offset = 0

        if size == offset:
            self.state.sizes[key] = size
            return []

        with path.open("rb") as handle:
            handle.seek(offset)
            chunk = handle.read(size - offset)

        # Stop at the last newline. Anything after it is a line still being
        # written; leaving it unread means it arrives whole next tick.
        last_newline = chunk.rfind(b"\n")
        if last_newline == -1:
            # A very long line still in flight. Do not advance.
            self.state.sizes[key] = size
            return []

        consumed = chunk[: last_newline + 1]
        self.state.offsets[key] = offset + len(consumed)
        self.state.sizes[key] = size

        text = consumed.decode("utf-8", errors="replace")
        # Split on \n and strip a trailing \r rather than using splitlines():
        # splitlines() also breaks on \v, \f and \x1c, which would tear a record
        # apart if one ever appeared unescaped. Live Claude Code transcripts use
        # bare LF (verified 2026-08-04), but a transcript written by another tool
        # or platform may well be CRLF, and a trailing \r reaching the parser is
        # the kind of thing that works until it silently does not.
        return [
            stripped
            for line in text.split("\n")
            if (stripped := line.rstrip("\r")).strip()
        ]
