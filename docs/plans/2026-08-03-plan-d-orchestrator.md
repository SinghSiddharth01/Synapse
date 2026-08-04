# Plan D — Orchestrator

**Track:** the local hub. One per machine, alongside the agent and the worker.
**Ownership:** deliberately **split by interface** — see below. Overall assignment is deferred by choice (amendment F Q3).
**Depends on:** Plan 0 Task 0.5 (the shell).

**Goal:** be the single place findings enter Synapse from this machine, whoever produced them — and the single place anything leaves it.

**Transport: localhost HTTP, one shared orchestrator per machine. Not stdio** — stdio spawns a server process per client, which would give one orchestrator per agent and destroy the single-egress structure. Both demo agents support HTTP MCP servers (Claude Code; Codex via `[mcp_servers.*]` with `url` in `~/.codex/config.toml`).

> **Caller identity — a protocol limitation to design around.** MCP gives a server only `clientInfo: {name, title, version}` at initialize. That identifies the client **product**, not a conversation; nothing in the spec identifies an Agent Session. Resolve it as `clientInfo.name` → Agent product, then the worker's live-session detection → Agent Session. **Documented limitation:** one active Agent Session per Agent product per machine; two Claude Code windows in the same Shared Session is ambiguous. The *distilled* path is unaffected — it never touches MCP and the worker knows the Agent Session exactly.

**Why this is a new component:** the reviewed architecture moved the MCP server *onto* the developer's machine. That collapses every coordination problem in the old design — who owns the LocalBinding, how the worker learns the `shared_id`, how awareness reaches local state — into one process's private state, and leaves exactly **one** egress point to audit.

---

## How three people build this in parallel

The orchestrator has three interfaces. Each is naturally owned by whoever owns the other side of it, so the tasks below parallelise cleanly against a shell that already exists from Plan 0.

| Task | Interface | Pairs with |
|---|---|---|
| D.1, D.2 | worker-facing | Plan A |
| D.3, D.6 | agent-facing | whoever owns agent integration |
| D.4 | service-facing | Plan C |
| D.5 | internal | either |

Coordination cost is the shell's config and `LocalBinding`, both frozen in Plan 0.

## Task D.1 — Producer endpoint

**Accepts `Finding[]` and nothing else.** One shape, one door.

- stamps Attribution from the `LocalBinding` for the originating Agent Session
- forwards to the service client

> **EGRESS RULE.** Nothing reaches the Synapse Service that has not passed through the distiller. The orchestrator hosts MCP, so agent-authored prose necessarily lands in it transiently — permitted. **Transcript-derived raw content must never enter this process at all**; the worker owns that and returns only findings.

**First failing tests:** posted findings are stamped with the binding's Contributor, Agent Session and Agent; a post for an unbound Agent Session is rejected; a payload that is not `Finding[]` is rejected.

## Task D.2 — LocalBinding and join

`synapse join <shared_id>` binds the Agent Sessions the worker has detected.

- writes the binding to a **local file** so it survives an orchestrator restart and so awareness can read it directly
- registers the Contributor with the service (`POST /members`)
- one laptop holds several bindings — one per Agent Session; Claude Code and Codex can sit in different Shared Sessions

**First failing tests:** join writes a binding readable after restart; joining with two agents detected produces two bindings; findings from an unbound Agent Session are not forwarded.

## Task D.3 — MCP server

Local, over localhost HTTP. Tools: `query(nl)` → ranked findings · `contribute(text)`.

**There is no `attach(shared_id)`.** At initialize the orchestrator already knows the product, the Agent Session and therefore the binding and `shared_id` — the agent never needs to be told which Shared Session it is in.

**The arrival briefing rides the `instructions` field of the initialize response**, generated per connection. That puts it in the agent-agnostic floor rather than a per-agent pack: any MCP client gets briefed on connect with no install and no hook. Claude Code uses this field ("server instructions help Claude understand when to search for your tools, similar to how skills work").

Tool descriptions are written in **trigger voice, not API voice** — *"call before exploring an unfamiliar subsystem, when debugging something a teammate may also be on, or before concluding something is a dead end."*

**First failing tests:** initialize returns a briefing in `instructions` reflecting the bound session's real counts and topics; the briefing stays within its token cap for a session with 40 findings; `query` returns ranked findings; an agent with no binding gets a valid initialize with no briefing rather than an error.

