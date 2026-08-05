"""The Orchestrator's MCP surface — currently just the transport shell.

Plan D Task D.3 specifies two tools: `query(nl)` and `contribute(text)`, plus
an arrival briefing riding the `instructions` field of the initialize
response. Neither tool is implemented here yet — `query` needs the Synapse
Service's retrieval endpoint and `contribute` needs the producer endpoint
(Plan D.1) both of which require Plan C, and none of that exists yet.

WHAT USED TO BE HERE. An earlier version of this file exposed a `start` MCP
prompt that let a user type something like `/mcp__synapse__start <id>` inside
a Claude Code conversation to bind that conversation to a Shared Session. That
directly contradicts Plan D Task D.3:

    "There is no attach(shared_id). At initialize the orchestrator already
    knows the product, the Agent Session and therefore the binding and
    shared_id — the agent never needs to be told which Shared Session it is
    in."

The plan's actual mechanism is `synapse join <shared_id>` (Plan A.7 / Plan
D.2) — a command run from a terminal, never from inside the agent
conversation, that binds whatever the worker's own detection currently finds
live. It does not let a human pick a specific transcript file, and accepts the
documented ambiguity of two windows of the same Agent product both being live
rather than resolving it with an explicit per-conversation pin. That command
lives in `synapse_worker.cli` (`synapse-worker join <shared_id>`), since it
only needs the worker's own detection plus the shared `SessionBinding`
read/write helpers in `synapse_contracts` — no MCP server, running or
otherwise, is on the path for join to work.

TRANSPORT IS HTTP, NOT STDIO — ADR 0001. stdio spawns one server process per
client, which would give one Orchestrator per Agent and dissolve the
single-egress property the Orchestrator exists to create.

#### Post-review amendment (2026-08-04), round 3

`register_tools` (below) used to close over a single `binding` resolved once
by `cli.main` at boot, and `cli.main` only called it `if binding is not
None`. Two consequences, both round 3 review findings: (1) an orchestrator
started before any `synapse-worker join` served a permanently tool-less MCP
server — the default `_DEFAULT_INSTRUCTIONS` below tells the agent to join
in a terminal, but nothing ever registered `query`/`contribute` for it to
use once it had; (2) even when a binding existed at boot, a LATER
`synapse-worker join <different_id>` was invisible to `query`/`contribute`
— they kept using the boot-time binding forever, while the producer
endpoint (app.py) re-resolved live, so contribute() could push a Finding to
one Shared Session while query() kept reading another, in the same process
and the same MCP session.

Fixed by making `register_tools` itself resolve nothing: it now takes
`resolve_binding`, a callable invoked fresh at the start of every
`query`/`contribute` call, and `cli.main` calls `register_tools`
unconditionally, once, at boot — never gated on whether a binding exists
yet. An unbound `query`/`contribute` call returns a plain "not joined" tool
result (`_NOT_JOINED` below) instead of not existing at all; the very next
call, in the same MCP session, after a `synapse-worker join`, picks up the
new binding with no restart. See `test_tools.py` for the exact
reproduction and `app.py`'s round 3 amendment note for the equivalent fix
on the producer path.
"""

import logging

from mcp.server.fastmcp import FastMCP

logger = logging.getLogger(__name__)

# Stable marker asserted by scripts/verify_instructions.py through a REAL MCP
# client. If a client ever fails to surface it, amendment F Q11's tier
# assignment (briefing = agent-agnostic floor) is wrong and must be revisited
# BEFORE more briefing work is built.
SENTINEL = "[synapse-briefing]"

_DEFAULT_INSTRUCTIONS = (
    f"{SENTINEL} Synapse passively distils this coding session into shared "
    "team memory. No session is bound yet — run `synapse-worker join "
    "<shared_id>` in a terminal to connect one."
)


def create_mcp(instructions: str | None = None) -> FastMCP:
    return FastMCP(name="synapse", instructions=instructions or _DEFAULT_INSTRUCTIONS)


mcp = create_mcp()

# No tools or prompts registered on the module-level `mcp`. A connecting
# client that never joined a session sees a named server with an empty
# capability set, which is honest: there is nothing usable without a binding.
# `register_tools` below is what the CLI calls once a binding exists.


_NOT_JOINED = (
    "Not joined to a Shared Session yet — run `synapse-worker join <shared_id>` "
    "in a terminal, then try again. No restart needed once you have: this tool "
    "re-checks the binding on every call."
)


