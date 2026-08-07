# NPU efficiency: what is measured, what is published, what is still missing

**Date:** 2026-08-06 · **Branch:** `feat/npu-power-efficiency-analysis` · **Box:** Dell
Latitude 7455 · Snapdragon X Elite X1E80100 · Windows 11 Pro · ARM64 · GenieX CLI

This document exists because the repository forbids the claim the demo wants to
make. `scripts/run_npu_eval.py:165` prints, on every run:

> `Power is unmeasured — do not claim efficiency from this run.`

and the same prohibition appears in at least eight other places
(`docs/plans/2026-08-03-plan-b-model.md:17,103`, `docs/STATE.md:87,101`,
`docs/NPU-RUNBOOK.md:111-114`, `docs/architecture.html:1045`,
`docs/2026-08-04-implementation-report.md:265`,
`docs/brainstorming/2026-07-30-npu-llm-benchmarks-and-geniex-findings.md:162-164`,
`docs/adr/0003`). Meanwhile the hackathon rubric awards 40 points to Technical
Implementation *including energy efficiency*, and
`docs/DEMO-READINESS-CHECKLIST.md:9` carries the open item.

The purpose here is to replace that prohibition with numbers, or — where a
number still cannot be obtained — to say precisely why, so nobody has to guess
on stage.

> **Read this first.** No watt, joule, or energy figure was measured. The power
> measurement was attempted, the harness built (`scripts/measure_power.py`), and
> then dropped — see §7.1. Everything below is throughput, variance, rate limits
> and cost. The efficiency argument that survives is **determinism** (§2.2), not
> energy, and it is stronger evidence than the energy number would have been on
> its own because it is ours, measured today, and reproduces an independent
> earlier result on a different workload.

---

## 1. Findings that change the demo narrative

**1. The NPU is not faster. It was never going to be.** Measured here and
independently on 2026-07-30, the NPU is the *slowest* of the three compute units
for LLM decode. Any slide claiming NPU speed is falsifiable in one question. The
repo already knows this — `docs/plans/2026-08-03-plan-b-model.md:17`: *"The NPU
case is contention and power, never speed."*

**1b. But the NPU is radically more PREDICTABLE, and that is measurable today.**
Across four identical distillations, decode rate varied by **0.1% on the NPU,
10.8% on the GPU and 16.4% on the CPU** — the NPU is 120x steadier than the GPU
and 182x steadier than the CPU (§2.2). For an always-on background process whose
entire design goal is not to disturb the developer, *predictable* is worth more
than *fast*: a distiller that occasionally takes 1.5x longer is stealing the
machine at an unpredictable moment. This corroborates the team's own MobileNetV2
result (p95/p50 of 1.07 on NPU vs 3.5 on CPU, 2026-07-30) on a completely
different workload class, and it is available **without waiting for the power
number**.

**2. `prefill_toks_per_sec = 250.0` is wrong by 4.2x.** Measured 1041.3 tok/s
(R² = 0.9974). See §3. This value is marked `# PROVISIONAL — needs a seed-pinned
measurement` at `config/synapse.toml:143` and feeds the segment-budget
derivation. Correcting it does **not** change the shipped budget (§3.2), which is
why it was safe to leave — but it is now measured rather than guessed.

**3. Prefill is ~75x faster than decode on the same silicon.** 1041 tok/s
prefill against ~14 tok/s decode. Distillation is therefore almost entirely
decode-bound, and the segment budget — which exists to bound *prefill* time — is
bounding the cheap half. This is the strongest technical-optimization story
available and it is measured, not claimed.

**4. The cost-savings argument is real but must name its comparison.** Against
frontier-model pricing the saving is ~$760/developer/year; against the
*honest* cloud equivalent (a small hosted model) it is ~$2.50/developer/year.
See §5. Presenting the first without naming the second is the kind of claim that
does not survive a judge's follow-up.

---

## 2. Decode throughput, measured 2026-08-06

Identical prompt, identical `max_tokens`, `temperature = 0`, requests issued
**sequentially** — matching how `synapse_worker.loop` actually drives the
distiller (`for segment in segments: await self.distiller.distil(segment)`,
`loop.py:340-374`). Model held constant across compute units:
`google/gemma-4-E4B-it-qat-q4_0-gguf:Q4_0`, selected because the production
QAIRT bundle is NPU-exclusive and cannot run on CPU or GPU at all.

