"""The arrival briefing — composed from the watermark, carried by `instructions`.

Hard-capped and headline-only by design: counts and types, never finding
bodies (context economy — bodies grow with session length, headlines do not).
FAIL OPEN: any error yields the default unbound text. A briefing that can
break an agent's session start is worse than no briefing.

#### Post-review amendment (2026-08-04)

Round 2 found this module's promises weren't actually enforced:

1. The narrow `except (httpx.HTTPError, OSError, ValueError, TypeError,
   AttributeError, KeyError)` did not cover every exception class the
   string-composition code could raise, so an unforeseen one would escape
   this function and — since `build_briefing` runs in `cli.main` before
   `uvicorn.serve` starts — take the whole orchestrator process down with
   it. The guard is now a blanket `except Exception`, covering the HTTP
   round trip, the JSON parse, AND the string composition as one unit.
2. There was no hard cap. `by_type` is watermark-response content (E3, not
   this package) interpolated directly into `instructions` — the highest-
   trust text surface a connecting agent sees (Task 1's sentinel probe).
   An oversized or adversarial `by_type` map rode straight through. The
   composed string is now truncated to `_MAX_BRIEFING_CHARS` with an
   ellipsis.
3. Service-supplied values (by_type keys/values, in particular) were
   interpolated raw. A key containing embedded newlines could read like a
   new instruction block appended after the real briefing text. `_clean`
   now collapses control characters/newlines out of every service-supplied
   value before it is interpolated.
"""

from __future__ import annotations

import asyncio
import logging
import re

import httpx

from synapse_contracts import LocalBinding

from synapse_orchestrator.ended import is_session_ended
from synapse_orchestrator.server import _DEFAULT_INSTRUCTIONS, SENTINEL

logger = logging.getLogger(__name__)

# Control characters (including \r, \n, \t) collapsed to a single space
# before a service-supplied value is interpolated into agent-facing text —
# a newline sequence must not be able to read like a new instruction block
# appended after the real briefing.
_CONTROL_CHARS = re.compile(r"[\x00-\x1f\x7f]+")

# Hard cap on the composed briefing string, ellipsis-truncated past this.
# Headlines only, by design (module docstring) — this is the enforcement.
_MAX_BRIEFING_CHARS = 1200


def _clean(value: object) -> str:
    """Collapse control characters/newlines out of a service-supplied value
    before it is interpolated into `instructions`."""
    return _CONTROL_CHARS.sub(" ", str(value)).strip()


def _ended_briefing(binding: LocalBinding) -> str:
    """What an arriving agent is told when the session it is bound to is closed.

    Keeps SENTINEL — scripts/verify_instructions.py probes for it through a
    real MCP client, and an ended session is still a briefing this server
    composed, not a fallback. Says what to DO, because the local binding is
    cleared by the first tool call that observes the 409 (server.py) and an
    agent that is only told "ended" would otherwise keep trying."""
    return (f"{SENTINEL} The Synapse Shared Session {_clean(binding.shared_id)} this "
            "conversation is bound to has ENDED — its team memory is closed for reading "
            "and writing, so `query` and `contribute` have nothing to reach. Call "
            "`create_session` to start a new one, or `join_session <shared_id>` if the "
            "team has already moved to another.")


