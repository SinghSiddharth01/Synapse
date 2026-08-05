#!/usr/bin/env python3
"""Freshness pointer — Claude Code awareness pack, signal ③.

Plan D.6's Pack row: "when the watermark moved since this agent last
looked." `docs/architecture.html#awareness`: "A `UserPromptSubmit` hook
checks a cheap watermark endpoint every turn and injects a single line
*only* when the version has moved since this agent last looked, naming the
topics that changed. Every other turn it is silent. That silence is the
feature: a nudge that fires constantly is one the agent learns to skip
past."

WHAT "SINCE THIS AGENT LAST LOOKED" MEANS HERE. The Synapse Service's own
`/watermark` response already carries a `new_since` field, but that counts
against `store.last_seen(sid, agent_session)` — which only advances when
this Agent Session calls the `query` tool (`packages/service/src/
synapse_service/api.py`). Using THAT to decide whether to speak would make
this hook fire on every turn from the moment the version moves until the
agent happens to call `query` — the opposite of "fires once." So this hook
keeps its OWN small state file recording the last version IT observed, and
compares against that instead. `new_since` and the topic labels from the
same response are still used verbatim in the message body once a move is
confirmed — composing what changed is not this hook's job, watermark
already did it.

STDLIB ONLY, BY REQUIREMENT (pinned by
`tests/test_awareness_pack.py::test_hook_imports_nothing_outside_the_standard_library`).
This is a shipped artifact (`packs/claude-code/`, installed per
`INSTALL.md`) that a teammate's machine runs as `python3 <this file>` with
no guarantee any of this repo's own dependencies — or even a venv — are on
that machine's `PATH`. `urllib.request` stands in for `httpx`; the binding
JSON is read by hand instead of through `synapse_contracts.read_binding`,
which would pull in `pydantic`.

FAIL OPEN, ABSOLUTELY (Plan D.6 rule 1: "If the service is unreachable,
slow, or returning nonsense, the signal is silently skipped. A memory
service that can break someone's coding session is worse than no memory
service."). `main()` wraps every step in one blanket `except Exception` and
always exits 0; `run()` prints nothing until its very last line, so no
partial output can ever reach stdout. Every network attempt is bounded by
`_TIMEOUT_SECONDS`, which is deliberately small — this hook sits on the
critical path of every prompt.

RATE-LIMITED, INDEPENDENTLY OF THE WATERMARK (Plan D.6 rule 2: "The
pointer speaks only when the version moved, rate-limited independently of
the watermark so a burst of teammate activity produces one notice rather
than one per turn."). `memory_version` bumps once per verdict round
APPLIED -- including no-op verdicts (`schemas.py`'s `SessionContext`
docstring, corrected 2026-08-05) -- so a version-only comparison would
speak on nearly every turn during a busy stretch with several
contributors and synthesis running: exactly the "fires constantly" failure
rule 1's silence exists to prevent. This hook's state file therefore
tracks `last_spoken_at` alongside `last_version`; a move inside
`_notice_cooldown_seconds()` of the last REAL notice stays silent, even
though the version baseline still advances underneath it -- see `run()`.
"""

from __future__ import annotations

import json
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

# This hook runs on the critical path of every prompt submission
# (architecture.html signal ③: "a thirty-second budget, so it can never
# make a model call"). Two seconds leaves ample headroom under that even
# accounting for local scheduling overhead, and is small enough that a
# wedged Synapse Service never reads to the person typing as the editor
# hanging.
_TIMEOUT_SECONDS = 2.0

# Defensive cap on how much of the watermark response body is ever read.
# This hook trusts the wire no more than `synapse_orchestrator.briefing`
# does for the same response shape: an oversized or adversarial body must
# cost no more than a fixed, small amount of work before the blanket
# except in `main()` discards it. A real watermark response is hard-capped
# near 200 tokens at the source (architecture.html), so this is headroom,
# not a limit anything legitimate will ever hit.
_MAX_RESPONSE_BYTES = 65_536