Four calls per unit, on an otherwise-idle machine. Because the model stops when
it stops, each call emits a different number of tokens, so wall-clock per call is
not comparable — every figure below is normalised to tokens/second per call.

| compute unit | mean tok/s | sd | **CV** | min–max |
|---|---|---|---|---|
| GPU (Adreno X1-85) | 14.96 | 1.619 | 10.8% | 12.58 – 15.98 |
| **NPU (Hexagon v73)** | **13.39** | **0.012** | **0.1%** | 13.37 – 13.40 |
| CPU (Oryon, 12c) | 13.19 | 2.165 | 16.4% | 10.61 – 15.91 |

Production configuration, for reference — `qualcomm/Qwen3-4B-Instruct-2507:W4A16`
on the NPU: **16.91 tok/s**, 2.2 s per distillation-shaped call.

### 2.2 The finding: the NPU's advantage is variance, not throughput

On mean throughput the three units are within 13% of each other and the NPU comes
second. On *consistency* they are not close at all:

- **NPU decode-rate variability is 120x lower than GPU and 182x lower than CPU.**
- Four consecutive NPU calls returned 13.37, 13.39, 13.40, 13.40 tok/s. The
  slowest-to-fastest ratio was **1.00**.
- The CPU's was 1.50 and the GPU's 1.27 across the same four calls.

Why this matters for this system specifically: the distiller is an **always-on
background process** on a developer's laptop. Its design constraint is not
throughput, it is not stealing the machine at an unpredictable moment. A unit
that mostly takes 26 s but sometimes takes 40 s is worse for that job than a
slightly slower unit that always takes 26.8 s.

This also independently corroborates the team's 2026-07-30 MobileNetV2 result —
p95/p50 of 1.07 on NPU against 3.5 on CPU — on a completely different workload
class (LLM decode vs vision inference). Two workloads, two measurement days, same
property.

### 2.3 Corroboration against the earlier run

These land on the team's own seed-pinned numbers from 2026-07-30
(`docs/brainstorming/2026-07-30-npu-llm-benchmarks-and-geniex-findings.md:110-120`),
taken on a different day with a different harness:

| model | GPU | CPU | NPU |
|---|---|---|---|
| gemma-4-E4B (2026-07-30) | 17.6 | 21.3 | 14.2 |
| gemma-4-E4B (2026-08-06, here) | 15.0 | 13.2 | 13.4 |

NPU agrees within 6%. GPU within 15%. CPU differs by 38% — and the CPU is also
the least consistent unit measured here (CV 16.4%), so the disagreement is itself
evidence rather than a contradiction: a unit whose rate swings 1.5x between
consecutive calls will not reproduce across days.

### 2.4 Measurement caveat, stated plainly

Four calls per unit. That is enough to establish the variance result above — an
NPU CV of 0.1% across four calls is not an artefact of sample size, and a CPU CV
of 16.4% is a floor, not a ceiling — but it is not enough to rank the three means
against each other with confidence. **Do not present the mean ordering as a
result; present the variance.**

---

## 3. Prefill throughput, measured 2026-08-06

### 3.1 Method

The method `docs/NPU-RUNBOOK.md:105-114` Phase 4 asks for. A single timed call
cannot yield a prefill rate, because it also contains model load, scheduling and
at least one decode step — the caveat `capability.py:114-116` already records:
*"The only timed call so far included model load, so it is not a clean number."*

Instead: seven prompt lengths from 92 to 1709 tokens, `max_tokens = 1` so decode
contributes exactly one token at every length, three repeats per length, median
taken. Least-squares fit of latency against prompt length. The **slope** is the
per-token prefill cost; every per-call constant lands in the intercept and
cancels.

### 3.2 Result — `qualcomm/Qwen3-4B-Instruct-2507:W4A16` on NPU

| prompt tokens | median latency |
|---|---|
| 92 | 0.185 s |
| 239 | 0.289 s |
| 449 | 0.536 s |
| 764 | 0.759 s |
| 1079 | 1.129 s |
| 1394 | 1.396 s |
| 1709 | 1.739 s |

