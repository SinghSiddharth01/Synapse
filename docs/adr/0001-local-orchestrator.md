# 1. The MCP server is local, as an Orchestrator

**Status:** Accepted (2026-08-03)

## Context

The original design put the MCP server beside the Synapse Service and had agents point at the service host: *"MCP transport is remote HTTP/SSE."* The Edge Worker pushed findings directly to the ingest API. Two planes — data (worker → ingest) and retrieval (agent → MCP) — crossing the device boundary at two different places.

That shape had a hole nobody could close. `LocalBinding` maps an Agent Session to a Shared Session, and both sides need it: the worker to tag findings, the awareness layer to know what to brief about. But joining happened either in the agent (via MCP, on a remote host that cannot write to the laptop) or in the worker (which the agent cannot see). Whichever end joined, the other end never found out. *"Teammates join with one command"* had no command that worked.

## Decision

The MCP server runs **on the developer's machine** as an Orchestrator, over localhost HTTP. The Edge Worker never talks to the Synapse Service; everything egresses through the Orchestrator, which owns the LocalBinding and stamps Attribution onto every Finding from any Producer.

Transport is HTTP rather than stdio deliberately: **stdio spawns a server process per client**, which would give one Orchestrator per agent and dissolve the single-egress property this decision exists to create.

## Consequences

**Good.** Every coordination problem becomes one process's private state. The privacy invariant has exactly **one** egress point to audit instead of two. The two-plane framing collapses into one path. And because the Orchestrator is local and knows the binding at connect time, the arrival briefing can ride the MCP `instructions` field — making it agent-agnostic rather than a per-agent hook pack.

**Bad.** A component now runs on every laptop that no plan previously accounted for, in a five-day build. Plan C's assumption that *"the ingest API and MCP live in one process"* on the service side is void. Mitigated by splitting Plan D at its three interfaces so it never blocks on one person, but the scope is real.

**Constraint we inherited.** MCP gives a server only `clientInfo: {name, title, version}` at initialize — the client *product*, not a conversation. Nothing in the protocol identifies an Agent Session. Resolved as `clientInfo.name` → product, then the worker's live-session detection, under a documented limitation: **one active Agent Session per Agent product per machine**. The distilled path is unaffected; it never touches MCP.

## Alternatives considered

**Agent joins via MCP, worker polls the service for its binding.** Keeps "one command" inside the agent, but the agent must somehow know its own Agent Session id to be bound, and there is a window where findings are produced un-taggable.

**Service is source of truth; both ends reconcile against it.** Most flexible, but the most moving parts, needs conflict rules when both sides join at once, and makes local awareness depend on the network on every run.
