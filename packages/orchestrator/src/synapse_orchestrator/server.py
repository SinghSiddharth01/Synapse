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


def register_tools(server: FastMCP, *, binding, service_url: str, relay,
                   distiller_factory, transport=None) -> None:
    """Tools speak trigger-voice; bodies stay small. `transport` is test-only."""
    import httpx as _httpx

    from synapse_contracts import Provenance, Segment

    @server.tool(description=(
        "Search the team's shared memory. Call BEFORE exploring an unfamiliar "
        "subsystem, when debugging something a teammate may also be working on, "
        "or before concluding something is a dead end."))
    async def query(question: str) -> str:
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
        from datetime import datetime, timezone
        ts = datetime.now(timezone.utc)
        event = {"role": "assistant", "kind": "text", "content": text,
                 "ts": ts.isoformat(), "agent_session_id": binding.agent_session_id}
        segment = Segment(id=f"contrib-{ts.strftime('%H%M%S')}",
                          agent_session_id=binding.agent_session_id,
                          events=[event], started_at=ts, ended_at=ts)
        distiller = distiller_factory()
        findings, stats = await distiller.distil(segment)
        for f in findings:
            f.provenance = Provenance.CONTRIBUTED
        if not findings:
            return "Nothing durable extracted from that — try stating the insight directly."
        relay.record(findings)
        sent, pending = await relay.flush()
        state = "shared with the team" if sent else f"queued ({pending} pending)"
        return f"{len(findings)} finding(s) {state}."
