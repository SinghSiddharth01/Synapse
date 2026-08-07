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

#### W5 amendment (2026-08-06) — arrival is two surfaces, not one

This module composed ONE string, `instructions`, and that turned out to be the
wrong shape for a joiner. `instructions` is read by
`create_initialization_options()` at MCP CONNECTION INIT — not at
`join_session` — so an agent that connected first and joined second (which is
the demo's exact ordering, and the ordinary one for anybody who joins a
session mid-conversation) got its briefing composed BEFORE it had a session,
and `join_session`'s own result carried no memory content at all. The
storyboard's awareness moment ("I have this context, ready to go") could not
happen, because nothing put the context anywhere the agent would speak from.

So there are now two, and they are deliberately different sizes:

  * `build_briefing` — the connection-time HEADLINE. Counts, version, topic
    labels, and (new here) the session's PURPOSE and MEMBERS, which the
    `/watermark` response has been returning all along and this module simply
    never read. Hard-capped; refreshed on a timer; must stay cheap.
  * `fetch_arrival_summary` — the JOIN-time BODY, served by the service's
    `/arrival` route and returned inside `join_session`'s tool result, where
    the agent will actually read and relay it. Two sections: what has
    accumulated, and what is new since this contributor's watermark.

Both fail open, and the second one harder than the first: a summary that
cannot be fetched must never turn a join that WORKED into a join that looks
like it failed.

#### Correction (2026-08-06, adversarial review finding #1) — the DOCUMENTED
#### join never calls `join_session`

The paragraph above fixed the in-conversation join and missed the join the
docs and the demo actually use. `docs/JOIN.md` step 3 has a teammate run
`scripts/serve_local.py --service-url --shared-id --contributor`, which
registers membership itself (`POST /v1/sessions/{sid}/members`) and writes
`bindings/claude-code.json` itself, THEN starts the orchestrator. Step 4 then
points Claude Code at an orchestrator that is ALREADY BOUND — so
`mcp__synapse__join_session` is never called, the tool result that carries the
summary is never produced, and the awareness moment does not happen. The
awareness pack (`packs/claude-code/skills/synapse-shared-memory/SKILL.md`)
says as much in its own words: it assumes the machine "joined before this
conversation connected".

On that path `instructions` is the ONLY surface the joining agent is handed,
so that is where the body has to go. `compose_instructions` is what the CLI
and the refresher now install: `build_briefing`'s headline, composed exactly as
before and hard-capped exactly as before, plus the same `/arrival` body —
separately fetched, separately capped, fail-open on its own, and introduced by
a directive that says to relay it to the user. `build_briefing` itself is
untouched, deliberately: it is the piece whose fail-open behaviour is pinned by
a dozen tests, and a summary that cannot be fetched must not be able to change
what it returns. The total is bounded by `_MAX_INSTRUCTIONS_CHARS`.

That is a deliberate softening of "headline-only, finding bodies never appear"
and it is worth stating why rather than quietly reversing it. The rule existed
for CONTEXT ECONOMY — bodies grow with session length, headlines do not. The
body appended here does NOT grow with session length: the service composes it
to a fixed budget (`arrival.MAX_ARRIVAL_CHARS`) whether the session holds six
findings or six hundred, which is the whole of what W5 built. The economy
argument survives; what does not survive is a briefing that tells an agent a
memory exists and gives it nothing to say about it.
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
#
# ⟨RAISED 1200 → 1600, W5⟩ Not a loosening: the cap exists to stop UNBOUNDED
# service-supplied content (`by_type`, `topics`) from riding into the
# highest-trust text surface an agent sees, and both are still capped by it and
# still the first things truncation eats. What grew is the FIXED part — the
# briefing now states the session's purpose and who is in it, each separately
# bounded below (`_MAX_PURPOSE_CHARS`, `_MAX_MEMBERS`). At 1200 those two
# clauses would have been paid for out of the counts, which is the one thing
# the briefing already did well.
_MAX_BRIEFING_CHARS = 1600

# Per-field bounds on the two clauses added in W5. Both are service-supplied
# and both are unbounded on the wire — a purpose is whatever a human typed into
# `create_session`, and a member list grows with the team — so each is capped
# where it is interpolated rather than left for the global cap to swallow
# whatever came after it.
_MAX_PURPOSE_CHARS = 90
_MAX_MEMBERS = 5
_MAX_MEMBER_CHARS = 24


