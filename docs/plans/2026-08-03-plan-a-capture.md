# Plan A — Capture

**Track:** local, deterministic. **No LLM, no hardware, no network** beyond a mock HTTP server.
**Suggested owner:** Akhil (ownership provisional — see the working notes).
**Depends on:** Plan 0 (contracts, fixtures).

**Goal:** turn whatever a coding agent writes to disk into `Segment`s, with zero modification to the agent, and hand distilled findings to the orchestrator.

**Why this track is safe to parallelise:** every stage is deterministic plumbing testable against committed fixtures. Nothing here waits on a model or a device.

> **⟨STATUS 2026-08-04⟩** Most of this track is **built and running end to end on real Claude Code data** — see `docs/2026-08-04-implementation-report.md`. Built: detection, follower with durable offset, `ClaudeCodeSource`, segmenter, producer with write-ahead log, worker loop, CLI (`join`/`run`/`status`/`replay`). **Not built: A.5 compaction, and A.5b triage — which is new and now load-bearing.** `CodexSource` is also still missing, and it is demo-path.
>
> Three properties the implementation established that this plan did not anticipate, all worth preserving:
> - **The open turn is held back.** A timer fires whenever it fires; at one instant `seg-001`'s transcript contains only *"I'll add pgbouncer in transaction pooling mode"* and condensing it yields the opposite of what happened, because the reversal had not been written yet. The segmenter emits a turn only once the next begins, with an idle flush so the final turn is not stranded.
> - **Crash ordering fails toward duplication, never loss.** Findings hit the write-ahead log *before* any send; the offset and the pending-turn buffer are persisted together, *after*. The pending buffer must be persisted with the offset — otherwise the offset advances past events still held in the segmenter, which is silent loss.
> - **Ticks never overlap.** `run()` awaits a full tick before sleeping, so the interval is a gap *between* ticks. Natural backpressure, and no concurrent NPU calls contending for the single resident model.

---

## Task A.1 — Agent registry and detection

The worker is never *configured* for an agent; it **detects** one.

- A static registry: agent name → transcript root(s) → transcript dialect → `Source` class.
  - `claude-code` → `~/.claude/projects/<slug>/*.jsonl`
  - `codex` → its session dir (**confirm the real path early** — it is on the demo path)
- Watch registered roots; a recently-growing transcript means an active Agent Session.
- Several agents active at once → several follower+adapter pairs. Everything downstream of the adapter is agent-blind.

**First failing tests:** a fixture tree containing a Claude Code transcript yields one detected Agent Session tagged `claude-code`; two agents' trees yield two; an unregistered directory yields none; a stale (non-growing) transcript is not treated as active.

## Task A.2 — File follower

Tails a live transcript being written by someone else's process.

**First failing tests:** appended lines are emitted in order; a partial trailing line waits for its newline rather than being parsed; malformed JSON is logged and skipped, never fatal; rotation (inode change or shrink) is detected and following resumes.

## Task A.3 — Source adapters

`ClaudeCodeSource` and `CodexSource`, both normalising into `AgentEvent`.

- Claude Code: `message.content` is a list of typed parts (`text` / `thinking` / `tool_use` / `tool_result`); unknown part types are logged and skipped.
- Compaction summaries the agent writes are ingested as events — they are high-density signal, not noise.
- `AgentEvent.agent_session_id` is the agent's own conversation id.

**First failing tests:** the real fixture transcript produces the expected `AgentEvent[]`; an unknown content-block type is skipped without raising; both adapters produce the *same* `AgentEvent` shape from equivalent input.

**Note:** `CodexSource` is on the demo path, not a stretch goal. The recorded demo shows a finding crossing between agents.

## Task A.4 — Segmenter

Splits on turn boundary **and** token budget.

- The budget is **read from the resolved distiller's capability record** (Plan B) — never a hard-coded constant. Swapping models re-budgets automatically.
- A turn exceeding budget becomes several sub-segments. No local merging: each sub-segment emits its own findings and dedup is synthesis's job.
- Optional session header, hard-capped ~200 tokens: Shared Session purpose + last 3–5 findings. The only device-side memory allowed.

**First failing tests:** an event run splits on turn boundary; **the frozen fixture Segments are reproduced exactly**; an empty turn yields no segment; a turn over budget yields several sub-segments, none exceeding it; changing the capability record changes the split.

## Task A.5 — Compaction

Deterministic code, not a model. Runs before prompt rendering.

