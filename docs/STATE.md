# Where things stand — 2026-08-04 end of day

**Code exists now.** Five packages, 191 tests green offline (100% coverage on both CLI entry points), and the capture → condense → push loop has run end to end against a real Claude Code transcript on the NPU. Session binding (`synapse-worker join`) has been verified live against real subprocesses and the real `~/.claude/projects` directory.

Full detail: **[`2026-08-04-implementation-report.md`](./2026-08-04-implementation-report.md)**. This file is the summary and the pointer.

**E4 Task 1 — amendment F Q11 verified live: PROVEN.** `scripts/verify_instructions.py` against a real `synapse-orchestrator` process (booted on `127.0.0.1:8787`) received the `initialize` response's `instructions` field containing `SENTINEL` (183 chars). A real MCP client (`mcp.client.streamable_http.streamablehttp_client` + `ClientSession`) does surface `instructions` over the wire — the agent-agnostic-floor tier assignment for the arrival briefing stands; no fallback to a per-agent pack is needed.

---

## Start here tomorrow

1. **[`2026-08-04-implementation-report.md`](./2026-08-04-implementation-report.md)** — what exists, what was measured, Part 4's session-binding correction, and Part 6's gap list.
2. **[`adr/0003-distiller-compresses-rather-than-judges.md`](./adr/0003-distiller-compresses-rather-than-judges.md)** — the architectural change made today and why. It moves work into a component that does not exist yet.
3. **`/CONTEXT.md`** — still the vocabulary. Unchanged and still authoritative.

Then pick from "The three that block progress" below.

## What exists

```
packages/contracts/     frozen schemas + SessionBinding, verbatim from Plan 0's source
packages/providers/     ModelProvider · FakeProvider · OpenAICompatible · NPUProvider
packages/distiller/     guards · promptpack · distiller · capability · config · evaluation
packages/worker/        claude_code source · follower · segmenter · producer · loop · discovery · cli (join/run/status/replay)
packages/orchestrator/  MCP transport shell only — no query/contribute yet, see report Part 4
config/                 synapse.toml + 4 versioned prompt packs
fixtures/               seg-001, seg-004 — PROVISIONAL, solo-authored
scripts/                run_npu_eval · trace_one · calibrate_prompt · dump_prompt · verify_orchestrator
```

Everything is configurable from `config/synapse.toml` or the environment: model, prompt pack, event kinds, render style, segment budget, poll interval, idle flush, sink.

## What changed in the plans

| Plan item | Before | Now |
|---|---|---|
| Distiller's job (Plan B.1) | abstract + judge + split + classify | **compress and abstract only** — `adr/0003` |
| `qairt` path (Plan B) | blocked, AI Hub 503 | **live**; AI Hub is back, QAIRT bundle is the default arm |
| `usable_context` (Plan B.5) | unmeasured | **4096, measured**, hard ceiling — no `--nctx` on `qairt` |
| `native_structured_output` (Plan B.4) | *"probe whether grammars reach HTTP"* | **False.** Probed; the server silently accepts unknown params, so acceptance proves nothing |
| Empty-segment discipline (Plan B.3) | the distiller's job | **triage's job** — and triage does not exist |
| `seg-004`'s golden (Plan 0.3) | tests the distiller | now tests **triage** |
| Prompt (Plan B.2) | module constant | versioned TOML packs, A/B-able, overhead calibrated |
| Session binding (Plan D.3) | *"there is no `attach(shared_id)`"* | **built once as one anyway** (an MCP prompt), then deleted and replaced with `synapse-worker join <shared_id>` to match the plan — see report Part 4 |

## The three that block progress

**1. Triage does not exist, and it is now load-bearing.** The distiller stopped judging durability; nothing took over. Trivia reaches the sink today. Tune for **recall** — a false negative is knowledge permanently lost and silent, because the follower never re-reads a position.

**2. The privacy claim is unverified.** `verbatim_overlap` uses 8-word n-grams and cannot see single-identifier leaks. It reported **0.00** on a finding containing `default_pool_size=25`. The harness currently prints a number that reads as proof and is not. Do not put it in a demo.

**3. The corpus is two fixtures, both mine, one of which was contaminated.** Every measurement rests on n=2. Three of Plan 0's five are missing. This gates the other two — triage recall cannot be measured without it.

## Still unmeasured, still unclaimable

- **Power.** Unchanged since 2026-07-30. The NPU rationale is contention-and-power, and the power half has no number. **Do not claim efficiency.**
- **`prefill_toks_per_sec = 250.0`** is a guess that feeds the segment budget. Marked PROVISIONAL in code.

## Open, unchanged

**Q3 — who builds the orchestrator.** Still open as a staffing decision, though it now has a real (if minimal) starting point: a tested MCP transport shell over `streamable-http`, verified live, with zero tools registered. Plan D.1's producer endpoint, D.3's `query`/`contribute`, and D.4's service client are all still unbuilt — `FileSink` still stands in for the whole egress path. `HttpSink` exists but has never talked to a real endpoint.

## Traps worth re-reading

Three from 2026-08-03 still stand. Six more earned today:

1. **`uv`'s managed interpreter is x86_64.** A bare `uv venv` silently builds the emulated Prism venv where NPU wheels cannot install. Pin `--python` at the ARM64 exe.
2. **GenieX accepts unknown request parameters silently.** A 200 response is not evidence a parameter was honoured. Send a deliberately bogus field as a control before believing any capability probe.
3. **Do not let one person write both the prompt and the eval target.** A few-shot that duplicated a fixture produced a "fix" that was pattern-matching, and it survived a full measurement cycle before anyone noticed.
4. **A 4B can reverse a fact stated twice in its own prompt.** It passes the canary, the `prompt_tokens` guard, schema validation and the verbatim metric while doing it. An inverted finding is worse than a missing one.
5. **`mcp` must be pinned to `1.9.4`.** `1.9.4` through `1.29.0`, and all of `2.x`, pull `pyjwt[crypto]` → `cryptography`, which has no ARM64 Windows wheel and fails building from source here.
6. **Check Plan A/D before presenting design options, not after building one.** The session-binding MCP prompt was built faithfully against an explicit choice, then turned out to contradict Plan D Task D.3 ("there is no `attach(shared_id)`"), discovered only by checking afterward. Deleted and rebuilt to match. See report Part 4.

## Not done

- No Codex adapter, no compaction, no synthesis, no retrieval, no producer endpoint, no `query`/`contribute`.
- The A/B demo measurement (Plan B.8) has not started.