def _clean(value: object, limit: int | None = None) -> str:
    """Collapse control characters/newlines out of a service-supplied value
    before it is interpolated into `instructions`, and optionally cap it."""
    text = _CONTROL_CHARS.sub(" ", str(value)).strip()
    if limit is not None and len(text) > limit:
        text = text[: limit - 1].rstrip() + "…"
    return text


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

        # PURPOSE and MEMBERS (W5). `/watermark` has returned both since the
        # session lifecycle spec landed and this module never read either, so
        # an arriving agent was told how many findings existed and nothing
        # about what the team was DOING — which is the first thing a joiner
        # needs and the first thing the storyboard has them say back to the
        # user. Both are tolerated as MISSING (a service that predates them)
        # rather than demanded, exactly like `topics` below; unlike `topics`
        # they are also tolerated as the wrong TYPE, because neither is
        # structured — a non-string purpose is rendered as nothing rather than
        # failing the whole briefing open over a cosmetic field.
        purpose_clause = ""
        raw_purpose = w.get("purpose")
        if isinstance(raw_purpose, str) and raw_purpose.strip():
            purpose_clause = (" The session's purpose: "
                              f"“{_clean(raw_purpose, _MAX_PURPOSE_CHARS)}”.")

        members_clause = ""
        raw_members = w.get("members")
        if isinstance(raw_members, list):
            names = [_clean(m, _MAX_MEMBER_CHARS) for m in raw_members
                     if isinstance(m, str) and m.strip()]
            if names:
                shown = ", ".join(names[:_MAX_MEMBERS])
                extra = len(names) - _MAX_MEMBERS
                members_clause = (f" In it with you: {shown}"
                                  + (f" and {extra} other(s)" if extra > 0 else "") + ".")

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
        # W5 places `purpose_clause` and `members_clause` between the tool
        # sentences and the counts. Both are separately capped above, so the
        # prefix ahead of the tool sentences stays fixed-size and the rule the
        # comment above states is unchanged: what truncation eats is still the
        # growable content, `types` first and `topics_clause` last.
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
            "your own evidence, because it may make that unnecessary."
            f"{purpose_clause}{members_clause} "
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
# The JOIN-time body (W5)
# ---------------------------------------------------------------------------

# The joiner's summary is a TOOL RESULT, not `instructions`, so it is allowed
# to be several times the size of the briefing and to carry newlines: it is
# delivered inside a delimited result the agent reads once, rather than
# concatenated into the server's own introduction. It is still capped here as
# well as at the service, because "the service already bounds it" is a claim
# about a process on somebody else's laptop.
_MAX_SUMMARY_CHARS = 3000

_SUMMARY_PREAMBLE = (
    "Here is what this session already holds. Summarise it for the user IN "
    "YOUR OWN WORDS, in a few sentences — say what the team is working on, what "
    "has already been established, and anything new since you last looked — "
    "then say you are ready. Do not paste this verbatim, and do not silently "
    "keep it to yourself: the user cannot see tool results, so a join they are "
    "not told about looks to them like nothing happened.\n\n"
)


async def _arrival_text(service_url: str, shared_id: str, *, contributor: str,
                        agent_session: str | None, timeout: float,
                        transport: httpx.AsyncBaseTransport | None) -> str | None:
    """The service's rendered arrival body, or None on ANY failure.

    None is the entire error contract, and it is shared by both callers below
    for the same reason: neither of them is asking a question whose failure the
    user should hear about. `join_session` has already succeeded by the time it
    calls this, and the connect-time briefing has already composed a headline
    that is true. A service too old to have `/arrival`, one that is down, one
    that answers something unparseable — all None, all invisible beyond a
    surface that says a little less.

    Both identity fields are sent for the same reason `build_briefing` sends
    both: `contributor` keys the watermark (what is new to this PERSON) and
    `agent_session` keys suppression (what is already in this CONVERSATION's
    context window) — decisions/001. A service that predates the split reads
    whichever it understands (`api._asking_contributor`).

    Capped here as well as at the service, because "the service already bounds
    it" is a claim about a process on somebody else's laptop. The cap sits
    ABOVE `arrival.MAX_ARRIVAL_CHARS` on purpose: truncating a body the service
    composed to fit would cut its NEW SINCE section off the end, which is the
    defect the service side just spent a rewrite removing (arrival.py's note on
    `MAX_ARRIVAL_CHARS`). Reaching this cap means the peer is not the service
    this was built against, and cutting from the end is then the least-wrong
    thing left.
    """
    url = f"{service_url.rstrip('/')}/v1/sessions/{shared_id}/arrival"
    params = {"contributor": contributor}
    if agent_session is not None:
        params["agent_session"] = agent_session
    try:
        async with httpx.AsyncClient(transport=transport, timeout=timeout) as client:
            resp = await client.get(url, params=params)
            resp.raise_for_status()
            body = resp.json()
        if not isinstance(body, dict):
            raise ValueError(f"arrival response was not an object: {body!r}")
        text = body.get("text")
        if not isinstance(text, str) or not text.strip():
            raise ValueError(f"arrival response carried no text: {body!r}")
    except Exception as exc:  # noqa: BLE001 — a join that worked stays a join that worked
        logger.info("Arrival summary unavailable for %s (%s)",
                    shared_id, exc.__class__.__name__)
        return None
    if len(text) > _MAX_SUMMARY_CHARS:
        text = text[: _MAX_SUMMARY_CHARS - 1].rstrip() + "…"
    return text


