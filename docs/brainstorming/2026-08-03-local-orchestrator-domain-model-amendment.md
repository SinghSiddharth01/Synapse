# Amendment 2026-08-03 (F): local orchestrator, domain model, durability

**Status:** Adopted 2026-08-03. All questions closed (§4); Q3 is a staffing decision, deferred by choice.
**Amends:** `2026-07-25-synapse-design.md` §2/§4/§5/§12 (marked ⟨F⟩) · supersedes the remote-MCP shape entirely.
**Companions:** `/CONTEXT.md` (vocabulary) · `docs/adr/0001-local-orchestrator.md` · `docs/adr/0002-semantic-merge-and-tombstones.md` · `docs/plans/` (execution).
**Relates to:** `2026-07-25-synapse-design.md` §4/§5 · Plan A · Plan C · `CONTEXT.md`

---

## 1. The architecture change

The reviewed diagram moves the MCP server **onto the developer's machine** and makes it an orchestrator. This revises §4, which says *"MCP transport is remote HTTP/SSE — agents point at the service host."*

```
  coding agent ────MCP────┐
                          │
  edge worker ──findings──┤──► ORCHESTRATOR ──HTTPS──► Synapse Service
   (RO access to          │    (local)                  (remote; on a
    the transcript)       │                              teammate's laptop
                          │                              for the demo)
  frontier worker ────────┘
   (future)
```

**Consequences:**

| Before | Now |
|---|---|
| MCP server co-located with the service, remote | MCP server local, one per machine |
| Worker POSTs findings to the ingest API directly | Worker never talks to the service; orchestrator is the sole egress |
| Two planes (data: worker→ingest; retrieval: agent→MCP) | One local orchestrator carries both |
| `LocalBinding` had no clear owner | Orchestrator owns it |

**Why this is better:** every coordination problem in the old shape — who owns the binding, how the worker learns the `shared_id`, how the awareness layer reaches local state — becomes one local process's private state. And the privacy invariant now has exactly **one** egress point to audit instead of two.

**Bonus:** a *local* MCP server may make the awareness layer agent-agnostic. MCP servers can act as channels that push into a session, so the freshness pointer could be protocol-native rather than a per-agent hook pack.

## 2. The orchestrator is a multi-producer ingress

Not "the thing that drives the worker" — **the one place findings enter Synapse from this machine, whoever produced them.**

| Producer | Emits | Provenance |
|---|---|---|
| Edge worker | Findings distilled from the transcript | `distilled` |
| Coding agent (`contribute`) | Free-form prose → wrapped as a synthetic Segment → distiller | `contributed` |

There is **no frontier producer** — see Q7 below.

**Producer endpoint accepts `Finding[]` and nothing else.** `contribute(text)` is an MCP tool on the orchestrator, not a producer: its handler routes the prose down to the worker's distiller and the resulting findings rejoin the same producer path. One distiller, one gate.

> **EGRESS RULE.** Nothing reaches the Synapse Service that has not passed through the distiller. The orchestrator hosts the MCP server, so agent-authored prose necessarily lands in it — that is permitted transiently. Transcript-derived raw content must never enter it at all; the worker owns that.

The orchestrator stamps Attribution (from the LocalBinding) and forwards. This strengthens the hybrid amendment's invariant: *"the local distiller is the sole gate"* becomes **"the orchestrator is the sole gate,"** with the distiller as one route through it.

**The worker↔orchestrator channel is genuinely bidirectional:**
- worker → orchestrator: POST findings (worker-initiated, autonomous)
- orchestrator → worker: "distil this synthetic segment" (the `contribute` path)

## 3. Settled in this session

Recorded in `CONTEXT.md`; contract changes still to be applied.

- **Three session terms:** Agent Session (one conversation) / Shared Session (the collaboration unit) / Agent Run (process lifetime — awareness only, never a contract).
- **Attribution** = `{contributor, agent_session, agent}`, carried as one value. A Finding holds a *list* of them.
- **Suppression** keys on `agent_session`, not Contributor — and only when *every* Attribution on a Finding is that same Agent Session.
- **Two stores:** Working Memory (bounded prose, keeps the merge prompt fixed-cost) and Finding Log (growing, curated). Retrieval ranks over the Log, not the prose — otherwise synthesis's dedup/trivia filter protects nothing a teammate reads.
- **Finding identity:** client-assigned UUID at distil time → idempotent ingest under retry; `Conflict` and lineage reference ids instead of embedding copies.
- **Semantic merge:** two Findings meaning the same thing produce a new **Synthesized Finding** capturing the essence of both, carrying both Attributions and `merged_from` lineage. Never discard-one — the second half of a pooled insight would be lost. The merged Finding is a *new* record, not a rewrite of one original, because rewriting would leave an id pointing at text its author never wrote.
- **Originals become tombstones, not deletions.** They keep text and Attribution but set `merged_into` and drop out of retrieval. This is a correctness requirement, not an audit preference: ingest upserts by id so a 5xx retry must find a known id; `Conflict` holds ids and must follow `merged_into` forward; and merge is judged by an 8B model performing the only irreversible action in the system. `RETRIEVABLE == merged_into is None and status is KEPT`.
- **Stretch:** surface tombstoned text dynamically when merge confidence or similarity is low, rather than hiding it unconditionally.
- **"Finding" is provisional** and its four-type taxonomy is open; the type field should tolerate values added later without a three-track contract break.
- **Shared Memory's shape is a deliberate first pass** and expected to evolve — it sits behind a storage seam and should not be over-frozen.

## 4. Questions — all closed

Ordered by how much else depended on them.

