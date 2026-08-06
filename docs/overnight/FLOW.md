# FLOW.md — how a contribution actually moves, and what bounds it

Written 2026-08-06 overnight (W3a investigation). Three sections, file:line
cited, read against post-7418a63 config semantics. Feeds W3b and W6.

---

## 1. The MCP contribute path

Repo root: `/Users/siddharthsingh/Dev/synapse`. All citations below are repo-relative `file:line`.

### 1.1 The hops, in order

| # | Hop | Lives in | Wiring |
|---|---|---|---|
| 0 | Agent → MCP tool call | orchestrator process, `:8787` `/mcp` | Streamable **HTTP**, never stdio — `app.py:140` (`server.streamable_http_app()`), served by `uvicorn.run(app, ...)` at `cli.py:502`. Rationale (ADR 0001, single egress) at `server.py:59-61`. |
| 1 | `contribute(text)` | `packages/orchestrator/src/synapse_orchestrator/server.py:573` | Registered unconditionally at boot, `cli.py:487-490`. Binding is resolved **fresh per call** (`server.py:574`, factory contract at `server.py:178-192`); unbound → `_NOT_JOINED` text (`server.py:125-130`). |
| 2 | Prose → `Segment` | same function, in-process | `server.py:577-583`. Hand-built: one synthetic event `{"role":"assistant","kind":"text"}`, `id=f"contrib-{HHMMSS}"`. **Does not go through `synapse_worker.segmenter`** — no turn boundary, no token budget, no `_split_oversized`. |
| 3 | Distillation (**model call #1**) | `packages/distiller/src/synapse_distiller/distiller.py:105`, provider in `packages/providers/` | `distiller_factory` is `build_npu_distiller` (`cli.py:114-165`), passed at `cli.py:488`, invoked per call with the freshly-resolved binding (`server.py:596`). Arm chosen by `SYNAPSE_DISTILLER` (`cli.py:132-143`): `npu` → `NPUProvider` over **HTTP** to `config.provider.base_url` (`config/synapse.toml:132` = `http://127.0.0.1:18181/v1`); `anthropic` → Messages API; `claude-cli` → **subprocess** (`claude_cli_provider.py`). |
| 4 | Provenance stamp | `server.py:604-605` | Every Finding forced to `Provenance.CONTRIBUTED`. Empty result → early return, nothing recorded (`server.py:606-607`). |
| 5 | Write-ahead log | `relay.py:243-271` | `relay.record(findings, shared_id=binding.shared_id)` at `server.py:615`. Append-only JSONL envelope `{shared_id, finding}` to `<state>/relay/findings.jsonl`. Per-call `shared_id` override exists specifically so a concurrent `rebind()` cannot retarget it (`relay.py:253-265`). |
| 6 | Egress (**HTTP**) | `relay.py:459` → `relay.py:387` | `await relay.flush()` at `server.py:616`. Groups pending by recorded `shared_id`, skips known-ended sessions without a request (`relay.py:483-495`), then **one POST per session** to `/v1/sessions/{sid}/findings`. Fresh `httpx.AsyncClient` per post, `timeout=120.0` (`relay.py:143`, `relay.py:415-416`). On success only, best-effort member registration, one POST per unseen `(session, contributor)` (`relay.py:344-385`, called at `relay.py:423`). |
| 7 | Service ingest | `packages/service/src/synapse_service/api.py:469` (route table `api.py:768`) | Liveness gate `_unavailable` (`api.py:471`, defined `api.py:266`) → 404/409. `store.upsert` (`api.py:478`) — findings are queryable **immediately**, before any synthesis (`api.py:492-499`). Then debounce (`api.py:500-510`) and budget gate (`api.py:511`). |
| 8 | Synthesis merge (**model call #2**) | `packages/service/src/synapse_service/synthesis.py:250`, awaited inline at `api.py:542` | Synchronous inside the POST — that is why the relay timeout is 120s (`relay.py:128-143`, with measured 12.6–52.8s round latencies). Provider is `AIC100Provider` (Llama-3.3-70B) when `SYNAPSE_SYNTHESIZER=aic100` (`packages/service/src/synapse_service/cli.py:16-20`). Spend charged after the fact at `api.py:543`. |
| 9 | Response back up | `api.py:559-561` | `{accepted, memory_version, synthesized, deferred, pending}`. |

There is **no queue and no background worker anywhere on this path** — `contribute()` blocks the agent's tool call through distillation, the WAL write, the HTTP push, and the synthesis model call. The only asynchrony is the debounce, which converts synthesis into "later, on someone else's push".

### 1.2 What is bounded

- **Distiller attempts:** exactly 2, prompt-mutating on the retry (`distiller.py:46`, `distiller.py:136`); then the segment is dropped (`distiller.py:173-177`).
- **Distiller output tokens:** `config.effective_max_tokens`, clamped to `response_reserve` (`cli.py:151`; `config.py:150-164`), deliberately clamped in *both* build sites so contribute matches the passive path.
- **Prompt-drop guard:** `assert_prompt_conditioned` before anything is read from the response (`distiller.py:144-146`).
- **Provider timeouts:** distiller/openai-compat 300s (`openai_compat.py:35`, `config/synapse.toml:135`); AIC100 60s, `INFERENCE_CLOUD_TIMEOUT`-overridable (`aic100.py:153`, `aic100.py:188`); relay push 120s (`relay.py:143`); orchestrator lifecycle/query client 15s (`server.py:234`).
- **AIC100 internal retry:** exactly 2 attempts, temperature nudged, truncation-aware shrink retry (`aic100.py:235-296`); 429 rotates keys, each tried at most once (`aic100.py:197-211`).
- **Synthesis merge rate:** floor of 60s per session (`api.py:48`), timestamp stamped *before* the await (`api.py:525`) so same-session concurrent pushes cannot double-merge.
- **Synthesis spend:** rolling-hour ledger, 25,000 tok/h and 20 req/h per key, next round costed at the max of the last five (`api.py:80-86`, `api.py:238-261`).
- **Synthesis prompt:** `CANDIDATE_WINDOW = 20` (`synthesis.py:34`), output budget derived from the provider's `max_tokens` (`synthesis.py:138-142`).
- **Retrieval prompt:** `TOP_K = DEFAULT_TOP_K = 14` (`api.py:39`, `lanes.py:58`).
- **Debug rings:** feed `deque(maxlen=MAX_FEED)` (`debug.py:44-45`), provider call log `maxlen=200` (`recording.py:44-45`).
- **Terminal vs retryable classification:** 404 retryable, other 4xx terminal, 409+`session_ended` terminal-and-remembered (`relay.py:425-457`).

### 1.3 What is NOT bounded

1. **`contribute(text)` input size.** `text` goes straight into a one-event Segment (`server.py:579-583`) with no length check and no segmentation. The passive path's whole budget system — `Segmenter(budget_tokens=config.segment_budget)` (`worker/cli.py:280`), `_split_oversized` (`segmenter.py:47`) — is bypassed. An oversized contribution overruns the model's context and reaches the operator only as `finish_reason=length` / a salvaged 400 (`openai_compat.py:88-95`, `openai_compat.py:104-110`).
2. **Relay batch size.** `flush()` sends *every* pending finding for a session in one request body (`relay.py:482`, `relay.py:412`). After an outage the first successful flush is a single unbounded POST, which then triggers one merge over the whole batch.
3. **Relay WAL growth and re-read cost.** `findings.jsonl` / `sent.jsonl` / `dropped.jsonl` are append-only with no rotation or compaction, and `_pending()` re-parses the entire log on every flush (`relay.py:273-299`). Same shape on the worker side (`producer.py:281-325`).
4. **Concurrency on the relay.** No lock anywhere. Two concurrent `contribute()` calls in one process both read `_pending()` and both POST the same findings (`relay.py:268`, `relay.py:474-512`). Harmless to the store (`store.upsert` skips identical resends, `store.py:217-224`) but it can cost a duplicate 70B merge round.
5. **Service-side deferred queue `_pending`.** Grows without cap while synthesis is deferred (`api.py:192`, `api.py:500-501`); drained only by a non-deferred push (`api.py:524`) or `POST /synthesize` (`api.py:589`).
6. **`InMemoryStore`.** No cap, no persistence, no eviction (`store.py:195`); retrieval is protected by `TOP_K` but the log itself grows unboundedly per session.
7. **Cross-session synthesis concurrency.** `_affordable()` is read at `api.py:511`, the merge is awaited at `api.py:542`, `_record_spend()` runs at `api.py:543`. Nothing serializes the interval — N concurrent pushes on N different sessions all observe the same pre-spend ledger and all proceed. The 60s floor is per-session (`_last_merge` keyed by `sid`, `api.py:191`), so it does not help here.
8. ~~**Worker → provider call rate.**~~ **CLOSED 2026-08-06 (W3b, decisions/002).** Was: no rate limiter on the distiller side at all — `WorkerLoop.tick` distilled every surviving segment sequentially with no per-tick cap and no token ledger (`loop.py:340-374`), and `run()` only sleeps between ticks (`loop.py:436-446`). Now `SeamLimiter` (`packages/worker/src/synapse_worker/limiter.py`) bounds three things from `[worker]` config: **4 calls/tick** (overflow DEFERRED, persisted to `deferred-segments.json`, never shed), **1 concurrent call** (a semaphore every worker→provider call in `loop.py` holds, so the ceiling is checked rather than implied by sequential awaits), and **64 deferred segments** — at which `tick()` stops reading new transcript bytes, leaving them on disk behind the follower's offset. Visible as a WARNING, an INFO, a `limiter` stats event and `TickResult.deferred`/`.backpressured`.
9. **`POST /v1/sessions/{sid}/synthesize` is ungated.** It charges `_record_spend()` (`api.py:594`) but never calls `_affordable()` (`api.py:584-591`) — a public, unauthenticated force-now that can spend past the ceiling. (`api.py:211-228` documents the sibling bug already fixed here: phantom charging of no-call rounds.)

### 1.4 Two observability gaps on this path

- **`deferred` never reaches the agent.** The service reports `deferred: true` (`api.py:560`), but `Relay._post` reads only the status code and discards the body (`relay.py:417-424`), so `contribute()` says `"N finding(s) shared with the team"` (`server.py:629`) for a push whose working memory will not move for up to a minute — or for the rest of the hour if the budget gate tripped (`api.py:511-522`).
- **The 15s query client sits under a model call.** `_client()` is `timeout=15.0` and its comment asserts "none of these routes runs a model" (`server.py:231-234`), but `query()` uses it (`server.py:492`) and `/query` *does* run one — `query_findings(retrieval_provider, ...)` at `api.py:725-726` → `provider.complete` at `retrieval.py:107`, against the same 70B whose measured round latencies are 12.6–52.8s (`relay.py:131`). A slow retrieval returns "Shared memory is unreachable right now" (`server.py:537`) while the model call keeps running and keeps spending the key.

### 1.5 The suspected metering hole — **CONFIRMED**, and **CLOSED 2026-08-06 (W3b, decisions/002)**

> **Status as of the W3b commit.** Every element below still described HEAD when
> W3b re-checked it (the handler spans `api.py:672-798` after W1's typed-503
> rewrite and contained neither `_record_spend()` nor `_affordable()`;
> `_record_spend` had exactly two call sites, both on the synthesis path). It is
> now fixed: `query_findings` grows an `on_usage` callback, `api.query` passes
> `_record_query_spend`, and `_spend` became the KEY's ledger — `(ts, tokens,
> component)` — so the hourly ceilings sum across synthesis *and* retrieval while
> merge pricing still reads only merges. A failed retrieval is charged
> `ASSUMED_QUERY_TOKENS` (1,500); a query that ranked nothing made no request and
> is not charged. `/query` is charged, never gated. The product question this was
> blocked on — "whose latency gives way?" — is answered in decisions/002. Read
> the rest of this section as the diagnosis, not as current behaviour.

Every element of the claim holds:

1. **One provider object, shared.** `synapse_service/cli.py:84` builds the app with a single `_provider()` (`cli.py:16-41`). `api.py:178-183` wraps that *same* object twice — `synthesis_provider` and `retrieval_provider` are two `RecordingProvider` façades over one instance, hence one API key and one hourly ceiling.
2. **`/query` makes a real model call.** `api.py:725-732` → `retrieval.py:107` (`await provider.complete(...)`).
3. **`/query` is never charged.** `_record_spend` has exactly two call sites, `api.py:543` (push) and `api.py:594` (`/synthesize`). The `query` handler spans `api.py:665-760` and contains neither `_record_spend()` nor `_affordable()`.
4. **`_affordable()` is consulted at exactly one place**, `api.py:511`, and reads only `_spend` (`api.py:238-261`) — a ledger that by construction contains synthesis rounds only.

So N queries burn N of the key's 20 requests/hour and their tokens, `_affordable()` still returns `(True, "")`, and the next merge 429s inside `AIC100Provider` — where the only mitigation is key rotation (`aic100.py:197-211`), which is a no-op with a single key, and where `scripts/local_model_server.py` (the proxy used in `--live`) holds one key with no rotation at all.

This is **known and deliberately open**, documented verbatim at `api.py:68-79`, including the reason (metering retrieval would let query traffic defer synthesis — a product decision) and the intended fix (one metered wrapper around the single provider, shared ledger, per-component policy). The `SYNTHESIS_` prefix on `SYNTHESIS_TOKENS_PER_HOUR` / `SYNTHESIS_REQUESTS_PER_HOUR` (`api.py:80-81`) is there to name the limitation.

Two extensions to the hole not covered by that comment, both confirmed above: `POST /synthesize` charges but is never gated (§1.3 item 9), and `_affordable()`'s check-then-await-then-charge sequence has no cross-session serialization (§1.3 item 7).## 2. Listener mode: batching, chunking, and the effective limits

### The pipeline, as it actually runs

One `synapse-worker run` follows exactly **one** transcript file with one Source adapter (`loop.py:148`, `AGENT_REGISTRY[agent].source_class()`); a second agent needs a second process (`cli.py:343-348`).

Each tick (`loop.py:290-434`) does, in this fixed order:

1. **Re-resolve the join binding from disk** (`loop.py:298` → `_sync_binding_from_disk`, `loop.py:204-234`) — runs even on the no-change path.
2. **Stat-gate.** `follower.has_new_data()` compares `st_size` against the last recorded size (`follower.py:85-91`). No change → retry-flush only (`loop.py:300-324`); if the buffer is non-empty and `idle >= idle_flush_seconds`, an idle flush is forced instead.
3. **Read the delta.** `read_new_lines` seeks to the durable offset and reads to EOF, then **truncates at the last `\n`** — a partial trailing line is left unread and the offset is not advanced (`follower.py:135-149`). If there is no newline in the whole chunk, the offset does not move at all (`follower.py:141-145`). Shrink → offset resets to 0 (`follower.py:120-129`). Split is on `\n` with `\r` stripped, deliberately not `splitlines()` (`follower.py:152-162`).
4. **Parse to `AgentEvent[]`** and append to the segmenter's in-memory `_pending` (`loop.py:331-335`).
5. **Segment** (`segmenter.py:103-190`). Two-stage:
   - **Turn split.** Boundary = `role == "user" and kind == "text"` only — tool results carry `role: "user"` and must not cut a turn (`segmenter.py:42-44`, `128-138`).
   - **Hold-back.** The newest turn is always retained unless `flush_incomplete` (`segmenter.py:114-121`). With one turn seen so far, `drain()` returns nothing.
   - **Oversized-event pre-split**, added by 7c42e96 (`segmenter.py:47-80`, applied at `155-156`): any single event longer than `budget_tokens × 3.5` chars is cut into pieces, preferring the last `\n` in the final 40% of the window (`segmenter.py:69`). Without it, the `current and` guard at `segmenter.py:162` admitted an over-budget first event unconditionally.
   - **Chunk packing.** Greedy accumulate until `estimate_tokens(candidate) > budget_tokens` (`segmenter.py:158-168`). Sub-segments are never re-merged; dedup is synthesis's job (`segmenter.py:145-147`).
6. **Triage → compaction → distil**, strictly in that order (`loop.py:340-370`; the ordering rationale is `compaction.py:34-61`). Triage is deterministic and keep-biased (`triage.py:96-147`); compaction only reshapes what the model sees.
7. **WAL then push.** `producer.record()` writes `{produced_at, shared_id, finding}` envelopes to `wal/findings.jsonl` **before** any send (`loop.py:415`, `producer.py:242-269`); `flush()` sends only envelopes whose recorded `shared_id` matches the current binding, holding mismatches rather than retargeting or dropping (`producer.py:309-364`). Delivery confirmation is an append of the id to `wal/sent.jsonl` (`producer.py:337-340`).
8. **Persist offset + pending buffer last** (`loop.py:430` → `_persist_state`, `loop.py:174-180`) so a crash costs duplicated work, never lost conversation.

Retry semantics are two-layer: the **distiller** retries once with a corrective `RETRY_NUDGE` message appended on attempt 2 only (`distiller.py:46-57`, `128-136`), then drops the segment; the **producer** retries indefinitely on every subsequent tick because unsent is derived from the log, not from memory (`producer.py:342-364`).

### The effective limits

Governing config is `/Users/siddharthsingh/Dev/synapse/config/synapse.toml` (`config.py:196-202` walks parents for `config/synapse.toml`). `scripts/serve_local.py` **does not spawn a worker at all** — it starts service + orchestrator only (`serve_local.py:347-348`, `458-460`) and injects `SYNAPSE_BASE_URL` / `SYNAPSE_DISTILLER` / `SYNAPSE_SYNTHESIZER` (`serve_local.py:327-346`, `419-420`), so nothing in that script changes a worker-side limit. The only script that overrides worker limits is `scripts/demo_local.py` (`demo_local.py:495-502`), and only for the demo.

Effective column assumes the shipped `[distiller] model = "qualcomm/Qwen3-4B-Instruct-2507:W4A16"` + `prompt_pack = "v4-condense"`, no `SYNAPSE_*` env set.

| Limit | Code default | Configured value | Effective at runtime | Where set (file:line) |
|---|---|---|---|---|
| `segment_budget` (tokens of Segment content per prompt) | derived: `min(usable_context − overhead − reserve, prefill×secs)` | `2787` (pinned override) | **2787 tokens** — `from_context = 4096−809−500 = 2787`; `from_latency = int(250.0×30.0) = 7500`; derived = 2787, so the pin is a no-op for the NPU arm and is validated (≤ `from_context`, ≥ 500) | `config/synapse.toml:96`; derivation `capability.py:51-101`; resolution `config.py:166-173`; consumed `cli.py:280` → `loop.py:150` |
| `usable_context` | 4096 (builtin `NPU_QWEN3_4B_INSTRUCT_2507`) | 4096 | **4096** (hard ceiling — no `--nctx` on qairt) | `config/synapse.toml:142`; builtin `capability.py:117-127` |
| `prompt_overhead_tokens` | chars/3.5 estimate | calibrated `809` | **809** (calibrated wins over estimate) | `config/prompts/v4-condense.toml:107`; `promptpack.py:85-89` |
| `response_reserve` | 500 (builtin) | 500 | **500** | `config/synapse.toml:144` |
| `prefill_toks_per_sec` | 250.0 | 250.0 (PROVISIONAL) | **250.0** — latency limit never binds (7500 > 2787) | `config/synapse.toml:143` |
| `max_seconds_per_call` | 30.0 | 30.0 | **30.0** | `config/synapse.toml:75`; `config.py:234-236` |
| `provider.max_tokens` (requested output) | 900 | 900 | **500** — clamped by `effective_max_tokens = min(900, response_reserve 500)`; the clamp *lowers*, never raises, and logs when it bites | default `config.py:115`; configured `config/synapse.toml:133`; clamp `config.py:149-164`; applied `cli.py:143-156` |
| ↳ same, `SYNAPSE_DISTILLER=claude-cli` | `OpenAICompatibleProvider` 1024 | 900 | **unbounded** — `ClaudeCliProvider(max_tokens=config.provider.max_tokens)` bypasses the clamp, and the CLI accepts-and-ignores the value anyway | `cli.py:133`; `claude_cli_provider.py:92,99-103` |
| ↳ same, `SYNAPSE_DISTILLER=anthropic` | 16000 | not consulted | **16000** — `AnthropicProvider()` constructed with no args | `cli.py:125`; `anthropic_provider.py:89,169` |
| Max chars in one event before pre-split | `budget × 3.5` | — | **9754 chars** (`int(2787 × 3.5)`); newline preferred in the last 40% of the window | `segmenter.py:56`, `69`; `_CHARS_PER_TOKEN` `segmenter.py:35` |
| Token estimator | `sum(len(content))/3.5 + 1` | — | **chars/3.5 + 1** (deliberately over-counts) | `segmenter.py:38-39` |
| `poll_interval_seconds` | 30.0 | 30.0 | **30.0s** (`--interval` overrides at `cli.py:333`; demo uses 5.0) | `config/synapse.toml:107`; default `config.py:96`; demo `demo_local.py:388,495` |
| `idle_flush_seconds` | 120.0 | 120.0 | **120.0s** (demo sets 10.0 via `SYNAPSE_IDLE_FLUSH`) | `config/synapse.toml:113`; `config.py:97`; `loop.py:302`; demo `demo_local.py:389,502` |
| `upstream_timeout_s` (HTTP sink) | 120.0 (`WorkerConfig`) — but `HttpSink.__init__` default is 30.0 | 120.0 (not present in TOML; falls to dataclass default) | **120.0s** — `cli.py:244` passes it explicitly, so the 30.0 sink default never applies on the `run` path | `config.py:109`; `producer.py:162`; wired `cli.py:243-247` |
| `sink` / `upstream_url` | `"file"` / `:8787/producer/findings` | `"file"`, `.synapse/upstream.jsonl` | **file sink** by default (demo forces `http`) | `config/synapse.toml:122-124`; `cli.py:243-247`; demo `demo_local.py:499-500` |
| `state_dir` (WAL + offset + pending) | `.synapse` | `.synapse` | **`.synapse/`**; WAL at `.synapse/wal/` | `config/synapse.toml:116`; `cli.py:248` |
| `attach_at_end` | `True` | `true` | **True** — history is skipped after priming the Source from the header | `config/synapse.toml:129`; `config.py:257`; `loop.py:182-202` |
| `MAX_ATTEMPTS` (distil retries) | 2 | not configurable | **2** (attempt 2 appends `RETRY_NUDGE`) | `distiller.py:46`, `50-57`, `128-136` |
| `MIN_USABLE_SEGMENT_TOKENS` | 500 | not configurable | **500** — an override below this raises `CapabilityError` | `capability.py:29`, `84-89` |
| `HEAD_TAIL_LINES` (tool_result truncation) | 15 | not configurable | **15 head + 15 tail** | `compaction.py:130`, `213-231` |
| `MAX_SURVIVOR_LINES` (buried error lines kept) | `= HEAD_TAIL_LINES` | not configurable | **15**, uncleared-ranked before cleared | `compaction.py:166`, `226-228` |
| `THINKING_LINES` | 2 | not configurable | **2** | `compaction.py:131`, `238-245` |
| `TRIVIAL_RESULT_MAX_CHARS` | 200 | not configurable | **200** (read-only result collapses to `""`) | `compaction.py:139`, `189`, `261` |
| `SUBSTANTIAL_PROSE_CHARS` (triage skip gate) | 300 | not configurable | **300** | `triage.py:39`, `139`, `144` |
| `provider.timeout_s` (model call) | 300.0 | 300.0 | **300.0s** | `config/synapse.toml:135`; `config.py:117`; `cli.py:155` |
| `temperature` | 0.0 | 0.0 | **0.0** — why a byte-identical retry cannot help, hence the nudge | `config/synapse.toml:134`; `distiller.py:130-136` |
| `distil_kinds` | `("text",)` via `DEFAULT_KINDS` | `["text"]` | **text only** — non-text-only segments hit the `skipped_empty` short-circuit | `config/synapse.toml:53`; `distiller.py:116-123` |
| `render_style` | `"labelled"` | `"labelled"` | **labelled** | `config/synapse.toml:71` |

### Three things worth flagging

- **The arithmetic the 7418a63 clamp closed is exactly at the edge.** A full-budget prompt is `2787 + 809 + 500 = 4096` — precisely `usable_context`, zero slack. The pre-clamp `900` gave `4496` against a 4096 ceiling (`cli.py:137-141`, `test_config.py:56-62`). Any increase in `v4-condense`'s calibrated overhead without a matching drop in `segment_budget` overruns immediately, and nothing validates that pairing at startup because the pin `2787` is exactly `from_context`, so `capability.py:74-83` accepts it silently.
- **The clamp is applied on the NPU branch only.** `cli.py:143` sits *after* the `anthropic` (`cli.py:122-125`) and `claude-cli` (`cli.py:130-133`) early returns. The comment at `cli.py:141-142` says this is deliberate to keep cloud arms untouched, but note the clamp would compute `min(900, 500) = 500` for those arms too, since `config.model` stays the NPU model regardless of `SYNAPSE_DISTILLER` — the cloud arms are protected by the branch order, not by their own capability records.
- **The oversized-event pre-split is size-based only.** `_split_oversized` (`segmenter.py:47-80`) cuts on `budget_tokens × 3.5` chars using the same over-counting estimator, so a segment can still exceed the real token budget if actual tokenization runs denser than 3.5 chars/token; the `finish_reason == "length"` log (`openai_compat.py:112-116`) and the `context_length_exceeded` 400-body salvage (`openai_compat.py:91-101`, `138+`) are the runtime backstops, not a pre-flight check.## 3. AI-100 configuration and the synthesis budget

The AIC100 arm is `SYNAPSE_SYNTHESIZER=aic100` → `AIC100Provider()` built with **no arguments** (`packages/service/src/synapse_service/cli.py:18-20`), so every number below comes from the provider's own constructor defaults or its `INFERENCE_CLOUD_*` env overrides — **not** from `config/synapse.toml`. Nothing in the service or provider packages imports `synapse_distiller.config` / `capability.py`, so the `[capability."Llama-3.3-70B"]` record is documentation for a human, not an input to code (verified: `response_reserve` is read only at `packages/distiller/src/synapse_distiller/config.py:164,190,225` and `capability.py:69,79`).

**Prompt (input) construction.** One merge call, two messages (`packages/service/src/synapse_service/synthesis.py:348-354`): system = `synth_system(budget.working_memory_words, budget.max_merges)` (`synthesis.py:145-175`), user = `PURPOSE: …` + `CURRENT WORKING MEMORY:` + `FINDINGS:` + the candidate listing. Candidates = **all** findings in this push (unbounded) + up to `CANDIDATE_WINDOW = 20` ranked "other" findings (`synthesis.py:34, 274-315, 335`); one line each, `[id] (type) text  (shares: …)` (`synthesis.py:339-347`). `AIC100Provider` then flattens both messages into a single `/completions` prompt and appends an **example instance** of the schema (never the schema itself) plus `"\nJSON:"` (`aic100.py:229-232`, rationale at `aic100.py:119-136`). Retry prompts re-use `base_prompt` + either a "much shorter" shrink instruction (truncation, `aic100.py:279-285`) or the bad text truncated to 500 chars (`aic100.py:287-290`).

**The derivation (ADR 0005, as implemented).** `SynthesisBudget.derive(output_tokens)` at `synthesis.py:110-135`:

```
spare          = output_tokens − JSON_ENVELOPE_TOKENS(40) − MIN_VERDICT_TOKENS(300)
words          = min(MAX_WM_WORDS(500), int(spare / TOKENS_PER_WORD(1.7)))
if words < MIN_WM_WORDS(120): raise SynthesisBudgetError
verdict_tokens = output_tokens − 40 − int(words × 1.7)
max_merges     = max(1, verdict_tokens // TOKENS_PER_VERDICT_ENTRY(70))
```

Verdict room is reserved **first**; the working-memory word cap is what is left, capped at 500. The derived `words`/`max_merges` are then *stated into the prompt* by `synth_system()` — the cap is never typed as a literal (`synthesis.py:145-175`, pinned by `packages/service/tests/test_synthesis_budget.py:60-67`). Input is `output_tokens = int(getattr(provider, "max_tokens", DEFAULT_OUTPUT_TOKENS=800))` (`synthesis.py:137-142`), read through `RecordingProvider`'s explicit forwarding property (`packages/providers/src/synapse_providers/recording.py:88-121`).

Verified by execution: `derive(800) → 270 w / 301 tok / 4 merges`; `derive(1600) → 500 / 710 / 10`; `derive(3000) → 500 / 2110 / 30`; `derive(543)` raises, `derive(544)` is the smallest workable cap (120 words).

| parameter | value | where set | notes |
|---|---|---|---|
| synthesizer selection | `SYNAPSE_SYNTHESIZER=aic100` → `AIC100Provider()`, no args | `packages/service/src/synapse_service/cli.py:16-20` | Default is `fake`. No `claude-cli` mode exists on this switch. |
| endpoint / model | `https://aisuite-indonesia.cirrascale.com/apis/v2`, `Llama-3.3-70B` | `packages/providers/src/synapse_providers/aic100.py:34, 151, 155-159` | `INFERENCE_CLOUD_BASE_URL` / `INFERENCE_CLOUD_MODEL`. Schema calls go to `POST /completions`, not `/chat/completions` (`aic100.py:216-245`). |
| **output cap (`max_tokens`)** | **800** default; **1600** under `serve_local.py`; endpoint accepts 3000 | `aic100.py:152, 187` (`INFERENCE_CLOUD_MAX_TOKENS`); `scripts/serve_local.py:50, 344` | **The single input to the whole derivation.** 800 was never a host limit — a bound on the shared credit pool (`aic100.py:175-178`). |
| client timeout | 60.0 s default; **180 s** under `serve_local.py` | `aic100.py:153, 188` (`INFERENCE_CLOUD_TIMEOUT`); `scripts/serve_local.py:51, 345` | Must be raised *with* `max_tokens`; a `ReadTimeout` is swallowed by `synthesis.py:363-366` and looks identical to truncation. |
| `TOKENS_PER_WORD` | 1.7 | `synthesis.py:68` | Measured off the live memory that broke: 3065 chars / 477 words ≈ 766 tok ⇒ 1.61, rounded **up** so the estimate errs toward refusing. |
| `JSON_ENVELOPE_TOKENS` | 40 | `synthesis.py:71` | The four keys + braces + quoting, content-independent. |
| `MIN_VERDICT_TOKENS` | 300 | `synthesis.py:78` | Reserved before the memory gets anything. |
| `TOKENS_PER_VERDICT_ENTRY` | 70 | `synthesis.py:75` | 2 source_ids (~12 each) + ~40 tok merged text + type; conflicts cost about the same. |
| `MAX_WM_WORDS` / `MIN_WM_WORDS` | 500 / 120 | `synthesis.py:82, 85` | 500 is a product ceiling (surplus goes to verdicts); below 120 → `SynthesisBudgetError` (`synthesis.py:92-93, 121-127`), the mirror of `capability.py`'s `MIN_USABLE_SEGMENT_TOKENS`. |
| `DEFAULT_OUTPUT_TOKENS` | 800 | `synthesis.py:89` | Used when a provider has no `max_tokens` (e.g. `FakeProvider`), keeping the existing suite on the shipped budget. |
| derived split @ 800 | 270 words / 301 verdict tok / 4 merges | derived, `synthesis.py:110-135` | The bug as a number: prompt used to demand 500 words the cap could not pay for. |
| derived split @ 1600 | **500 words / 710 verdict tok / 10 merges** | derived; 1600 supplied by `scripts/serve_local.py:50` | The adopted operating point — restores the original 500-word intent with real verdict room. |
| derived split @ 3000 | 500 / 2110 / 30 | derived | Probed-accepted host ceiling; unused, credit-pool bound. |
| refusal threshold | `derive(543)` raises; `derive(544)` = 120 words | derived | Verified by execution. |
| context window (input) | **128 000, advertised — not measured, and never enforced in code** | `config/synapse.toml:192-196` (`[capability."Llama-3.3-70B"]`) | `/models` on this host returns names only. **No code reads this record** — the AIC100 path has no input-token check at all. Largest failing synthesis used 2110 input tokens (1.6%); input climbed 2110 → 2413 → 2924 as the memory grew (`docs/adr/0005-…:30`). |
| `response_reserve` = 1600 | documentation only for this arm | `config/synapse.toml:195-196` | Comment says "THE number this record exists for — see synthesis.py", but the wiring is via `INFERENCE_CLOUD_MAX_TOKENS`; the TOML value is inert. Drift risk if someone edits one and not the other. |
| input bounding | `CANDIDATE_WINDOW = 20` (others only) + the full current push | `synthesis.py:34, 274-315, 335` | Deliberately **not** trimmed to fit the output budget — ADR 0005 §6 / rejected alternative; a candidate is an input cost that only becomes an output cost when it merges. |
| truncation detection | `finish_reason == "length"` OR `completion_tokens >= max_tokens` | `aic100.py:293-318` | The host returns `finish_reason: "stop"` on a response cut off at exactly the cap — the usage-based test is the one that actually fires. |
| retries | exactly 2 attempts, temp 0.0 then 0.2 | `aic100.py:235-245` | Truncation retry shrinks the ask; schema retry echoes ≤500 chars of the bad text. |
| rate governor (spend) | 25 000 tok/hour, 20 req/hour, × `SYNAPSE_SYNTHESIS_KEYS` (default 1) | `packages/service/src/synapse_service/api.py:80-82` | Prices the next round at the **max of the last five** rounds, not the mean (`api.py:239-260`). Charges failed/truncated rounds too. |
| assumed round cost | 4 000 tokens | `api.py:86` | Used until a real round reports usage; also the charge when a call raised. |
| latency floor | `MERGE_MIN_INTERVAL_S = 60` (`SYNAPSE_MERGE_MIN_INTERVAL_S`) | `api.py:48` | Explicitly *not* the rate limit; rehearsal sets it to 0 (`scripts/rehearse_demo.py:186`). |

**Discrepancy worth flagging (new):** `scripts/demo_local.py:442-450` starts the service with `SYNAPSE_SYNTHESIZER=aic100` and sets `INFERENCE_CLOUD_BASE_URL` / `_API_KEY` / `_MODEL` but **not** `INFERENCE_CLOUD_MAX_TOKENS` or `INFERENCE_CLOUD_TIMEOUT` (grep for `MAX_TOKENS|TIMEOUT` over `demo_local.py`, `rehearse_demo.py`, `_rehearsal_service.py` returns nothing). So the `demo_local.py` entry point still runs the pre-ADR-0005 numbers: 800-token cap → a 270-word / 4-merge prompt, on a 60 s timeout. Only `scripts/serve_local.py:344-345` carries the fix. Anything measured or demoed through `demo_local.py --live` is running the budget the ADR was written to retire.

### For W6: is any Anthropic Haiku arm pinned to a 4K context/budget?

**No. Nothing anywhere caps a Haiku arm to 4K — context or output.** Concretely:

- `AnthropicProvider.__init__` takes `max_tokens: int = DEFAULT_MAX_TOKENS` = **16000**, with no per-model table and no env override (`packages/providers/src/synapse_providers/anthropic_provider.py:84-89, 164-204`). The model id is chosen independently via `SYNAPSE_ANTHROPIC_MODEL` (`anthropic_provider.py:195`), so `claude-haiku-4-5-20251001` gets Opus 5's 16000 cap verbatim.
- Both distiller call sites construct it with **no arguments**, bypassing even the NPU arm's clamp: `packages/worker/src/synapse_worker/cli.py:122-125` and `packages/orchestrator/src/synapse_orchestrator/cli.py:134-136`. `config.effective_max_tokens` (= `min(provider.max_tokens, record.response_reserve)`, `packages/distiller/src/synapse_distiller/config.py:149-164`) is applied **only** on the `NPUProvider` branch (`worker/cli.py:143-155`, `orchestrator/cli.py:151-155`).
- `config/synapse.toml` has `[capability."claude-opus-5"]` (1M / 16000, lines 198-202) and `[capability."claude-cli-sonnet"]` (200000 / 4096, lines 204-208) — **no Haiku record at all**. Segmentation would not use one anyway: `[distiller] segment_budget = 2787` is pinned (`config/synapse.toml:96`) and the override wins over any record.
- If synthesis were pointed at Haiku (`SYNAPSE_SYNTHESIZER=anthropic`, `cli.py:37-39`), `SynthesisBudget.derive(16000)` yields 500 words / 15110 verdict tokens / 215 merges — no clamp, no refusal.
- The only `4096` in reach is `[capability."claude-cli-sonnet"].response_reserve` (`config/synapse.toml:207`), explicitly "a budgeting figure only" for the CLI arm, and it is inert: `ClaudeCliProvider` accepts `max_tokens` and ignores it (`packages/providers/src/synapse_providers/claude_cli_provider.py:92-103`).

**Where such a cap would belong.** Split by which limit is meant:
- **Output budget (max_tokens) → provider**, matching `AIC100Provider`: a per-model default plus an env override on `AnthropicProvider`, so `SynthesisBudget.for_provider` and `synth_system` re-derive the word cap automatically. Setting it in config would not reach synthesis at all — the service reads `provider.max_tokens`, never a capability record.
- **Context/segmentation → config**, as a `[capability."claude-haiku-…"]` record with `usable_context` / `response_reserve`, which is what `config.record` / `effective_max_tokens` / `segment_budget` already consume — but note (a) `[distiller] segment_budget = 2787` currently overrides every record, and (b) the Anthropic branch never calls `effective_max_tokens`, so a config-only cap would be silently ignored on today's code path. A minimal honest fix is `AnthropicProvider(max_tokens=config.effective_max_tokens)` at `worker/cli.py:125` and `orchestrator/cli.py:136`, plus the missing Haiku record — otherwise `config.record` raises `CapabilityError` only for the model named in `[distiller] model`, which the Anthropic arm does not change.