_DEFAULT_SERVICE_URL = "http://127.0.0.1:8899"

_HOOK_EVENT_NAME = "UserPromptSubmit"

# Plan D.6 rule 2 / architecture.html #awareness: "the pointer is
# rate-limited independently of the watermark so a burst of teammate
# activity produces one notice rather than one per turn." `memory_version`
# bumps once per verdict round APPLIED -- including no-op verdicts
# (schemas.py's SessionContext docstring, corrected 2026-08-05) -- so with
# a couple of contributors and synthesis running, the version can move on
# nearly every turn; without a cooldown independent of the watermark
# itself, "silence is the feature" (rule 2's other half) fails exactly
# when it matters most. Five minutes absorbs a realistic burst of
# back-to-back merges while still surfacing real news within one sitting.
# Overridable via SYNAPSE_FRESHNESS_COOLDOWN_SECONDS for tests, the same
# way SYNAPSE_STATE_DIR/SYNAPSE_SERVICE_URL are.
_DEFAULT_NOTICE_COOLDOWN_SECONDS = 300.0


def _notice_cooldown_seconds() -> float:
    override = os.environ.get("SYNAPSE_FRESHNESS_COOLDOWN_SECONDS")
    if override:
        try:
            return float(override)
        except ValueError:
            pass
    return _DEFAULT_NOTICE_COOLDOWN_SECONDS


def _state_dir() -> Path:
    """`SYNAPSE_STATE_DIR` wins outright — a test/override knob. Otherwise
    `.synapse` under `CLAUDE_PROJECT_DIR`, which Claude Code sets to the
    project root for every hook invocation, or, failing that, the
    process's own cwd. Same resolution order `synapse-worker`/
    `synapse-orchestrator` use for their `--state-dir` default (`.synapse`
    relative to where they're run), so a binding written by `synapse-worker
    join` from an ordinary terminal in the project root is found without
    any configuration this pack has to invent."""
    override = os.environ.get("SYNAPSE_STATE_DIR")
    if override:
        return Path(override)
    project_dir = os.environ.get("CLAUDE_PROJECT_DIR")
    return Path(project_dir if project_dir else os.getcwd()) / ".synapse"


# This pack lives in packs/claude-code/, is installed into a project's own
# .claude/, and emits UserPromptSubmit output for a Claude Code agent only
# -- see `_load_binding`.
_AGENT_PRODUCT = "claude-code"


def _load_binding(state_dir: Path) -> dict | None:
    """This pack's OWN product's binding, `bindings/claude-code.json`, and
    only that — deliberately NOT "the most recently pinned binding across
    every Agent product" the way `synapse_orchestrator.cli._resolve_binding`
    picks for its single "current MCP connection" context. That pick is
    correct for the orchestrator, which serves exactly one live connection
    at a time and needs one answer to "which binding is current"; it is
    wrong here, because this hook is a per-PRODUCT artifact that can only
    ever legitimately speak for its own product's Agent Session.

    Divergence across products is explicitly supported, not hypothetical
    (Plan D.2: "one laptop holds several bindings -- one per Agent Session;
    Claude Code and Codex can sit in different Shared Sessions"), and
    `join_session` (`synapse_worker.discovery`) writes to exactly one
    product's binding file per invocation, never touching the other's. A
    hook that read across products would, on a machine with both joined,
    inject a DIFFERENT session's version, `new_since` count and topic
    labels into this Claude Code agent's context the moment a teammate ran
    `synapse-worker join` for Codex more recently than this project's own
    Claude Code join -- the same class of cross-Shared-Session leak
    `synapse_orchestrator.cli._resolve_binding_for_agent`'s docstring
    records fixing on the egress side (round 3 review), mirrored here on
    ingress.

    Returns the raw dict, not a `SessionBinding` — no pydantic dependency
    here (see module docstring) — or None if the file is missing, corrupt,
    or not the expected shape.
    """
    path = state_dir / "bindings" / f"{_AGENT_PRODUCT}.json"
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(data, dict):
        return None
    if not (isinstance(data.get("shared_id"), str)
            and isinstance(data.get("agent_session_id"), str)
            and isinstance(data.get("pinned_at"), str)):
        return None
    return data


