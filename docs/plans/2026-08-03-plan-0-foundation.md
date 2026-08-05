# Plan 0 — Foundation

**Owner:** all three, together. **Blocking:** nothing parallel starts until this is green.
**Target:** half a day. If it runs past one day, cut scope — the walking skeleton matters more than completeness.

**Goal:** freeze the shared types, commit the ground-truth fixtures, and prove the contracts *compose* with a walking skeleton before anyone builds a real implementation behind them.

**Why together:** the fixtures encode a quality bar and a segmentation boundary that two different tracks must agree on. Written by one person, they become that person's opinion; written together, they are the contract.

---

## Prerequisites

- Read `/CONTEXT.md`. The plans use Agent Session / Shared Session / Agent Run / Attribution / Tombstone precisely, and mixing them up is how the tracks drift.
- Python 3.12, `uv`, `pytest`, Pydantic v2.

## Task 0.1 — Monorepo scaffold

```
packages/
  contracts/     synapse_contracts      # frozen types, zero dependencies beyond pydantic
  providers/     synapse_providers      # ModelProvider + FakeProvider
  worker/        synapse_worker         # Plan A
  distiller/     synapse_distiller      # Plan B
  service/       synapse_service        # Plan C
  orchestrator/  synapse_orchestrator   # Plan D
fixtures/
  segments/      *.json                 # hand-authored Segments
  findings/      *.findings.json        # golden Finding[] per segment
```

**Done when:** `uv sync` succeeds and every package imports.

**Rule:** every contract lives in `synapse_contracts`. Other packages import; they never redefine. Enforced by review, not tooling.

## Task 0.2 — Freeze the contracts

Authoritative source: the schema block in `docs/brainstorming/2026-07-25-plan-0-foundation.md` Task 2, plus its contract-revision table. Copy it into `synapse_contracts/schemas.py` verbatim; do not re-derive it from prose.

> ⟨2026-08-04⟩ **Built.** One bug found and fixed at the source: that document's `__init__.py` listed `Attribution`, `FindingId`, `FindingStatus` and `Provenance` in `__all__` without importing them, so `from synapse_contracts import Attribution` raised `ImportError`. Corrected in the source doc.
>
> One addition beyond the freeze: **`SessionBinding`** — the on-disk form of `LocalBinding`, plus which transcript it pins and when. It lives in `synapse_contracts` so the write side (worker `join`) and the read side (`resolve_transcript`) need no dependency on each other.

Types: `AgentEvent` · `Segment` · `Attribution` · `Finding` (+`FindingId`, `FindingType`, `Provenance`, `FindingStatus`) · `SynapseSession` · `LocalBinding` · `Conflict` · `SessionContext` · `ModelUsage` · `ModelResult`, plus the ingest/producer request-response pairs.

**First failing tests:**
- a `Finding` round-trips through JSON with multiple `attributions`
- `RETRIEVABLE == merged_into is None and status is KEPT` holds for a kept finding, a tombstone, and a trivial finding
- a `Conflict` referencing two `FindingId`s validates; one referencing a `Finding` object does not
- `Segment` and `AgentEvent` expose `agent_session_id` and **no** field called `session_id`

**Done when:** green, and `grep -r "source_session\|local_agent_session_id" packages/` returns nothing.

## Task 0.3 — Fixture Segments and golden Findings

The single most valuable artefact in this plan. Hand-author, co-authored by all three, committed to the repo.

**Required fixtures — at least five:**

| Fixture | Purpose |
|---|---|
| `seg-001` | ordinary turn; 2–4 findings covering learning / decision / dead_end / open_question |
| `seg-002` | a second ordinary turn, different shape |
| `seg-003` | **oversized `tool_result`** — golden proves compaction preserves the error line and the budget is never exceeded |
| `seg-004` | **all noise** — golden is an **empty array**. Small models invent findings for boring input; this is the guard |
| `seg-005` | **two near-duplicate findings** across two Contributors — the input to the semantic-merge test in Plan C |

**Done when:** every fixture parses into `Segment`, every golden parses into `Finding[]`, and a loader test proves it.

**Do not shortcut this.** The goldens *are* the eval target and the quality bar. Plan A's segmenter must reproduce the segments exactly; Plan B's distiller is measured against the findings.

