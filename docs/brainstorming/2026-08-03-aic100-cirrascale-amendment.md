# Amendment 2026-08-03: Provider layer — hosted Cirrascale AI-100, GenieX NPU runtime, model selection

**Status:** Adopted 2026-08-03 (hackathon Day 1)
**Amends:** `2026-07-25-synapse-design.md` §3/§4/§6/§9/§12 · Plan B Tasks 4–6 · Plan C Tasks 2–3
**Verified:** Cirrascale endpoint probed live 2026-08-03 (ping, chat, embeddings, structured-output); GenieX server mode confirmed from github.com/qualcomm/geniex; model catalog from aihub.qualcomm.com (GenieX-runtime filter)
**Superseded in part:** `2026-07-30-npu-llm-benchmarks-and-geniex-findings.md` (measured on hardware, newer evidence despite the earlier date) overrides Parts 2–4 where they disagree — `qairt` blocked by AI Hub 503 (`llama_cpp` is the validated path), NPU slowest for LLM decode (rationale is contention/power, not speed), Gemma-4-E4B `vlm` prompt-drop bug, GBNF flags found.

The hackathon materials resolved both hardware unknowns in the design — and both landed on *hosted/turnkey* rather than *DIY bring-up*. Net effect: **both Day-1–2 hardware spikes shrink, every non-Claude provider now speaks OpenAI-compatible HTTP, and the freed time goes to Synthesis + MCP + eval.**

---

## Part 1 — AI-100 = hosted Cirrascale Inference Cloud