## Task D.4 — Service client + durable log

The sole egress. Wraps the ingest API and the watermark endpoint; retries with bounded backoff.

**Write-ahead, not fallback buffering.** Findings are appended to a durable local log **the moment they arrive**, before any send is attempted. On restart, anything unsent replays.

**Retain after send — do not delete on ack.** The Synapse Service holds Shared Memory in memory, so a service restart would otherwise wipe it for everyone. Because each machine keeps its log and ingest upserts by `Finding.id`, a `resync` that re-pushes everything is safe by construction. A total-loss failure becomes a recoverable one for free.

**First failing tests:** findings forward against a mock service; a finding is durable on disk *before* the first send attempt; unsent findings replay after a restart; a replayed send is a no-op at the far end (same ids); `resync` re-pushes the whole log; **an unreachable service does not take the agent's MCP surface down with it**.

## Task D.5 — `contribute()` round-trip

Free-form prose from the agent → wrapped as a synthetic `Segment` → **the worker's existing distiller** → `Finding[]` tagged `provenance: contributed` → back through the same producer endpoint.

No second distiller. The local distiller stays the sole gate into shared memory regardless of how capable the source was — and the deterministic verbatim-copy check applies to contributed findings too, so a careless digest that quotes a secret is caught at the same choke point as everything else.

**First failing tests:** contributed prose yields findings tagged `contributed`; they enter via the producer endpoint like any other; the tool is absent when disabled for a session.

## Task D.6 — Awareness signals

Delivery of what Plan C's Task C.6 supports. **Tier one is MCP-native and agent-agnostic; per-agent packs are additive.**

| Signal | Fires | Tier |
|---|---|---|
| Ambient | always | **Floor** — tool descriptions, any MCP client |
| Arrival briefing | on connect | **Floor** — `instructions` at initialize (Task D.3), any MCP client |
| Freshness pointer | when the watermark moved since this agent last looked | Pack — needs mid-session delivery, which `instructions` cannot do (sent once per connection) |
| Relevance | when the work matches | Pack — a shipped skill whose description enumerates when team memory matters |

Only the bottom two need per-agent work. A long-lived connection also means the briefing can go stale, which is the other reason the pointer exists.

Two rules that keep it from becoming a liability:

1. **Fail open, always.** If the service is unreachable, slow, or returning nonsense, the signal is silently skipped. A memory service that can break someone's coding session is worse than no memory service.
2. **Silence is the feature.** The pointer speaks only when the version moved, rate-limited independently of the watermark so a burst of teammate activity produces one notice rather than one per turn. A nudge that fires constantly is one the agent learns to skip.

Because the MCP server is now local, some of this may be **protocol-native rather than per-agent** — worth probing before building hook packs.

**First failing tests:** the pointer is silent when the version is unchanged; it fires once when the version moves; an unreachable service produces no signal and no error; a briefing stays within its token cap.

---

## Exit criteria

1. Worker findings reach the service, Attribution-stamped, through this process alone.
2. `synapse join` binds and survives a restart.
3. An MCP client can attach, query, and contribute.
4. Every awareness signal fails open, verified by test.
5. `grep` proves no transcript-derived raw content path into this package.

## Scope / YAGNI

**In:** producer endpoint, binding + join CLI, local MCP server, service client, `contribute` round-trip, awareness signals.
**Out (stretch):** per-agent packs beyond the first; a local finding buffer for offline resilience; protocol-native push if probing says it is not cheap.

## Risks

| Risk | Mitigation |
|---|---|
| Unowned scope in a five-day build | Split by interface as above so it never blocks on one person. Overall ownership is amendment F Q3 — deferred by choice |
| Orchestrator down → agent loses MCP *and* capture stops reaching the service | Fail open on the agent side; both hops write ahead to a durable log and replay unsent on restart (amendment F Q5) — nothing is lost |
| Raw content leaks into the egress process | Egress rule is a review gate, not a hope. The worker holds transcripts; this process holds findings |
| A local MCP server is not equally reachable from every agent | Confirmed: Codex takes MCP servers via `[mcp_servers.*]` with `url` in `~/.codex/config.toml` (amendment F Q9) |
| Two agents on one laptop confuse Attribution | One orchestrator sees both and holds a binding per Agent Session — verify with a two-agent test |