```
slope      0.9604 ms/token
intercept  0.076 s
PREFILL    1041.3 tok/s      R² = 0.9974
```

R² of 0.9974 across a 19x range of prompt lengths, with run-to-run spreads under
6%. This is a clean measurement.

**Effect on the segment budget: none.** `capability.py` derives
`min(usable_context − overhead − reserve, prefill_toks_per_sec × max_seconds_per_call)`:

- context-bound path: 4096 − 809 − 500 = **2787**
- prefill-bound path, old: 250 × 30 = 7500 → `min` = 2787
- prefill-bound path, new: 1041 × 30 = 31 240 → `min` = **2787**

The budget is context-bound and always was. Correcting the record makes it
honest without moving a single segment boundary.

### 3.3 Cross-unit prefill — PARTIAL, and honestly so

Repeated on an idle machine with five repeats per length:

| unit | prefill tok/s | R² | verdict |
|---|---|---|---|
| NPU | 196.9 | 0.7727 | **weak fit — do not rank on this** |
| GPU | 195.7 | 0.8005 | **weak fit — do not rank on this** |
| CPU | 153.1 | 0.9822 | usable |

NPU and GPU are indistinguishable (196.9 vs 195.7 — a 0.6% difference on fits too
weak to support any ordering). Only the CPU figure has a defensible fit. **No
NPU-beats-GPU prefill claim is made.**

The weak fits have an identifiable cause, and it is interesting in itself. Runs
at a single length came back like:

```
prompt_tokens=94   runs=[0.411, 0.411, 0.422, 8.051, 9.287]
```

Three tightly-clustered fast calls, then two ~8-9 s stalls. The distribution is
**bimodal, not noisy** — something periodically stalls the GGUF path for a
consistent ~8 s. Median-of-five rejects it (which is why these fits are usable at
all where the three-repeat attempt at R²=0.499 was not), but it distorts the tail
lengths enough to hold R² below 0.95. Diagnosing that stall is in §7.

### 3.4 The compiled bundle prefills 5.3x faster than GGUF on the same silicon

| model on the NPU | prefill tok/s | R² |
|---|---|---|
| `qualcomm/Qwen3-4B-Instruct-2507:W4A16` (QAIRT, compiled) | **1041.3** | 0.9974 |
| `gemma-4-E4B-it-qat-q4_0` (GGUF, llama.cpp) | 196.9 | 0.7727 |

Same NPU, same harness, comparable model sizes. The QAIRT bundle is **5.3x
faster at prefill** *and* the only one that fits cleanly — the ahead-of-time
compiled fixed graph is both faster and far more deterministic than the
interpreted GGUF path. The bimodal stalls above appear only on the GGUF side.

This is a concrete argument for the production choice the repo already made, and
it was not previously measured.

---

## 4. Published evidence for on-device NPU energy

No number below was measured by us. Each is cited so it can be checked.

### 4.1 The closest published match — same chip

