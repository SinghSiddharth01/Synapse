# Synapse — Design Spec

**Status:** Approved design, pre-implementation
**Date:** 2026-07-25
**Amended:** 2026-08-03 — provider layer (hosted Cirrascale AI-100, GenieX NPU runtime, model selection); see `2026-08-03-aic100-cirrascale-amendment.md`. Amended sections marked ⟨A⟩.
**Amended:** 2026-08-03 — hybrid frontier/local strategy (Part 1: design-time frontier leverage, local-only runtime; Part 2: contribute module, deferred); see `2026-08-03-hybrid-frontier-local-amendment.md`. Amended sections marked ⟨B⟩.
**Amended:** 2026-08-03 — segment token budget + compaction + 4B quality calibration; see `2026-08-03-segment-budget-compaction-amendment.md`. Amended sections marked ⟨C⟩.
**Event:** Snapdragon Multiverse internal hackathon (build week Aug 3–7 2026; prep week ~Jul 27–31)
**Team:** Siddsing (architect/integration), Akhil (edge worker / low-level), Aditya (ML/distillation, owns NPU laptop)

---

## 1. Problem

Every engineer now works alongside an AI coding agent, but each agent is blind to the team. When several people work toward the same goal, their agents repeat the same explorations and duplicate hours of work. Teams lack a passive listener — running on the Copilot+ PC each member already has — that quietly captures what every agent learns and turns it into shared knowledge.

## 2. Solution

Synapse observes any coding agent's session **unmodified** (by reading the session-transcript JSONL the agent already writes to disk) and turns isolated agents into shared team intelligence.

A teammate creates an opt-in shared session from their Copilot+ PC, declaring its purpose; teammates join with one command. Each member holds a different slice of context for the same task. A per-user small model distills each agent's activity into structured findings — learnings, decisions, dead ends, open questions — each tagged with contributor and time. **Raw work stays on device; only distilled findings leave the machine.** A large model on Cloud AI 100 merges everyone's findings into one shared working memory, flags conflicts, and organizes against the session's purpose. Agents retrieve on demand through MCP (pull-only, natural-language queries, ranked results).

