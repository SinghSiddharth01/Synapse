"""What this machine remembers about a Shared Session ITSELF: who created it,
and what it is for.

`sessions.json` in the orchestrator's state dir, `shared_id -> {created_by,
purpose}`, written when THIS machine creates (`server.create_session`) or joins
(`server.join_session`) a session and read by `cli.cmd_resync`.

WHY THIS EXISTS (2026-08-06). The Relay's retained log carries FINDINGS and
nothing about the session they belong to. So after a genuine service restart —
the case the whole recovery path exists for — `cmd_resync`'s create-or-return
pass had no session identity to send and made one up:
`{"purpose": "(recovered by resync)", "created_by": "resync"}`. Against a live
service that is harmless (`store.create_session` returns an existing session
unchanged), which is exactly why it survived: the only run in which it does
anything is the one nobody rehearses. After a restart the store is empty, so
that POST is a real CREATE, and `api.end_session`'s creator-only gate
(`if ended_by != session.created_by: 403`) then refused the human who actually
created the session — "only resync can end this session" — while admitting
anyone who had read the source and sent `{"ended_by": "resync"}`. A gate that
refuses the owner and admits everyone else is worse than no gate.

The fix is to stop inventing: send the REAL creator and purpose when this
machine has a record, and `created_by=None` — an honest unknown, never a
sentinel string — when it does not. `None` is what tells the service to fall
back to "any current member may end it" instead of naming a creator that never
existed.

STOPGAP, exactly like `synapse_orchestrator.ended`, and for the same reason.
Session identity is not durable because the SERVICE's log is not durable
(`synapse_service.store` is entirely in-memory); the real fix is service-side
log persistence (STATE.md's first post-demo entry, the lifecycle spec's
"Durability caveat"), after which the session survives the restart and there is
nothing for this file to restore. Until then, this is a per-machine cache and
its limits are worth stating plainly: **another machine's resync knows nothing
about it.** If Aditya created the session and Akhil's orchestrator is the one
that resyncs after the restart, Akhil's machine has a record only if Akhil
joined through the tools, and if he joined before the service half started
returning `created_by` it holds `None` — so the session comes back ownerless and
falls to the membership gate. That is the honest outcome, and it is the reason
this is labelled a stopgap rather than a design.

WHY A SIBLING OF `ended.py` RATHER THAN PART OF IT. `ended.py` is cohesive
around ONE fact — "this Shared Session is closed" — plus the wire-level
recogniser (`is_session_ended`) for the 409 that announces it; its docstring's
first sentence says so. Session identity is a different fact with a different
shape (a map, not a set), a different writer (create/join, not end), and a
different failure mode when it is lost (a wrong owner, versus a resurrected
session). Filing "who created this" under `ended.py` would have made that
module's name actively misleading to the next reader looking for it. Rejected
alternative: factoring the atomic-write dance out of both into a shared
`_atomic.py`. It is four lines, and the two modules genuinely differ in what
they do on a corrupt read (an empty set versus an empty map) and in their
idempotence check — a third module would cost more than it saved.

Growth is unbounded in principle: one small object per Shared Session this
machine has ever created or joined, never pruned, exactly as `ended.json` is
never pruned. At one JSON object per session that is not worth a compaction
pass; if either file ever needs one they should get it together.
"""

from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass
from pathlib import Path

logger = logging.getLogger(__name__)

_SESSIONS_FILE = "sessions.json"

# The purpose a `resync` recreates a session with when NOBODY on the recreating
# machine knows what it was for. Lives here rather than in `cli` (which imports
# it from this module) because `record_session` has to RECOGNISE it coming back
# off the wire: a machine that observes this string has observed the absence of
# a purpose, not a purpose, and recording it as knowledge is how a placeholder
# minted by one machine's recovery becomes every machine's idea of what the
# session is for. See `record_session`.
UNKNOWN_PURPOSE = "(recovered by resync)"


@dataclass(frozen=True)
class SessionMeta:
    """Session identity as this machine last observed it.

    Both fields are `| None` because "this machine does not know" is a real
    answer and must stay distinguishable from every string a real answer could
    be. `created_by=None` is the whole point of the contract change that goes
    with this module (`SynapseSession.created_by: str | None`): an ownerless
    session falls back to a membership check, whereas a made-up creator locks
    the session against its owner forever. `purpose=None` is the same
    distinction one level down — a join against a service that does not report
    the purpose knows nothing about it, which is not the same as a session
    whose purpose is the empty string.
    """

    created_by: str | None = None
    purpose: str | None = None
    # WHICH CONVERSATION ran `create_session` (its agent_session_id), recorded
    # so `end_session` can tell "ending from where I started it" (clean) from
    # "ending from another window" (ask the human to confirm first). None for
    # sessions created before this field existed, joined sessions, and every
    # other machine — an unknown here degrades to "no confirmation step", never
    # to a refusal, because the service's creator-only gate is the real guard.
    created_by_agent_session: str | None = None


def sessions_path(state_dir: Path | str) -> Path:
    return Path(state_dir) / _SESSIONS_FILE


