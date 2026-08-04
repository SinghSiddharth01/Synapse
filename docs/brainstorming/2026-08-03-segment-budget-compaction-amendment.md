# Amendment 2026-08-03: Segment token budget, compaction, and 4B quality expectations

**Status:** Adopted 2026-08-03
**Amends:** `2026-07-25-synapse-design.md` §5 (Segment contract) / §9 (NPU spike) / §12 (risks) · Plan A (segmenter) · Plan B Tasks 4 & 6 · Plan C (synthesis prompt)
**Context:** Design-session learnings on context length for the on-device distiller and on what quality to expect from a ~4B model. Companion to `2026-08-03-hybrid-frontier-local-amendment.md` (which owns the frontier/local split; this doc owns the local path's budget, compaction, and calibration).

---

## Part 1 — Context length is a segment-level budget problem, not a session-level one

The pipeline is a map-reduce: the edge model **maps** over bounded chunks, and the AI-100 synthesis **is the reduce**. A whole session is never summarized on device — findings accumulate incrementally and merge in the cloud. What the original design left unbounded is the *chunk*: nothing caps a Segment's token size, and real Claude Code turns (the fixture session: 5,579 lines, 1,212 tool calls) can run 20–50K tokens of raw content, dominated by tool_results.

Two constraints define the real budget, and neither is the model's advertised window:

1. **Compiled bundle context.** AI Hub qairt bundles are compiled with a fixed context length — typically 4K — regardless of what the model supports on paper (Qwen3-4B claims 256K; irrelevant on the NPU path). Must be measured, not assumed (see Part 3).
2. **Prefill cost.** The distiller is always-on; prefilling tens of K tokens per segment kills the "passive listener that doesn't steal cycles or power" thesis even if the window allowed it.

**Rejected alternatives** (recorded so they aren't re-proposed):

| Alternative | Why rejected |
|---|---|
| Rolling / hierarchical summary on device (carry a running summary, re-summarize summaries) | Adds mutable state, compounding drift, and extra prefill per call — to produce a compression `Finding[]` + cloud merge already provide for free |
| Buy a bigger window (long-context GGUF via `llama_cpp`, Qwen3-8B) | Sessions are unbounded, so any fixed window still needs chunking; trades memory/thermals for a problem it doesn't solve. Only a fallback if qairt bundles compile below ~2K |

## Part 2 — Segment budget + compaction (contract change)

- **`Segment` gains a token budget.** The segmenter splits on turn boundaries **and** on budget — a monster turn becomes several sub-segments, never one unprocessable blob.
- **Budget math** (working assumption until the spike measures the bundle): 4K window − ~400 system prompt − ~350 few-shots (hybrid amendment Part 1) − ~800 response reserve → **~2–2.5K tokens of events per segment**. Final number derives from measured context + prefill tok/s.
- **Compaction is deterministic code, not a model.** Before prompt rendering: head/tail-truncate tool_results (first/last ~15 lines — errors and conclusions live at the edges), trim or drop `thinking` blocks, skip trivial tool calls entirely (cheaper than telling the model to ignore them), strip binary/base64. Weight retained content toward assistant text (the weighting principle from the hybrid amendment, item 3).
- **No local merge.** Each sub-segment emits its own `Finding[]`; dedup across sub-segments is synthesis's existing job. The reduce step exists — reuse it.
- **Optional session header** — the only device-side memory allowed: session `purpose` + last 3–5 findings, hard-capped ~200 tokens, prepended to each distill call. Grounds the 4B against emitting ungrounded restatements of already-captured findings.
- **New fixture:** one segment containing an oversized tool_result, with golden findings proving truncation preserves the signal (budget never exceeded, error line survives).

## Part 3 — NPU spike additions (Plan B Task 4)

- **New go/no-go axis: usable compiled context length** of the GenieX bundle (alongside residency, tok/s, power, schema-valid rate). This is the number Part 2's budget math keys off.
- **Derive the segment budget from prefill tok/s**, not taste: at 500 tok/s prefill, a 2.5K-token segment costs ~5s per call — fine for a background worker; 30K would be ~60s and a power problem. Record the measured curve in the spike runbook.

## Part 4 — Quality calibration for the ~4B distiller

Recorded so the eval results are read against expectations, not hopes.

**Why the task suits a small model:** distillation here is *grounded extraction* — everything needed is in the prompt, output is a few short sentences of structured JSON. That is the regime where 4B instruct models overperform their size. The 4K window bounds *scope per call*, not insight: findings are mostly local to a turn, and cross-turn arcs are covered by the session header + cloud synthesis.

**Expected failure modes, ranked by concern:**

| # | Failure mode | Note |
|---|---|---|
| 1 | **Verbatim copying** | Abstracting is harder than copying; small models take the easy path. This is a *privacy property*, not a quality dip — hence the deterministic n-gram verbatim-copy metric (hybrid amendment Part 1, item 4) |
| 2 | Triviality ("the agent ran Grep") | Restating events instead of insight; the judgment-heavy part of the task |
| 3 | Empty-segment discipline | Small models invent findings for boring segments; hence the all-noise fixture with an empty golden |
| 4 | Missed dead ends | Requires linking an error to a later pivot; compaction can lose the error line — the fixture in Part 2 guards this |
| 5 | Schema compliance | Least concern: flat schema, temperature 0, tolerant parse + retry — expect >90% schema-valid from Qwen3-4B |

**Realistic prediction:** judged against Claude goldens, expect the 4B around **0.5–0.7** — it catches explicit decisions and loud errors, misses subtle arcs, over-produces trivia. That is an acceptable product result (a teammate's agent knowing ~60% of what yours learned, minutes later, at zero cloud cost) and the honest delta *is* the demo narrative (§12).

**Synthesis as trivia filter (Plan C, synthesis prompt):** add one instruction — *drop findings that merely restate actions without insight, and drop duplicates* — so the 8B cleans the 4B's noise before anything reaches a teammate. The quality backstop most edge-SLM designs lack.

**Escalation ladder** (unchanged, for completeness): few-shots + prompt-optimizer loop first (hybrid amendment) → Qwen3-8B bundle swap if the eval table says 4B genuinely disappoints (provider amendment Part 3).

---

## Plan impact summary

| Plan item | Before | Now |
|---|---|---|
| `Segment` contract (§5) | bounded only by turn boundary | + token budget (~2–2.5K, final number from spike); deterministic compaction before rendering |
| Plan A segmenter | split on turn boundary | split on turn boundary AND budget; compaction stage (truncation/skip/strip rules above) |
| Plan B Task 4 (NPU spike) | residency, tok/s, power, schema-valid | + usable compiled context length; segment budget derived from measured prefill |
| Plan B Task 6 (eval) | judge score, cost, latency (+ hybrid-amendment axes) | read against the calibration in Part 4; oversized-tool_result fixture added |
| Plan C synthesis prompt | merge, dedup, conflicts | + trivia filter line |
| Fixtures | 2 hand-authored segments | + oversized tool_result fixture, + all-noise/empty-golden fixture (hybrid amendment) |
