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

from __future__ import annotations

from mcp.server.fastmcp import FastMCP

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

# No tools or prompts registered. A connecting client sees a named server with
# an empty capability set, which is honest: there is nothing usable here yet.
