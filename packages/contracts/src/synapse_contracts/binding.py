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
import tempfile
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
    #: WHICH SERVICE this `shared_id` lives on. ⟨2026-08-07⟩
    #:
    #: A shared_id is only meaningful relative to a server — the ids are minted
    #: per service, and two services will happily hold different sessions. Until
    #: this field existed a binding named the session but not the server, while
    #: `service.url` sat in config as global mutable state, so REPOINTING THE
    #: CONFIG SILENTLY INVALIDATED EVERY BINDING ON THE MACHINE. They kept
    #: resolving, kept looking valid, and failed only at push time as a 404 from
    #: a service that had never heard of the id. Measured on a live machine:
    #: `service.url` moved 192.168.4.81 -> 192.168.4.44 -> localhost across one
    #: night, and 431 findings queued against a session only the first host had.
    #:
    #: REQUIRED, not optional, and deliberately so. An optional field would let
    #: a binding written before this change keep masquerading as valid, which is
    #: precisely the failure being fixed. Pre-first-release there is no install
    #: worth accommodating: an old pin fails validation, reads as "not joined",
    #: and one `join_session(...)` from the agent writes a correct one.
    service_url: str

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

    The temp file gets a UNIQUE name (2026-08-06 review). It used to be
    `<path>.tmp`, one fixed name per destination, and since W2 every bind
    refreshes the SHARED mirror `bindings/<agent>.json` — so two windows
    joining at once both wrote `bindings/claude-code.json.tmp` and both then
    `os.replace`d it. The loser does not tear a read: it raises
    `FileNotFoundError` out of `os.replace` (reproduced, 4000 iterations x 2
    processes), which nothing up the call stack catches, so a concurrent
    `synapse-worker join` dies with a traceback. `mkstemp` in the destination
    directory keeps the replace atomic (same filesystem) while making the
    two writers' temp files distinct, and the `finally` unlink means a failed
    write leaves no litter behind.
    """
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(binding.model_dump_json(indent=2))
        os.replace(tmp_name, path)
    finally:
        # Present only if the replace never happened. `missing_ok` rather than
        # a flag: the success path has already renamed it away.
        Path(tmp_name).unlink(missing_ok=True)


def same_service(a: str | None, b: str | None) -> bool:
    """Whether two service URLs name the same server.

    Trailing slashes only: `http://x:8899` and `http://x:8899/` are one server,
    and refusing on punctuation would be a worse bug than the one this check
    exists for. Deliberately NOT normalising host aliases — `localhost` and
    `127.0.0.1` usually are the same box and `192.168.4.44` might be too, but
    "usually" is what produced the silent 404s, and a binding that resolves
    against a server the operator did not name is the failure, not the fix.
    """
    if a is None or b is None:
        return True
    return a.rstrip("/") == b.rstrip("/")


def read_binding(path: Path, *,
                 expected_service_url: str | None = None) -> SessionBinding | None:
    """None if no session has been pinned, or the pin file is unreadable, or it
    belongs to a DIFFERENT service than the caller is pointed at.

    Deliberately returns None rather than raising: an absent or corrupt binding
    is a normal state (no `/synapse start` was ever run in this repo) and the
    caller's job is to fall back to detection, not to crash.

    ⟨2026-08-07⟩ `expected_service_url` is the whole point of `service_url`. A
    shared_id means nothing without the server that minted it, so a binding for
    another service must read as UNSET rather than resolve — otherwise the
    producer pushes to a service that 404s, which is invisible until someone
    reads the orchestrator log. The refusal is logged at WARNING naming BOTH
    urls, because "not joined" on its own sends you looking in the wrong place.
    Callers that legitimately do not know the service (e.g. `synapse health`
    reporting what is on disk) pass nothing and get the binding.
    """
    path = Path(path)
    if not path.is_file():
        return None
    try:
        binding = SessionBinding.model_validate_json(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, ValueError) as exc:
        logger.warning("Binding at %s is unreadable (%s); treating as unset", path, exc)
        return None
    if not same_service(binding.service_url, expected_service_url):
        logger.warning(
            "Binding at %s is for session %s on %s, but this client is pointed at "
            "%s. TREATING AS NOT JOINED: pushing to a service that never minted "
            "that id 404s every finding. Re-join from your agent to bind against "
            "the service you are actually using, or point `service.url` back.",
            path, binding.shared_id, binding.service_url, expected_service_url,
        )
        return None
    return binding


def clear_binding(path: Path) -> None:
    """Remove a pin, e.g. when a session ends. Never raises if already absent."""
    Path(path).unlink(missing_ok=True)
