# E7 — Demo-Window Closeout Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Close the remaining implementable gaps before the Aug 7 demo: the Codex adapter (last demo-path item), the worker WAL re-join envelope (last privacy-class gap), compaction A.5 (the deferred recall story), and the Claude Code awareness pack. Three chains; the specs already exist in this repo — this plan is pointers, constraints, and acceptance, not re-specification.

**Baseline:** main at 567 green. That is the floor at every commit; the closed-loop test passes unmodified.
**Style:** these chains follow the existing code's conventions exactly — read the sibling module before writing a new one.

---

## Chain A — `CodexSource` + registry (worktree `exec/e7a`)

**Spec:** Plan A.1/A.3 (`docs/plans/2026-08-03-plan-a-capture.md`) · registry in `packages/worker/src/synapse_worker/discovery.py` · adapter sibling `sources/claude_code.py`.

1. **Research first, record findings.** Confirm Codex CLI's on-disk session format from primary sources (github.com/openai/codex — session/rollout files, expected under `~/.codex/sessions/`; confirm the actual layout and JSON shape from the source tree, not blog posts). Write what you find, with links, into the PR-style commit message and a `fixtures/raw_lines/codex/README.md`.
2. **Fixtures before code:** `fixtures/raw_lines/codex/` — a basic turn, a tool call + result, a malformed line; hand-authored to the confirmed shape.
3. **`CodexSource`** mirroring `ClaudeCodeSource`'s interface (`parse_line`), normalizing to `AgentEvent` with `agent_session_id` from Codex's own session id. **Parity test:** both adapters produce the same `AgentEvent` shape from equivalent input (Plan A.3's test, finally executable).
4. **Registry entry** in `discovery.py` (`codex` → roots + dialect + source class) + detection tests mirroring the existing claude-code ones. Binding storage already keys by product (`bindings/codex.json`) — exercise it with a two-product test.
5. If the real format cannot be confirmed with confidence, STOP after step 1 and report — a guessed adapter is worse than no adapter.

**Done when:** detection finds a Codex fixture tree; parity test green; suite ≥567.

## Chain B — WAL envelope, then compaction (worktree `exec/e7b`, sequential — both touch `loop.py`)

### B1 — WAL re-join envelope
**Spec:** STATE.md trap #8 · the relay's round-3 partitioning note (`packages/orchestrator/src/synapse_orchestrator/relay.py` docstring) — apply the same record-time principle to the worker's WAL (`packages/worker/src/synapse_worker/producer.py`).

- `Producer.record` captures the **current binding's `shared_id`** into each WAL line (envelope `{shared_id, finding}`; bare legacy lines read as `shared_id=None` = current, so existing WALs keep draining).
- `flush()` sends **only** findings whose recorded `shared_id` matches the current binding; others are **held**, never retargeted and never dropped. `pending_count()` splits into deliverable vs held; `synapse-worker status` and the debug page's PUSH node show `held (other session)` when nonzero.
- **The pinning test is the re-join scenario:** record under session A, re-join to B, flush → nothing sent; re-join back to A → drains. Plus: legacy bare-line WAL still drains.

### B2 — Compaction (A.5)
**Spec:** Plan A.5 verbatim (`docs/plans/2026-08-03-plan-a-capture.md`) — deterministic, before triage in `WorkerLoop.tick` (triage must see compacted-but-complete signal; A.5b's "reads the full Segment" note means compaction may truncate *within* events but never drop the kinds triage keys on — keep error lines by construction).

- New `packages/worker/src/synapse_worker/compaction.py`: head/tail-truncate `tool_result` (first/last 15 lines), strip binary/base64 runs, drop trivial tool calls (read-only tool_use with tiny ok results), trim `thinking` to first 2 lines. Idempotent. Pure function `compact(segment) -> Segment`.
- **seg-003 activates:** the plan-0 fixture's original assertion — budget respected AND the buried `ConnectionResetError` line survives compaction — becomes a real test.
- A `compaction` feed event on the dashboard (`render` tag's sibling): `events kept n/m · chars saved`. `distil_kinds` default stays `["text"]` — widening it is a *measured* decision for the NPU eval, record that in the commit message, do not change the default.

**Done when:** re-join scenario pinned; seg-003's buried error survives; suite ≥567; dashboards show held/compaction honestly.

## Chain C — Claude Code awareness pack (worktree `exec/e7d`)

**Spec:** Plan D.6 (`docs/plans/2026-08-03-plan-d-orchestrator.md`) tier table — the two Pack rows · amendment F Q11's tier split · `docs/architecture.html`'s awareness section (signals ③ and ④).

- `packs/claude-code/`: a `hooks/freshness_pointer.py` (stdlib-only python: read `.synapse/bindings/*.json`, GET service `/watermark` with a **2s timeout**, emit `hookSpecificOutput.additionalContext` **only when the version moved** since the last check — state file in `.synapse/`; absolutely fail-open: any error → empty output, exit 0), a `skills/synapse-shared-memory/SKILL.md` (trigger-voiced description per the architecture page's frontmatter draft), a `settings-snippet.json` (UserPromptSubmit wiring), and an `INSTALL.md`.
- Tests (`packages/worker/tests/test_awareness_pack.py` or a new top-level `tests/`): the hook script run as a subprocess — silent when version unchanged; speaks once when moved; **fail-open pinned**: service down → exit 0, empty stdout, under 3s.
- The pack is a *shipped artifact*, not installed into this repo's own `.claude/` — installing it on a dev machine is `INSTALL.md`'s job.

**Done when:** hook behavior pinned incl. fail-open; skill description matches CONTEXT.md vocabulary; suite ≥567.

---

**Merge order:** B → A → C (B touches shared worker files most). Orchestrator (Fable) merges, rehearses, and runs the live smoke + Cirrascale domain probe — not agents.
