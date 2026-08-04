# Implementation report — 2026-08-04

**First code in the repository.** `docs/STATE.md` as of 2026-08-03 read *"Design and documentation only. No code exists yet."* That is no longer true.

**Verified on:** Snapdragon X Elite X1E80100 · Hexagon NPU v73 · Windows 11 Pro · GenieX CLI v0.3.18 (QAIRT v2.45.0.260326) · `qualcomm/Qwen3-4B-Instruct-2507:W4A16`

**Status:** 149 tests green, offline, no hardware or network required. The end-to-end loop has been run against a real Claude Code transcript on the NPU.

---

## TL;DR

1. **The capture loop works end to end.** A live agent transcript is followed from a durable offset, the delta is segmented on turn boundaries, condensed on the Hexagon NPU, written to a write-ahead log, and pushed upstream. Verified on real Claude Code data, not fixtures.
2. **The distiller was reframed from judge to compressor** — see `adr/0003`. A 4B model was reversing facts stated twice in its own prompt while composing findings; it stops when asked only to restate.
3. **Three blockers on the earlier evidence cleared.** AI Hub is back up, the `qairt` NPU-exclusive path is live, and the QAIRT context ceiling is now a measured number rather than an assumption.
4. **The privacy property is currently unverified.** The metric that is supposed to prove it cannot see the leaks that actually occur.

---

## Part 1 — What was built

Four packages, in a `uv` workspace pinned to a **native ARM64** interpreter.

### `synapse_contracts`

The frozen cross-track types, copied verbatim from `brainstorming/2026-07-25-plan-0-foundation.md` Task 2 as Plan 0 requires.

> **One deliberate divergence.** That document's `__init__.py` exports `Attribution`, `FindingId`, `FindingStatus` and `Provenance` in `__all__` but omits them from the import list — `from synapse_contracts import Attribution` raises `ImportError`. Fixed, with a comment at the divergence. The schemas themselves are unmodified.

### `synapse_providers`

`ModelProvider` · `ProviderCapabilities` · `FakeProvider` · `OpenAICompatibleProvider` · `NPUProvider`.

`NPUProvider` is the thin subclass Plan B Task B.4 predicted: `geniex serve` is OpenAI-shaped and the standard body works unchanged.

### `synapse_distiller`

`guards` · `prompt` · `promptpack` · `distiller` · `capability` · `config` · `fixtures` · `evaluation`.

Prompts are **versioned TOML packs**, not module constants, so they can be A/B'd without a code change. Four ship: `v1-baseline` (frozen, contaminated — see Part 4), `v2-hardened`, `v3-text`, and `v4-condense` (default).

### `synapse_worker`

`sources/claude_code` · `follower` · `segmenter` · `producer` · `loop` · `discovery` · `cli`.

```
uv run synapse-worker status              # detect live transcripts, show queue depth
uv run synapse-worker run --interval 15   # follow and condense periodically
uv run synapse-worker replay              # drain undelivered findings
```

---

## Part 2 — The capture loop

```
transcript (.jsonl, written by the agent)
   │  stat-gate: size unchanged -> one syscall, no work
   ▼
follower ──── durable byte offset ────────────► delta only, complete lines only
   ▼
ClaudeCodeSource ──► AgentEvent[]
   ▼
Segmenter ─── turn boundary + token budget ───► holds the OPEN turn back
   ▼
Distiller (NPU) ──► Finding[]
   ▼
write-ahead log ──► push upstream (FileSink | HttpSink)
```

### Three properties that are load-bearing

**The open turn is held back.** A timer fires whenever it fires. At one instant `seg-001`'s transcript contains only *"I'll add pgbouncer in transaction pooling mode"* — condensing that yields a finding saying transaction pooling was chosen, the opposite of what happened, because the reversal had not been written yet. The segmenter emits a turn only once the next one begins, with an idle flush so the final turn is not stranded when the developer stops typing.

**Crash ordering fails toward duplication, never loss.** Findings reach the write-ahead log *before* any send is attempted; the offset and the pending-turn buffer are persisted together, *after* that. A crash costs repeated NPU work. It never loses conversation. The pending buffer must be persisted with the offset — without it, the offset advances past events still sitting in the segmenter, which is silent loss.

**Ticks never overlap.** `run()` awaits a full tick before sleeping, so the interval is a *gap between* ticks, not a fixed period. A 60 s tick on a 30 s interval next runs at t≈90 s. Natural backpressure, no queue growth, and no concurrent NPU calls contending for the single resident model.

### Why polling rather than a filesystem watch

Considered and rejected for now:

- **`watchdog` / `ReadDirectoryChangesW`** — replaces only the *trigger*; offset-based reads are unchanged. Windows coalesces events under heavy writes. Plan A already lists it as an explicit stretch.
- **Claude Code hooks** — semantically ideal, fires exactly at turn boundaries, but breaks Plan A's "zero modification to the agent" goal and is Claude-Code-specific, which kills the agent-agnostic story.
- **Cheap polling** — chosen. `os.stat()` gates every tick, so an idle tick is one syscall. Latency barely matters: findings are not user-facing in real time and one segment takes ~10 s regardless.

