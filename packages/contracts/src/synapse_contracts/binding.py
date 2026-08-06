"""SessionBinding — the on-disk record produced by an explicit join.

Not part of the original Plan 0 freeze in `schemas.py`; added when the worker's
transcript selection turned out to be a pure heuristic (most-recently-written
`.jsonl` under the project's Claude Code directory) with no way for a user to
say "no, follow *that* conversation specifically." Two Claude Code windows open
on the same repo made that heuristic genuinely ambiguous.

`SessionBinding` is what `LocalBinding` becomes once persisted: the same
`{agent_session_id, shared_id, contributor, agent}` plus which transcript file
it points at and when the pin was made. The Orchestrator writes it — the local
hub is where `CONTEXT.md` says `LocalBinding` lives — and the worker reads it
instead of re-guessing on every tick. Living in `synapse_contracts` rather than
in either package keeps the read side and the write side from needing a
dependency on each other; both already depend on contracts.

File presence is the only "is a session active" signal — there is no separate
flag. One file, one binding: `CONTEXT.md`'s documented limitation of one active
Agent Session per Agent product per machine WAS enforced by only ever writing to
a single known path. Since 2026-08-06 (W2) that limitation is lifted: the worker
writes one file per Agent SESSION under `bindings/<agent>/<session>.json` and
keeps refreshing the single-file `bindings/<agent>.json` as a compatibility
mirror, so a reader that has not been upgraded still sees exactly the
most-recently-joined binding it saw before. The record itself did not have to
change for that — a binding names its own `agent_session_id` and always has.

`scope` is the one field the split did need. It answers "who does this binding
speak for": a `session`-scoped binding (every binding a real `join` writes) is
one conversation's, and a reader comparing its own session id against it should
refuse on a mismatch; a `machine`-scoped binding is a stand-in written before
any conversation exists (`scripts/serve_local.py`), meaning "this machine is
joined — any conversation here speaks for it, under its own real session id".
Absent in every file written before this field existed, which is why the default
is `session`: the stricter of the two, so an old file never silently widens.
"""

from __future__ import annotations

import json
import logging
import os
from datetime import datetime
from pathlib import Path
from typing import Literal

from pydantic import BaseModel

from synapse_contracts.schemas import LocalBinding

logger = logging.getLogger(__name__)


class SessionBinding(BaseModel):
    """A LocalBinding pinned to a specific transcript file, persisted to disk."""

    agent_session_id: str
    shared_id: str
    contributor: str
    agent: str
    transcript_path: str
    pinned_at: datetime
    scope: Literal["session", "machine"] = "session"

    def to_local_binding(self) -> LocalBinding:
        return LocalBinding(
            agent_session_id=self.agent_session_id,
            shared_id=self.shared_id,
            contributor=self.contributor,
            agent=self.agent,
        )


def write_binding(path: Path, binding: SessionBinding) -> None:
    """Atomic: write to a temp file, then replace.

    Mirrors the pattern in synapse_worker.follower.TranscriptFollower.save —
    a crash mid-write must never leave a corrupt binding, since the worker
    would otherwise fail to start or silently fall back to the heuristic this
    file exists to replace.
    """
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(binding.model_dump_json(indent=2), encoding="utf-8")
    os.replace(tmp, path)


def read_binding(path: Path) -> SessionBinding | None:
    """None if no session has been pinned, or the pin file is unreadable.

    Deliberately returns None rather than raising: an absent or corrupt binding
    is a normal state (no `/synapse start` was ever run in this repo) and the
    caller's job is to fall back to detection, not to crash.
    """
    path = Path(path)
    if not path.is_file():
        return None
    try:
        return SessionBinding.model_validate_json(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, ValueError) as exc:
        logger.warning("Binding at %s is unreadable (%s); treating as unset", path, exc)
        return None


def clear_binding(path: Path) -> None:
    """Remove a pin, e.g. when a session ends. Never raises if already absent."""
    Path(path).unlink(missing_ok=True)