def register_tools(server: FastMCP, *, resolve_binding, service_url: str, relay,
                   distiller_factory, transport=None) -> None:
    """Tools speak trigger-voice; bodies stay small. `transport` is test-only.

    `resolve_binding` is called fresh at the START of every `query`/
    `contribute` invocation — never captured once at registration time. See
    the module docstring's round 3 amendment note for why: this is what
    lets `register_tools` be called unconditionally, once, at boot, and
    still have a `synapse-worker join` run afterwards take effect on the
    very next tool call, in the SAME MCP session, with no restart.
    `distiller_factory` mirrors this — it takes the freshly resolved
    binding as its argument, so `contribute()`'s Distiller (and the
    Attribution it stamps) always matches whichever Shared Session the
    Finding is about to be recorded under, never a binding captured at
    registration time.
    """
    import httpx as _httpx

    from synapse_contracts import Provenance, Segment

    @server.tool(description=(
        "Search the team's shared memory. Call BEFORE exploring an unfamiliar "
        "subsystem, when debugging something a teammate may also be working on, "
        "or before concluding something is a dead end."))
    async def query(question: str) -> str:
        binding = resolve_binding()
        if binding is None:
            return _NOT_JOINED
        url = f"{service_url.rstrip('/')}/v1/sessions/{binding.shared_id}/query"
        try:
            async with _httpx.AsyncClient(transport=transport, timeout=15.0) as client:
                resp = await client.post(url, json={
                    "query": question, "agent_session": binding.agent_session_id})
                resp.raise_for_status()
                data = resp.json()
            # A well-formed empty result ({"findings": []}) and a response that
            # doesn't even have a "findings" list are different situations —
            # the first is a true "nothing relevant", the second is E3's
            # contract not matching what this reads, and must not be rendered
            # as the same confident "checked, found nothing" answer.
            if not isinstance(data, dict) or "findings" not in data:
                raise ValueError(f"response had no 'findings' list: {data!r}")
            findings = data["findings"]
            if not isinstance(findings, list):
                raise ValueError(f"'findings' was not a list: {findings!r}")
            lines = []
            for f in findings:
                attributions = f["attributions"]
                if not attributions:
                    raise ValueError(f"finding {f.get('id')!r} has no attributions")
                # A Synthesized Finding carries every source it merged from
                # (CONTEXT.md) — crediting only attributions[0] would silently
                # turn a pooled, teammate-derived insight into what reads as
                # one person's, in the common case the asking agent's own.
                contributors = dict.fromkeys(a["contributor"] for a in attributions)
                lines.append(f"- [{f['type']}] {f['text']} — {', '.join(contributors)}")
        except (_httpx.HTTPError, OSError) as exc:
            return f"Shared memory is unreachable right now ({exc.__class__.__name__})."
        except (ValueError, KeyError, TypeError) as exc:
            logger.warning("query: response from %s didn't match the expected shape (%s)",
                           url, exc)
            return ("Shared memory returned something `query` couldn't parse — try again, "
                    "or ask a teammate directly for now.")
        if not lines:
            return "Team memory has nothing relevant to that. (Checked — not skipped.)"
        return "Relevant team findings, best first:\n" + "\n".join(lines)

    @server.tool(description=(
        "Push an insight to the team's shared memory. Call when you have learned "
        "something non-obvious a teammate would benefit from — a root cause, a "
        "dead end, a decision and its why. A few sentences of plain prose."))
    async def contribute(text: str) -> str:
        binding = resolve_binding()
        if binding is None:
            return _NOT_JOINED
        from datetime import datetime, timezone
        ts = datetime.now(timezone.utc)
        event = {"role": "assistant", "kind": "text", "content": text,
                 "ts": ts.isoformat(), "agent_session_id": binding.agent_session_id}
        segment = Segment(id=f"contrib-{ts.strftime('%H%M%S')}",
                          agent_session_id=binding.agent_session_id,
                          events=[event], started_at=ts, ended_at=ts)
        # Post-review amendment (2026-08-04): "Fail open, always" (Global
        # Constraints) applies to contribute() exactly as it does to query()
        # above — an unreachable NPU, a tripped prompt-drop guard
        # (synapse_distiller.guards.PromptDropError), or a bad on-disk config
        # (missing synapse.toml, an unknown prompt pack — build_npu_distiller,
        # cli.py) are all real, expected failure modes of a laptop-local
        # model. Both `distiller_factory()` itself and `distil()` are inside
        # this guard: a bad config can raise from the factory before distil()
        # is ever called. Nothing may raise out of this MCP tool — FastMCP
        # would otherwise wrap it as a raw internal exception string handed
        # to the agent, and the contributed prose would simply be gone.
        try:
            distiller = distiller_factory(binding)
            findings, stats = await distiller.distil(segment)
        except Exception as exc:
            logger.warning("contribute: distillation failed (%s: %s)",
                           exc.__class__.__name__, exc)
            return (f"Couldn't process that right now ({exc.__class__.__name__}) — "
                    "your note was not recorded. Try again in a moment, or mention it "
                    "to a teammate directly.")
        for f in findings:
            f.provenance = Provenance.CONTRIBUTED
        if not findings:
            return "Nothing durable extracted from that — try stating the insight directly."
        # `shared_id=binding.shared_id` (relay.py's per-call override, round 3
        # amendment) rather than relaying on `relay.shared_id`: this is the
        # SAME live-resolved binding query() would use right now, so a
        # contribute() and a query() issued back-to-back never disagree about
        # which Shared Session they are talking to, even if the producer
        # endpoint has rebound `relay.shared_id` in between (round 2/3 review).
        relay.record(findings, shared_id=binding.shared_id)
        sent, pending = await relay.flush()
        state = "shared with the team" if sent else f"queued ({pending} pending)"
        return f"{len(findings)} finding(s) {state}."
