# Plan B — Model

**Track:** distillation, on-device inference, measurement.
**Suggested owner:** Aditya (owns the X Elite laptop).
**Depends on:** Plan 0 (contracts, fixtures, `FakeProvider`).
**Read first:** `docs/brainstorming/2026-07-30-npu-llm-benchmarks-and-geniex-findings.md` — measured hardware evidence that supersedes earlier assumptions.

**Goal:** turn a `Segment` into a validated `Finding[]` on-device, and produce the numbers that answer *"why not just call Claude?"*.

---

## What the measurements already settled

Do not re-litigate these; they were measured on the X1E80100 on 2026-07-30.

- `geniex serve` is **verified live** — OpenAI-shaped, ready ~1 s after launch. `NPUProvider` is a thin subclass, not a custom client.
- The NPU is the **slowest** unit for LLM decode (14.2 tok/s @ ~4B vs CPU 21.3, GPU 17.6). Decode is memory-bandwidth-bound. The NPU case is **contention and power, never speed** — it keeps always-on work off the critical path. Power is still unmeasured; do not claim efficiency until it is.
- The `qairt` path is **blocked** (AI Hub 503). `llama_cpp` GGUF is the validated path; pull models from Docker Hub / HF and **cache them now**.
- `Gemma-4-E4B-it` ships mistyped as `vlm` and **silently drops prompts** — fix with `geniex model set-type <model> llm`.

## Task B.1 — Distiller

`Distiller(provider, binding) : Segment -> Finding[]`.

- Renders a compacted Segment into a prompt + the `Finding[]` schema.
- Stamps `Finding.id` (client-assigned UUID) and builds `attributions: list[Attribution]` from the LocalBinding. `provenance` defaults to `distilled`. `status`, `merged_from`, `merged_into` are **service-written** — leave them at defaults.
- Providers without native structured output use prompt-instructed JSON + tolerant parse + **one** retry, then drop.

**First failing tests:** fixture Segment + `FakeProvider` → schema-valid `Finding[]` matching the golden shape; every `FindingType` reachable; malformed model output triggers exactly one retry then drops; the all-noise fixture yields an **empty array**; ids are unique and stable across a re-render of the same Segment object.

## Task B.2 — Prompt and few-shots

Rules-based system prompt plus **1–2 Claude-authored few-shots** frozen into it, including one that *abstracts* rather than quotes. Budget ~300–400 tokens inside the segment budget.

Attacks the two worst expected failure modes of a small model: verbatim copying and triviality.

**Explicitly first-pass.** The prompt is not a contract. It will change mid-week; the eval loop is how.

## Task B.3 — Safety guards (mandatory, not optional)

A mistyped model emits confident, fluent, schema-plausible findings **invented from nothing**, which would then stream into shared memory that teammates build on. Silent knowledge poisoning that passes casual review.

1. **Assert `usage.prompt_tokens > 1` on every distiller call.** Log and hard-fail the finding if it trips. Cheapest possible guard against this entire class of failure.
2. **Canary fixture** in the eval harness: a prompt with one unambiguous extractable fact. If the answer does not contain it, **fail the model before it is scored** — do not run the corpus.
3. **Verify `ModelType` after every `geniex pull`.** Any candidate shipping an `mmproj` file is at risk. Prefer text-only GGUFs.

**First failing tests:** a provider returning `prompt_tokens: 1` raises rather than yielding findings; a model failing the canary is excluded from the benchmark run.

## Task B.4 — `NPUProvider`

Thin `OpenAICompatibleProvider` subclass at `http://127.0.0.1:18181/v1`. No custom client, no chat-template rendering, no optional native dependency.

Set `native_structured_output` **from evidence**: GenieX's CLI exposes `--enable-json` / `--grammar-path` / `--grammar-string`. **Probe whether these reach `serve`'s HTTP API** — narrow, high value. If they do, the edge path gets sampler-*guaranteed* JSON, which is a better structured-output guarantee than the cloud synthesizer has.

**First failing tests:** mirror the OpenAI-compat suite against `pytest-httpserver`; capability flag honoured; schema request without native support falls back to prompt-instructed JSON.

## Task B.5 — Per-model capability records

The record Plan A's segmenter reads to derive its budget. One row per candidate model:

```
usable_context          measured, not advertised (qairt bundles are often 4K
                        regardless of what the model claims on paper)
prefill_toks_per_sec    measured
system_prompt_tokens    known
few_shot_tokens         known
response_reserve        known
```

```
budget = usable_context − system_prompt − few_shots − response_reserve
         clamped by:  budget ≤ prefill_toks_per_sec × max_seconds_per_call
```

**First failing tests:** the derivation is exercised with two different records and yields two different budgets; a record implying a budget below one minimum segment fails loudly rather than emitting unusable segments.

## Task B.6 — Bake-off