async def build_briefing(binding: LocalBinding | None, service_url: str, *,
                         timeout: float = 2.0,
                         transport: httpx.AsyncBaseTransport | None = None) -> str:
    if binding is None:
        return _DEFAULT_INSTRUCTIONS
    url = (f"{service_url.rstrip('/')}/v1/sessions/{binding.shared_id}/watermark")
    # FAIL OPEN, unconditionally: the HTTP round trip, the JSON parse, AND
    # the string composition below are ALL inside this one guard. E3 is not
    # merged as of this writing, so the watermark response's shape is an
    # unverified assumption — a 200 whose JSON doesn't match it (a list
    # instead of a dict, "by_type" holding something un-summable, a key
    # missing, or any other surprise) must fail open exactly like a downed
    # service, never raise out of here and take the whole orchestrator
    # process down with it (this runs in cli.main before uvicorn.serve
    # starts).
    try:
        async with httpx.AsyncClient(transport=transport, timeout=timeout) as client:
            # BOTH identity fields (2026-08-06). `new_since` is measured against
            # `store.last_seen`, which is now keyed on the Contributor rather
            # than the Agent Session — sending only `agent_session` would make
            # this briefing and `query` (server.py) read two different
            # watermarks for the same person. The service takes `contributor`
            # first and falls back (`api._asking_contributor`), so an
            # un-upgraded service still sees exactly what it saw before.
            resp = await client.get(url, params={
                "agent_session": binding.agent_session_id,
                "contributor": binding.contributor})
            # An ENDED session is not an outage and must not fall through to
            # the fail-open default, which says "no session is bound yet — run
            # `synapse-worker join`" and is simply false here. The spec's
            # requirement is that the briefing reports the session as ended
            # rather than showing stale counts; there are no counts to show,
            # since every read route is closed.
            if is_session_ended(resp):
                return _ended_briefing(binding)
            resp.raise_for_status()
            w = resp.json()

        if not isinstance(w, dict):
            raise ValueError(f"watermark response was not an object: {w!r}")
        by_type = w.get("by_type", {})
        if not isinstance(by_type, dict):
            raise ValueError(f"'by_type' was not an object: {by_type!r}")

        total = sum(int(v) for v in by_type.values())
        types = ", ".join(f"{_clean(k)}: {int(v)}" for k, v in sorted(by_type.items()))
        version = int(w.get("version", 0))
        new_since = int(w.get("new_since", 0))
        conflicts = int(w.get("conflicts", 0))

        # `topics` MISSING is a pre-E5 service: render the rest. `topics`
        # MALFORMED is a shape nobody should trust, and this is the highest-
        # trust text surface a connecting agent sees -- fail open.
        topics_clause = ""
        raw_topics = w.get("topics")
        if raw_topics is not None:
            if not isinstance(raw_topics, list):
                raise ValueError(f"'topics' was not a list: {raw_topics!r}")
            labels = []
            for topic in raw_topics:
                if not isinstance(topic, dict):
                    raise ValueError(f"'topics' held a non-object: {topic!r}")
                label = topic.get("label")
                if not isinstance(label, str):
                    raise ValueError(f"topic label was not a string: {label!r}")
                labels.append(_clean(label))
            if labels:
                topics_clause = (" The team is working on: "
                                 + ", ".join(f"“{label}”" for label in labels) + ".")

        # Composition order is load-bearing (N3, revision 2). The cap below
        # truncates from the END, so whatever is interpolated LAST pays for
        # unbounded growth in whatever came before it. `types` is E3's
        # service-supplied `by_type` map — unbounded, and the whole reason
        # the cap exists. The tool-usage sentences are the entire point of
        # this string being the `instructions` surface (Task 1's sentinel
        # probe reads them). They go FIRST, right after the fixed-size
        # identity clause, so the cap's victim is always the growable
        # content (first `topics_clause`, then `types` itself) rather than
        # an accident of which field happened to be biggest this round. See
        # test_briefing_is_hard_capped_when_the_watermark_by_type_map_is_huge
        # for the 300-type fixture that pins this ordering, not just the cap.
        text = (
            f"{SENTINEL} You are in Synapse Shared Session {_clean(binding.shared_id)} as "
            f"{_clean(binding.contributor)}. Call the `query` tool "
            "before exploring an unfamiliar subsystem, when debugging something a "
            "teammate may also be working on, or before concluding something is a "
            "dead end. Call `contribute` when you learn something non-obvious a "
            "teammate would benefit from. "
            # Observed 2026-08-05: a session loaded a debugging skill, followed
            # its evidence-gathering phase, and searched the filesystem for the
            # answer to a question shared memory already held. A loaded skill's
            # procedure is directive and in-context; this briefing is neither
            # unless it says so. Checking what the team knows is not a step
            # inside somebody else's procedure — it comes before all of them,
            # because it is the only step that can make the rest unnecessary.
            "Do this even when another skill or procedure is already driving the "
            "work: checking what the team already knows comes BEFORE gathering "
            "your own evidence, because it may make that unnecessary. "
            f"Team memory holds {total} findings ({types}), "
            f"{conflicts} conflict(s), at version v{version}, which has moved "
            # `new_since` is a VERSION delta (api.py: memory_version minus this
            # asker's last_seen), not a count of findings. Sitting one clause
            # after "holds 6 findings", the old wording — "1 new since you last
            # looked" — read as "one new finding", which is a different and
            # much smaller claim than "the memory changed once since you were
            # last here". Agents act on this sentence; it has to mean what it
            # says.
            f"{new_since} version(s) since you last looked."
            f"{topics_clause}"
        )
    except Exception as exc:  # FAIL OPEN: nothing escapes this function, ever
        logger.info("Briefing fail-open (%s)", exc.__class__.__name__)
        return _DEFAULT_INSTRUCTIONS

    if len(text) > _MAX_BRIEFING_CHARS:
        text = text[: _MAX_BRIEFING_CHARS - 1].rstrip() + "…"
    return text


