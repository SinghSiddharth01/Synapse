# Plan A — Capture

**Track:** local, deterministic. **No LLM, no hardware, no network** beyond a mock HTTP server.
**Suggested owner:** Akhil (ownership provisional — see the working notes).
**Depends on:** Plan 0 (contracts, fixtures).

**Goal:** turn whatever a coding agent writes to disk into `Segment`s, with zero modification to the agent, and hand distilled findings to the orchestrator.

**Why this track is safe to parallelise:** every stage is deterministic plumbing testable against committed fixtures. Nothing here waits on a model or a device.

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

## Task A.6 — Producer client + durable log

POSTs `Finding[]` to the orchestrator's producer endpoint. Worker-initiated — the worker owns the transcript, so it owns the trigger, and passivity is preserved end to end.

**Findings are persisted the moment the distiller returns, before any send is attempted.** Distillation is the most expensive step in the system (~14 tok/s at 4B) and is **unrepeatable** — the worker never re-reads a transcript position, so a dropped finding is NPU work permanently lost. Write-ahead, not fallback buffering.

- POSTs `Finding[]` and nothing else (the egress rule; see Plan D)
- appends to a durable local log on produce; marks sent on success; replays unsent on restart
- retains after send so the log can be resynced
- **`Finding.id` is stamped at distil time**, so replay is idempotent at the far end with no dedup logic

**First failing tests:** a finding is durable on disk *before* the first POST; unsent findings replay after a worker restart; a replayed POST carries identical ids; findings survive the orchestrator being down and drain when it returns; sends never block distillation.

## Task A.7 — Worker orchestration + CLI

Wires detection → follow → adapt → segment → compact → distil → POST, plus a minimal CLI (`synapse join <shared_id>`, status, run).

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
