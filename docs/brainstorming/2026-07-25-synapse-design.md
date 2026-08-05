# Synapse — Design Spec

**Status:** Approved design, pre-implementation
**Date:** 2026-07-25
**Amended:** 2026-08-03 — provider layer (hosted Cirrascale AI-100, GenieX NPU runtime, model selection); see `2026-08-03-aic100-cirrascale-amendment.md`. Amended sections marked ⟨A⟩.
**Amended:** 2026-08-03 — hybrid frontier/local strategy (Part 1: design-time frontier leverage, local-only runtime; Part 2: contribute module, deferred); see `2026-08-03-hybrid-frontier-local-amendment.md`. Amended sections marked ⟨B⟩.
**Amended:** 2026-08-03 — segment token budget + compaction + 4B quality calibration; see `2026-08-03-segment-budget-compaction-amendment.md`. Amended sections marked ⟨C⟩.
**Amended:** 2026-08-03 — agent auto-detection, pre-recorded A/B demo, per-model segment budget, shared-memory storage seam; see `2026-08-03-agent-detection-demo-storage-amendment.md`. Amended sections marked ⟨D⟩.
**Amended:** 2026-08-03 — measured NPU/GenieX evidence folded in (benchmarks taken 2026-07-30, superseding ⟨A⟩ where they disagree); see `2026-07-30-npu-llm-benchmarks-and-geniex-findings.md`. Amended sections marked ⟨E⟩.
**Amended:** 2026-08-03 — domain-model + architecture revision: **local Orchestrator** (§4 rewritten; the old "MCP transport is remote HTTP/SSE" is retired), Attribution, Finding identity, semantic merge with tombstones, two-store Shared Memory, write-ahead durability. See `2026-08-03-local-orchestrator-domain-model-amendment.md`, `/CONTEXT.md`, and `docs/adr/`. Amended sections marked ⟨F⟩.
**Amended:** 2026-08-04 — first implementation. The on-device distiller **compresses; it does not judge** (`docs/adr/0003`), so a **triage** stage joins the edge pipeline; `qairt` is live and `usable_context = 4096` measured; `native_structured_output = False` proved by control probe; AI-100 is entitled to 70B/32B but capacity-blocked. See `docs/2026-08-04-implementation-report.md`. Amended sections marked ⟨G⟩.
**Event:** Snapdragon Multiverse internal hackathon (build week Aug 3–7 2026; prep week ~Jul 27–31)
**Team:** Siddsing (architect/integration), Akhil (edge worker / low-level), Aditya (ML/distillation, owns NPU laptop)

---

## 1. Problem

Every engineer now works alongside an AI coding agent, but each agent is blind to the team. When several people work toward the same goal, their agents repeat the same explorations and duplicate hours of work. Teams lack a passive listener — running on the Copilot+ PC each member already has — that quietly captures what every agent learns and turns it into shared knowledge.

## 2. Solution

Synapse observes any coding agent's session **unmodified** (by reading the session-transcript JSONL the agent already writes to disk) and turns isolated agents into shared team intelligence.

A teammate creates an opt-in shared session from their Copilot+ PC, declaring its purpose; teammates join with one command. Each member holds a different slice of context for the same task. A per-user small model distills each agent's activity into structured findings — learnings, decisions, dead ends, open questions — each tagged with contributor and time. **Raw work stays on device; only distilled findings leave the machine.** A large model on Cloud AI 100 merges everyone's findings into one shared working memory, flags conflicts, and organizes against the session's purpose. Agents retrieve on demand through MCP (pull-only, natural-language queries, ranked results).

**Division of labor (the "Why Qualcomm" thesis) ⟨E⟩:** edge distillation on the Snapdragon X Elite NPU — always-on and **off the critical path**, leaving CPU and GPU free for the developer's own compile/test loop (power efficiency expected but **not yet measured**; measured decode throughput says the NPU is *not* the fast path — the case is contention + power, never speed) + cloud synthesis on Cloud AI 100 (sustained low-cost multi-user serving).

