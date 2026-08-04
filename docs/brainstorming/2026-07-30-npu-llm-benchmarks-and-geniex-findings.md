# Amendment: NPU LLM benchmarks, GenieX findings, and the distiller-placement decision

**Status:** Evidence gathered 2026-07-30 on the X Elite laptop (measured, not projected)
**Amends:** `2026-08-03-aic100-cirrascale-amendment.md` Part 2 / Part 3 / Part 4 · `2026-07-25-synapse-design.md` §3 · Plan B Tasks 4–6
**Verified on:** Snapdragon X Elite X1E80100 · Hexagon NPU v73 · Adreno X1-85 · 31.6 GB RAM · Windows 11 Pro · GenieX CLI v0.3.18 (QAIRT v2.45.0.260326, LlamaCPP hash 6ba5ef2)

> **Note on the date:** this file is dated with the real date the measurements were taken (2026-07-30). It is *newer* evidence than the `2026-08-03` amendment despite sorting before it alphabetically — that doc's dates are hackathon-day labels. Read this one as superseding it where they disagree.

---

## TL;DR — four things that change the plan

1. **The NPU is the *slowest* compute unit for LLM/SLM token generation** on the `llama_cpp` backend — measured across three model sizes. CPU wins at ≥4B, GPU wins at ≤2B. This challenges §3's premise as *stated* (though not necessarily the *decision* — see Part 4).
2. **`Gemma-4-E4B-it` — our listed "least-friction alternate" — silently drops the prompt** and emits confident, fluent, completely unrelated text. One-command fix found. **This is the single most dangerous failure mode for a distiller** (Part 2).
3. **GenieX server mode is verified live and OpenAI-compatible.** `NPUProvider` as a thin `OpenAICompatibleProvider` subclass is confirmed viable — Plan B Task 5 is de-risked.
4. **Plan B Task 4 step 1 (`qairt` bundle from AI Hub) is currently blocked** — AI Hub returns HTTP 503, and `/quad-aihub` (Part 4 use #2) is broken server-side. Step 2 (`llama_cpp` GGUF) is the only working path today, and it is already validated.

---

## Part 1 — GenieX verified facts

Installed at `C:\Users\<user>\AppData\Local\GenieX CLI\geniex.exe`. (The `geniex-cli.exe` in Downloads is only the installer.)

### CLI surface

```
Model:      pull · remove · clean · list · model set-type
Inference:  infer · serve · run
Management: config · version · update
```

| Flag | Note |
|---|---|
| `-c, --compute` | `cpu \| gpu \| npu \| hybrid` — **default `npu`** |
| `-n, --ngl` | layers offloaded, `-1` = all (llama_cpp only) |
| `--nctx` | context window, default 4096 (llama_cpp only) |
| `--think` | **defaults to ON** — spends tokens reasoning before answering |
| `-s / -i / -p` | system prompt / prompt file / inline prompt |
| `--enable-json`, `--grammar-path`, `--grammar-string` | **see Part 5** |
| `--spec-type`, `--draft-model` | speculative decoding (llama_cpp only) |

### Server mode — confirmed OpenAI-compatible

```powershell
geniex serve -c npu     # 127.0.0.1:18181, --keepalive 300 default
```

Verified live: `GET /v1/models` returns the cached-model list; `POST /v1/chat/completions` returns a full OpenAI-shaped response (`choices[].message.content`, `usage`, `finish_reason`). Server was ready **~1 s** after launch.

**→ Plan B Task 5 confirmed.** `NPUProvider` = `OpenAICompatibleProvider(base_url="http://127.0.0.1:18181/v1")`. No custom client, no `onnxruntime-genai` bindings, no hand-rolled chat template. Standard `openai` Python client works unchanged.

### Two backends are physically present

| Backend | Contents | Meaning |
|---|---|---|
| `llama_cpp/` | `ggml-hexagon.dll`, `libggml-htp-v73/v75/v79/v81.so`, `ggml-opencl.dll` | Generic GGUF. **`htp-v73` matches our chip exactly.** OpenCL = Adreno path. |
| `qairt/` | `QnnHtp.dll`, `QnnHtpV73Stub.dll`, `geniex_vlm.dll`, `geniex-proc-vision.dll` | AI Hub pre-compiled bundles, NPU-exclusive |

Every model we tested resolved to `PluginId: llama_cpp`. **We have not yet exercised the `qairt` path** — it needs an AI Hub Genie bundle, and AI Hub is down (Part 6).

### Model hubs

`geniex pull <name> --model-hub aihub|hf|docker|localfs --model-type llm|vlm`

Docker Hub works well and is fast: `ai/smollm2` (271 MB) and `ai/qwen2.5` (4.7 GB) both pulled at 24–32 MB/s with no auth. Useful **venue-WiFi-independent** fallback source if AI Hub stays down.

---

## Part 2 — ⚠️ CRITICAL: the `vlm` prompt-drop bug

**`google/gemma-4-E4B-it-qat-q4_0-gguf` — the model Part 3 lists as our "least-friction alternate" — ignores the prompt entirely.**

### Symptom

It loads fine, reports NPU residency, streams at 14 tok/s, and produces *fluent, well-formatted, confident English that answers a completely different question every time.* Four prompts produced: a fantasy short story, a 2023–24 marketing trend report, a text-adventure game, and a quantum-computing quiz. None related to the input.

### Proof it is a bug, not prompt-formatting

The server's own `usage` block:

```json
"usage": { "prompt_tokens": 1, "completion_tokens": 434 }
```

A 13-token prompt became **1 token** — the stray `.` printed after `encoding...`. Reproduced identically via `infer` **and** the HTTP API, on **both** CPU and NPU. So it is not the NPU, not the transport, and not our prompt.

### Root cause and fix

The model's manifest had `ModelType: "vlm"` (it ships an `mmproj.gguf`). The VLM code path discards plain-text prompts. A text-only model (`ai/smollm2`, `ModelType: llm`) conditioned correctly on the identical prompt via the identical call.

```powershell
geniex model set-type google/gemma-4-E4B-it-qat-q4_0-gguf llm
```

After this, the same prompt returns `"The capital of France is Paris."` — 7 tokens, correct. **No re-download of the 5.7 GB required.**

### Why this matters more for Synapse than for most projects

Our distiller's entire job is reading agent-session context and emitting structured findings. Under this bug it would emit **confident, well-formed, schema-plausible findings invented from nothing** — and stream them into the *shared team memory* that other engineers' agents then build on. Silent knowledge poisoning, with no error, no exception, and output that passes casual review.

**Mandatory mitigations (add to Plan B Task 4 and the provider layer):**

1. **Assert `usage.prompt_tokens > 1` on every distiller call.** Cheapest possible guard against this whole class of failure. Log and hard-fail the finding if it trips.
2. **Add a canary fixture** to the eval harness: a prompt with one unambiguous extractable fact. If the answer doesn't contain it, fail the model before it reaches the corpus — don't score it.
3. **Verify `ModelType` after every `pull`.** Any candidate shipping an `mmproj` file is at risk. Prefer text-only GGUFs for the distiller regardless.
4. Report upstream — `prompt_tokens: 1` is an unambiguous repro for a Developer-Preview tool.

---

## Part 3 — Measured throughput: the NPU is last for decode

Seed-pinned (`--seed 42`), 100–120 tokens, identical prompt, same session.

| Model | Params | GPU | CPU | NPU | hybrid |
|---|---|---|---|---|---|
| `ai/smollm2` | 1.7B | **94.7** | 58.0 | 33.0 | — |
| `gemma-4-E4B-it-qat-q4_0` | ~4B eff | 17.6 | **21.3** | 14.2 | 15.9 |
| `ai/qwen2.5` | 7B | 13.3 | **16.8** | 12.0 | — |

*(tok/s; first-token latency 0.1–0.6 s everywhere except `hybrid` at **3.0 s**)*

### Findings

- **The NPU is slowest in all three rows.** At 1.7B it is 2.9× slower than GPU; at 7B it is ~1.4× slower than CPU.
- **`hybrid` is a trap** — slowest or near-slowest throughput *and* 5–15× worse first-token latency. Do not use.
- **GPU scales inversely with size**: dominant at 1.7B (94.7), loses to CPU by 4B. Consistent with Adreno being bandwidth-favourable for small weight sets.
- **Gemma-4-E4B beats Qwen2.5-7B on CPU** (21.3 vs 16.8) — the MatFormer/E4B effective-parameter design is genuinely cheaper per token.

### Why

LLM decode is autoregressive and single-token: it streams gigabytes of weights per token and is **memory-bandwidth-bound**, not compute-bound. The Hexagon's 45 TOPS has nothing to bite on. Additionally, Gemma's E4B architecture (per-layer embeddings, altup/laurel blocks) almost certainly does not map cleanly onto HTP kernels — and `ggml-hexagon` is a young backend.

### Contrast — the NPU absolutely does win, on the right shape

Measured the same day on the same chip, MobileNetV2 (batch-1 224×224, 100 iters after 20 warmup):

| Runtime | Mean | p50 | p95 | Throughput |
|---|---|---|---|---|
| **NPU int8** | **0.29 ms** | 0.28 | 0.30 | 3468 FPS |
| NPU fp32 | 0.60 ms | 0.58 | 0.72 | 1662 FPS |
| CPU int8 | 1.74 ms | 1.13 | 3.94 | 574 FPS |
| CPU fp32 | 4.28 ms | 3.58 | 7.98 | 234 FPS |

**14.8× NPU speedup**, and far better tail behaviour (p95/p50 = 1.07 on NPU vs 3.5 on CPU). Dense batched convolution is compute-bound → the NPU wins decisively.

**The lesson is workload shape, not silicon.** "Put it on the NPU" is not a strategy; measuring per workload is.

---

## Part 4 — What this means for distiller placement

§3 and the README justify the NPU as: *"makes always-on observation viable without stealing CPU cycles from the developer's work."*

**That rationale is about CPU contention and power, not raw throughput — and it survives this data.** But the *stated* framing (NPU as the fast path) does not. Both readings need separating, because they lead to different demos.

| Option | Throughput | Argument |
|---|---|---|
| **Distiller on NPU** | Slowest (14 tok/s @ 4B) | Leaves Oryon CPU free for the developer's actual compile/test loop; expected lowest power for always-on. **The original design intent.** |
| **Distiller on CPU** | Fastest (21.3 tok/s @ 4B) | Best latency — but contends with the very workload we're observing. Self-defeating for always-on. |
| **Distiller on GPU** | Best ≤2B (94.7 tok/s) | Frees CPU *and* is fast. Contends with display/compositor; unmeasured for sustained load. **Underexplored — worth a spike.** |

### Honest gap: we have not measured power

Every number above is latency/throughput. The energy-efficiency half of the NPU argument — and of the judging criteria — is **unmeasured**. Until we have watts, "NPU for efficiency" is a reasonable hypothesis, not a finding. Do not put it in the demo narrative as fact.

### Recommendation

1. **Keep the NPU as the distiller target** — the contention/power argument is the real one and is unaffected by throughput ranking.
2. **Restate the rationale** in README/§3: *"the NPU runs always-on distillation off the critical path, leaving CPU and GPU free for the developer"* — not *"the NPU makes it fast."* Defensible under judging questions; the current wording is not.
3. **Add GPU as a measured third arm** in the Task-4 bake-off. At 1.7B it is 2.9× the NPU, and `Qwen3-1.7B` is already our power/speed-floor candidate.
4. **Measure power** before claiming efficiency. This is the one number that decides the argument.
5. **Sizing:** at 4B expect ~14 tok/s on NPU. For an always-on background distiller that is likely fine (findings are not user-facing in real time) — but it must be stated as a design budget, not discovered during integration.

---

## Part 5 — Structured output: evidence found (resolves an open question)

Part 2 of the prior amendment left `native_structured_output=False` *"until measured — the llama.cpp backend may support GBNF/json_schema grammars. 10-minute probe in the spike."*

**The CLI exposes exactly those flags:**

```
--enable-json                enable json output
--grammar-path string        path to grammar file
--grammar-string string      grammar in string format
```

This is strong evidence the `llama_cpp` backend supports **GBNF-constrained generation** — i.e. schema-valid JSON can be *guaranteed at the sampler* rather than hoped for and tolerant-parsed. That would be a material quality win for the distiller and removes a whole retry path.

**Not yet probed:** whether these flags are plumbed through `geniex serve`'s HTTP API (e.g. via `response_format` or an extension field) or are CLI-only. **This is now a concrete, narrow spike** — if the server exposes grammars, `NPUProvider` can set `native_structured_output=True`, which is a real differentiator versus the AI-100 path (where `json_schema` is silently ignored and JSON must be smuggled through `/completions`).

Worth noting the asymmetry: **the edge provider may end up with *better* structured-output guarantees than the cloud synthesizer.**

---

## Part 6 — QUAD / AI Hub status: what is blocked right now

Verified live 2026-07-30.

| Component | Status |
|---|---|
| QUAD MCP server (`quad.infra.foundries.io/mcp`) | ✅ **Up** — handshake OK, v3.4.4, ~850 ms |
| `hardware_detect`, `convert_model`, `profile_workload`, … | ✅ Registered and callable |
| **`workbench.aihub.qualcomm.com`** (compile/quantize jobs) | ❌ **HTTP 503** |
| **`aihub_select` / `/quad-aihub`** | ❌ Server missing `qai_hub_models` package |
| **Server-side `qairt-converter`** (`aihub="never"`) | ❌ Broken: `ImportError: libpython3.10.so.1.0` |
| ONNX Model Zoo · HuggingFace · Docker Hub | ✅ HTTP 200 |

### Direct plan impacts

- **Plan B Task 4 step 1** (`qairt` bundle from AI Hub) — **blocked**, not merely finicky. Go straight to step 2 (`llama_cpp` GGUF), which is validated.
- **Part 4 use #2** (`/quad-aihub` to score distiller candidates against the chipset) — **unavailable**. Score candidates with our own Plan B eval harness instead; it was going to decide selection anyway (Part 3 already says selection is by harness, not on paper). No real loss.
- **Part 4 use #3** (profiling for the benchmark story) — `profile_workload` still works, but note the server is an **AMD EPYC cloud VM with `available_runtimes: ["cpu"]`** — no Adreno, no real Hexagon. **It cannot profile our NPU.** All NPU numbers must be measured locally, as the tables above were. The prior amendment's caveat here was correct but understated: it is not that LLM-under-GenieX "may not fit" — it is that the server has no NPU at all.
- **Distiller model bundles must come from Docker Hub / HF**, not AI Hub, until 503 clears. The demo-day checklist item *"distiller model bundle cached locally"* is therefore **more urgent** — cache now, from a source that works.

---

## Part 7 — Reusable environment gotchas

Cost real time this session; all verified.

| Gotcha | Detail |
|---|---|
| **Two Python interpreters** | The repo `.venv` is **emulated x86-64** under Prism (`machine()` → `AMD64` on ARM hardware). NPU wheels are win-arm64-only and cannot install there. A separate native-arm64 venv is mandatory for anything touching the NPU. |
| **QNN EP is a plugin (ORT 2.x)** | It does **not** appear in `get_available_providers()`. `providers=[("QNNExecutionProvider", {...})]` **silently runs on CPU.** Must `register_execution_provider_library()` + `get_ep_devices()` + `SessionOptions.add_provider_for_devices()`, then **assert** `"QNNExecutionProvider" in sess.get_providers()`. |
| **`qai-hub-models` cannot install on win-arm64** | Pulls `torch`, which has no `win_arm64` wheel. Genie bundle *builds* need WSL/x86/Linux; the X Elite is run-host only. WSL2 works here but has **no general-purpose distro** — needs `wsl --install -d Ubuntu`. |
| **GitHub LFS** | `raw.githubusercontent.com` returns a **130-byte pointer stub** for LFS files. Use `media.githubusercontent.com/media/...`. This silently broke QUAD's `github` model source. |
| **Opset ≥13 for per-channel QDQ** | Per-channel quantization emits `DequantizeLinear` with an `axis` attribute that does not exist in opset 12 → `INVALID_GRAPH`. Upgrade with `version_converter.convert_version(m, 17)` before quantizing. |
| **Calibration data dominates INT8 quality** | Calibrating from a synthetic placeholder image collapsed confidence 67% → 29%. INT8 accuracy is a *data* problem, not a quantizer problem. Use 50–200 real domain images. |
| **Adreno via QNN EP is not usable** | QNN GPU backend rejects ORT's NHWC-transformed `Conv` (`com.ms.internal.nhwc`). GPU *is* reachable for LLMs via GenieX's OpenCL path — different stack, different answer. |

---

## Plan impact summary

| Plan item | Before | Now |
|---|---|---|
| Plan B Task 4 step 1 (`qairt` from AI Hub) | primary path | **Blocked (AI Hub 503).** Start at step 2 (`llama_cpp` GGUF), already validated. |
| Plan B Task 4 axes | residency, tok/s, power, JSON rate | **Add GPU arm** + **add prompt-token canary**. tok/s partly pre-measured (Part 3). Power still unmeasured. |
| Plan B Task 5 (`NPUProvider`) | thin subclass, unverified | ✅ **Verified live** — `/v1/models` + `/v1/chat/completions` OpenAI-shaped at `:18181`. |
| Plan B Task 6 (eval) | schema-valid rate via tolerant parse | Probe `--enable-json` / GBNF over HTTP first — may make schema-validity *guaranteed* on the edge path. |
| Part 3 candidate `Gemma-4-E4B-it` | "least-friction alternate" | ⚠️ **Ships as `vlm` and drops prompts.** Usable only after `model set-type llm`. |
| Part 4 use #2 (`/quad-aihub` scoring) | score candidates vs chipset | **Unavailable** — use Plan B harness (as Part 3 already intended). |
| Part 4 use #3 (QUAD profiling) | "may not fit LLM" | **Server has no NPU** (`runtimes: ["cpu"]`). All NPU numbers measured locally. |
| README / §3 NPU rationale | "makes always-on viable" (reads as speed) | **Restate as contention/power**, not throughput — NPU is slowest for decode. |
| Distiller model source | AI Hub | **Docker Hub / HF** while 503 persists. Cache before demo day. |

## Immediate next actions

- [ ] `geniex model set-type <model> llm` for any candidate shipping an `mmproj`; verify `ModelType` after every pull.
- [ ] Add `assert usage.prompt_tokens > 1` to the provider layer + a canary fixture to the eval harness.
- [ ] Probe whether `--enable-json` / GBNF is exposed through `geniex serve`'s HTTP API (narrow, high-value).
- [ ] Add a **GPU** arm to the Task-4 bake-off, especially at 1.7B.
- [ ] **Measure power** for NPU vs CPU vs GPU distillation — the one number the efficiency argument rests on.
- [ ] Restate the §3 / README NPU rationale as contention + power.
- [ ] Cache distiller candidates locally from Docker Hub / HF now (venue-WiFi + AI Hub 503 hedge).
- [ ] Re-check AI Hub 503 periodically; if it clears, the `qairt` NPU-exclusive path becomes testable and may change the Part 3 ranking entirely.

## Open questions

1. Does the `qairt` backend beat `llama_cpp` on NPU by enough to reverse Part 3's ranking? **Unknowable until AI Hub recovers** — this is the single biggest open variable in the edge story.
2. Is GBNF exposed over HTTP, or CLI-only?
3. What is the actual power draw per compute unit under sustained distillation?
4. Does sustained GPU distillation degrade the developer's UI responsiveness?
5. Is the `vlm` prompt-drop fixed in a GenieX build newer than v0.3.18? (`geniex update` not run — would change tooling mid-hackathon.)

---

## Reproduction

All measurements are reproducible from `C:\Users\<user>\Downloads\QUAD\QUAD-Client-main\work_mbv2\`:

| File | Purpose |
|---|---|
| `make_static.py` | Pin dynamic batch dims to 1 |
| `quantize_int8.py` | Local INT8 QDQ quantization (AI-Hub-independent) |
| `bench_runtimes.py` | CPU/NPU/GPU ONNX comparison with fallback detection |
| `npu_infer.py` | Reusable `NPUSession` — correct plugin-EP registration |
| `run.ps1` / `requirements.txt` | Arch-aware bootstrap (detects ARM64, recreates wrong-arch venv) |

GenieX numbers: `geniex infer <model> -c <cpu|gpu|npu|hybrid> --max-tokens 100 --seed 42 -p "<prompt>"`.