The real problem was never the trigger. It was the turn boundary.

### End-to-end run, real data

```
tick 1 — 10 lines · 2 events · 0 segments · 0 findings · 2 events held (turn open)
shutdown — 1 segments · 1 findings · 1 sent · idle-flushed
```

```json
{ "id": "7a97f5cd-…", "type": "learning",
  "text": "The API has reached its specified usage limits and will be available again on 2026-09-01 at 00:00 UTC.",
  "attributions": [{"contributor": "aditya",
                    "agent_session": "52c17ea4-d585-4926-82ac-8abfa1dc5c6a",
                    "agent": "claude-code"}],
  "provenance": "distilled", "status": "kept", "merged_into": null }
```

Re-running produced `tick 1 — no change`, zero NPU calls, upstream unchanged.

> That finding is **trivia**. Under `adr/0003` that is correct distiller behaviour and useless output — filtering is triage's job, and triage does not exist yet.

---

## Part 3 — Measurements

All on the hardware named above, 2026-08-04.

### Three earlier blockers cleared

| 2026-07-30 | 2026-08-04 |
|---|---|
| AI Hub returns HTTP 503 | `aihub`, `app.aihub`, `workbench.aihub` all **200** |
| *"`qairt` path blocked, untestable"* | `qualcomm/Qwen3-4B-Instruct-2507:W4A16` cached and **running on the NPU** |
| `llama_cpp` GGUF is the only working path | QAIRT is the default arm; llama.cpp is now the untested alternative |

### QAIRT context ceiling — measured

`prompt_tokens=3809` succeeded; ~7000 returned **HTTP 400**. Usable context is **4096**, and on the `qairt` path it is a hard ceiling — precision, context length and KV cache are baked in at compile time, and there is no `--nctx`. Plan B Task B.5 predicted this exactly (*"qairt bundles are often 4K regardless of what the model claims on paper"*); it is now confirmed rather than assumed.

### Prompt overhead — calibrated against the model's own tokenizer

`scripts/calibrate_prompt.py` sends a pack with an empty segment and reads `prompt_tokens` back.

| Pack | chars/4 estimate | **measured** | drift |
|---|---|---|---|
| `v1-baseline` | 615 | **497** | −118 |
| `v2-hardened` | 878 | **719** | −159 |
| `v3-text` | 850 | **658** | −192 |
| `v4-condense` | 1031 | **809** | −222 |

The estimate over-counts consistently — safe (budgets come out smaller) but it was leaving ~200 tokens unused on a 4096-token bundle.

**Segment budget is derived, never configured:** `usable_context − prompt_overhead − response_reserve`, clamped by prefill rate × latency budget. Editing a prompt re-budgets segmentation automatically. Currently **2787** tokens.

### Structured output — a negative result

Plan B Task B.4 asked whether GenieX's `--enable-json` / `--grammar-path` flags reach `serve`'s HTTP API. **They do not, and the way they fail is a trap.**

Three requests, identical prompt: (A) no structured-output field, (B) `response_format: {"type":"json_object"}`, (C) `enable_json: true` — a control, not a real OpenAI field. **All three returned HTTP 200 with byte-identical output.** The server accepts unknown top-level parameters silently, so a successful response proves nothing. Request C is the proof: a server honouring B had no reason to accept C.

`native_structured_output = False`, and `NPUProvider` deliberately does not send `response_format` — sending it would produce a request that looks enforced and is not. This reverses the optimistic reading in `brainstorming/2026-07-30-…` Part 5.

### Throughput

Decode **12–14 tok/s** at 4B on the NPU, consistent with the 14.2 tok/s measured 2026-07-30. Prefill is still not cleanly measured.

### Render style — measured, then reverted

| | prompt tokens | `seg-001` fidelity |
|---|---|---|
| `content` (labels stripped) | 1661 | **inverted** |
| `labelled` | 1673 | correct |

Stripping role labels saves **12 tokens (0.7%)** — role labels are ~3 tokens each — and in that run flipped a fact. `labelled` is the default. Labels are `developer:` / `agent:` rather than `user:` / `assistant:`, because the segment rides inside a `role=user` message with literal `user`/`assistant` few-shots above it, and `CONTEXT.md` lists both words under *_Avoid_*.

---

## Part 4 — Two process failures worth recording

Both were mine, both survived a full measurement cycle, and both are the kind that produce confident wrong numbers rather than errors.

### A contaminated few-shot

`v1-baseline`'s second few-shot was a near-duplicate of fixture `seg-004` — both ruff import-sorting, both with the identical `Found 3 errors (3 fixed, 0 remaining)` tool result. I wrote the prompt and the eval target, and they converged.

Every `seg-004` score from that pack measured pattern-matching against the nearest few-shot, not generalization. **A result I had already reported as "the hardening fixed it" was void** — with a structurally different noise example, the model reverted to inventing.

