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
critical path of every prompt. The stdin read below is bounded the same
way, by `_STDIN_READ_TIMEOUT_SECONDS`, for the same reason.

RATE-LIMITED, INDEPENDENTLY OF THE WATERMARK, AND DEFERRED RATHER THAN
DROPPED (Plan D.6 rule 2: "The pointer speaks only when the version moved,
rate-limited independently of the watermark so a burst of teammate
activity produces one notice rather than one per turn."). `memory_version`
bumps once per verdict round APPLIED -- including no-op verdicts
(`schemas.py`'s `SessionContext` docstring, corrected 2026-08-05) -- so a
version-only comparison would speak on nearly every turn during a busy
stretch with several contributors and synthesis running: exactly the
"fires constantly" failure rule 1's silence exists to prevent. This hook's
state file therefore tracks `last_spoken_at` alongside
`last_notified_version`; a move inside `_notice_cooldown_seconds()` of the
last REAL notice stays silent this turn.

Post-review fix (2026-08-05): the FIRST version of this cooldown compared
against a "last checked" baseline that advanced on every invocation,
spoken or not. That silently DROPPED a move that landed inside the
cooldown: once the cooldown elapsed, the checked-baseline already equalled
the current version, so the suppressed move was never delivered. `run()`
now compares against `last_notified_version`, which only ever advances on
a turn that actually speaks (see `run()`'s own comment) -- so a suppressed
move stays "pending" across any number of silent checks, however the
version moves in the meantime, until a turn is both pending AND cooled
down. Rule 2 asks for one notice for a burst, not zero notices for
everything after the burst's first tick.

SCOPED TO THE CONVERSATION THAT ACTUALLY INVOKED IT (post-review fix,
2026-08-05). Signal ③ is specified per Agent Session ("since THIS agent
last looked" — Plan D.6's Pack row), but a machine can have several Claude
Code windows open on the same project while only one of them is "the
joined" conversation on disk (`SessionBinding`'s own docstring: "one
active Agent Session per Agent product per machine"). Every OTHER window
still fires this hook on its own `UserPromptSubmit`. Claude Code writes
each invocation's own conversation id to the hook's stdin as `session_id`
-- the same id `sources/claude_code.py` reads off each transcript line as
`sessionId` and stores as `LocalBinding.agent_session_id`. `run()` reads
that payload (`_read_stdin_session_id`) and returns immediately, before
any network call or state read/write, when it disagrees with the joined
binding -- otherwise a window that never joined would both (a) get shared
memory injected into a conversation `docs/architecture.html` promises is
"inert" until it joins, and (b) silently consume the joined window's own
pending notice, because the two windows would otherwise share one state
entry. State is additionally keyed by `(shared_id, agent_session_id)`, not
`shared_id` alone, so a re-join to a fresh Agent Session under the same
Shared Session starts its own "since I last looked" clock rather than
inheriting a stranger session's baseline.

BOUNDED OUTPUT (post-review fix, 2026-08-05). `_compose_message` used to
join every topic label from the watermark response with no length cap.
The sibling this hook's own comments cite -- `synapse_orchestrator.
briefing` -- hard-caps the identical composed-from-the-same-response
string at 1200 characters, because `topics` is unbounded service-supplied
content interpolated into agent-facing text. `_compose_message` now
enforces the same cap (`_MAX_MESSAGE_CHARS`), so this hook actually holds
the parity its own docstring claims rather than merely asserting it.

COMPOSITION ORDER IS LOAD-BEARING (post-review fix, 2026-08-05), the same
way it is in the sibling above. The cap truncates from the END, so
whatever is interpolated LAST pays for unbounded growth in whatever came
before it. `topics_clause` is unbounded service-supplied content (a
teammate can label a topic with an arbitrarily long string); the
`` `query` `` instruction is the entire point of this hook existing --
losing it to truncation silently degrades signal (3) to a truncated list
of topic labels with the nudge removed. `_compose_message` previously put
`topics_clause` BEFORE the instruction, inverted from `briefing.py`'s
documented ordering, so an oversized topics list truncated away the one
sentence that matters. It now matches: fixed-size identity clause, then
the instruction, then the growable `topics_clause` last -- see
`test_the_notice_is_hard_capped_when_the_watermark_topics_list_is_huge`'s
`assert "query" in context`, which pins the ordering itself, not just the
cap.

VOCABULARY (post-review fix, 2026-08-05). This composed line is the one
string signal (3) ever injects into an agent's context, and it said
"shared memory for this session" -- bare "session", which CONTEXT.md's
vocabulary lists under _Avoid_ for both Agent Session and Shared Session.
The sibling composer gets this right (`briefing.py`: "You are in Synapse
Shared Session {shared_id} as {contributor}."); this hook now says "for
this Shared Session" to match.
"""

from __future__ import annotations

import json
import os
import sys
import threading
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

# Same reasoning as _TIMEOUT_SECONDS, applied to reading this hook's own
# stdin (`_read_stdin_session_id`): Claude Code always writes the payload
# and closes its end promptly, but this is stdlib subprocess code running
# on someone else's machine and must not trust that invariant blindly. A
# stdin that is piped but never closed must not be able to add much to
# this hook's own "critical path of every prompt" budget.
_STDIN_READ_TIMEOUT_SECONDS = 1.0

# Defensive cap on how much of the watermark response body is ever read.
# This hook trusts the wire no more than `synapse_orchestrator.briefing`
# does for the same response shape: an oversized or adversarial body must
# cost no more than a fixed, small amount of work before the blanket
# except in `main()` discards it. A real watermark response is hard-capped
# near 200 tokens at the source (architecture.html), so this is headroom,
# not a limit anything legitimate will ever hit.
_MAX_RESPONSE_BYTES = 65_536

# Same headroom reasoning as _MAX_RESPONSE_BYTES, applied to the stdin
# payload Claude Code writes to this hook (`_read_stdin_session_id`).
_MAX_STDIN_BYTES = 65_536

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

# Parity with `synapse_orchestrator.briefing._MAX_BRIEFING_CHARS` -- see
# the module docstring's "BOUNDED OUTPUT" section. Same value, same
# justification: `topics` is unbounded service-supplied content.
_MAX_MESSAGE_CHARS = 1200


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


def _read_stdin_bounded() -> str:
    """At most `_MAX_STDIN_BYTES` of stdin, at most
    `_STDIN_READ_TIMEOUT_SECONDS` of waiting, on every platform.

    The reader runs on a daemon thread so a stdin that is piped but never
    written to or closed cannot extend this hook's critical-path budget:
    `join` returns after the timeout with whatever arrived (nothing), and
    a daemon thread never delays interpreter exit. Returns "" for both
    "nothing arrived in budget" and "read failed", which the caller treats
    identically -- as "can't tell", not as "silence".

    `os.read` on the raw descriptor, NOT `sys.stdin.buffer.read()`: the
    buffered reader takes its own lock for the duration of the call, and a
    daemon thread still blocked on an unclosed pipe holds that lock through
    interpreter finalization, which then deadlocks trying to close the same
    object -- the process hangs instead of exiting, exactly what
    test_fail_open_when_stdin_is_piped_but_never_written_or_closed pins.
    A raw descriptor read holds no Python-level lock, so finalization is
    free to proceed and the abandoned thread dies with the process.
    """
    box: list[bytes] = []

    def _reader() -> None:
        chunks: list[bytes] = []
        try:
            fd = sys.stdin.fileno()
            total = 0
            while total < _MAX_STDIN_BYTES:
                chunk = os.read(fd, min(4096, _MAX_STDIN_BYTES - total))
                if not chunk:
                    break  # EOF
                chunks.append(chunk)
                total += len(chunk)
        except (OSError, ValueError, AttributeError):
            pass
        box.append(b"".join(chunks))

    thread = threading.Thread(target=_reader, daemon=True)
    thread.start()
    thread.join(_STDIN_READ_TIMEOUT_SECONDS)
    return (box[0] if box else b"").decode("utf-8", errors="replace")


def _read_stdin_session_id() -> str | None:
    """The `session_id` Claude Code writes into a `UserPromptSubmit` hook's
    own stdin payload — the SAME id `sources/claude_code.py` reads off each
    transcript line as `sessionId` and stores as
    `LocalBinding.agent_session_id`. `run()` uses this to make sure the
    hook only ever speaks for the conversation that actually invoked it —
    see the module docstring's "SCOPED TO THE CONVERSATION" section.

    Returns None -- deliberately treated by `run()` as "nothing to compare
    against, don't gate" rather than "silence" -- for every case this hook
    cannot tell apart from a legitimate invocation: an interactive terminal
    (`sys.stdin.isatty()`, exactly INSTALL.md's direct-run verification
    command, which pipes nothing), a stdin that is piped but never written
    to or closed within `_STDIN_READ_TIMEOUT_SECONDS`, or a payload that
    isn't parseable JSON carrying a string `session_id`. The read is
    bounded for the same reason `_TIMEOUT_SECONDS` bounds the HTTP call: a
    hook on the critical path of every prompt must never trust an external
    write to ever arrive.

    PLATFORM (fix 2026-08-05). The bound was originally `select.select()`
    on `sys.stdin.fileno()`. On Windows `select()` accepts SOCKETS ONLY --
    a pipe fd raises `OSError: [WinError 10093]` -- which this function's
    own `except OSError` then swallowed into `return None`, so on Windows
    the gate in `run()` (`stdin_session_id is not None and ...`) could
    never fire and EVERY unjoined Claude Code window got a notice meant
    for the joined one, while also consuming that window's pending notice
    (state is keyed by the BINDING's agent_session_id). Caught by
    test_silent_and_no_network_call_when_the_stdin_session_id_does_not_
    match_the_joined_agent_session, which passed on POSIX and failed here.
    A daemon thread doing an ordinary blocking read, joined with a
    timeout, is bounded identically and is portable: if the write never
    arrives the thread simply never finishes, `join` returns on schedule,
    and the process exits without waiting on it.
    """
    try:
        if sys.stdin.isatty():
            return None
        raw = _read_stdin_bounded()
    except (OSError, ValueError):
        return None
    if not raw:
        return None
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError:
        return None
    if not isinstance(payload, dict):
        return None
    session_id = payload.get("session_id")
    return session_id if isinstance(session_id, str) else None


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


def _load_state_entry(path: Path, shared_id: str, agent_session_id: str) -> dict:
    """This (shared_id, agent_session_id) pair's raw entry from this hook's
    own state file (`{"last_notified_version": ..., "last_spoken_at":
    ...}`) — or `{}` if the file is missing, corrupt, or has no entry for
    this pair yet. Fail-open extends to this hook's own persistence too: a
    corrupt read is treated exactly like no prior state, never an error.

    Keyed by BOTH shared_id and agent_session_id — see the module
    docstring's "SCOPED TO THE CONVERSATION" section for why a single
    Shared Session's state must not be shared across the several Agent
    Sessions that can join it over time."""
    try:
        state = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    if not isinstance(state, dict):
        return {}
    by_shared = state.get(shared_id)
    if not isinstance(by_shared, dict):
        return {}
    entry = by_shared.get(agent_session_id)
    return entry if isinstance(entry, dict) else {}


def _last_notified_version(entry: dict) -> int | None:
    version = entry.get("last_notified_version")
    return version if isinstance(version, int) else None


def _last_spoken_at(entry: dict) -> float | None:
    value = entry.get("last_spoken_at")
    return float(value) if isinstance(value, (int, float)) else None


def _save_state_entry(path: Path, shared_id: str, agent_session_id: str,
                      entry: dict) -> None:
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
    function makes about itself to be true.

    Nested by shared_id then agent_session_id — a read-modify-write of the
    whole file, so writing this pair's entry never clobbers any other
    (shared_id, agent_session_id) pair's state."""
    try:
        try:
            state = json.loads(path.read_text(encoding="utf-8"))
            if not isinstance(state, dict):
                state = {}
        except (OSError, json.JSONDecodeError):
            state = {}
        by_shared = state.get(shared_id)
        if not isinstance(by_shared, dict):
            by_shared = {}
        by_shared[agent_session_id] = entry
        state[shared_id] = by_shared
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
    # Composition order is load-bearing (module docstring's "COMPOSITION
    # ORDER" section, mirroring synapse_orchestrator.briefing's own note).
    # The cap below truncates from the END, so whatever is interpolated
    # LAST pays for unbounded growth in whatever came before it.
    # `topics_clause` is unbounded service-supplied content; the `query`
    # instruction is the entire point of this notice existing, so it goes
    # BEFORE topics_clause, not after.
    text = (
        f"Synapse: shared memory for this Shared Session moved to v{version} "
        f"({new_since} new since last checked). Call the `query` tool if "
        "this is relevant to what you're doing."
        f"{topics_clause}"
    )
    # Parity with synapse_orchestrator.briefing's identical cap on the same
    # response shape -- see the module docstring's "BOUNDED OUTPUT" section.
    # `topics` is unbounded service-supplied content; this is what actually
    # enforces the bound rather than merely documenting it.
    if len(text) > _MAX_MESSAGE_CHARS:
        text = text[: _MAX_MESSAGE_CHARS - 1].rstrip() + "…"
    return text


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

    # Signal ③ is "since THIS agent last looked" (Plan D.6 Pack row), not
    # "since any Claude Code window on this machine last looked." See the
    # module docstring's "SCOPED TO THE CONVERSATION" section: a window
    # whose own stdin session_id disagrees with the joined binding never
    # joined this Shared Session and must get nothing -- not a notice, not
    # even a network call or a state read/write. `stdin_session_id is
    # None` (can't tell) falls back to the old un-gated behavior rather
    # than going silent for a case this hook cannot actually distinguish
    # from "yes, this is the joined session."
    stdin_session_id = _read_stdin_session_id()
    if stdin_session_id is not None and stdin_session_id != agent_session_id:
        return

    service_url = os.environ.get("SYNAPSE_SERVICE_URL", _DEFAULT_SERVICE_URL)

    watermark = _fetch_watermark(service_url, shared_id, agent_session_id)
    version = int(watermark["version"])

    state_path = _state_path(state_dir)
    entry = _load_state_entry(state_path, shared_id, agent_session_id)
    last_notified_version = _last_notified_version(entry)
    last_spoken_at = _last_spoken_at(entry)

    # `last_notified_version is None`: first-ever check for THIS agent
    # session -- nothing to compare against, so this establishes the
    # baseline rather than reporting a "move" from nothing. Any OTHER
    # difference -- including a DECREASE, which a Synapse Service restart
    # can legitimately produce (Shared Memory lives in memory; Plan D.4's
    # `resync` climbs back up from whatever a machine's durable log
    # replays) -- is real news, PENDING until a turn actually delivers it.
    #
    # Comparing against `last_notified_version` rather than a "last
    # checked" baseline is what makes a cooldown-suppressed move DEFERRED
    # instead of DROPPED (module docstring): `last_notified_version` only
    # ever advances on a turn that actually speaks (see below), so a move
    # that lands inside the cooldown leaves it untouched -- still pending
    # on every later check, however many silent checks happen in between
    # and whatever the version does in the meantime.
    pending = last_notified_version is not None and last_notified_version != version

    now = time.time()
    # Rule 2 (Plan D.6 / architecture.html #awareness): "rate-limited
    # independently of the watermark so a burst of teammate activity
    # produces one notice rather than one per turn." `last_spoken_at is
    # None` covers both "never spoken yet" and "spoken long enough ago it
    # doesn't matter" identically, so there's no cooldown to respect on the
    # very first notice ever.
    cooled_down = (last_spoken_at is None
                   or (now - last_spoken_at) >= _notice_cooldown_seconds())
    should_speak = pending and cooled_down

    if should_speak:
        # Emit BEFORE persisting, deliberately: this line is the one thing
        # `_save_state_entry`'s own docstring promises a write failure can
        # never cost. Persisting after also means a crash between the two
        # (there isn't one here, but the ordering is what makes the
        # property hold regardless) still leaves the notice already on
        # stdout.
        _emit(_compose_message(watermark))

    if last_notified_version is None:
        # First-ever check for this agent session: always persist the
        # baseline, even though should_speak is necessarily False here (see
        # `pending` above) -- otherwise EVERY future check would look like
        # a first-ever check again and never establish a comparison point.
        _save_state_entry(state_path, shared_id, agent_session_id,
                          {"last_notified_version": version})
    elif should_speak:
        # Advance the baseline ONLY when this turn actually spoke. Leaving
        # it exactly where it was in every other case (pending-but-cooled-
        # down-false, or not pending at all) is what keeps a suppressed
        # move pending rather than silently adopted as the new "nothing to
        # report" baseline -- see the module docstring's "RATE-LIMITED"
        # section and
        # test_a_move_suppressed_by_the_cooldown_is_not_dropped_once_the_window_elapses.
        _save_state_entry(state_path, shared_id, agent_session_id,
                          {"last_notified_version": version, "last_spoken_at": now})
    # else: nothing to persist -- either nothing moved, or a move is still
    # pending and cooling down. The stored entry stays exactly as it was.


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
