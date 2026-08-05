# Plan B — Model

**Track:** distillation, on-device inference, measurement.
**Suggested owner:** Aditya (owns the X Elite laptop).
**Depends on:** Plan 0 (contracts, fixtures, `FakeProvider`).
**Read first:** `docs/brainstorming/2026-07-30-npu-llm-benchmarks-and-geniex-findings.md` — measured hardware evidence that supersedes earlier assumptions.

**Goal:** turn a `Segment` into a validated `Finding[]` on-device, and produce the numbers that answer *"why not just call Claude?"*.

---

## What the measurements already settled

Do not re-litigate these. Measured on the X1E80100 — 2026-07-30, updated 2026-08-04.

- `geniex serve` is **verified live** — OpenAI-shaped. `NPUProvider` is the thin subclass this plan predicted; the standard body works unchanged.
- The NPU is the **slowest** unit for LLM decode (12–14 tok/s @ 4B, consistent across both measurement days; CPU 21.3, GPU 17.6). Decode is memory-bandwidth-bound. The NPU case is **contention and power, never speed**. Power is still unmeasured; do not claim efficiency until it is.
- ⟨2026-08-04⟩ **AI Hub is back up and the `qairt` path is live.** `qualcomm/Qwen3-4B-Instruct-2507:W4A16` is cached and running on the NPU. QAIRT is now the default arm; `llama_cpp` became the untested alternative. This reverses the 2026-07-30 blocker.
- ⟨2026-08-04⟩ **`usable_context = 4096`, measured** — `prompt_tokens=3809` succeeded, ~7000 returned HTTP 400. On `qairt` this is a *hard* ceiling: precision, context length and KV cache are baked in at compile time and there is no `--nctx`. Task B.5 predicted this exactly; it is now confirmed.
- ⟨2026-08-04⟩ **`native_structured_output = False`, and the probe method is the finding.** Three requests — no field, `response_format: {"type":"json_object"}`, and a deliberately bogus `enable_json: true` control — returned **HTTP 200 with byte-identical output**. GenieX accepts unknown top-level parameters silently, so a 200 proves nothing. `NPUProvider` deliberately does not send `response_format`; sending it would produce a request that looks enforced and is not. This reverses the optimistic reading in the 2026-07-30 evidence.
- `Gemma-4-E4B-it` ships mistyped as `vlm` and **silently drops prompts** — fix with `geniex model set-type <model> llm`.

> **Generalise the control-probe lesson.** Any capability probe against any provider needs a deliberately invalid parameter as a control. Acceptance is not evidence of support, and this class of mistake produces confident wrong capability flags rather than errors.

## Task B.1 — Distiller ⟨REFRAMED 2026-08-04 — see `adr/0003`⟩

`Distiller(provider, binding) : Segment -> Finding[]`.

> **The distiller compresses and abstracts. It does not judge what is worth keeping.** This plan originally asked a 4B for four jobs at once — abstract, judge durability, choose granularity, classify. Measured across six configurations it did one reliably. It invented a finding from an all-noise segment in **6 of 6**, merged a `dead_end` into a `decision` despite a rule forbidding exactly that in those words, and **reversed a comparison stated twice in its own prompt** — while passing the canary, the `prompt_tokens` guard, schema validation and the verbatim metric. An invented trivial finding is noise a reader discards; an inverted one is misinformation that reaches a teammate's agent looking identical to a correct finding.
>
> Durability judgment moved upstream to **triage** (Plan A.5b, deterministic, does not exist yet) and downstream to **synthesis** (Plan C.4 — `FindingStatus.TRIVIAL` was always documented as service-written, so this reclaims a filter the design already had).
>
> The prompt carries an explicit fidelity rule, which is what fixed the inversion: *"Never reverse a comparison. Never turn a drawback into a benefit. Where what the session says conflicts with what you would expect to be true, follow the session."* On n=1. The failure mode — a prior beating explicit context — is not one a prompt can be assumed to close.

- Renders a compacted Segment into a prompt + the `Finding[]` schema.
- **`Finding.type` is best-effort, not authoritative.** It labelled *"No other changes were made"* a `decision`. Harmless if synthesis re-types; a cross-track conversation if anything downstream trusts it.
- Stamps `Finding.id` (client-assigned UUID) and builds `attributions: list[Attribution]` from the LocalBinding. `provenance` defaults to `distilled`. `status`, `merged_from`, `merged_into` are **service-written** — leave them at defaults.
- Providers without native structured output use prompt-instructed JSON + tolerant parse + **one** retry, then drop.

**First failing tests:** fixture Segment + `FakeProvider` → schema-valid `Finding[]` matching the golden shape; every `FindingType` reachable; malformed model output triggers exactly one retry then drops; the all-noise fixture yields an **empty array**; ids are unique and stable across a re-render of the same Segment object.

## Task B.2 — Prompt and few-shots ⟨BUILT — as versioned packs⟩

Rules-based system prompt plus **1–2 Claude-authored few-shots**, including one that *abstracts* rather than quotes.

**Prompts are versioned TOML packs in `config/prompts/*.toml`, not module constants**, so they can be A/B'd without a code change. Each declares `judges_durability` so the harness does not score a compression pack against a job it was never asked to do, and `contaminated_fixtures` (see below). Four ship: `v1-baseline` (frozen, contaminated), `v2-hardened`, `v3-text`, `v4-condense` (default).