def _fetch_watermark(service_url: str, shared_id: str, agent_session_id: str) -> dict:
    query = urllib.parse.urlencode({"agent_session": agent_session_id})
    path = urllib.parse.quote(shared_id, safe="")
    url = f"{service_url.rstrip('/')}/v1/sessions/{path}/watermark?{query}"
    request = urllib.request.Request(url, method="GET")
    with urllib.request.urlopen(request, timeout=_TIMEOUT_SECONDS) as response:
        body = response.read(_MAX_RESPONSE_BYTES)
    data = json.loads(body)
    if not isinstance(data, dict):
        raise ValueError(f"watermark response was not an object: {data!r}")
    return data


def _state_path(state_dir: Path) -> Path:
    return state_dir / "awareness" / "freshness.json"


def _load_state_entry(path: Path, shared_id: str) -> dict:
    """This shared_id's raw entry from this hook's own state file
    (`{"last_version": ..., "last_spoken_at": ...}`, the second key added
    for rule 2's cooldown -- see `run()`), or `{}` if the file is missing,
    corrupt, or has no entry for this shared_id yet. Fail-open extends to
    this hook's own persistence too: a corrupt read is treated exactly like
    no prior state, never an error."""
    try:
        state = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    if not isinstance(state, dict):
        return {}
    entry = state.get(shared_id)
    return entry if isinstance(entry, dict) else {}


def _last_version(entry: dict) -> int | None:
    version = entry.get("last_version")
    return version if isinstance(version, int) else None


def _last_spoken_at(entry: dict) -> float | None:
    value = entry.get("last_spoken_at")
    return float(value) if isinstance(value, (int, float)) else None


def _save_state_entry(path: Path, shared_id: str, entry: dict) -> None:
    """Best-effort, atomic when it succeeds. A write failure here (e.g. a
    read-only filesystem) is not escalated — this hook's own persistence is
    subordinate to fail-open too; the worst case is re-evaluating from a
    stale baseline (and, if this turn spoke, a stale last-spoken-at) next
    turn, never a broken prompt submission.

    That promise covers the WHOLE function, not just the read: `mkdir`,
    `write_text` and `os.replace` are wrapped in the same `except OSError`
    as the read, not left to propagate. `run()` also calls `_emit` (when
    this turn earns a notice) BEFORE calling this function, so even a
    total failure here — `mkdir` refused on a read-only or root-owned
    project directory, a full disk, a sandboxed hook execution — costs at
    most a stale baseline next turn. It can never cost the notice this
    turn, which is what "not escalated" has to mean for a promise this
    function makes about itself to be true."""
    try:
        try:
            state = json.loads(path.read_text(encoding="utf-8"))
            if not isinstance(state, dict):
                state = {}
        except (OSError, json.JSONDecodeError):
            state = {}
        state[shared_id] = entry
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(path.suffix + ".tmp")
        tmp.write_text(json.dumps(state), encoding="utf-8")
        os.replace(tmp, path)
    except OSError:
        pass


def _clean(value: object) -> str:
    """Strip non-printable characters out of a service-supplied value
    before it is interpolated into agent-facing text — same discipline as
    `synapse_orchestrator.briefing._clean`, applied here because the
    watermark's topic labels are exactly as untrusted a surface here as
    they are there."""
    return "".join(ch if ch.isprintable() else " " for ch in str(value)).strip()


