# Where things stand — 2026-08-03 end of day

Design and documentation only. **No code exists yet.** Everything below is decided and written down; tomorrow starts by executing Plan 0.

---

## Start here tomorrow

1. **`/CONTEXT.md`** — the vocabulary. Fifteen terms with `_Avoid_` lists. Read it first; the plans use Agent Session / Shared Session / Attribution / Tombstone precisely, and mixing them up is how three parallel tracks drift.
2. **`docs/plans/README.md`** — the plan set and the five invariants every plan must preserve.
3. **`docs/plans/2026-08-03-plan-0-foundation.md`** — the blocking gate. Nothing parallel starts until it is green.

## What today produced

**Morning — evidence and amendments.** Folded Aditya's measured NPU benchmarks (2026-07-30, real hardware) through the docs, and adopted amendment D (agent auto-detection, pre-recorded A/B demo, per-model segment budget, storage seam).

**Afternoon — a grilling / domain-modeling session** that revised the architecture and the contracts, then replaced the plan set.

### Decisions made today

| Decision | Where it lives |
|---|---|
| **The MCP server is local, as an Orchestrator** — the edge worker never talks to the service; one egress point | `docs/adr/0001-local-orchestrator.md` |
| **Semantic merge produces a new Finding; originals become tombstones** — never discard-one, never rewrite in place, never delete | `docs/adr/0002-semantic-merge-and-tombstones.md` |
| Three session terms: Agent Session / Shared Session / Agent Run | `CONTEXT.md` |
| `Attribution = {contributor, agent_session, agent}`, plural on a Finding | contracts, `CONTEXT.md` |
| Shared Memory is two things — Working Memory (bounded prose) and Finding Log (curated, what retrieval ranks) | `CONTEXT.md`, Plan C |
| `Finding.id` client-assigned at distil time → idempotent ingest under retry | contracts |
| Write-ahead durability at both hops; retained after send so a service restart is a `resync` | Plans A and D |
| Arrival briefing rides the MCP `instructions` field → agent-agnostic floor, no `attach` tool | Plan D |
| A/B the *agents* on a replayed capture, not the humans | Plan B Task B.8 |
| Frontier worker is a benchmarking arm, not a component — zero new scope | amendment F Q7 |

### Documents

```
CONTEXT.md                    glossary — read first
docs/STATE.md                 this file
docs/adr/000{1,2}-*.md        the two decisions worth a record
docs/plans/                   Plan 0 / A / B / C / D + README  ← execute these
docs/architecture.html        overview page, rebuilt against the current design
docs/brainstorming/
  2026-07-25-synapse-design.md          design spec, amended ⟨A⟩–⟨F⟩
  2026-07-30-npu-*.md                   measured hardware evidence
  2026-08-03-*-amendment.md             amendments A, B/C, D, F
  2026-07-25-plan-*.md                  ⚠️ SUPERSEDED — do not execute
```

---

## Open, and what to do about it

**Q3 — who builds the orchestrator.** The only question deliberately left open. It is a staffing decision, not a design one. Plan D is split at its three interfaces so it never blocks on one person (worker-facing → Plan A's owner, agent-facing → agent integration, service-facing → Plan C's owner), and Plan 0 Task 0.5 builds the shell all three attach to. **Decide this before Plan 0 finishes** — it is the largest unassigned scope in a five-day build.

**Measurements still missing.** Power draw for NPU vs CPU vs GPU distillation is the number the entire efficiency argument rests on, and it is unmeasured. Do not claim efficiency until it exists.

**Blocked externally.** AI Hub returns 503, so the `qairt` NPU-exclusive path is untestable; `llama_cpp` GGUF is validated and primary. Re-check periodically — if it clears, it could reverse the compute-unit ranking.

**Deliberately provisional.** The distiller and synthesis prompts are first-pass Claude drafts, not contracts. The shape of Shared Memory is a first pass behind a storage seam. "Finding" and its four-type taxonomy are provisional names.

## Traps worth re-reading before writing code

Three mistakes that would be invisible until they mattered:

1. **Retrieval must rank over the Finding Log, not the Working Memory prose.** Get this wrong and synthesis's dedup and trivia filter protect nothing a teammate ever sees. Plan C has an explicit test for it.
2. **`assert usage.prompt_tokens > 1` on every distiller call.** A mistyped model silently drops its prompt and emits confident, fluent, schema-plausible findings invented from nothing — straight into shared memory that teammates build on. Highest-severity failure in the project.
3. **The segmenter must reproduce the frozen fixture Segments exactly.** That test is the anti-drift gate between Plans A and B.

## Loose ends closed today

- Old plans marked SUPERSEDED rather than deleted; their Plan 0 contract block is still the source the new Plan 0 copies from.
- Design doc §4 rewritten to the orchestrator architecture; the two-plane framing is retired.
- `docs/architecture.html` rebuilt — new flow diagram, orchestrator stage, revised contracts table, protocol-native briefing.
- Working notes promoted to amendment F with every question closed and its reasoning kept.

## Not done

- **No code.** Not a line. Plan 0 has not been executed.
- The Miro board still holds the pre-revision diagrams; the architecture page's SVGs are now the current source of truth.
- Codex's exact transcript path is unconfirmed — it is on the demo path, so confirm it on day one.