def retained_sessions(state_dir: Path | str) -> dict[str, SessionMeta]:
    """Every Shared Session this machine has created or joined, by id.

    Empty on ANY read failure rather than raising, for the same reason
    `ended.ended_session_ids` is: the callers are `cmd_resync` and two MCP
    tools, where nothing may raise, and the cost of forgetting is a resync that
    recreates the session ownerless — the honest unknown, and strictly better
    than the invented creator this replaces — rather than a resync that dies.

    `ValueError` as well as `OSError`, and not `json.JSONDecodeError`: as
    `ended.py` records from an observed failure, `read_text` runs the utf-8
    decode BEFORE json sees the bytes, and `UnicodeDecodeError` is a
    `ValueError`, not a `JSONDecodeError`.

    Entries that are not shaped like `{"created_by": ..., "purpose": ...}` are
    dropped individually rather than poisoning the whole map: half a file
    written by an older or newer version is still worth the half that parses.
    """
    path = sessions_path(state_dir)
    if not path.is_file():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        logger.warning("Session metadata at %s is unreadable (%s); treating as empty",
                       path, exc)
        return {}
    if not isinstance(data, dict):
        logger.warning("Session metadata at %s was not an object; treating as empty", path)
        return {}
    out: dict[str, SessionMeta] = {}
    for shared_id, entry in data.items():
        if not isinstance(shared_id, str) or not isinstance(entry, dict):
            continue
        created_by = entry.get("created_by")
        purpose = entry.get("purpose")
        creator_conv = entry.get("created_by_agent_session")
        out[shared_id] = SessionMeta(
            created_by=created_by if isinstance(created_by, str) else None,
            purpose=purpose if isinstance(purpose, str) else None,
            created_by_agent_session=(creator_conv if isinstance(creator_conv, str)
                                      else None))
    return out


def record_session(state_dir: Path | str, shared_id: str, *,
                   created_by: str | None, purpose: str | None,
                   created_by_agent_session: str | None = None) -> None:
    """Remember who owns `shared_id` and what it is for. Never raises.

    FIRST WRITE WINS, per field. A field this machine already knows is never
    replaced; a field it does not know is filled in by the first answer that
    arrives, from either `create_session` (sent, first-hand) or `join_session`
    (observed on the wire). Both fields are immutable for the life of a session
    — `store.create_session` returns an existing session UNCHANGED — so the
    EARLIEST answer is always the one closest to the origin and a later
    different one can only be a post-restart reconstruction.

    ⟨CORRECTED 2026-08-06⟩ This used to let any non-None observation win,
    justified by "in practice the two agree". They do not agree across a
    restart, because the recreated session is a NEW object carrying whatever
    identity the recreating machine had, and both directions of that were
    reproduced by execution:

      * PURPOSE. Alice creates sh-X ("fec decode on the NPU"). The service
        restarts; Bob, who has no record, resyncs and recreates sh-X with the
        placeholder purpose. Alice re-joins, the members response reports the
        placeholder, and last-write-wins OVERWROTE the only surviving copy of
        what the session was for. `SessionContext.purpose` goes verbatim into
        the merge prompt, so every later synthesis round would be run against
        "(recovered by resync)".
      * CREATED_BY, in the mixed-version window this file is explicitly built
        to survive. A peer still on the previous release resyncs and mints
        `created_by="resync"`; every upgraded machine that re-joins then
        overwrites the real creator it was holding with that sentinel and
        re-mints it on the NEXT restart — resurrecting, out of upgraded code,
        the exact gate-inversion this module exists to delete.

    A `None` still never overwrites anything, for the original reason: a join
    against a service too old to report `created_by` learns nothing, and
    "learned nothing" must not erase what `create_session` recorded here.

    `UNKNOWN_PURPOSE` observed on the wire is treated AS a None — it is a
    placeholder some machine's recovery minted, i.e. a report that nobody
    knows, and storing it as knowledge is what lets one machine's ignorance
    propagate to the whole team. Recognising one literal is narrow and
    deliberately so; it is not a general "does this look made up" test, and it
    stops being needed the day the service's log is durable.

    Rejected alternative: an `authoritative=True` flag so `create_session` can
    still overwrite. Nothing needs it — a create always mints a fresh id, so
    its write is always the first one for that id — and the flag would exist
    only to enable the overwrite that caused this defect.

    Written atomically (temp file + `os.replace`), mirroring `ended.record_ended`
    and `synapse_contracts.binding.write_binding`: a crash mid-write must not
    leave a truncated `sessions.json`, because an unreadable one silently
    degrades every session on this machine to "creator unknown".
    """
    try:
        known = retained_sessions(state_dir)
        existing = known.get(shared_id, SessionMeta())
        if purpose == UNKNOWN_PURPOSE:
            purpose = None
        merged = SessionMeta(
            created_by=(existing.created_by if existing.created_by is not None
                        else created_by),
            purpose=existing.purpose if existing.purpose is not None else purpose,
            created_by_agent_session=(
                existing.created_by_agent_session
                if existing.created_by_agent_session is not None
                else created_by_agent_session))
        if shared_id in known and merged == existing:
            return
        known[shared_id] = merged
        path = sessions_path(state_dir)
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(path.suffix + ".tmp")
        tmp.write_text(
            json.dumps({sid: {"created_by": m.created_by, "purpose": m.purpose,
                              "created_by_agent_session": m.created_by_agent_session}
                        for sid, m in sorted(known.items())}, indent=2),
            encoding="utf-8")
        os.replace(tmp, path)
    except (OSError, ValueError) as exc:
        # Swallowed on purpose, exactly as `record_ended` swallows: every caller
        # is an MCP tool, and nothing may raise out of one. A failure here
        # degrades to "resync will recreate this session ownerless", which is
        # the same place a machine that never held a record ends up.
        logger.warning("Could not record session metadata for %r under %s (%s)",
                       shared_id, state_dir, exc)