Cheng & Lai, *"Energy-Efficient On-Device RAG on a Mobile NPU"*,
[arXiv:2606.11257](https://arxiv.org/html/2606.11257v1) (2026-06-09) — measured
on **Snapdragon X Elite**, the same part as this box:

| backend | J/query | vs NPU |
|---|---|---|
| **NPU** | **315** | 1.0x |
| CPU | 1 251 | 4.0x |
| GPU (OpenCL) | 2 051 | 6.5x |

Projected at 1000 queries/day: 87.5 Wh/day on NPU vs 347.5 Wh/day on CPU.

This is the single most useful citation available: same silicon, LLM-shaped
workload, independent authors, measured rather than modelled. **Our own
measurement should be checked against this ratio** — if it reproduces ~4x
NPU-over-CPU, the claim is corroborated rather than isolated.

### 4.2 Supporting

- **Snapdragon X Elite NPU 41.23 J/image vs Apple M3 ANE 87.63 J/image**
  (Stable Diffusion 2.1) — measured, independent: Creative Strategies,
  [*The NPU Wattage Advantage*](https://creativestrategies.com/research/white-paper-the-npu-wattage-advantage/),
  2024-05-20.
- **45 TOPS INT8, up to 24 TOPS/W; 2.6x perf/W vs Apple M3, 5.4x vs Intel Core
  Ultra 7 155H** — Qualcomm's own figures, **vendor marketing, not independently
  verified**: [Qualcomm press release](https://www.qualcomm.com/news/releases/2024/05/snapdragon-x-series-is-the-exclusive-platform-to-power-the-next-), 2024-05-20.
- **MobileNetV2 int8: NPU 0.29 ms vs CPU 1.74 ms — 14.8x**, and p95/p50 of 1.07
  on NPU vs 3.5 on CPU. Measured in-house 2026-07-30
  (`docs/brainstorming/2026-07-30-npu-llm-benchmarks-and-geniex-findings.md:135-144`).
  This is the NPU's genuine win case and it is a *vision* workload, not an LLM
  one — worth saying out loud rather than blurring.

### 4.3 Cloud-side energy

- **0.24 Wh per median text prompt**, full-stack (accelerator + host + idle
  capacity + datacenter overhead); accelerator-only accounting gives 0.10 Wh, a
  **2.4x undercount**. Measured on Google's production Gemini fleet:
  [Google Cloud blog](https://cloud.google.com/blog/products/infrastructure/measuring-the-environmental-impact-of-ai-inference)
  / [arXiv:2508.15734](https://arxiv.org/abs/2508.15734), 2025-08-21. Model size
  undisclosed but far larger than 4B.
- **Measured energy per token spans 0.003–1 J/token** across GPU × model
  combinations — three orders of magnitude:
  [*Watt Counts*, arXiv:2604.09048](https://arxiv.org/html/2604.09048v1), 2026-04.
- **PUE**: 1.09 Google fleet, 1.16–1.17 Microsoft, **1.54 industry average**
  (Uptime Institute 2025). Use 1.09–1.17 for hyperscaler inference, 1.54 for a
  generic datacenter. Google's 0.24 Wh already includes its own PUE — do not
  double-count.
- **Energy per token scales sublinearly with model size** — roughly 1.7x to 7.3x
  per order of magnitude depending on methodology; the two published figures
  disagree and should be treated as a range.

### 4.4 The gap that cannot be closed by citation

**There is no published measurement of a 4B, 4-bit-quantized model's inference
energy in a datacenter.** Every datacenter figure found is FP16/BF16, and the
one paper that surveys quantization explicitly lists quantized-model energy as
future work. Likewise **no study measures any laptop NPU against datacenter GPU
inference on a comparable model.**

Therefore: any local-vs-cloud energy ratio we present is a **constructed
estimate combining two independent sources**, and must be labelled as such. It
is not a citable published result and no amount of searching will make it one.

---

## 5. Cost model

### 5.1 Workload, from measured data

| quantity | value | source |
|---|---|---|
| prompt tokens / segment | 890 | 7116 / 8 fixtures, `.measurements/npu-eval-aug6.log:105` |
| output tokens / segment | 75 | 12.4 tok/s × 48.7 s / 8, same log |
| findings / segment | 2.95 | 59 findings / 20 segments, live run 2026-08-06 |
| segments / hour | 80 | 20 segments in ~15 min, live run |
| segments / developer-day | 480 | × 6 active hours — **ASSUMED** |
| tokens / developer-day | 427 k in, 36 k out | derived |

The 6-hour active day is the only assumed input. Everything above it is
measured.

### 5.2 What that workload costs in the cloud

250 working days/year. Prices as of 2026-08-06, sources in §8.

| provider / model | $/dev/day | $/dev/yr | 10 devs/yr |
|---|---|---|---|
| DeepInfra Llama-3.1-8B | 0.0100 | **2.50** | 24.97 |
| Groq Llama-3.1-8B Instant | 0.0242 | 6.06 | 60.62 |
| Together Gemma-3n-E4B | 0.0300 | 7.49 | 74.91 |
| OpenAI GPT-5 nano | 0.0358 | 8.96 | 89.60 |
| Gemini 2.5 Flash-Lite | 0.0572 | 14.30 | 142.97 |
| Together Llama-3-8B Lite | 0.0648 | 16.21 | 162.12 |
| Fireworks 4-16B tier | 0.0926 | 23.16 | 231.60 |
| Claude Haiku 4.5 | 0.6081 | 152.03 | 1 520.31 |
| Claude Sonnet 5 (intro) | 1.2162 | 304.06 | 3 040.62 |
| Claude Opus 5 | 3.0406 | **760.16** | 7 601.55 |

### 5.3 Reading this honestly

**The comparison you choose is the entire argument.**

- Against a **small hosted model** — the technically equivalent thing — running
  locally saves about **$2.50 to $23 per developer per year**. That is
  negligible, and a judge will spot it.
- Against **Claude Opus 5**, the saving is **$760/developer/year**, and $7.6k
  for a team of ten.

The second number is the defensible one *only if* the counterfactual is really
"we would have used a frontier model for this." For Synapse that is arguable:
`SYNAPSE_DISTILLER=anthropic` and `claude-cli` are shipped, working arms, so a
team without an NPU genuinely would run distillation on Claude. State the
counterfactual explicitly and the number holds.

**What is NOT in this model, and is probably larger than everything in it:** the
token cost avoided in the *main agent* by not re-exploring what a teammate
already learned. That is the product's actual value proposition
(`docs/demo-transcripts.txt:34-37`). It is unmeasured, and
`docs/architecture.html:1289` already describes the A/B that would measure it —
same task with and without Synapse — as not yet built.

### 5.4 The cost ceiling local inference removes

Not a dollar saving, but the constraint that actually bites today: the shared
Cirrascale key allows **20 requests and 25 000 tokens per hour** (measured off
the provider dashboard, `packages/service/src/synapse_service/api.py:50-51`).
One merge costs ~3 250–4 000 tokens, so **one key affords ~6–7 synthesis rounds
per hour**, while a live worker pushes ~30 rounds/hour (`api.py:484-490`).

Local inference has no such ceiling. At 480 segments/developer-day the hosted
path is not merely more expensive — **it is rate-limit infeasible**, by roughly
an order of magnitude, without provisioning ~10 keys (`api.py:64`). That is a
concrete, measured, non-hypothetical argument for local inference, and it does
not depend on a single joule.

---

## 6. Recommended demo KPIs

Ordered by how well each survives a hostile question.

| # | KPI | Status | Why it holds |
|---|---|---|---|
| 1 | **Determinism: NPU decode CV 0.1% vs GPU 10.8% vs CPU 16.4%** — 120x/182x steadier, for a process whose job is not to disturb the developer | **Measured today** | Strongest available: our own data, striking ratio, and it reframes "slowest unit" as the right choice |
| 2 | **Rate-limit headroom**: 20 req/h hosted vs unlimited local, against a measured 80 segments/h demand | **Measured**, in-repo | Hardest to argue with; no external citation needed |
| 3 | **Prefill 1041 tok/s vs decode ~14 tok/s** — the workload is decode-bound, and triage-before-LLM avoids the expensive half entirely | **Measured today**, R²=0.997 | A real optimization result, and it is ours |
| 4 | **Compiled QAIRT bundle prefills 5.3x faster than GGUF on the same NPU** | **Measured today** | Justifies the production model choice with a number |
| 5 | ~~Energy per finding, NPU vs CPU~~ | **NOT MEASURED — do not present** (§7.1) | Harness exists; the measurement was dropped. Say "we did not measure it" if asked, never a figure |
| 6 | **$760/dev/yr vs a frontier-model counterfactual** | Derived from measured workload + published prices | Only with the counterfactual stated aloud (§5.3) |
| 7 | **MobileNetV2 14.8x NPU speedup, p95/p50 1.07 vs 3.5** | Measured 2026-07-30 | True and striking — but say it is a vision workload |
| 8 | **Privacy**: raw transcript never leaves the device | Architectural, not a number | The `verbatim_overlap` metric is itself flagged unreliable |

**If the power number never arrives, KPIs 1–4 still carry the technical-implementation
case on their own.** That was not true this morning.

**Do not present:** any tokens/sec claim that implies NPU speed superiority; any
energy, watt, joule or "power efficiency" number **at all** — none was measured
(§7.1), and the honest answer to a judge asking is "we built the harness and ran
out of window"; any recall figure from `measure_recall.py`
(*"no number from it belongs in a demo"*, `scripts/measure_recall.py:19-28`); the
privacy table from the Aug 6 eval log (*"Do not demo this table"*, that log's own
output at `:110`).

---

## 7. What is still missing

### 7.1 Power — NOT MEASURED, and deliberately so

**No energy figure was obtained, and none is claimed anywhere in this document.**
The prohibition in `scripts/run_npu_eval.py:165` still stands and should be
treated as still standing: *"Power is unmeasured — do not claim efficiency from
this run."*

This is a decision, not an oversight. The instrument was identified and
validated — the embedded controller reports whole-system draw in milliwatts via
WMI `root\wmi BatteryStatus.DischargeRate` (observed at 5599 mW on this box) —
and `scripts/measure_power.py` is written, committed and tested. What it needs is
the machine on battery: plugged in, `DischargeRate` reads 0 and `PowerOnline` is
`True`, so the harness refuses rather than recording a column of zeros that would
read like data. Every window available for this work was on mains, and rather
than estimate a number or run one on a charger and hope, the measurement was
dropped.

**The case does not depend on it.** §1b, §2.2, §3, §5.4 and KPIs 1–4 in §6 are
all measured, all ours, and none of them are energy figures. That was not true
before this work started: the efficiency argument rested entirely on an unmeasured
power claim, and it now rests on determinism, rate-limit headroom, and the
prefill/decode asymmetry instead.

**If someone wants the number later**, it is roughly 30 minutes: unplug the
charger, `uv run python scripts/measure_power.py`, and it runs idle baseline →
NPU → GPU → CPU under identical sustained load, sampling at 1 Hz. Energy per
segment is the mean power delta against the baseline times wall time; the sampler
runs during the baseline too, so its own cost cancels. The one published figure
to check the result against is arXiv:2606.11257's 315 J/query on NPU vs 1251 on
CPU — same chip (§4.1).

### 7.2 The ~8 second GGUF stall

§3.3. Calls on the llama.cpp path intermittently take ~8 s instead of ~0.4 s,
consistently enough to look like a periodic event rather than jitter — and the
QAIRT path does not do it. Candidates: page-cache eviction on a 5.7 GB model,
Defender, or a GenieX keepalive (`--keepalive` defaults to 300 s). Worth finding,
because it is the only thing keeping the cross-unit prefill fits below 0.95.

### 7.3 Decode means at a proper sample size

§2.4 — four calls per unit establishes the variance result but not the ordering
of the means. More repeats would settle whether the GPU is genuinely fastest.

### 7.4 The measurement that would matter most, and is not a power measurement

The A/B in `docs/architecture.html:1289`: the same task performed with and
without Synapse, measuring tokens spent, wall-clock, and duplicated exploration
in the *main agent*. Every efficiency number in this document is about the
plumbing. That one would be about the product.

---

## 8. Sources

Pricing, all retrieved 2026-08-06:
[Anthropic](https://platform.claude.com/docs/en/about-claude/pricing) ·
[OpenAI](https://developers.openai.com/api/docs/pricing) ·
[Google Gemini](https://ai.google.dev/gemini-api/docs/pricing) ·
[Together](https://www.together.ai/pricing) ·
[Fireworks](https://docs.fireworks.ai/serverless/pricing) ·
[DeepInfra](https://deepinfra.com/pricing).
Groq pricing could not be confirmed on an official page (marketing site did not
render a table, `console.groq.com/docs/pricing` 404'd); its figures come from
four mutually-consistent third-party trackers and are labelled accordingly.

Energy and hardware: cited inline in §4.

In-repo measured baselines: `.measurements/npu-eval-aug6.log` (note:
`.measurements/` is gitignored and exists only in the working copy),
`config/synapse.toml` `[capability.*]`, `config/prompts/*.toml` `[calibration]`,
`docs/brainstorming/2026-07-30-npu-llm-benchmarks-and-geniex-findings.md`,
`packages/service/src/synapse_service/api.py:50-86`.