Same corpus, seed-pinned, across **three compute arms** — NPU, GPU, CPU — and the candidate models: Qwen3-4B-Instruct (primary), Gemma-4-E4B (after the `set-type` fix), Qwen3-1.7B (power/speed floor), Qwen3-8B (quality ceiling, only if 4B disappoints).

Axes: schema-valid rate · prefill and decode tok/s · usable context · **power** · quality vs goldens.

Adreno is 2.9× the NPU at 1.7B, so the GPU arm is not a formality. **Power is the number the whole efficiency argument rests on and is still unmeasured** — get it.

## Task B.7 — Eval harness

Corpus × provider → a table of quality, cost, latency. Nearly free because `usage` and `latency_ms` are already in `ModelResult`.

**Deterministic axes that need no LLM:**
- **verbatim-copy rate** — n-gram overlap between finding text and segment content. This is the *privacy* metric, first-class in the table.
- **empty-segment discipline** — the all-noise fixture must yield an empty array.

**LLM-judged axis:** quality vs the goldens, with Claude as judge. A five-segment corpus is a directional signal, not a statistical claim — say so.

**Frontier baseline:** `distiller: claude` through this same harness *is* the frontier arm. It is not a component to build.
> **Guardrail:** benchmark on the **committed fixture corpus only**, never on live team sessions. Pointing a frontier baseline at a real teammate's transcript is the raw-segment escalation the design rejected.

## Task B.8 — Demo A/B measurement

The recorded demo's numbers come from here, and the baseline must be defensible.

**A/B the agents, not the humans.** The same task cannot be run twice by the same people — whoever goes second already knows the answer, and no ordering fixes it (baseline first inflates every number; Synapse first hands the baseline so much advantage it wins). The claim is about agents anyway.

```
capture   one real multi-person session → real worker → real distiller
                                        → Shared Memory (populated)
run A     agent, cold, task prompt T, repo @ commit C
run B     agent, same T, same C, Synapse attached
measure   turns to resolution · tokens (ModelResult.usage)
          tool calls / files re-explored · dead ends re-entered
```

Reproducible, no human learning effect, runnable N times so the recording shows variance rather than one anecdote.

> **Honesty constraint:** the pre-populated findings must come from an actual capture distilled by the real pipeline — never hand-authored for the demo. Hand-authoring them makes the whole comparison theatre.

**First failing tests:** a harness run from a fixed repo state and prompt produces a comparable metric row for both arms; the populated-memory arm is seeded from a real capture file, not a literal.

**Failure-analysis loop:** feed low-scoring cases (segment → output vs golden) back to Claude for diagnosis and a proposed prompt revision. ~50 lines on top of the harness. Frontier as prompt engineer, never as a runtime dependency.

---

## Calibration — read results against this, not against hope

Expect the 4B around **0.5–0.7** vs Claude goldens. Failure modes, ranked:

| # | Mode | Note |
|---|---|---|
| 1 | Verbatim copying | Abstracting is harder than copying. A *privacy* property, not a quality dip |
| 2 | Triviality | Restating events instead of insight — the judgment-heavy part |
| 3 | Inventing findings for boring segments | Hence the all-noise fixture |
| 4 | Missed dead ends | Requires linking an error to a later pivot; compaction can lose the error line |
| 5 | Schema compliance | Least concern — flat schema, temperature 0, tolerant parse |

0.5–0.7 is an acceptable product result: a teammate's agent knowing ~60% of what yours learned, minutes later, at zero cloud cost. **The honest delta is the demo narrative.**

## Exit criteria

1. Distiller green against fixtures with `FakeProvider`.
2. Guards in place and tested — no path to a finding from a dropped prompt.
3. `NPUProvider` runs the corpus against live `geniex serve`.
4. Capability records populated with measured numbers; Plan A's budget derives from them.
5. Benchmark table across three compute arms and the model candidates, including power.

## Scope / YAGNI

**In:** distiller, prompt + few-shots, guards, `NPUProvider`, capability records, bake-off, eval harness, failure-analysis loop.
**Out (stretch):** distiller output caching; streaming distillation; a fine-tuned SLM (out of scope — the point is that an off-the-shelf bundle suffices).

## Risks

| Risk | Mitigation |
|---|---|
| Model silently drops prompts | `prompt_tokens > 1` assertion + canary. **The highest-severity risk in the project** — it poisons shared memory invisibly |
| AI Hub 503 persists, `qairt` never testable | `llama_cpp` is validated and primary; cache models from Docker Hub. If AI Hub recovers it may reverse the compute ranking — re-check periodically |
| Power turns out worse than CPU | Then the NPU rationale collapses and we say so. Better found in the bake-off than on stage |
| Schema-valid rate below ~10% | Drop `NPUProvider` from the demo and lean on the AI-100 story; report the number honestly |
| Judge bias against small-model phrasing | Note it. Five segments is directional, not statistical |
| Corpus too small to convince | Expand to 6–8 fixtures during the week if bandwidth allows. Not blocking |
