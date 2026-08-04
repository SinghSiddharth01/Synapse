# Synapse — build plans (2026-08-03)

These supersede everything in `docs/brainstorming/2026-07-25-plan-*.md`, which were written against the pre-revision architecture (remote MCP, worker→service egress, pre-Attribution contracts) and are now **historical only**. Do not execute them.

## Why rewritten rather than amended

The old plans accumulated five amendment layers and ~30 inline code snippets showing contract shapes that no longer exist. Chasing that with more amendment notes was costing more than it saved. These plans deliberately carry **no full code listings** — code listings are exactly what went stale. Contracts live in one place (Plan 0); every other plan references them.

## Reading order

| Plan | Track | Blocking? |
|---|---|---|
| [Plan 0 — Foundation](./2026-08-03-plan-0-foundation.md) | all three, together | **Yes** — nothing parallel starts until it is green |
| [Plan A — Capture](./2026-08-03-plan-a-capture.md) | local, no model, no network | parallel |
| [Plan B — Model](./2026-08-03-plan-b-model.md) | distillation, NPU, measurement | parallel |
| [Plan C — Service](./2026-08-03-plan-c-service.md) | ingest, synthesis, memory, retrieval | parallel |
| [Plan D — Orchestrator](./2026-08-03-plan-d-orchestrator.md) | the local hub | split across the three tracks by interface |

## Supporting documents

- `/CONTEXT.md` — vocabulary. Agent Session vs Shared Session vs Agent Run; Attribution; Tombstone. Read this first; the plans use these terms precisely.
- `docs/brainstorming/2026-08-03-local-orchestrator-domain-model-amendment.md` — **amendment F**: the architecture revision, the domain model, and every question closed with its reasoning.
- `docs/adr/` — the two decisions worth a record: the local orchestrator, and semantic merge with tombstones.
- `docs/brainstorming/2026-07-30-npu-llm-benchmarks-and-geniex-findings.md` — measured hardware evidence. Plan B depends on it heavily.
- `docs/architecture.html` — the overview page, rebuilt against this architecture.

## The architecture these plans build

```
  coding agent ────MCP────┐
   (any; detected)        │
                          │
  edge worker ──findings──┤──► ORCHESTRATOR ──HTTPS──► Synapse Service
   RO access to           │    (local, per machine)     (remote; a teammate's
   the transcript         │    sole egress               laptop for the demo)
                          │    owns LocalBinding
  [frontier baseline]  ───┘    stamps Attribution
   benchmark only,
   never pushed
```

**Five invariants every plan must preserve:**

1. **Egress rule.** Nothing reaches the Synapse Service that has not passed through the distiller. The orchestrator hosts MCP, so agent-authored prose lands in it transiently — that is permitted. Transcript-derived raw content must never enter it at all.
2. **Retrieval reads the Finding Log, not the Working Memory.** The prose is bounded and read only by the next merge. If `query()` ranks over raw pushed findings, synthesis's dedup and trivia filter protect nothing a teammate sees.
3. **Awareness suppresses a Finding only when *every* Attribution is the asking agent's own Agent Session.** Scoped to Agent Session, never Contributor.
4. **Findings are durable the moment they are produced**, before any send is attempted, and retained after sending. Distillation is the most expensive step in the system and is unrepeatable — the worker never re-reads a transcript position. Retention also makes a service restart recoverable by `resync`, since ingest upserts by `Finding.id`.
5. **A merged Finding is a new record; originals become tombstones, never deletions.** Never discard-one (the second half of a pooled insight is the point) and never rewrite in place (the id would point at text its author never wrote).
