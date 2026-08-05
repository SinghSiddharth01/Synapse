# Where things stand — 2026-08-05 end of day

**Code exists now.** Six packages, 266 tests green offline, and two working loops: capture → condense → push on the NPU, and append → fold → retrieve in the service. Session binding has been verified live against real subprocesses and the real `~/.claude/projects` directory.

Full detail lives in two reports: **[`2026-08-04-implementation-report.md`](./2026-08-04-implementation-report.md)** (capture and distillation) and **[`2026-08-05-service-implementation-report.md`](./2026-08-05-service-implementation-report.md)** (shared memory). This file is the summary and the pointer.

---

## Start here tomorrow

1. **[`2026-08-05-service-implementation-report.md`](./2026-08-05-service-implementation-report.md)** — what shared memory does now, the two flaws measurement found, and why the topic lane is on notice.
2. **[`adr/0004-the-log-is-append-only-and-state-is-a-fold.md`](./adr/0004-the-log-is-append-only-and-state-is-a-fold.md)** — the architectural change made today. It supersedes ADR 0002's *mechanism* and leaves one contract question open for the team.
3. **[`2026-08-04-implementation-report.md`](./2026-08-04-implementation-report.md)** — capture, distillation, Part 4's session-binding correction, Part 6's gap list.
4. **`/CONTEXT.md`** — still the vocabulary. Unchanged and still authoritative.

Then pick from "The three that block progress" below.

## What exists

```
packages/contracts/     frozen schemas + SessionBinding, verbatim from Plan 0's source
packages/providers/     ModelProvider · FakeProvider · OpenAICompatible · NPUProvider
packages/distiller/     guards · promptpack · distiller · capability · config · evaluation
packages/worker/        claude_code source · follower · segmenter · producer · loop · discovery · cli
packages/service/       log · fold · symbols · lexical · semantic · lanes · store · corpus · recall
packages/orchestrator/  MCP transport shell only — no query/contribute yet, see 08-04 report Part 4
config/                 synapse.toml + 4 versioned prompt packs
fixtures/               seg-001, seg-004 — PROVISIONAL, solo-authored
scripts/                run_npu_eval · trace_one · calibrate_prompt · dump_prompt · verify_orchestrator · measure_recall
```

Everything is configurable from `config/synapse.toml` or the environment. `synapse_service` depends only on `synapse-contracts` — no model, no network, no key.

## What changed in the plans

| Plan item | Before | Now |
|---|---|---|
| Distiller's job (Plan B.1) | abstract + judge + split + classify | **compress and abstract only** — `adr/0003` |
| Storage (Plan C.2) | `store_findings` / `get_context` / `query_candidates` | **append-only log + fold**; `candidates(text, top_k)` — `adr/0004` |
| Idempotency (C.3, D.4) | idempotent upsert by `Finding.id` | **append; the fold takes the first write of an id.** Same external property, now true by construction |
| Tombstones (`adr/0002`) | `merged_into` set on the original | **derived** from a later `Merged` entry. Intent unchanged, mechanism superseded |
| Merge input (Plan C.4) | Working Memory + the new findings | **+ retrieved candidates**, or two Contributors only merge inside one batch |
| Topics (C.4/C.6) | implied model-assigned labels | **centroid membership**; a model names a cluster once, and naming is cosmetic |
| `qairt` path (Plan B) | blocked, AI Hub 503 | **live**; QAIRT bundle is the default arm |
| `usable_context` (Plan B.5) | unmeasured | **4096, measured**, hard ceiling |
| `native_structured_output` (B.4) | *"probe whether grammars reach HTTP"* | **False.** The server silently accepts unknown params, so acceptance proves nothing |
| Empty-segment discipline (B.3) | the distiller's job | **triage's job** — and triage does not exist |
| Session binding (Plan D.3) | *"there is no `attach(shared_id)`"* | `synapse-worker join <shared_id>` — see 08-04 report Part 4 |

## The three that block progress

**1. The corpus is still two fixtures, and it now blocks two tracks.** Every recall number in the service report rests on a *synthetic* corpus written by the same author as the lanes it measures — trap #3, in full. Three of Plan 0's five fixtures are missing, and none of them are cross-contributor duplicate pairs, which is what merging actually needs. This gates triage recall *and* candidate recall.