async def fetch_arrival_summary(service_url: str, shared_id: str, *,
                                contributor: str,
                                agent_session: str | None = None,
                                timeout: float = 10.0,
                                transport: httpx.AsyncBaseTransport | None = None
                                ) -> str | None:
    """The two-section summary for a joining conversation, or None."""
    text = await _arrival_text(service_url, shared_id, contributor=contributor,
                               agent_session=agent_session, timeout=timeout,
                               transport=transport)
    return None if text is None else _SUMMARY_PREAMBLE + text


# ---------------------------------------------------------------------------
# The CONNECT-time body (W5, adversarial review finding #1)
# ---------------------------------------------------------------------------

# Said differently from `_SUMMARY_PREAMBLE` because the moment is different. The
# join-time preamble arrives inside a tool result the agent asked for, mid
# conversation, and can say "then say you are ready". This one arrives in
# `instructions`, BEFORE the user has typed anything — there is no turn to speak
# into yet — so it asks for the summary in the agent's first reply instead.
_CONNECT_PREAMBLE = (
    "\n\nHere is what this session already holds. In your FIRST reply in this "
    "conversation, before anything else, summarise it for the user in your own "
    "words in two or three sentences — what the team is working on, what has "
    "already been established, and anything new since you last looked — then "
    "carry on with whatever they asked. Do not paste it verbatim, and do not "
    "keep it to yourself: the user cannot see this text, so a machine that "
    "joined a shared memory and never mentions it looks to them like nothing "
    "happened.\n\n")

# The whole `instructions` string: the capped headline, the fixed preamble, and
# the capped arrival body. Stated as its own constant rather than left implicit
# so that "how big can this surface get" has one answer somebody can read, and
# asserted in test_tools.py against a service returning a body far larger than
# any it is capable of composing.
_MAX_INSTRUCTIONS_CHARS = (_MAX_BRIEFING_CHARS + len(_CONNECT_PREAMBLE)
                           + _MAX_SUMMARY_CHARS)


async def compose_instructions(binding: LocalBinding | None, service_url: str, *,
                               timeout: float = 2.0,
                               transport: httpx.AsyncBaseTransport | None = None
                               ) -> str:
    """What actually goes into MCP `instructions`: the headline, then the body.

    ⟨ADDED 2026-08-06, adversarial review finding #1⟩ The join beat did not fire
    on the documented path. `docs/JOIN.md` step 3 has the teammate run
    `scripts/serve_local.py`, which registers membership and writes the binding
    ITSELF and then starts the orchestrator; step 4 points Claude Code at an
    orchestrator that is already bound, so `join_session` — the only place the
    arrival body was delivered — is never called. The teammate's agent connected
    knowing the counts and nothing it could say back.

    So the body is delivered on the surface that path DOES reach. Two things are
    deliberately not merged into `build_briefing`: the fetch (a second network
    hop, which must not be able to change what the headline says) and the cap
    (the headline stays headline-sized whatever the body does).

    NOT appended when the headline is not a live briefing. `_DEFAULT_INSTRUCTIONS`
    means no binding or a briefing that failed open, and `_ended_briefing` means
    a closed session whose read routes all answer 409 — in both cases there is
    no memory to summarise, and appending a stale or empty body would contradict
    the sentence just above it.
    """
    headline = await build_briefing(binding, service_url, timeout=timeout,
                                    transport=transport)
    if binding is None or headline == _DEFAULT_INSTRUCTIONS:
        return headline
    if headline == _ended_briefing(binding):
        return headline
    body = await _arrival_text(service_url, binding.shared_id,
                               contributor=binding.contributor,
                               agent_session=binding.agent_session_id,
                               timeout=timeout, transport=transport)
    if body is None:
        return headline
    return headline + _CONNECT_PREAMBLE + body


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

    `compose_instructions`, not `build_briefing` (finding #1): the refresher is
    what keeps `instructions` true for agents that connect LATER, and since the
    arrival body is now part of `instructions`, refreshing only the headline
    would hand a teammate who connects an hour in a body composed at boot. The
    extra hop is a cached, model-free read service-side (decisions/004), and it
    fails open independently — a body that cannot be fetched leaves the headline
    exactly where it was rather than reverting it.
    """
    text = await compose_instructions(resolve_binding(), service_url,
                                      transport=transport)
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