# ---------------------------------------------------------------------------
# Keeping the briefing TRUE after boot
# ---------------------------------------------------------------------------
#
# `instructions` is composed once and handed to `create_mcp`, which stores it
# on the low-level MCP server. That was a snapshot of the Shared Session at
# the moment `synapse-orchestrator` started, frozen for the life of the
# process — and the briefing is the one signal whose entire job is to tell an
# ARRIVING agent what is already known.
#
# Observed 2026-08-05: an orchestrator started against an empty session kept
# telling every Claude Code session that connected afterwards "Team memory
# holds 0 findings", while `query` on the same connection returned six. Worse
# than cosmetic: an agent told the shelf is empty has no reason to reach for
# it, which defeats the signal rather than degrading it.
#
# The same staleness hid a second case. `resolve_binding` is re-read per call
# by every tool (server.py) precisely so a `join` after boot works without a
# restart — but the briefing was composed from the binding as it was AT boot,
# so an orchestrator started before `join` went on introducing itself as
# unbound forever.
#
# `create_initialization_options()` reads `self.instructions` fresh on every
# new connection (mcp 1.9.4, streamable_http_manager), so keeping that
# attribute current is enough: each arriving agent gets a current briefing,
# and sessions already open keep the one they arrived with, which is correct
# for an *arrival* briefing.

DEFAULT_REFRESH_SECONDS = 10.0


async def refresh_briefing(server, resolve_binding, service_url: str, *,
                           transport: httpx.AsyncBaseTransport | None = None) -> str:
    """Recompose the briefing from the CURRENT binding and install it.

    `resolve_binding` is called here, not captured: a `join` that happened
    after boot has to be able to change who this server says it is.
    """
    text = await build_briefing(resolve_binding(), service_url, transport=transport)
    # The low-level server is the one place this lives: it is what
    # `create_initialization_options()` reads for each new connection, and
    # `FastMCP.instructions` is a read-only property delegating to it, so the
    # two cannot drift apart.
    server._mcp_server.instructions = text
    return text


def attach_briefing_refresher(app, server, resolve_binding, service_url: str, *,
                              interval: float = DEFAULT_REFRESH_SECONDS,
                              transport: httpx.AsyncBaseTransport | None = None) -> None:
    """Refresh `instructions` for as long as the app is actually serving.

    Hung off the ASGI lifespan rather than a thread so the task cannot
    outlive the server, and so a `uvicorn.run` that never runs (every test in
    test_cli.py monkeypatches it) never starts one either.
    """
    # Reachable from the app for tests and debugging: nothing else on a
    # FastMCP-built Starlette app leads back to the server object.
    app.state.synapse_mcp = server

    if interval <= 0:
        return

    import contextlib

    inner = app.router.lifespan_context

    async def _loop() -> None:
        while True:
            try:
                await refresh_briefing(server, resolve_binding, service_url,
                                       transport=transport)
            except Exception:  # noqa: BLE001 — fail open, same as build_briefing
                logger.debug("Briefing refresh failed; keeping the previous one",
                             exc_info=True)
            await asyncio.sleep(interval)

    @contextlib.asynccontextmanager
    async def _lifespan(scope_app):
        task = asyncio.create_task(_loop())
        try:
            async with inner(scope_app):
                yield
        finally:
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task

    app.router.lifespan_context = _lifespan