**2. Triage does not exist, and it is load-bearing.** The distiller stopped judging durability; nothing took over. Trivia reaches the sink today. Tune for **recall** — a false negative is knowledge permanently lost and silent, because the follower never re-reads a position.

**3. The privacy claim is unverified.** `verbatim_overlap` uses 8-word n-grams and cannot see single-identifier leaks. It reported **0.00** on a finding containing `default_pool_size=25`. Do not put it in a demo. (The service's symbol lane is built to respect this — it extracts from abstracted finding text only, enforced by a test — but the underlying metric is still not a proof.)

## Still unmeasured, still unclaimable

- **Power.** Unchanged since 2026-07-30. The NPU rationale is contention-and-power, and the power half has no number. **Do not claim efficiency.**
- **`prefill_toks_per_sec = 250.0`** is a guess that feeds the segment budget. Marked PROVISIONAL in code.
- **Every service recall number.** Synthetic corpus, self-authored, and the offline embedder has no paraphrase signal — so the two lanes that exist to catch paraphrase were measured with that capability switched off. Regression guard only.
- **`K = 14`.** Fixed with no measured basis for how it should grow with N.

## The topic lane is on notice

It surfaced **zero** partners at 422 findings and zero at 2,022, none uniquely. The governing band it exists for sits at 25%. Some of that is the offline embedder; some is a corpus artifact. But it is the piece the design leaned on hardest and it is currently the weakest thing in it. Lane yield on a real corpus decides whether it stays, and the design is arranged so that deleting it changes nothing else.

## Open, unchanged

**Q3 — who builds the orchestrator.** Still open as a staffing decision. Plan D.1's producer endpoint, D.3's `query`/`contribute`, and D.4's service client are all still unbuilt — `FileSink` still stands in for the whole egress path. `HttpSink` exists but has never talked to a real endpoint.

**New: `Finding.merged_into` and `Finding.status` on egress.** The service derives supersession and does not write those fields. Either the ingest API projects them on the way out (lower risk, the assumed default) or they leave the contract. See `adr/0004` Consequences. Until decided, treat both as **undefined on anything the service returns**.

## Traps worth re-reading

Six from 2026-08-04 still stand. Three more earned today:

1. **`uv`'s managed interpreter is x86_64.** A bare `uv venv` silently builds the emulated Prism venv where NPU wheels cannot install. Pin `--python` at the ARM64 exe.
2. **GenieX accepts unknown request parameters silently.** A 200 response is not evidence a parameter was honoured. Send a deliberately bogus field as a control.
3. **Do not let one person write both the prompt and the eval target.** It has now happened twice — the contaminated few-shot on 08-04, and the service's synthetic corpus today, which is labelled as such in three places precisely because the label is the only thing stopping it being quoted.
4. **A 4B can reverse a fact stated twice in its own prompt.** It passes the canary, the `prompt_tokens` guard, schema validation and the verbatim metric while doing it.
5. **`mcp` must be pinned to `1.9.4`.** Later versions pull `cryptography`, which has no ARM64 Windows wheel.
6. **Check Plan A/D before presenting design options, not after building one.**
7. **Rank fusion assumes its inputs are independent — check that they are.** BM25 and a bag-of-words embedder read the same surface form, so their agreement double-counts one signal. It pushed a pair sharing a symbol held by 2 of 422 findings out of the top 14. Re-check when a real embedder lands; a real one is genuinely independent and changes the relationship.
8. **A hedge that competes on rank crowds out what it hedges for.** The recency lane spent 8 of 14 slots on noise, at rank 4. Unranked signals need a reserved floor, not a place in the ranking.
9. **Design review did not catch either of the above.** Both were invisible until measured, in code that had been reasoned about carefully for hours. Build the measurement before trusting the reasoning.

## Not done

- No Codex adapter, no compaction, no triage.
- Service: no ingest API, no synthesis call, no watermark endpoint, no awareness support, no persistence, no real embedder wired, no alerting on topic health.
- Orchestrator: no producer endpoint, no `query`/`contribute`, no service client.
- The A/B demo measurement (Plan B.8) has not started.