**Demo vehicles ⟨D⟩:** Claude Code (capture verified) **and** OpenAI Codex are the demo pair. The worker **auto-detects** which agent is active from a registry of known transcript roots and selects the matching `Source` adapter — no per-agent workflow exists anywhere downstream of the adapter. Design is agent-agnostic by construction (new agent = one registry entry + one Source, nothing else changes); the hackathon scopes it to these two agents, the solution applies to any. The demo itself is **pre-recorded A/B**: the same task run without Synapse and with it, side-by-side, with measured deltas (wall-clock, tokens, duplicated exploration) — a live run is the encore, not the dependency.

## 3. Verified assumptions (as of 2026-07-25)

- **Passive capture is real.** Claude Code writes `~/.claude/projects/<slug>/<uuid>.jsonl`, one JSON event per line. A real 5,579-line session confirmed: events carry `type` (user/assistant/system), `timestamp`, `cwd`, `gitBranch`, `sessionId`, `uuid`/`parentUuid`; `message.content` is a list of typed parts (`thinking` / `text` / `tool_use` / `tool_result`); 1,212 tool calls in that session. The "what the agent learned / tried / hit a wall on" signal is directly present. Capture = tailing these files, zero agent modification. This corpus is also the eval fixture + TDD corpus.
- **On-device LLM on X Elite NPU is turnkey — via GenieX ⟨A⟩:** `geniex serve` exposes an OpenAI-compatible API at `localhost:18181/v1` with `qairt` (NPU-exclusive, pre-compiled AI Hub bundles) and `llama_cpp` backends. Distiller candidates from the GenieX-optimized catalog: Qwen3-4B-Instruct-2507 (primary), Gemma-4-E4B-it, Qwen3-1.7B. (Original ONNX-QNN/Phi-3.5/Llama-3.2-3B path is stale — those models aren't in the GenieX catalog.)
  ⟨E⟩ **Measured 2026-07-30 on the X1E80100:** server mode verified live (OpenAI-shaped, ready ~1 s — Plan B Task 5 de-risked). But the NPU is the **slowest** compute unit for LLM decode on `llama_cpp` (14.2 tok/s @ ~4B vs CPU 21.3, GPU 17.6; decode is memory-bandwidth-bound — the same chip wins 14.8× on batched conv). Design budget: ~14 tok/s at 4B, fine for a background distiller. The `qairt` path is **blocked** (AI Hub 503) — `llama_cpp` GGUF is the validated path, with models cached from Docker Hub/HF; whether `qairt` reverses the ranking is the biggest open edge variable. Gemma-4-E4B ships mistyped as `vlm` and **silently drops prompts** (fix: `geniex model set-type … llm`). Add a GPU arm to the bake-off (Adreno is 2.9× the NPU at 1.7B).
- **AI-100 serves LLMs via an OpenAI-compatible-shaped API — hosted ⟨A⟩:** Cirrascale Inference Cloud (`https://aisuite.cirrascale.com/apis/v2`, bearer key, `Llama-3.1-8B` only), verified live 2026-08-03. No self-hosting. Caveats: `response_format` ignored; JSON output must route via `/completions` (chat endpoint misparses it into empty tool calls). AI-100, Ollama, and GenieX all sit behind one HTTP adapter.

## 4. Architecture

⟨F⟩ Revised: the MCP server moved onto the developer's machine as an **Orchestrator**. The Edge Worker no longer talks to the service at all.

```
   Coding agent (Claude Code / Codex / any) ── writes the transcript it already writes
        │  MCP (localhost HTTP)                        ▲ RO access
        ▼                                              │
   ORCHESTRATOR (local, one per machine) ──────────────┼──────────────
     MCP server: query(nl) · contribute(text)          │
       briefing rides `instructions` at initialize     │
     Producer endpoint: accepts Finding[] ONLY  ◄──────┤
     Owns LocalBinding; stamps Attribution             │
     Durable log (write-ahead, retained → resync)      │
        │                                              │
        │                          EDGE WORKER (local, same machine)
        │                            Agent detect (registry of transcript roots) ⟨D⟩
        │                            Follower → Source adapter → AgentEvent
        │                            Segmenter → Segment (turn boundary + per-model budget)
        │                            Triage ⟨G⟩ — deterministic; is this worth the NPU?
        │                            Distiller ── ModelProvider(SLM on NPU) → Finding[]
        │                              compresses and abstracts; does NOT judge ⟨G⟩
        │                            Durable log (write-ahead) ── POST findings
   ─ ─ ─│─ ─ device boundary: only Findings cross · ONE egress point ─ ─ ─
        ▼
   SYNAPSE SERVICE (any laptop; a teammate's, for the demo) ⟨A⟩ ──────
     Ingest API (idempotent upsert by Finding.id)
     Synthesis → Working Memory + Conflict[] + memory_version
       semantic merge → Synthesized Finding; originals become tombstones
       └─ calls hosted Cloud AI 100 (Cirrascale, Llama-3.1-8B) over HTTPS ⟨A⟩
     Finding Log behind a storage seam ← what retrieval ranks over
     Retrieval = LLM-as-retriever (query + Working Memory + candidates → ranked)
```

⟨F⟩ **The two-plane framing is retired.** Data and retrieval both run through the local Orchestrator, which is the sole egress — one boundary to audit rather than two. The Edge Worker owns raw transcripts and returns only Findings; the Orchestrator hosts MCP so agent-authored prose lands in it transiently, but **nothing reaches the service that has not passed through the distiller**. The service stays decoupled from the accelerator: it runs anywhere and reaches Cloud AI 100 over HTTPS.

⟨F⟩ **Caller identity is a known protocol limit.** MCP gives a server only `clientInfo: {name, title, version}` at initialize — the client *product*, not a conversation. The Orchestrator resolves the Agent Session as `clientInfo.name` → product, then the worker's live-session detection. Named limitation: one active Agent Session per Agent product per machine. The distilled path is unaffected — it never touches MCP.

## 5. Contracts (frozen Day 0, in `synapse/contracts`)

| Contract | Shape (essentials) |
|---|---|
| `AgentEvent` | `{role, kind: text\|thinking\|tool_use\|tool_result, content, tool_name?, ts, session_id, cwd, git_branch}` — agent-agnostic, internal to worker |
| `Segment` | bounded run of `AgentEvent`s split on turn boundary **and token budget (derived per model from its measured capability record — usable context, prefill tok/s, prompt reserves — never a shared hard-coded constant ⟨D⟩; ~2–2.5K on a 4K qairt bundle), events deterministically compacted (tool_result head/tail truncation, thinking trimmed, trivial calls dropped)** ⟨C⟩ — the distiller's input |
| `Attribution` ⟨F⟩ | `{contributor, agent_session, agent}` — where a Finding came from, at three levels, carried as one value so they cannot drift apart |
| `Finding` ⟨F⟩ | `{id, type, text, attributions[], ts, refs?, provenance: distilled\|contributed\|synthesized, status: kept\|superseded\|trivial, merged_from[], merged_into?}` — `text` is **abstracted, not verbatim code**; `id` is stamped client-side at distil time so retried pushes are idempotent; `attributions` is a list because a merged Finding carries every source; `status`/`merged_*` are service-written |
| `SynapseSession` | `{shared_id, purpose, members[] (Contributors), created_by}` |
| `LocalBinding` ⟨F⟩ | `{agent_session_id → shared_id, contributor, agent}` — set at join; owned by the local orchestrator, which stamps it onto every Finding from any local producer |
| `Conflict` ⟨F⟩ | `{finding_a: FindingId, finding_b: FindingId, description}` — references, not embedded copies, so a conflict can be updated or resolved |
| `SessionContext` ⟨F⟩ | Working Memory (bounded prose) + `Conflict[]` + `memory_version`, organized by `purpose`. **Only half of Shared Memory** — the Finding Log lives behind the service's storage interface and is what retrieval ranks over |
| `ModelProvider` | `complete(messages, response_schema?) -> ModelResult{data, usage{in,out}, latency_ms, provider_id, schema_valid}` |
| Ingest API | request/response for findings-push + session create/join (so Sync client can test against a mock) |
| MCP tools ⟨F⟩ | `query(nl) -> ranked Finding[]`, `contribute(text)` (stretch). **No `attach`/`join` tool** — joining is a local CLI (`synapse join <shared_id>`), and at initialize the Orchestrator already knows the binding, so the agent is never told which Shared Session it is in. The arrival briefing rides the MCP `instructions` field. |

## 6. ModelProvider — on/off-target mode

Every model-using component depends only on `ModelProvider`. A **mode is a pair** `{distiller, synthesizer}`, resolved once at startup from config.

| Provider | Wraps | Role |
|---|---|---|
| `FakeProvider` | scripted deterministic outputs | **all unit/contract tests** (offline, instant, CI) |
| `ClaudeProvider` | Anthropic SDK | off-target + **quality/cost baseline** |
| `OllamaProvider` | local Ollama (Llama-3.2-3B) | off-target dev on Mac + offline demo fallback |
| `AIC100Provider` ⟨A⟩ | Cirrascale hosted Cloud AI 100 (`Llama-3.1-8B`) | synthesis — reachable from Day 1, dev included |
| `NPUProvider` ⟨A⟩ | GenieX `serve` @ `localhost:18181/v1` (`llama_cpp` **verified live** ⟨E⟩; `qairt` blocked by AI Hub 503) | on-target distillation |

Ollama / AI-100 / GenieX share one OpenAI-compatible HTTP adapter (differ by base URL); only Claude needs a distinct adapter. **Structured output is capability-flagged**: providers without native constrained decoding (NPU **and** aic100 ⟨A⟩ — Cirrascale ignores `response_format` and schema calls route via `/completions`) use prompt-instructed JSON + tolerant parse + one retry; the conformance test *measures* schema-valid rate per provider rather than assuming it. ⟨E⟩ GenieX's CLI exposes `--enable-json` / GBNF grammar flags — if the probe finds them plumbed through `serve`'s HTTP API, the edge path gets sampler-**guaranteed** JSON (`native_structured_output=True`), potentially *better* structured-output guarantees than the cloud synthesizer.

Config example:
```
# off-target (dev, this week)        # on-target (hackathon)
distiller:   ollama | claude          distiller:   npu
synthesizer: claude                   synthesizer: aic100
```

**Benchmark engine, not just dev aid.** `ModelResult.usage` + `latency_ms` are in the contract, so running the fixture corpus through different providers yields quality-vs-Claude, cost, and latency with no extra harness code. This is the answer to "why not just call Claude?" — quantified.

## 7. Testing strategy

- **Determinism:** all unit/contract tests run against `FakeProvider` — CI-able, no keys, no GPU. LLM output *quality* is a **measurement** (eval harness), never a pass/fail unit gate.
- **Fixtures are ground truth:** hand-authored, frozen `Segment` blobs + golden `Finding[]` checked into the repo Day 0. Aditya's distiller builds against those exact blobs; Akhil's segmenter must *reproduce* them. This pins the `Segment` boundary so the two tracks cannot drift.
- **Walking skeleton (mid-week milestone):** wire the thinnest end-to-end path with all fakes (`FakeSource → segment → FakeProvider distiller → in-memory synthesis → MCP query`) to prove the contracts *compose* before real implementations land.

## 8. Team split & ownership

| Owner | Component | Rationale |
|---|---|---|
| **Akhil** | Edge worker: Source adapters, file follow/rotation, segmentation, Sync client | Deterministic, hard-spec, edge-case-heavy plumbing; no hardware unknown (pure TDD against fixtures + mock ingest server) |
| **Aditya** | Distiller (`Segment→Finding[]`) + NPU bring-up + eval/benchmark harness | ML/profiling; owns the X Elite NPU laptop |
| **Siddsing** | contracts + `ModelProvider`/providers + Synthesis + MCP server | Architect/integration; owns the AI-100 box |

## 9. Pre-hackathon week plan

**Day 0 (all three, blocking — nothing parallel starts until done):** freeze `contracts` (incl. ingest API) · scaffold monorepo · commit fixture `Segment`s + golden `Finding[]` (co-authored — this defines the quality bar) · ship `FakeProvider` · write a red walking-skeleton test.

**Then three parallel tracks:**

- **Siddsing:** `ModelProvider`+`FakeProvider` first (unblocks teammates — top Day-0 priority). ~~AI-100 spike Day 1–2~~ ⟨A⟩ **spike retired — GO 2026-08-03**: hosted Cirrascale endpoint verified end-to-end (ping/chat/embeddings); residual work is the `/completions` schema path in `AIC100Provider`. Freed time → Synthesis (incremental merge) + MCP (LLM-as-retriever).
- **Aditya:** NPU spike Day 1–2 ⟨A⟩ **via GenieX**: `/quad-detect` sanity → `geniex serve` with `qairt` AI Hub bundle → run fixture corpus through Qwen3-4B-Instruct-2507 / Gemma-4-E4B-it / Qwen3-1.7B (go/no-go: NPU residency, prefill/generate tok/s, power, **schema-valid JSON rate**, **usable compiled context length** ⟨C⟩; grammar-support probe worth 10 min — fallback GenieX `llama_cpp`, then Mac/CPU Ollama). Then Distiller (TDD vs `FakeProvider`) + eval harness (corpus × provider → quality/cost/latency table).
- **Akhil:** No hardware spike. `ClaudeCodeSource` (JSONL→`AgentEvent`) · follower (tail/rotation/partial/malformed) · segmenter (turn-boundary→`Segment`, must reproduce fixtures) · Sync client (findings push + join, tested vs mock HTTP server).

**Spike discipline:** each risky spike has a one-line success assertion, a drop-dead time, and a fallback. Off-target mode *is* the fallback (e.g. AI-100 red by end of Day 2 → demo synthesis on Claude, present AI-100 as validated separately).

**End-of-week exit criteria:** (1) both hardware spikes have a written go/no-go + runbook; (2) every component green vs `FakeProvider` in CI; (3) full pipeline runs **off-target** end-to-end on the Mac (Claude both roles) against fixtures; (4) Day-1 integration = flip config to `{distiller: npu, synthesizer: aic100}`, run the same suite.

## 10. TDD "definition of done" per component

| Component | First failing tests |
|---|---|
| `ClaudeCodeSource` | real fixture → expected `AgentEvent[]`; malformed line skipped not crashed; partial trailing line waits |
| Segmenter | event run → segments split on turn boundary; must reproduce frozen fixture Segments; empty turn → no segment |
| Sync client | findings POST w/ contributor+shared_id; join binds local→shared; retries on 5xx (vs mock server) |
| Distiller | fixture Segment + `FakeProvider` → schema-valid `Finding[]`; each type populated; malformed model output → tolerant parse + one retry |
| Providers | conformance: same input → schema-valid output; `usage`/`latency` populated; capability flag honored |
| Synthesis | two contributors' findings merge; contradictory pair → `Conflict`; incremental merge stays bounded |
| MCP | create/join/query happy paths; query returns ranked findings; unknown session errors cleanly |
| Eval harness | corpus × provider → quality/cost/latency table |

## 11. Scope / YAGNI

**In:** the pipeline above, off/on-target modes, opt-in shared sessions, conflict flagging, benchmark harness, **agent auto-detection + `CodexSource` (demo pair) ⟨D⟩**.
**Out (stretch goals):** **retrieval-optimized shared-memory store ⟨D⟩ — vector RAG / findings graph / purpose→topic hierarchy behind the storage interface (in-memory + LLM-as-retriever is the first pass)**, **contribute module — MCP `contribute(text)` tool, agent self-distills in NL, local model gates it into shared memory (first post-walking-skeleton stretch; see hybrid amendment Part 2)** ⟨B⟩, cross-session persistence, mobile contributions (photos/voice notes), team dashboard, auth beyond opt-in join.

## 12. Known risks & mitigations

| Risk | Mitigation |
|---|---|
| ~~AI-100 provisioning/serving~~ ⟨A⟩ retired — hosted endpoint verified GO | — |
| Shared Cirrascale credit pool / unknown rate limits ⟨A⟩ | bound `max_tokens` on every call; dev loops on Ollama; usage tracked via `ModelResult.usage` |
| Synthesis quality on single 8B model (no 70B on our key) ⟨A⟩ | tight working-memory bound + simple prompts; benchmark table Claude-vs-aic100 *is* the demo narrative; ask office hours re 70B |
| Cirrascale chat endpoint misparses JSON output into empty tool calls ⟨A⟩ | schema calls route via `/completions` + tolerant extraction (probed + verified) |
| Venue WiFi down ⟨A⟩ | demo is pre-recorded A/B ⟨D⟩ — network only threatens the live encore; for that, synthesis falls back to `synthesizer: ollama` (one env var); edge path (GenieX) is fully local |
| NPU/aic100 can't emit structured JSON | capability flag + prompt-instructed JSON + tolerant parse; measured, not assumed; ⟨E⟩ GenieX GBNF flags may make the edge path sampler-guaranteed — probe |
| Model mistyped as `vlm` silently drops the prompt → confident fabricated findings poison shared memory ⟨E⟩ | assert `usage.prompt_tokens > 1` on every distiller call (hard-fail the finding); canary fixture in the eval harness; verify `ModelType` after every pull |
| AI Hub down (503) blocks `qairt` bundles ⟨E⟩ | `llama_cpp` GGUF validated as the working path; cache candidates from Docker Hub/HF now; re-check 503 periodically |
| Provider parity (3B mangles schema Claude honors) | shared conformance test; this *is* the quality signal to measure |
| Segment/distiller drift between Akhil & Aditya | frozen hand-authored fixture Segments are the shared ground truth |
| "Isn't this Notion + RAG?" | no human writes notes — agents' own work becomes shared memory, in-loop, in minutes |
| Cross-machine ordering | wall-clock timestamps assumed sufficient at hackathon scale (named limitation) |
| Shared-memory store is naive (in-memory + LLM-as-retriever) ⟨D⟩ | acceptable at hackathon scale; a narrow storage interface isolates the choice; RAG / graph / hierarchy evaluated as the scaling story |
| Distiller & synthesis prompts are first-pass Claude drafts ⟨D⟩ | explicitly not contracts; eval loop + prompt-optimizer (hybrid amendment) is the tuning mechanism |
| Findings lost on a transient failure — NPU work is expensive and unrepeatable ⟨F⟩ | write-ahead durable log at worker and orchestrator: persist on produce, before any send; replay unsent on restart |
| **A 4B reverses a fact stated in its own prompt, passing every guard** ⟨G⟩ | highest-severity risk now. Fidelity rule in the prompt fixed it on n=1; needs a real corpus. An inverted finding is indistinguishable from a correct one downstream |
| **The privacy metric cannot see the leaks that occur** ⟨G⟩ | 8-gram overlap reports 0.00 on a finding containing `default_pool_size=25`. Needs an identifier-shaped-token check. **Do not report the current number — it reads as proof and is not** |
| **Nothing filters triviality** ⟨G⟩ | `adr/0003` moved judgment to triage (unbuilt) and synthesis (unbuilt). Trivia reaches the sink today |
| Service restart wipes in-memory Shared Memory for everyone ⟨F⟩ | logs are retained after send, so every orchestrator can `resync`; safe because ingest upserts by `Finding.id` |
| MCP cannot identify which Agent Session is calling ⟨F⟩ | `clientInfo.name` → product + worker live-session lookup. Named limit: one Agent Session per product per machine |
| Demo A/B baseline is contaminated by human learning ⟨F⟩ | A/B the *agents* on a replayed capture from fixed repo state, N runs for variance; pre-populated findings must come from a real distilled capture |
| Single turn exceeds NPU context / prefill power budget ⟨C⟩ | budgeted sub-turn segments + deterministic compaction; synthesis dedupes across sub-segments; usable bundle context is a spike go/no-go axis |