This is precisely the failure Plan 0 Task 0.3 warns about: *"Written by one person, they become that person's opinion."* Packs now declare `contaminated_fixtures`, and the harness prints `VOID` and excludes the score rather than reporting an inflated one.

### A misattributed cause

I attributed the `seg-001` factual inversion to `v3-text`'s prose-only few-shots and wrote that into two config files. Isolating render style later showed `v2-hardened` produces the same inversion under `render_style = "content"`. No single setting causes it — the fact is fragile across perturbations generally. Both files were corrected.

---

## Part 5 — Gaps

Ordered by what they would cost.

### 1. Triage does not exist — and is now load-bearing

Under `adr/0003` the distiller no longer judges durability. **Nothing does.** Trivia flows to the sink today, demonstrated by the first real run condensing an API rate-limit notice.

Sits in the worker between compaction and distillation. No contract change. Keys on errors, non-zero exits, `thinking` blocks, compaction summaries, and decision language (*"instead"*, *"rather than"*, *"dead end"*, *"switching to"*); skips read-only runs that all succeeded and lint/format with nothing remaining.

> **Design constraint.** False negatives are unrecoverable and silent — the follower never re-reads a position, so a wrongly-skipped segment is knowledge permanently gone with nothing in the log to notice. False positives merely cost NPU time. **Tune for recall, and log every skip with its reason** or the false-negative rate cannot be measured at all.

### 2. The privacy property is unverified

`verbatim_overlap` uses 8-word n-grams. A single leaked token never fills that window.

| Leaked into a finding | Metric reported |
|---|---|
| `default_pool_size=25` | **0.00** |
| `dates.py`, `strings.py` | 0.10 |

The harness prints a number that reads as "fully abstracted" while identifiers leak. **Reporting it today would be a false claim about the property the architecture rests on.**

Needs an identifier-shaped-token check (`snake_case`, `dotted.host`, `CamelCase`, paths, `file.ext`). The hard part is that not every identifier is private — `pgbouncer`, `asyncpg` and `ruff` are public vocabulary the goldens use deliberately.

### 3. The corpus is two fixtures, solo-authored

Plan 0 requires five. `seg-002`, `seg-003` (oversized `tool_result`) and `seg-005` (near-duplicates across Contributors) do not exist. Both existing fixtures were written by one person, and one was contaminated.

Every conclusion in Part 3 rests on n=2. It cannot distinguish *"v4 fixed the prompt"* from *"v4 fits `seg-001`"*, and it cannot produce a triage recall rate.

The cases that would actually test things: **insight with no error** (a decision reached conversationally, nothing for a keyword filter to catch), **an error that is not insight** (a typo, immediately fixed), **a `dead_end` whose pivot lands in the next segment**, and **noise that superficially looks like signal**.

Also: `seg-004`'s golden empty array now encodes a triage expectation, not a distiller one.

### 4. Numbers still carried but not measured

- **`prefill_toks_per_sec = 250.0` is a guess.** Marked PROVISIONAL in code, but it feeds the segment-budget derivation, so a wrong value mis-sizes segments.
- **Power is unmeasured.** Unchanged since 2026-07-30. The NPU placement rests on a contention-and-power argument, and the power half has no number. **Do not claim efficiency.**

### 5. Structural pieces absent

- **No orchestrator.** `FileSink` is a stand-in for Plan 0 Task 0.5. `HttpSink` exists and is untested against a real endpoint. The egress rule is enforced at the producer — only `Finding` objects reach a sink — but there is nothing on the other end.
- **No Codex adapter.** The agent-agnostic claim is asserted, not shown. Plan A calls this demo-path, not stretch.
- **No compaction** (Plan A.5). `thinking` blocks and oversized `tool_result`s pass through whole. Currently masked because `distil_kinds = ["text"]`.
- **Sidechains are skipped.** Subagent conversations are real agent work, excluded to avoid attributing a subagent's findings to the parent Agent Session. Counted, not silently dropped.
- **`Finding.type` from the distiller is best-effort.** It labelled *"No other changes were made"* a `decision`. Harmless if synthesis re-types; a cross-track conversation if anything downstream trusts it.

### 6. Unresolved model behaviour

The factual inversion is fixed **on n=1** by `v4-condense`'s fidelity rule. There is no evidence it holds across other facts, and the failure mode — a prior beating explicit context — is not one a prompt can be assumed to close. It passes every existing guard.

---

## Reproduction

```powershell
uv sync --python "$env:LOCALAPPDATA\Programs\Python\Python312-arm64\python.exe"
uv run pytest -q                              # 149 tests, offline

geniex serve                                  # terminal 1
uv run python scripts/run_npu_eval.py         # corpus + metrics
uv run python scripts/trace_one.py seg-001    # events -> prompt -> raw output -> findings
uv run python scripts/calibrate_prompt.py v4-condense
uv run synapse-worker status
```

> **`uv`'s managed interpreter is x86_64.** A bare `uv venv` builds the emulated Prism venv that `brainstorming/2026-07-30-…` Part 7 warns about, where NPU wheels cannot install. Pin `--python` at the ARM64 executable; there is an assertion on `platform.machine()`.
