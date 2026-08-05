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
"""

from __future__ import annotations

import json
import os
import sys
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


def _load_binding(state_dir: Path) -> dict | None:
    """The most recently pinned binding across every Agent product — same
    "most recently joined wins" rule as
    `synapse_orchestrator.cli._resolve_binding`, reimplemented by hand
    here (no `synapse_contracts` import: see module docstring). Comparing
    `pinned_at` as a plain string is safe because
    `SessionBinding.model_dump_json` always writes it as fixed-width-
    microsecond UTC ISO-8601 (`...T10:00:00.000000Z`), which sorts
    correctly without parsing it as a datetime.

    Returns the raw dict, not a `SessionBinding` — no pydantic dependency
    here — or None if no binding is readable. One corrupt or
    partially-written file is skipped, not fatal to the others.
    """
    bindings_dir = state_dir / "bindings"
    if not bindings_dir.is_dir():
        return None
    best: dict | None = None
    for path in sorted(bindings_dir.glob("*.json")):
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if not isinstance(data, dict):
            continue
        if not (isinstance(data.get("shared_id"), str)
                and isinstance(data.get("agent_session_id"), str)
                and isinstance(data.get("pinned_at"), str)):
            continue
        if best is None or data["pinned_at"] > best["pinned_at"]:
            best = data
    return best


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


def _load_last_version(path: Path, shared_id: str) -> int | None:
    try:
        state = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(state, dict):
        return None
    entry = state.get(shared_id)
    if not isinstance(entry, dict):
        return None
    version = entry.get("last_version")
    return version if isinstance(version, int) else None


def _save_last_version(path: Path, shared_id: str, version: int) -> None:
    """Best-effort, atomic when it succeeds. A write failure here (e.g. a
    read-only filesystem) is not escalated — this hook's own persistence is
    subordinate to fail-open too; the worst case is re-evaluating from a
    stale baseline next turn, never a broken prompt submission."""
    try:
        state = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(state, dict):
            state = {}
    except (OSError, json.JSONDecodeError):
        state = {}
    state[shared_id] = {"last_version": version}
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(state), encoding="utf-8")
    os.replace(tmp, path)


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
    last_version = _load_last_version(state_path, shared_id)
    # Persist the new baseline unconditionally, whether or not this turn
    # speaks -- the next comparison is always against the LATEST observed
    # version, never against the version last spoken about. That is what
    # makes this "fires once": the turn right after a move updates the
    # baseline to the new version, so the turn after THAT compares
    # equal and stays silent, with no dependency on `query` ever being
    # called.
    _save_last_version(state_path, shared_id, version)

    if last_version is None or last_version == version:
        # `last_version is None`: first-ever check for this shared_id --
        # nothing to compare against, so this establishes the baseline
        # rather than reporting a "move" from nothing. `==`: no movement
        # since the last check. Either way, silent.
        return

    # Any other difference -- including a DECREASE, which a Synapse
    # Service restart can legitimately produce (Shared Memory lives in
    # memory; Plan D.4's `resync` climbs back up from whatever a machine's
    # durable log replays) -- is real news worth a line, not just an
    # increase.
    _emit(_compose_message(watermark))


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