Two more axes are configurable the same way and both env-overridable: **`distil_kinds`** (which `AgentEvent` kinds reach the model, default `["text"]` — halves the prompt on `seg-001` with no goldens lost, pinned by a test) and **`render_style`** (`"labelled"` vs `"content"`). Stripping role labels saves 12 tokens, 0.7%, and in one run flipped a fact — `labelled` is the default. Labels are `developer:`/`agent:`, since `CONTEXT.md` lists `user`/`assistant` under *_Avoid_*.

**Overhead is calibrated against the model's own tokenizer**, not estimated: `scripts/calibrate_prompt.py` sends a pack with an empty segment and reads `prompt_tokens` back. The chars/4 estimate over-counts by 118–222 tokens depending on pack — safe, but it was leaving ~200 unused on a 4096 bundle.

> **Never let one person write both the prompt and the eval target.** `v1-baseline`'s second few-shot was a near-duplicate of fixture `seg-004` — same activity, identical tool-result string. Every `seg-004` score from that pack measured pattern-matching against the nearest few-shot rather than generalization, and **a reported "the hardening fixed it" result was void**; with a structurally different noise example the model reverted to inventing. Packs now declare `contaminated_fixtures` and the harness prints `VOID` and excludes the score rather than reporting an inflated one. This is exactly what Plan 0 Task 0.3 warns about.

**Explicitly first-pass.** The prompt is not a contract. The eval loop is how it changes.

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
usable_context          4096 — MEASURED 2026-08-04, hard ceiling on qairt
prefill_toks_per_sec    250.0 — ⚠️ STILL A GUESS, marked PROVISIONAL in code
system_prompt_tokens    measured via the model's own tokenizer, per pack
few_shot_tokens         measured, same way
response_reserve        known
```

Current derived budget: **2787** tokens. Editing a prompt re-budgets segmentation automatically — the budget is derived, never configured.

> `prefill_toks_per_sec = 250.0` is the last unmeasured input to the budget derivation, so a wrong value mis-sizes every segment. Measure it or stop clamping on it.

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
- **empty-segment discipline** — the all-noise fixture must yield an empty array. ⟨2026-08-04⟩ **This now scores triage, not the distiller** (`adr/0003`), and triage does not exist.

> **⟨BLOCKER 2026-08-04⟩ The verbatim metric cannot see the leaks that actually happen, and it is currently reporting a number that reads as proof.** It uses 8-word n-grams; a single leaked token never fills that window. It scored **0.00** on a finding containing `default_pool_size=25`, and 0.10 on one that copied two filenames. The architecture's central claim is that raw work never leaves the device — **reporting this number today would be a false claim about the property everything else rests on.** Keep it out of the demo until fixed.
>
> Needs an identifier-shaped-token check: `snake_case`, `dotted.host`, `CamelCase`, paths, `file.ext`. The hard part is that not every identifier is private — `pgbouncer`, `asyncpg` and `ruff` are public vocabulary the goldens use deliberately, so the check needs an allowlist or a "does this appear outside this session" notion, not a blanket ban.
>
> `adr/0003` made this **worse**, knowingly: compression pulls wording toward the source, which is the opposite pressure from "state it in your own words." Overlap rose 0.00 → 0.10. That trade was accepted to stop factual inversions; it raises the priority of fixing the metric rather than lowering it.

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
| 3 | ~~Inventing findings for boring segments~~ | **Confirmed and unfixable by prompting — 6 of 6 configurations. Moved to triage (`adr/0003`).** Four independent prompt attempts moved it exactly zero |
| 4 | Missed dead ends | Requires linking an error to a later pivot; compaction can lose the error line |
| 5 | Schema compliance | **Confirmed a non-issue** — clean JSON on the first attempt, zero retries across every run |
| ★ | **Factual inversion** ⟨NEW, unranked in the original list⟩ | **The one nobody predicted, and the most dangerous.** A 4B reversed a comparison stated twice in its own prompt, following a prior about persistent connections over the explicit context. It passed the canary, the `prompt_tokens` guard, schema validation *and* the verbatim metric while doing it. Fixed on n=1 by a fidelity rule; no evidence it holds. An inverted finding reaches a teammate's agent looking exactly like a correct one |

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
| Model silently drops prompts | `prompt_tokens > 1` assertion + canary. Built and holding |
| **A 4B inverts a fact while passing every guard** ⟨2026-08-04⟩ | **Now the highest-severity risk in the project** — it poisons shared memory invisibly and, unlike a dropped prompt, produces output indistinguishable from correct. Fidelity rule in `v4-condense` fixed it on n=1. Needs a real corpus, and possibly a verification pass (deferred: doubles the cost of the slowest step and asks the same model to check itself) |
| ~~AI Hub 503~~ | **Cleared 2026-08-04.** AI Hub is up, `qairt` is the default arm, `llama_cpp` is now the untested alternative |
| Power turns out worse than CPU | Then the NPU rationale collapses and we say so. Better found in the bake-off than on stage |
| Schema-valid rate below ~10% | Drop `NPUProvider` from the demo and lean on the AI-100 story; report the number honestly |
| Judge bias against small-model phrasing | Note it. Five segments is directional, not statistical |
| Corpus too small to convince | Expand to 6–8 fixtures during the week if bandwidth allows. Not blocking |