Cloud AI 100 is provided as a **hosted service** (Cirrascale Inference Cloud, Cloud AI 100 Ultra) — not a box we provision. `AIC100Provider` wraps this HTTPS API. **No QEfficient/vLLM-`qaic` bring-up, no AWS DL2q fallback, no "AI-100 box."** We use the raw HTTP API, not the Imagine SDK (SDK = hand-downloaded wheel → install friction that costs Deployment & Accessibility points; its value is LangChain/CrewAI wrappers we don't use).

### Verified service facts (2026-08-03)

| Fact | Value |
|---|---|
| Base URL | `https://aisuite.cirrascale.com/apis/v2` (config: `INFERENCE_CLOUD_BASE_URL`) |
| Auth | `Authorization: Bearer <key>` (config: `INFERENCE_CLOUD_API_KEY`; key lives in gitignored `api-1.json` / `.env`, never in the repo) |
| LLMs on our key | **`Llama-3.1-8B` only** (no 70B; ask at office hours whether it can be enabled — if so it's a config-line change) |
| Embeddings | `BAAI/bge-large-en-v1.5` (1024-dim, verified) + `bge-base` |
| Other | reranker endpoint (BAAI/bge-reranker), sdxl-turbo, transcribe/translate |
| Endpoints | `POST {base}/chat/completions`, `{base}/completions`, `{base}/embeddings`, `{base}/ping`, `{base}/models` — OpenAI-*shaped* responses |
| Latency | ~0.7 s short chat completion (dev Mac, home network) |

### Behavioral gotchas (probed, not assumed)

1. **`response_format: json_schema` is silently ignored** — request succeeds, free prose comes back. → `native_structured_output=False`.
2. **`/chat/completions` eats JSON output.** Any prompt that leads the model to emit a `{...}` object (plain, fenced, or sentinel-prefixed — all three probed) triggers the server's tool-call parser: `content: ""` plus an **empty** `tool_calls` entry. Tolerant parse never sees the text — the failure is upstream of us.
3. **Workaround (verified): `POST {base}/completions`** (raw completion, no chat template) returns prompt-instructed JSON inline in `.choices[0].text`; a tolerant extractor (first balanced `{...}` block) recovers it.

### `AIC100Provider` changes (Plan C Task 2)

Remains a subclass of `OpenAICompatibleProvider`, but overrides more than `base_url`:

- `base_url="https://aisuite.cirrascale.com/apis/v2"`, `model="Llama-3.1-8B"`, `api_key` from `INFERENCE_CLOUD_API_KEY`.
- `default_capabilities = ProviderCapabilities(native_structured_output=False, streaming=False)`.
- `complete(...)` without schema → `POST /chat/completions` unchanged.
- `complete(...)` **with** schema → flatten messages to one prompt (system + turns concatenated) + "return JSON of this shape" instruction → `POST /completions` → extract first balanced JSON object from `.text` → one retry on parse failure. Never send `response_format`; never expect JSON to survive the chat endpoint.
- `max_tokens` explicitly bounded on every call — the credit pool is shared across the team.
- Plan C Task 2's `test_aic100_provider_uses_json_schema_response_format` is **obsolete as written**; replace with tests for the completions-endpoint path + JSON extraction.

---

## Part 2 — NPU runtime = GenieX (`geniex serve`)

Design §3 assumed DIY bring-up (ONNX Runtime GenAI + QNN EP, or llama.cpp QNN-HTP). **GenieX replaces that**: Qualcomm's on-device GenAI runtime with two backends — `qairt` (NPU-exclusive, pre-compiled per-chipset AI Hub bundles) and `llama_cpp` (NPU/GPU/CPU, generic GGUF) — and, decisively, a server mode:

> `geniex serve` → OpenAI-compatible API at `http://127.0.0.1:18181/v1`

### `NPUProvider` changes (Plan B Task 5)

- `NPUProvider` becomes a **third thin subclass of `OpenAICompatibleProvider`** pointed at `localhost:18181/v1`. The Task-5 plan to wrap `onnxruntime-genai` Python bindings is **retired** — no custom client, no optional dep, no chat-template hand-rolling (`_render_chat` gone).
- Ollama / Cirrascale / GenieX now all share the one HTTP adapter; only Claude keeps a custom adapter.
- `native_structured_output=False` until measured — GenieX docs don't mention constrained output; the llama.cpp backend *may* support GBNF/json_schema grammars. **10-minute probe in the spike**, then set the flag from evidence.

### NPU spike changes (Plan B Task 4)

Same go/no-go axes (NPU residency, prefill/generate tok/s, power, schema-valid JSON rate), new tool order:

1. `geniex serve` + `qairt` bundle from AI Hub → point `NPUProvider` at localhost, run the fixture corpus.
2. If `qairt` is finicky → GenieX `llama_cpp` backend (GGUF), same server, same provider.
3. Final fallback unchanged: `SYNAPSE_DISTILLER_MODE=ollama` on the Mac.

ONNX Runtime GenAI + QNN EP drops out of the plan entirely (kept in history as the pre-GenieX path).

---

## Part 3 — Model selection (Qualcomm-optimized only)

**Synthesizer (AI-100):** `Llama-3.1-8B` — the only LLM on our key. Design synthesis prompts for 8B: tight working-memory bound (~500 words, already in spec), simple instructions, tolerant-parse path assumed.

**Distiller (NPU):** AI Hub's GenieX-runtime catalog (`geniex_qairt,geniex_llamacpp`, compute/X-Elite class) has 12 models. Excluding vision models and GPT-OSS-20B (MoE, overkill for an always-on background distiller), the candidates:

| Model | Size | Role in the spike |
|---|---|---|
| **Qwen3-4B-Instruct-2507** | 4B | Primary — recent instruct refresh, strong structured output for its class |
| **Gemma-4-E4B-it** | ~4B | Least-friction alternate — the GenieX repo's documented example (`qat-q4_0` GGUF) |
| **Ministral-3-3B-Instruct-2512** | 3B | Matches the design's original 3B sizing |
| **Qwen3-1.7B** | 1.7B | Power/speed floor if 4B causes thermal/memory pressure while the developer works |
| Qwen3-8B | 8B | Quality ceiling, only if 4B distillation quality disappoints |

**The design doc's Phi-3.5-mini / Llama-3.2-3B assumption is stale — neither is in the GenieX catalog.** Selection is decided by the Plan B eval harness, not on paper: run the fixture corpus through the top 3 candidates, table schema-valid rate × prefill tok/s × power, pick the winner. The table itself is demo material (Technical Implementation, 40 pts).

---

## Part 4 — QUAD (tooling, not runtime)

QUAD never appears in the Synapse dataflow. Uses:

1. `/quad-detect` + `/quad-doctor` on the X Elite laptop before the spike — chipset/NPU/SDK sanity.
2. `/quad-aihub` — score the distiller candidates against the exact chipset.
3. **Profiling for the benchmark story:** `profile_workload`/`profile-device` give latency, power, memory, per-op HTP cycle counts on real silicon — hard numbers for the Technical Implementation criteria next to the app-level eval table. Caveat: QUAD's convert/profile flow targets ONNX→QNN artifacts; LLM-under-GenieX may not fit `profile_workload` cleanly. Use where it fits (NPU-utilization/power while GenieX runs still supports the energy story); don't contort the pipeline for it.

We do **not** use QUAD `convert_model`/`generate_code` for the distiller — that's the DIY path GenieX eliminates, and its generated runners live outside our provider abstraction.

---

## Plan impact summary

| Plan item | Before | Now |
|---|---|---|
| Plan C Task 3 (AI-100 spike, Day 1–2, DL2q fallback) | provision box, serve model, curl | **Retired — GO on Day 1.** Endpoint verified end-to-end; residual work = completions-path tests in Task 2. |
| Plan B Task 4 (NPU spike) | ONNX-QNN bring-up, riskiest spike | GenieX-first (server mode), candidate-model bake-off; same go/no-go axes, much smaller surface |
| Plan B Task 5 (`NPUProvider`) | custom onnxruntime-genai wrapper | thin `OpenAICompatibleProvider` subclass @ `localhost:18181/v1` |
| Plan B Task 6 (eval) | cost row "aic100 self-hosted $/hour" | aic100 cost = shared credit pool (per-token usage from `ModelResult.usage`) |
| Deployment (§4 "service on AI-100 box") | ingest+MCP co-located with accelerator | service runs on **any laptop**; synthesis calls Cirrascale over HTTPS |
| On/off-target modes (§6) | `aic100` only on-target | `aic100` reachable from anywhere → on-target synthesis from Day 1; Ollama stays the offline/dev-loop synthesizer (credit + latency hygiene); Claude stays quality baseline/judge |
| Retrieval stretch | vector RAG "out", no infra | bge-large + reranker on same key → stretch is cheap if LLM-as-retriever underperforms; still out of core scope |
| Risks (§12) | AI-100 provisioning; NPU bring-up | shared credit pool (bound `max_tokens`, dev on Ollama) · unknown rate limits · single-8B synthesis quality · chat-endpoint JSON misparse (routed around) · **venue-WiFi dependency for the demo** (GenieX + Ollama configs are the offline fallback) |

## Demo-day checklist additions

- [ ] `{base}/ping` from the venue network (HaQathon) on the demo machine.
- [ ] `models` still lists `Llama-3.1-8B`; credits remaining sane.
- [ ] Fallback config ready: `synthesizer: ollama` one env var away.
- [ ] `geniex serve` starts cleanly on the X Elite; distiller model bundle cached locally (no venue-WiFi dependency on the edge path).
