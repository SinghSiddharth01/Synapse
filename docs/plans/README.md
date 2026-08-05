# Synapse — build plans (2026-08-03)

These supersede everything in `docs/brainstorming/2026-07-25-plan-*.md`, which were written against the pre-revision architecture (remote MCP, worker→service egress, pre-Attribution contracts) and are now **historical only**. Do not execute them.

## Why rewritten rather than amended

The old plans accumulated five amendment layers and ~30 inline code snippets showing contract shapes that no longer exist. Chasing that with more amendment notes was costing more than it saved. These plans deliberately carry **no full code listings** — code listings are exactly what went stale. Contracts live in one place (Plan 0); every other plan references them.

## Reading order

| Plan | Track | Status as of 2026-08-05 |
|---|---|---|
| [Plan 0 — Foundation](./2026-08-03-plan-0-foundation.md) | all three, together | contracts + `FakeProvider` built · fixtures complete at 8 of 8 (E1) — **still solo-authored, PROVISIONAL until co-review**, no longer a cross-track blocker |
| [Plan A — Capture](./2026-08-03-plan-a-capture.md) | local, no model, no network | running end to end on real data · **A.5b triage built and merged (E2)** · **A.5 compaction still unbuilt** · no `CodexSource` |
| [Plan B — Model](./2026-08-03-plan-b-model.md) | distillation, NPU, measurement | distiller + NPU + eval built and measured · **reframed by `adr/0003`** · privacy metric now includes `identifier_leaks()` (E1), not just the 8-gram check |
| [Plan C — Service](./2026-08-03-plan-c-service.md) | ingest, synthesis, memory, retrieval | **built and merged (E3)**: ingest, synthesis (semantic merge + tombstones), retrieval, watermark, `AIC100Provider` — verified against `FakeProvider` only, live Cirrascale flip still open |
| [Plan D — Orchestrator](./2026-08-03-plan-d-orchestrator.md) | the local hub | **built and merged (E4)**: producer endpoint, durable `Relay`, `query`/`contribute` MCP tools, watermark-driven briefing; D.2's binding landed in the worker earlier |
| [Plan E — Brain integration](./2026-08-05-plan-e-brain.md) | the service's retrieval core | **built and verified on `feat/brain-integration` (E5), pending merge** — integrates `feat/shared-memory-store` (append-only log, fold, five-lane candidate selection, recall harness) *under* main's registry; amends Plan C.2's storage seam and C.4's `CANDIDATE_WINDOW` mechanism, and lands `adr/0004` on the branch with a dated amendment — verdict clean with residuals, see `docs/STATE.md` |

All four exec plans below are merged; the closed loop across all three packages is verified in-process (zero real sockets — a real-socket two-machine run is still open, see `docs/STATE.md`). E5 (below) is a fifth, built and verifier-clean but not yet merged. Detail: [`docs/2026-08-04-implementation-report.md`](../2026-08-04-implementation-report.md) (pre-merge state) · current summary: [`docs/STATE.md`](../STATE.md)

Plan E was originally scoped with no separate `exec/` layer of its own — its ten tasks (E.1–E.10, each independently revertable, the demo loop touched only from E.6 on) were meant to double as both spec and execution order. A proper TDD execution plan followed the same day once the two-day Aug 7 deadline made bite-sized, commit-per-green tasks worth writing out separately, the same shape as E1–E4: **E5**, in the table below. Plan E's reasoning lives in [`docs/brainstorming/2026-08-05-brain-integration-design.md`](../brainstorming/2026-08-05-brain-integration-design.md); the plan adds nothing new to it.

## Execution plans (`exec/`)

The plans above are the **specs**. The `exec/` plans are their execution layer: bite-sized TDD tasks with the failing test written out, the run command, the expected output, and a commit per green — written so someone with zero context on this repo can execute them. In dependency order:

| Exec plan | Implements | Depends on | Status | Parallel-safe owner |
|---|---|---|---|---|
| [E1 — Corpus + privacy metric](./exec/2026-08-04-e1-corpus-and-privacy-metric.md) | Plan 0.3 completion · Plan B.7's leak detector | nothing | **Merged** — dev + adversarial review + adjudicated fixes, verifier clean | Aditya (+ co-author gate on goldens) |
| [E2 — Triage](./exec/2026-08-04-e2-triage.md) | Plan A.5b | E1 Task 4 for its final task only | **Merged** — verifier clean; its Task 4 dependency on E1 is satisfied (see integration items) | Akhil |
| [E3 — Service](./exec/2026-08-04-e3-service.md) | Plan C.1–C.6 + `AIC100Provider` | nothing (fixture pair inlined if E1 lags) | **Merged** — verifier clean; two residual findings closed post-merge (see integration items) | Siddsing |
| [E4 — Orchestrator content](./exec/2026-08-04-e4-orchestrator-content.md) | Plan D.1/D.3/D.4 + amendment F Q11 verification | E3 Tasks 1–4 (its Task 1 is independent — run it first) | **Merged** — verifier clean, no residual findings | split by interface |
| [E5 — Brain integration](./exec/2026-08-05-e5-brain-integration.md) | Plan E.1–E.10 (`adr/0004`, storage seam, five-lane candidate selection) | E3 (storage seam + `CANDIDATE_WINDOW` it amends), E4 (`relay.py`'s tri-state it extends) | **Built on `feat/brain-integration`, verifier clean — pending merge.** Handoff claimed 520 passed matching the plan's count chain; verified suite is **526 passed, 1 pre-existing warning**, a discrepancy the branch's own `docs/STATE.md` draft already discloses as unresolved drift (Done-when #14) — see `docs/STATE.md` | Siddsing |

**Integration items** — work landed on `main` directly after all four branch merges, closing seams between them rather than within a single plan: triage repinned against the now-complete corpus expectation map (satisfies E2 Task 4's dependency on E1); the three-package closed-loop test (worker → orchestrator → service → query, E4 Task 5, in-process only — see `docs/STATE.md`'s caveat on which producer-routing branch it exercises); E3's `CANDIDATE_WINDOW`-starvation fix pinned at the route; and a `POST /v1/sessions/{sid}/synthesize` resync-self-heal endpoint. Two docs-only passes also repaired amendments the verifiers had flagged as incomplete or self-contradictory (E2's Task 3 deviations, E3's Task 5 extractor amendment) — the E2 plan still has one known-false sentence outstanding, see `docs/STATE.md`'s "What remains".

Still parked: Codex adapter, compaction (A.5), A/B measurement (B.8), freshness pointer + relevance skill.

## Supporting documents

- `/CONTEXT.md` — vocabulary. Agent Session vs Shared Session vs Agent Run; Attribution; Tombstone. Read this first; the plans use these terms precisely.
- `docs/brainstorming/2026-08-03-local-orchestrator-domain-model-amendment.md` — **amendment F**: the architecture revision, the domain model, and every question closed with its reasoning.
- `docs/adr/` — the three decisions worth a record: the local orchestrator, semantic merge with tombstones, and the distiller compressing rather than judging.
- `docs/2026-08-04-implementation-report.md` — **what was actually built, measured, and left open.** Plan B's Tasks B.1–B.5 are partly superseded by it; read it before executing them.
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
6. ⟨2026-08-04⟩ **The on-device Distiller compresses; it does not judge.** Durability judgment belongs to triage upstream (Plan A.5b) and synthesis downstream (Plan C.4) — `adr/0003`. A 4B asked to judge invents findings from noise and, worse, reverses facts stated in its own prompt while passing every guard.