**Division of labor (the "Why Qualcomm" thesis):** edge distillation on the Snapdragon X Elite NPU (continuous, ingest/prefill-heavy — the NPU's strength) + cloud synthesis on Cloud AI 100 (sustained low-cost multi-user serving).

**Demo vehicles:** Claude Code is the **primary** path (capture verified). OpenAI Codex is the **agent-agnostic proof** — a second `Source` adapter normalizing into the same `AgentEvent` schema; building it is a week **stretch goal**, not required for the core pipeline. Design is agent-agnostic by construction (new agent = one new Source, nothing else changes).

## 3. Verified assumptions (as of 2026-07-25)

- **Passive capture is real.** Claude Code writes `~/.claude/projects/<slug>/<uuid>.jsonl`, one JSON event per line. A real 5,579-line session confirmed: events carry `type` (user/assistant/system), `timestamp`, `cwd`, `gitBranch`, `sessionId`, `uuid`/`parentUuid`; `message.content` is a list of typed parts (`thinking` / `text` / `tool_use` / `tool_result`); 1,212 tool calls in that session. The "what the agent learned / tried / hit a wall on" signal is directly present. Capture = tailing these files, zero agent modification. This corpus is also the eval fixture + TDD corpus.
- **On-device LLM on X Elite NPU is turnkey — via GenieX ⟨A⟩:** `geniex serve` exposes an OpenAI-compatible API at `localhost:18181/v1` with `qairt` (NPU-exclusive, pre-compiled AI Hub bundles) and `llama_cpp` backends. Distiller candidates from the GenieX-optimized catalog: Qwen3-4B-Instruct-2507 (primary), Gemma-4-E4B-it, Qwen3-1.7B. (Original ONNX-QNN/Phi-3.5/Llama-3.2-3B path is stale — those models aren't in the GenieX catalog.)
- **AI-100 serves LLMs via an OpenAI-compatible-shaped API — hosted ⟨A⟩:** Cirrascale Inference Cloud (`https://aisuite.cirrascale.com/apis/v2`, bearer key, `Llama-3.1-8B` only), verified live 2026-08-03. No self-hosting. Caveats: `response_format` ignored; JSON output must route via `/completions` (chat endpoint misparses it into empty tool calls). AI-100, Ollama, and GenieX all sit behind one HTTP adapter.

## 4. Architecture

```
   Coding agent (Claude Code / Codex)  ── writes JSONL it already writes
              │
   EDGE WORKER (per machine, Akhil) ─────────────────────────────────
     Source adapter (ClaudeCodeSource / CodexSource) → AgentEvent
     File follower (tail, rotation, partial/malformed lines)
     Segmenter → Segment (turn-boundary batches of AgentEvent)
              │  Segment
     Distiller (Aditya) ── ModelProvider(SLM) → Finding[]   raw work stays local
              │  Finding[]
     Sync client ── POST findings + session join over HTTP
   ─ ─ ─ ─ ─ ─│─ ─ device boundary: only Findings cross ─ ─ ─ ─ ─ ─ ─
              ▼
   SYNAPSE SERVICE (any laptop, Siddsing) ⟨A⟩ ──────────────────────
     Ingest API (findings in)
     Synthesis (ModelProvider(large) → incremental merge → SessionContext + Conflict[])
       └─ calls hosted Cloud AI 100 (Cirrascale, Llama-3.1-8B) over HTTPS ⟨A⟩
     MCP server (HTTP/SSE): create_session / join_session / query(nl) → ranked Finding[]
       Retrieval = LLM-as-retriever (query + shared-memory doc → ranked findings)
```

Two planes: **data plane** (workers → ingest API) and **retrieval/control plane** (agents → MCP). One service hosts both; MCP transport is **remote HTTP/SSE** — agents point at the service host. ⟨A⟩ The service is decoupled from the accelerator: it runs on any machine and reaches Cloud AI 100 through the hosted Cirrascale API, so only distilled Findings ever cross either boundary (device → service, service → cloud).

## 5. Contracts (frozen Day 0, in `synapse/contracts`)

| Contract | Shape (essentials) |
|---|---|
| `AgentEvent` | `{role, kind: text\|thinking\|tool_use\|tool_result, content, tool_name?, ts, session_id, cwd, git_branch}` — agent-agnostic, internal to worker |
| `Segment` | bounded run of `AgentEvent`s split on turn boundary **and token budget (~2–2.5K tok; final number from NPU spike prefill/context measurement), events deterministically compacted (tool_result head/tail truncation, thinking trimmed, trivial calls dropped)** ⟨C⟩ — the distiller's input |
| `Finding` | `{type: learning\|decision\|dead_end\|open_question, text, contributor, ts, source_session, refs?, provenance: distilled\|contributed}` ⟨B⟩ — `text` is **abstracted, not verbatim code** (distiller redacts by design); `provenance` defaults to `distilled`, reserved for the Part-2 contribute module |
| `SynapseSession` | `{shared_id, purpose, members[], created_by}` |
| `LocalBinding` | `{local_agent_session_id → shared_id, contributor}` — set at join |
| `Conflict` | `{finding_a, finding_b, description}` |
| `SessionContext` | merged shared memory + `Conflict[]`, organized by `purpose` |
| `ModelProvider` | `complete(messages, response_schema?) -> ModelResult{data, usage{in,out}, latency_ms, provider_id, schema_valid}` |
| Ingest API | request/response for findings-push + session create/join (so Sync client can test against a mock) |
| MCP tools | `create_session(purpose)`, `join_session(shared_id)`, `query(nl) -> ranked Finding[]` |

## 6. ModelProvider — on/off-target mode

Every model-using component depends only on `ModelProvider`. A **mode is a pair** `{distiller, synthesizer}`, resolved once at startup from config.

| Provider | Wraps | Role |
|---|---|---|
| `FakeProvider` | scripted deterministic outputs | **all unit/contract tests** (offline, instant, CI) |
| `ClaudeProvider` | Anthropic SDK | off-target + **quality/cost baseline** |
| `OllamaProvider` | local Ollama (Llama-3.2-3B) | off-target dev on Mac + offline demo fallback |
| `AIC100Provider` ⟨A⟩ | Cirrascale hosted Cloud AI 100 (`Llama-3.1-8B`) | synthesis — reachable from Day 1, dev included |
| `NPUProvider` ⟨A⟩ | GenieX `serve` @ `localhost:18181/v1` (`qairt`/`llama_cpp` on Hexagon) | on-target distillation |

Ollama / AI-100 / GenieX share one OpenAI-compatible HTTP adapter (differ by base URL); only Claude needs a distinct adapter. **Structured output is capability-flagged**: providers without native constrained decoding (NPU **and** aic100 ⟨A⟩ — Cirrascale ignores `response_format` and schema calls route via `/completions`) use prompt-instructed JSON + tolerant parse + one retry; the conformance test *measures* schema-valid rate per provider rather than assuming it.

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

**In:** the pipeline above, off/on-target modes, opt-in shared sessions, conflict flagging, benchmark harness.
**Out (stretch goals):** `CodexSource` adapter (agent-agnostic proof — nice-to-have for the week, not core), **contribute module — MCP `contribute(text)` tool, agent self-distills in NL, local model gates it into shared memory (first post-walking-skeleton stretch; see hybrid amendment Part 2)** ⟨B⟩, vector-embedding retrieval (LLM-as-retriever suffices at hackathon scale; vector RAG is the scaling story), cross-session persistence, mobile contributions (photos/voice notes), team dashboard, auth beyond opt-in join.

## 12. Known risks & mitigations

| Risk | Mitigation |
|---|---|
| ~~AI-100 provisioning/serving~~ ⟨A⟩ retired — hosted endpoint verified GO | — |
| Shared Cirrascale credit pool / unknown rate limits ⟨A⟩ | bound `max_tokens` on every call; dev loops on Ollama; usage tracked via `ModelResult.usage` |
| Synthesis quality on single 8B model (no 70B on our key) ⟨A⟩ | tight working-memory bound + simple prompts; benchmark table Claude-vs-aic100 *is* the demo narrative; ask office hours re 70B |
| Cirrascale chat endpoint misparses JSON output into empty tool calls ⟨A⟩ | schema calls route via `/completions` + tolerant extraction (probed + verified) |
| Venue WiFi down ⟨A⟩ | synthesis falls back to `synthesizer: ollama` (one env var); edge path (GenieX) is fully local |
| NPU/aic100 can't emit structured JSON | capability flag + prompt-instructed JSON + tolerant parse; measured, not assumed |
| Provider parity (3B mangles schema Claude honors) | shared conformance test; this *is* the quality signal to measure |
| Segment/distiller drift between Akhil & Aditya | frozen hand-authored fixture Segments are the shared ground truth |
| "Isn't this Notion + RAG?" | no human writes notes — agents' own work becomes shared memory, in-loop, in minutes |
| Cross-machine ordering | wall-clock timestamps assumed sufficient at hackathon scale (named limitation) |
| Single turn exceeds NPU context / prefill power budget ⟨C⟩ | budgeted sub-turn segments + deterministic compaction; synthesis dedupes across sub-segments; usable bundle context is a spike go/no-go axis |