**Q1 — CLOSED.** One producer endpoint, `Finding[]` only; `contribute` round-trips through the worker's distiller. Egress rule as stated in §2.

**Q2 — CLOSED.** The worker stays autonomous (detect → tail → segment → distil) and POSTs when findings are ready. Passivity preserved; raw never leaves the worker. Pacing is no longer load-bearing — the write-ahead log in Q5 means nothing is lost if a send is slow.

**Q3 — DEFERRED (staffing, not design).** Plan D is split by interface so it never blocks on one person: worker-facing (D.1–2) pairs with Plan A, agent-facing (D.3, D.6) with agent integration, service-facing (D.4) with Plan C. Plan 0 Task 0.5 builds the shell all three attach to.

**Q4 — CLOSED.** One shared orchestrator per machine, over **localhost HTTP** (not stdio — stdio spawns a server process per client, which would destroy the single-egress structure).
> **The constraint that forced this decision:** the MCP spec gives a server only `clientInfo: {name, title, version}` at initialize. That identifies the client *product*, **not a conversation** — there is nothing in the protocol identifying a specific Agent Session. Resolution: `clientInfo.name` → Agent product, then the worker's live-session detection → Agent Session.
> **Documented limitation:** one active Agent Session per Agent product per machine. Two Claude Code windows in the same Shared Session is ambiguous and is a named limit, like cross-machine ordering.
> The *distilled* path is unaffected — it never touches MCP, and the worker knows the Agent Session exactly. Only the agent-facing path is blind.

**Q5 — CLOSED. Write-ahead, not fallback buffering.**
Distillation is the most expensive step in the system (~14 tok/s at 4B) and is **unrepeatable** — the worker never re-reads a transcript position. So findings are persisted **the moment they are produced**, before any send is attempted:

```
worker         distiller returns → append to durable log → attempt send → mark sent
orchestrator   findings received → append to durable log → attempt forward → mark sent
restart        replay anything unsent
```

**Retain after send, do not delete on ack** — which buys resilience we were not otherwise getting. The Synapse Service holds Shared Memory **in memory**, so a service restart currently wipes it for everyone. If each machine keeps its findings, every orchestrator can `resync` by re-pushing its log, and ingest upserts by `Finding.id` so replay is safe by construction. A total-loss failure becomes recoverable, free, because of the identity work in Q4 of the earlier round.

**Q6 — CLOSED.** LocalBinding is a **local file** owned by the orchestrator. Survives restart, readable directly by awareness code without a network call, one entry per Agent Session.

**Q7 — CLOSED, and it costs nothing to build.** The frontier worker is a **benchmarking arm, not a runtime path** — a baseline for profiling cost, efficiency and quality against the local and hybrid approaches. Nothing it produces is pushed. That means it is not a new component at all: it is `distiller: claude` through Plan B Task 6's existing eval harness, where `ClaudeProvider` is already defined as the baseline. No new producer, no new provenance value, no seam ③ risk.
> **Guardrail:** benchmark on the **committed fixture corpus**, never on live team sessions. Fixtures are already in the repo and already Claude-derived, so nothing new is exposed. A frontier baseline pointed at a real teammate's transcript would be seam ③ in a lab coat.

**Q8 — CLOSED.** `provenance = distilled | contributed | synthesized`. Only `synthesized` is written by the service; the other two are stamped locally. `FindingType` stays a strict enum — adding a member later is a one-line change all tracks pick up on the next pull, unlike the optional *field* addition that made `provenance` worth paying for up front. The taxonomy is recorded as provisional in `CONTEXT.md`; no tolerance machinery is built.

**Q9 — CLOSED, verified.** Codex supports MCP: `~/.codex/config.toml`, `[mcp_servers.*]`, taking either `command` (stdio) or `url` (streamable HTTP); the ChatGPT desktop app, Codex CLI and IDE extension share that config. A localhost HTTP orchestrator is reachable from both demo agents.

**Q10 — CLOSED. A/B the agents on a replayed session, not the humans.**
The same task cannot be run twice by the same people — whoever goes second already knows the answer, and no ordering fixes it (baseline first inflates every number; Synapse first hands the baseline so much advantage it wins). The claim being tested is about *agents* anyway; humans are the noise source in the middle.

```
capture   one real multi-person session → real worker → real distiller
                                        → Shared Memory (populated)
run A     agent, cold, task prompt T, repo @ commit C
run B     agent, same T, same C, Synapse attached
measure   turns to resolution · tokens (ModelResult.usage)
          tool calls / files re-explored · dead ends re-entered
```

Reproducible, no human learning effect, and runnable N times to show variance rather than one anecdote.
> **Honesty constraint:** the pre-populated findings must come from an actual capture distilled by the real pipeline — never hand-authored for the demo.

**Q11 (new) — CLOSED. The arrival briefing rides the MCP `instructions` field.**
At initialize the orchestrator already knows the product (`clientInfo`), the Agent Session (worker lookup) and therefore the binding and `shared_id` — so **the agent never needs to be told which session it is in**, and `attach(shared_id)` loses its argument. The spec returns an `instructions` string in the initialize response, generated per connection, and Claude Code uses it ("server instructions help Claude understand when to search for your tools, similar to how skills work").

**This moves the arrival briefing out of the per-agent pack and into the agent-agnostic floor.** Awareness now tiers as:

| Tier | Signal | Per-agent? |
|---|---|---|
| Floor (any MCP client) | ambient tool descriptions + server `instructions` + arrival briefing | No |
| Pack | freshness pointer (needs mid-session delivery; `instructions` is sent once per connection) | Yes |
| Pack | relevance skill | Yes |