> **⟨STATUS 2026-08-04⟩ Corpus completion is implemented on branch `exec/e1`, pending merge to `main`.** A fix-and-verify round is complete — verifier verdict **clean**, no residual findings, nothing refuted. The corpus grows from two fixtures to eight: `seg-002` (insight with no error), `seg-003` (oversized `tool_result`, buried error), `seg-005a`/`seg-005b` (the semantic-merge pair ADR 0002 needs), `seg-006`/`seg-007` (the adversarial triage pair), plus `fixtures/triage.json` — the per-fixture keep/skip expectation map E2's triage tests consume — and a fixture/prompt-pack six-gram contamination guard (`test_fixture_contamination.py`). Until the merge lands, `main` still has only `seg-001`/`seg-004` and cannot produce a triage recall rate.
>
> Two plan amendments recorded post-review on `docs/plans/exec/2026-08-04-e1-corpus-and-privacy-metric.md`, both closing documentation gaps rather than reversing direction. **Task 2:** `seg-003` exercises only prose-restated `dead_end` recall under the shipped `distil_kinds=["text"]` default — no budget-splitting, no `tool_result` recall, until compaction (A.5) lands — a gap between the task's opening promise and the shipped default, not a design reversal. **Task 4:** `seg-006`/`seg-007`'s goldens were reversed from empty to non-empty under `adr/0003` — a triage-kept segment reaching a compress-only distiller should yield findings; empty goldens encoded a durability judgment the distiller no longer makes. Review found the reversal correct and named the real defect as the missing amendment and a false "no deviations" report, not the direction.
>
> `seg-005` (near-duplicates across Contributors) is no longer missing — see above; Plan C's ADR 0002 exit criterion now has its input. Of the four cases suggested beyond the original five, three landed with this corpus (insight-with-no-error, error-that-is-not-insight, noise-that-looks-like-signal); the fourth — **a `dead_end` whose pivot lands in the next segment** — is explicitly deferred in the E1 plan pending a cross-segment eval axis, not forgotten.
>
> Goldens remain **PROVISIONAL** — unsigned by co-review, per the sign-off table in `fixtures/README.md`. `seg-004`'s golden empty array still encodes a *triage* expectation under `adr/0003`, not a distiller one — unchanged by this round.

## Task 0.4 — `ModelProvider` + `FakeProvider`

`complete(messages, response_schema?) -> ModelResult` carrying `data`, `usage`, `latency_ms`, `provider_id`, `schema_valid`, plus a `ProviderCapabilities` flag for `native_structured_output`.

`FakeProvider` returns scripted deterministic output. **This is the top priority in the whole plan** — Plans B and C are both blocked on it, and it takes an hour.

**First failing tests:** scripted input → expected output; `usage` and `latency_ms` populated; capability flag readable; an unscripted input raises rather than inventing a response.

## Task 0.5 — Orchestrator shell

Just the shell — Plan D fills it in. It exists here because the walking skeleton cannot walk without it.

- process that starts, reads config, holds `LocalBinding` in memory
- an in-process producer endpoint accepting `Finding[]`
- a service client interface with a fake implementation

**First failing test:** posting `Finding[]` to the producer endpoint stamps Attribution from the binding and hands it to the service client.

## Task 0.6 — Walking skeleton (the point of the whole plan)

Wire the thinnest possible end-to-end path with everything fake:

```
FakeSource → segmenter → FakeProvider distiller → orchestrator
           → in-memory synthesis → query() → ranked Finding[]
```

**First failing test:** a fixture Segment enters, and a `query()` against the resulting shared memory returns a Finding whose text came from the golden. One test, no mocks beyond the fakes, no network, no hardware.

**Done when:** it passes. That proves the contracts compose, which is the only thing that makes three parallel tracks safe.

---

## Exit criteria

1. `uv run pytest` green across all packages, offline, no keys, no hardware.
2. Five fixtures + goldens committed.
3. Walking skeleton passes.
4. `CONTEXT.md` terms used consistently in code (`agent_session_id`, `attributions`, `merged_into`).

## Scope / YAGNI

**In:** scaffold, contracts, fixtures, `FakeProvider`, orchestrator shell, walking skeleton.
**Out:** every real implementation — source adapters (A), distiller and NPU (B), real providers and synthesis (C), MCP surface and awareness (D).

## Risks

| Risk | Mitigation |
|---|---|
| Fixtures encode a segmentation boundary Plan A later disagrees with | Co-author them. The segmenter's contract *is* the fixture, not a doc |
| Goldens encode a quality bar the 4B cannot reach | Co-author them. If even Claude scores poorly against them, the fixtures are wrong — revisit them, not the model |
| Contracts drift after the freeze | One package owns them; others import. Any change is a three-person conversation, not a commit |
| Plan 0 sprawls and blocks everyone | Timebox to a day. `FakeProvider` and the fixtures are the parts that unblock others — ship those first |