- head/tail-truncate `tool_result` (first/last ~15 lines — errors and conclusions live at the edges)
- trim or drop `thinking` blocks
- skip trivial tool calls entirely
- strip binary/base64
- weight retained content toward assistant text (it is already frontier-authored distillation of the agent's own work)

**First failing tests:** `seg-003` (oversized `tool_result`) compacts within budget **and the error line survives**; binary content is stripped; a trivial call is dropped; compaction is idempotent.

## Task A.5b — Triage ⟨NEW 2026-08-04, load-bearing⟩

**Status: does not exist. Nothing filters triviality today, at either end.**

`adr/0003` removed durability judgment from the distiller — measured, a 4B invented a finding from an all-noise segment in **6 of 6** prompt configurations. That judgment moved to two places: **triage here** (deterministic code, upstream) and synthesis downstream. Triage was never built, so trivia flows straight to the sink: the first real end-to-end run condensed an API rate-limit notice into a `learning`.

Deterministic code in the worker, between compaction and distillation. Decides whether a segment reaches the NPU at all. No contract change.

- **Keep on:** errors, non-zero exits, `thinking` blocks, compaction summaries, decision language (*"instead"*, *"rather than"*, *"dead end"*, *"switching to"*).
- **Skip:** read-only runs that all succeeded; lint/format with nothing remaining.

> **Tune for recall.** A false positive costs NPU time. A false negative is knowledge permanently lost *and silent* — the follower never re-reads a position. **Log every skip with its reason**, or the false-negative rate cannot be measured at all.

> **Triage must read the full Segment, not the distiller's filtered view.** `distil_kinds` defaults to `["text"]` and the filter is applied at *render* time inside the distiller, so the `Segment` arriving here still carries every kind. That is load-bearing: triage keys on errors, exit codes and `thinking` blocks — precisely the kinds the distiller never sees. Filtering earlier "for efficiency" would blind triage to its own signals.

> **Record the byte range on every skip.** The report calls false negatives unrecoverable because the follower never re-reads. That is a property of the current implementation, not a physical one — the transcript is immutable and still on disk. If the skip log carries `(start_offset, end_offset, reason)`, a wrong skip becomes re-runnable via `replay --skipped` instead of permanent loss. Cheap now, impossible to retrofit once offsets are gone.

**First failing tests:** an all-noise segment (`seg-004`) is skipped and never reaches the model; a segment containing a non-zero exit is kept; a segment whose only signal is a `thinking` block is kept **even though `distil_kinds` excludes thinking**; every skip appears in the log with a reason and a byte range; recall is measurable from the log alone.

## Task A.6 — Producer client + durable log

POSTs `Finding[]` to the orchestrator's producer endpoint. Worker-initiated — the worker owns the transcript, so it owns the trigger, and passivity is preserved end to end.

**Findings are persisted the moment the distiller returns, before any send is attempted.** Distillation is the most expensive step in the system (~14 tok/s at 4B) and is **unrepeatable** — the worker never re-reads a transcript position, so a dropped finding is NPU work permanently lost. Write-ahead, not fallback buffering.

- POSTs `Finding[]` and nothing else (the egress rule; see Plan D)
- appends to a durable local log on produce; marks sent on success; replays unsent on restart
- retains after send so the log can be resynced
- **`Finding.id` is stamped at distil time**, so replay is idempotent at the far end with no dedup logic

**First failing tests:** a finding is durable on disk *before* the first POST; unsent findings replay after a worker restart; a replayed POST carries identical ids; findings survive the orchestrator being down and drain when it returns; sends never block distillation.

## Task A.7 — Worker orchestration + CLI ⟨BUILT⟩

Wires detection → follow → adapt → segment → compact → **triage** → distil → POST, plus the CLI: `join <shared_id>` · `run --interval` · `status` · `replay`.

**Session binding landed here, not in the orchestrator.** Plan D.2 said the orchestrator writes the binding; in practice it needs only detection (already here) and no MCP server has to be running for a developer to bind a session. `SessionBinding` lives in `synapse_contracts` so the read and write sides need no dependency on each other. Storage is keyed by Agent product (`bindings/claude-code.json`), so a second adapter needs no reshape. Revisit if Plan D.1's producer endpoint ever has to stamp Attribution from a binding it cannot otherwise see.

**Still missing from `join`:** Plan D.2's "register the Contributor with the service (`POST /members`)". No service exists; `cmd_join` logs the skip rather than omitting it silently.

**First failing test:** given a fixture tree and a `FakeProvider` distiller, running the worker POSTs the golden findings to a mock producer endpoint.

---

## Exit criteria

1. Fixture transcript → expected `AgentEvent[]`.
2. **Frozen fixture Segments reproduced exactly** — this is the anti-drift gate with Plan B.
3. Oversized and all-noise fixtures behave per their goldens.
4. Both adapters produce identical downstream shapes.
5. Everything green offline against `FakeProvider` and a mock HTTP server.

## Scope / YAGNI

**In:** registry + detection, follower, both Source adapters, segmenter with per-model budget, compaction, producer client, worker orchestration, minimal CLI.
**Out (stretch):** inotify/watchdog following (polling is fine at this scale); more than two agents in the registry; log compaction or retention limits.

## Risks

| Risk | Mitigation |
|---|---|
| Codex's on-disk format is unknown or unstable | Confirm on day 1 — it is on the demo path. If it slips, Claude Code alone still demos; the agent-agnostic claim degrades from shown to asserted |
| Real transcripts carry content-block types we do not handle | Log-and-skip is already the behaviour; widen the adapter as encountered |
| Segmenter disagrees with the frozen fixtures | The reproduction test catches it. Resolve as a co-authoring conversation with Plan B's owner, never a solo tweak |
| Budget depends on Plan B's capability record, which lands later | Ship against a provisional record with the measured numbers filled in later; the derivation must be tested, not the constant |
| Rotation heuristic misses an edge case | Documented behaviour; demo runs are short enough that a follower restart is acceptable |