def _compose_message(watermark: dict) -> str:
    version = int(watermark["version"])
    new_since = int(watermark.get("new_since", 0))
    topics = watermark.get("topics") or []
    labels: list[str] = []
    if isinstance(topics, list):
        for topic in topics:
            if isinstance(topic, dict) and isinstance(topic.get("label"), str):
                labels.append(_clean(topic["label"]))
    topics_clause = f" Topics: {', '.join(labels)}." if labels else ""
    return (
        f"Synapse: shared memory for this session moved to v{version} "
        f"({new_since} new since last checked).{topics_clause} "
        "Call the `query` tool if this is relevant to what you're doing."
    )


def _emit(message: str) -> None:
    print(json.dumps({
        "hookSpecificOutput": {
            "hookEventName": _HOOK_EVENT_NAME,
            "additionalContext": message,
        },
    }))


def run() -> None:
    state_dir = _state_dir()
    binding = _load_binding(state_dir)
    if binding is None:
        return  # not joined -- architecture.html: "before an agent joins, Synapse is inert"

    shared_id = binding["shared_id"]
    agent_session_id = binding["agent_session_id"]
    service_url = os.environ.get("SYNAPSE_SERVICE_URL", _DEFAULT_SERVICE_URL)

    watermark = _fetch_watermark(service_url, shared_id, agent_session_id)
    version = int(watermark["version"])

    state_path = _state_path(state_dir)
    entry = _load_state_entry(state_path, shared_id)
    last_version = _last_version(entry)
    last_spoken_at = _last_spoken_at(entry)

    # `last_version is None`: first-ever check for this shared_id --
    # nothing to compare against, so this establishes the baseline rather
    # than reporting a "move" from nothing. `==`: no movement since the
    # last check. Either way, no move. Any OTHER difference -- including a
    # DECREASE, which a Synapse Service restart can legitimately produce
    # (Shared Memory lives in memory; Plan D.4's `resync` climbs back up
    # from whatever a machine's durable log replays) -- is real news, but
    # still subject to rule 2's cooldown below.
    moved = last_version is not None and last_version != version

    now = time.time()
    # Rule 2 (Plan D.6 / architecture.html #awareness): "rate-limited
    # independently of the watermark so a burst of teammate activity
    # produces one notice rather than one per turn." A move that lands
    # inside the cooldown since the last REAL notice stays silent too --
    # `last_spoken_at is None` covers both "never spoken yet" and "spoken
    # long enough ago it doesn't matter" identically, so there's no
    # cooldown to respect on the very first notice ever.
    cooled_down = (last_spoken_at is None
                   or (now - last_spoken_at) >= _notice_cooldown_seconds())
    should_speak = moved and cooled_down

    if should_speak:
        # Emit BEFORE persisting, deliberately: this line is the one thing
        # `_save_state_entry`'s own docstring promises a write failure can
        # never cost. Persisting after also means a crash between the two
        # (there isn't one here, but the ordering is what makes the
        # property hold regardless) still leaves the notice already on
        # stdout.
        _emit(_compose_message(watermark))

    # The version baseline advances on EVERY check, spoken or not -- same
    # "fires once" property rule 1's silence relies on: the turn right
    # after a move updates the baseline to the new version, so the turn
    # after THAT compares equal and stays silent, with no dependency on
    # `query` ever being called. `last_spoken_at` is different: it only
    # advances when this turn actually spoke. A move suppressed by the
    # cooldown leaves it untouched, so the cooldown window is always
    # measured from the last REAL notice, not extended by a turn that
    # stayed silent because of it.
    new_entry: dict = {"last_version": version}
    spoken_at = now if should_speak else last_spoken_at
    if spoken_at is not None:
        new_entry["last_spoken_at"] = spoken_at
    _save_state_entry(state_path, shared_id, new_entry)


def main() -> int:
    try:
        run()
    except Exception:
        # FAIL OPEN, UNCONDITIONALLY. No exception from any step above --
        # a missing/malformed binding field, a connection refused, a
        # timeout, a non-JSON body, a disk write failure -- may ever
        # escape this function.
        pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
