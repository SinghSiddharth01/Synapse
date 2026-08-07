# One Shared Session per conversation + worker multi-transcript fan-out

**Date:** 2026-08-07
**Status:** Approved by Sid (design discussion in Shared Session sh-29053bc5)

## Problem

Two related defects surfaced on 2026-08-07 while debugging "worker logs `tick — no
change` while the conversation is active":

1. The one-conversation-one-Shared-Session rule is enforced only in the
   orchestrator's `create_session` (`_already_bound`, `server.py`). The
   `join_session` MCP tool and the worker CLI `join` can still bind a
   conversation that is already bound, silently stacking or replacing bindings.
2. `WorkerLoop` follows exactly one Source per process, resolved once at
   startup and never re-resolved (commit `093549c` records full fan-out as
   "future work"). With two Claude Code conversations open, exactly one is
   captured; every new join stays dark until the next worker restart, which
   then flips *which* conversation is dark. The startup "note also bound" line
   only covers other agent products via `AGENT_REGISTRY`, so a second
   conversation of the same agent gets no warning at all.

## Decisions (made with Sid, 2026-08-07)

- Enforce the one-session rule on **both** remaining bind paths (MCP
  `join_session` and CLI `join`), via a shared helper.
- Fan out over **claude-code bindings only**. Codex keeps today's
  single-follow behavior and the "note also bound" line.
- Tail **all** per-session bindings whose transcript file exists — no
  recency window. Idle followers cost one `stat()` per tick.
- Architecture: **in-process supervisor** — one worker process, N
  `WorkerLoop`s, shared provider/limiter/debug server (approach A; one
  process per binding and single-loop-with-tagged-events were rejected —
  the latter would unwind W2's per-session isolation).

## Part 1 — the one-session guard

Move the body of `_already_bound` from `orchestrator/server.py` into the
discovery module as a shared helper (name: `already_bound(state_dir, here,
projects_root, agent_session_id)`), preserving its semantics exactly:

- Only **per-conversation** bindings count. Machine-scope bindings (the
  `scripts/serve_local.py` demo path) never trigger a refusal.
- With an explicit `agent_session_id`: exact match across every registered
  agent, no fallback.
- Without one: only an unambiguously detected live conversation is checked;
  the ambiguous case is left for `_bind`'s own refusal.

Call sites:

| Path | Behavior |
| --- | --- |
| `create_session` (MCP) | Unchanged — already refuses before the service POST. Now calls the shared helper. |
| `join_session` (MCP) | **New.** Check before any service mutation. If bound to a *different* session: refuse, naming the bound session and contributor, instructing `leave_session` first. If bound to the *same* session being joined: return a friendly "already in this session" success carrying the usual session recap — not a refusal, and no duplicate member registration. |
| CLI `join` (worker package) | **New.** Same check; on refusal print the bound session and the leave command, exit nonzero, write nothing. |

## Part 2 — worker fan-out (claude-code only)

`run` without `--transcript` builds a **supervisor** instead of a single loop:

- **Desired set:** every `bindings/claude-code/<agent_session_id>.json` whose
  `transcript_path` exists on disk. Recomputed every tick interval.
  - New binding file → start a `WorkerLoop` for it within one tick (~30s);
    a fresh join is picked up live, no restart.
  - Binding file removed (`leave_session` / `end_session` — commit `40b0744`
    made both delete per-session bindings) → stop that loop.
  - Transcript path missing → skip this tick, recheck next; not an error.
- **Per-loop isolation is already built** (W2): each `WorkerLoop` keeps its own
  follower, segmenter, producer, WAL, and `sessions/<agent_session_id>/`
  state dir. The supervisor adds no shared mutable state between loops.
- **Shared between loops:** the provider client, the rate limiter (NPU
  distillation stays serialized through the existing limiter and its
  deferred-segments backpressure, which is already per-session), and the
  debug server.
- **Stop path:** on binding removal, the loop flushes its open turn, runs its
  deferred segments through distillation, drains its WAL, then shuts down.
  Failures during the flush follow the existing per-loop retry/deferral
  rules; the supervisor never blocks other loops on one loop's drain.
- **Unchanged behavior:**
  - `run --transcript <path>`: explicit single-follow, no supervisor.
  - Codex: current single-follow resolution and the `note also bound` line.
  - Legacy tree (top-level `bindings/claude-code.json` only, no per-session
    dir): follow it as a single loop — byte-for-byte today's semantics, so
    machine-scope/demo setups are untouched.

## Part 3 — debug server

- `stats.json` becomes per-session:
  `{"sessions": [{"agent_session_id", "shared_id", "transcript", ...existing per-loop stats}]}`
  with rail totals aggregated across loops.
- Page header lists every followed transcript (not just one).
- The feed merges all loops' events; each entry carries a short session tag
  so interleaved sessions stay readable.
- One port (`:8790`), as today.

## Part 4 — startup and runtime output

- `run` prints one line per followed binding:
  `following  <agent_session_id> -> <shared_id>  <transcript_path>`.
- Runtime reconcile changes are logged:
  `now following <id> -> <shared_id>` / `stopped following <id> (binding removed)`.
- The `tick — no change` line stays per-tick but reflects the whole set; a
  tick where any loop saw changes says so.

## Part 5 — testing (TDD throughout)

- **Reconciler:** binding appears mid-run → followed within one tick; binding
  removed mid-run → loop flushes (open turn + deferred + WAL) and stops;
  transcript missing → skipped without error, picked up when it appears.
- **Guard:** `join_session` refuses when bound elsewhere (names session and
  leave step); same-session join returns success without duplicate member
  registration; CLI `join` refuses with nonzero exit and writes nothing;
  `create_session` refusal regression-pinned against the shared helper.
- **Unchanged-behavior pins:** legacy single-follow, `--transcript` mode,
  codex note.
- Existing `test_per_session_bindings`, `test_loop`, `test_cli` suites pass
  unmodified except where they pin the old single-selection startup print.

## Out of scope

- Codex fan-out (mechanism generalizes; deliberately not enabled now).
- Pruning stale per-session bindings.
- Any change to the distiller, service, or finding schema.
- `synapse health` verification that the worker follows the current binding
  (worth doing later; the fan-out makes the failure mode structurally
  impossible for claude-code, which is the case that bit).
