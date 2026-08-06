"""Recognising a closed Shared Session, and remembering that it closed.

Two small things that three modules need (`server.py`, `relay.py`,
`briefing.py`, plus `cli.cmd_resync`) and none of them owns:

1. `is_session_ended(response)` — the ONE definition of "the service says this
   session is closed". The service's liveness gate returns a uniform
   `409 {"error": "session_ended"}` from every route that serves or extends the
   memory (`synapse_service.api._unavailable`, 2026-08-06), and this reads
   exactly that shape. It is deliberately NOT "any 409": a 409 is a generic
   conflict status, and the local reaction to this one is destructive (clear
   the binding, drop queued findings), so a future unrelated 409 from any hop
   in between — a proxy, a load balancer, a route added later — must not be
   able to trigger it. Status AND body, or it is not an ended session.

2. A locally retained set of ended Shared Session ids, `ended.json` in the
   orchestrator's state dir.

STOPGAP, and labelled as one where it is used. `synapse_service.store` is
entirely in-memory, so a service restart resurrects an ended session:
`SessionEnded` is a log event (the spec's whole point — status is a fold, per
adr/0004), but the log itself does not survive the process. Until the
service-side log persistence item lands (STATE.md's first post-demo entry,
called out in the lifecycle spec's "Durability caveat"), `synapse-orchestrator
resync` would happily re-create a session the team deliberately closed and push
its whole retained log back into it. This file is what stops that, on this
machine only. It is not a substitute for the durable log: another machine's
resync knows nothing about it, which is exactly why the real fix is
service-side and this is a stopgap.

Rejected alternative: keeping the set in the Relay's own directory next to
`findings.jsonl`/`sent.jsonl`. Those three files are all *per finding* ledgers
that the Relay alone reads and writes; "which sessions are closed" is a fact
about sessions that `server.py` and `cmd_resync` need with no Relay in hand,
and burying it under `relay/` would have meant either reaching into another
module's directory layout or passing a Relay around purely to ask it a
question about a session id.
"""

from __future__ import annotations

import json
import logging
import os
from pathlib import Path

import httpx

logger = logging.getLogger(__name__)

# The exact body the service's liveness gate returns alongside its 409.
SESSION_ENDED_ERROR = "session_ended"

_ENDED_FILE = "ended.json"


def is_session_ended(response: httpx.Response) -> bool:
    """True only for THE 409 that means "this Shared Session is closed".

    Both halves are load-bearing — see this module's docstring. A 409 with an
    unreadable body, a non-object body, or a different `error` string is NOT an
    ended session and callers must keep treating it as the ordinary terminal
    4xx it has always been.
    """
    if response.status_code != 409:
        return False
    try:
        body = response.json()
    except ValueError:
        # A 409 whose body is not JSON at all is something other than the
        # service's liveness gate — an intermediary, most likely. Not ours.
        return False
    return isinstance(body, dict) and body.get("error") == SESSION_ENDED_ERROR


def ended_path(state_dir: Path | str) -> Path:
    return Path(state_dir) / _ENDED_FILE


def ended_session_ids(state_dir: Path | str) -> set[str]:
    """Every Shared Session this machine has observed to be ended.

    Empty on any read failure rather than raising: this is consulted on the
    startup path (`cli.main`) and inside MCP tools, where nothing may raise,
    and the cost of forgetting is a resync that re-creates a dead session —
    the pre-existing behaviour — rather than an orchestrator that will not
    start.
    """
    path = ended_path(state_dir)
    if not path.is_file():
        return set()
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    # ValueError, not json.JSONDecodeError (2026-08-06 review): `read_text`
    # runs the utf-8 decode BEFORE json ever sees the bytes, and a
    # UnicodeDecodeError is a ValueError, NOT a JSONDecodeError. Caught only
    # the narrow one, a single non-utf-8 byte in ended.json -- disk corruption,
    # or the file replaced by some other artifact -- escaped this function and
    # took down `cli.main` at startup (it calls this before `uvicorn.run`) and
    # `record_ended` inside `end_session`, where nothing may raise out of an
    # MCP tool. JSONDecodeError is itself a ValueError, so the narrower clause
    # is subsumed rather than lost.
    except (OSError, ValueError) as exc:
        logger.warning("Ended-session set at %s is unreadable (%s); treating as empty",
                       path, exc)
        return set()
    if not isinstance(data, list):
        logger.warning("Ended-session set at %s was not a list; treating as empty", path)
        return set()
    return {item for item in data if isinstance(item, str)}


def record_ended(state_dir: Path | str, shared_id: str) -> None:
    """Remember that `shared_id` is closed. Idempotent, and never raises.

    Written atomically (temp file + `os.replace`), mirroring
    `synapse_contracts.binding.write_binding`: a crash mid-write must not leave
    a truncated `ended.json`, because the failure mode of an unreadable one is
    a resync quietly resurrecting every session in it.
    """
    try:
        known = ended_session_ids(state_dir)
        if shared_id in known:
            return
        path = ended_path(state_dir)
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(path.suffix + ".tmp")
        tmp.write_text(json.dumps(sorted(known | {shared_id}), indent=2),
                       encoding="utf-8")
        os.replace(tmp, path)
    # ValueError as well as OSError, for the same reason `ended_session_ids`
    # widened above: this body CALLS that function, so an unreadable-in-a
    # non-OSError-way `ended.json` would otherwise propagate straight through
    # a function whose docstring promises it "never raises" -- and its callers
    # (`end_session`, `_forget_ended`, the Relay's egress) are all places where
    # nothing may raise.
    except (OSError, ValueError) as exc:
        # Swallowed on purpose: every caller is either an MCP tool (nothing may
        # raise out of one) or the Relay's egress path (fail open, always). A
        # failure here degrades to "resync may re-create this session", which
        # is exactly the pre-stopgap behaviour.
        logger.warning("Could not record ended session %r under %s (%s)",
                       shared_id, state_dir, exc)
