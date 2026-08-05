# Where things stand — 2026-08-05 end of day

**All four exec plans are merged to `main`.** E1 (corpus + privacy metric), E2 (triage), E3 (service), E4 (orchestrator content) each landed through the same gate — dev branch, adversarial review, adjudicated fixes, verifier verdict **clean** — and then `main` picked up six more commits closing the seams between them: triage pinned against the now-complete corpus expectation map, the three-package closed-loop test, two E3 residual fixes (`CANDIDATE_WINDOW` pinned at the route, a `/synthesize` resync-self-heal endpoint), and two docs passes repairing amendments the verifiers had flagged. 387 tests green offline, 96% overall line coverage, 100% on both CLI entry points (`synapse-worker` and `synapse-orchestrator`).

The system now runs end to end: a Claude Code transcript → segmented → triaged → distilled on the NPU → pushed through the orchestrator's relay → synthesized by the service → retrieved by a teammate's query, all in-process and all verified — with one honest caveat on that last claim, see below.

Full prior detail: **[`2026-08-04-implementation-report.md`](./2026-08-04-implementation-report.md)** (pre-merge state; Part 4's session-binding correction and Part 6's gap list are still accurate background) and **[`adr/0003-distiller-compresses-rather-than-judges.md`](./adr/0003-distiller-compresses-rather-than-judges.md)** (the architecture the merged code now implements throughout).

---

## Built and merged

```
packages/contracts/     frozen schemas + SessionBinding
packages/providers/     ModelProvider · FakeProvider · OpenAICompatible · NPUProvider · AIC100Provider
packages/distiller/     guards · promptpack · distiller · capability · config · evaluation (+ identifier_leaks)
packages/worker/        claude_code source · follower · segmenter · triage · triage_log · producer · loop · discovery · cli (join/run/status/replay[--skipped])
packages/orchestrator/  server (query/contribute MCP tools + instructions briefing) · app (producer endpoint) · relay (write-ahead egress) · briefing · cli
packages/service/       api (sessions/findings/synthesize/watermark/query) · synthesis (semantic merge) · retrieval · store · cli
config/                 synapse.toml + 4 versioned prompt packs
fixtures/               8 segments (seg-001…seg-007, seg-005 split a/b) + triage.json — PROVISIONAL, solo-authored
scripts/                run_npu_eval · trace_one · calibrate_prompt · dump_prompt · verify_orchestrator · verify_instructions
```

**Corpus of 8 + the leak metric (E1).** The fixture corpus grew from 2 to 8: `seg-002`, `seg-003`, `seg-005a`/`seg-005b`, `seg-006`, `seg-007` joined `seg-001`/`seg-004`, alongside `fixtures/triage.json` (the keep/skip expectation map E2's tests consume) and a fixture/prompt-pack contamination guard. The blind 8-gram `verbatim_overlap` metric is joined by `identifier_leaks()`, which catches what n-grams can't — `default_pool_size=25` and its kind — and `scripts/run_npu_eval.py` now prints a per-fixture `LEAKED IDENTIFIERS` line plus a corpus-wide summary that refuses to claim a clean bill of health when leaks are present.

**Triage + `replay --skipped` (E2).** `triage()` makes the recall-tuned keep/skip call before a segment ever reaches the NPU — tuned so a false negative (permanently and silently lost, since the follower never re-reads a transcript position) is far more expensive than a false positive. It's wired into the tick: skipped segments are logged as full, replayable entries rather than dropped, and `synapse-worker replay --skipped` re-distils them and archives the skip log, gated by the same `check_canary` pre-flight `cmd_run` uses (a model that's stopped reading its prompt must not write invented findings into the write-ahead log) and with per-segment catch-and-requeue so one bad segment can't crash the whole batch. `triage()` is now pinned directly against `fixtures/triage.json`'s corpus-wide expectation map — recall is measurable, not asserted.

**The full service (E3).** `packages/service` is no longer an empty package: idempotent ingest (`upsert` by `Finding.id`, first-write-wins so a replay can never clobber a tombstone), synthesis (`Synthesizer.merge` — one bounded-window model call, verdict validated as **one atomic unit** via a pydantic model so a malformed nested entry invalidates the whole verdict rather than silently dropping just that entry — no partial application), retrieval, and the watermark. `AIC100Provider` routes schema-constrained calls through `/completions` (Cirrascale's `/chat/completions` eats JSON into `tool_calls`), gates `schema_valid=True` behind a structural check, and retries with a repair prompt at `temperature=0.2` rather than a no-op identical resend. A `CANDIDATE_WINDOW`-starvation fix is now pinned at the route level, and a `POST /v1/sessions/{sid}/synthesize` endpoint lets a failed push's synthesis be re-run without needing a later push to trigger it.

**Orchestrator content (E4).** The transport shell has content now: the producer endpoint (`POST /producer/findings`, 422 on anything that isn't a `Finding`), a durable `Relay` (write-ahead log partitioned by the Shared Session bound *when each Finding was recorded*, not whatever's bound at send time — see Traps), and the MCP surface's `query`/`contribute` tools plus a watermark-driven arrival briefing riding the `initialize` response's `instructions` field (amendment F Q11 — verified live against a real `synapse-orchestrator` process, not just FakeProvider). Every route re-resolves its binding live rather than caching one at boot, and `contribute()` never raises out of the MCP tool.

**The closed loop.** `test_end_to_end.py` sends one `Finding` through all three packages — worker's `Producer` → orchestrator's producer endpoint → `Relay` → the real service's `/findings` and `/query` — over in-process ASGI transports (zero real sockets) and gets it back out through a teammate's query; a second test pins awareness suppression across the same full chain. **Caveat:** both tests exercise the producer endpoint's legacy no-`resolver` branch (`relay.record(findings)` against a single `shared_id`), matching the plan's own usage — not the `resolve_binding_for_agent` per-agent-routing branch that `cli.main` actually wires up in production. The routing logic that resolves each Finding's Agent to its own Shared Session has unit coverage elsewhere, but the closed-loop test does not exercise it in combination with the other two hops.

## What remains

- [ ] **Golden co-review sign-off:** all 8 fixture goldens (`fixtures/findings`, expectation notes in `fixtures/triage.json`) are still PROVISIONAL and solo-authored — a second human must co-sign per `fixtures/README.md` and the E1 co-author gate before any recall/quality number is quoted.
- [ ] **Live Cirrascale flip (E3 Task 6):** point `AIC100Provider` at the real hosted endpoint with `INFERENCE_CLOUD_API_KEY`, re-verify both probed gotchas (`response_format` silently ignored; `/chat/completions` eating JSON into `tool_calls`) against the live service, and watch the shared credit pool — everything here was verified against transport fakes only.
- [ ] **Live NPU eval + power measurement:** run `scripts/run_npu_eval.py` over the full 8-fixture corpus on the real GenieX NPU (`geniex serve`) and record recall + `LEAKED IDENTIFIERS` output; the power half of the NPU rationale still has no measured number — do not claim efficiency until it does. `prefill_toks_per_sec=250.0` also remains a PROVISIONAL guess feeding the segment budget.
- [ ] **Codex adapter (`CodexSource`):** unbuilt, still parked — a human decision on whether it makes the demo scope.
- [ ] **Compaction (Plan A.5):** unbuilt, still parked; seg-003's amendment notes `tool_result` recall is deferred until it lands.
- [ ] **A/B demo measurement (Plan B.8):** not started.
- [ ] **Refresh `docs/STATE.md` and `docs/plans/README.md`'s status table to post-merge reality before pushing (see findings)** — and fix the false "Task 4 remains blocked" sentence in `docs/plans/exec/2026-08-04-e2-triage.md`; a human should sign off on what the repo now claims about itself.
- [ ] **Decide the worker-side WAL re-join gap:** a Finding queued in the worker's own write-ahead log across a re-join of the SAME Agent product still gets retargeted to the new Shared Session on retry (`relay.py` round-3 note names this as needing a worker-side envelope change). Explicitly not closed by any merged branch — needs a prioritization call.
- [ ] **Real-socket, two-machine run before the demo:** the closed-loop tests are in-process ASGI by design ("zero sockets"); run worker → orchestrator → a teammate-hosted service over real HTTP at least once, and remember the `mcp==1.9.4` pin trap for any ARM64 Windows teammate.

## Traps worth re-reading

Five of the six numbered traps from 2026-08-04 still stand; #6 is now closed. Q3 ("who builds the orchestrator"), previously tracked on its own as "Open, unchanged," is also closed as of E4's merge — folded in here as #7 rather than kept as a separate section. Two more traps, #8 and #9, earned during this merge.

1. **`uv`'s managed interpreter is x86_64.** A bare `uv venv` silently builds the emulated Prism venv where NPU wheels cannot install. Pin `--python` at the ARM64 exe.
2. **GenieX accepts unknown request parameters silently.** A 200 response is not evidence a parameter was honoured. Send a deliberately bogus field as a control before believing any capability probe.
3. **Do not let one person write both the prompt and the eval target.** A few-shot that duplicated a fixture produced a "fix" that was pattern-matching, and it survived a full measurement cycle before anyone noticed.
4. **A 4B can reverse a fact stated twice in its own prompt.** It passes the canary, the `prompt_tokens` guard, schema validation and the verbatim metric while doing it. An inverted finding is worse than a missing one.
5. **`mcp` must be pinned to `1.9.4`.** `1.9.4` through `1.29.0`, and all of `2.x`, pull `pyjwt[crypto]` → `cryptography`, which has no ARM64 Windows wheel and fails building from source here. Still live — the real-socket two-machine run above will hit this on the first ARM64 Windows teammate who syncs.
6. ~~**Check Plan A/D before presenting design options, not after building one.**~~ **Closed.** The session-binding MCP prompt that motivated this trap was deleted and rebuilt as `synapse-worker join <shared_id>` to match Plan D Task D.3 before E4 started; nothing merged since has repeated the mistake.
7. ~~**Q3 — who builds the orchestrator, still open as a staffing decision.**~~ **Closed by E4's merge.** The producer endpoint, `Relay`, `query`/`contribute`, and the watermark-driven briefing are all on `main` now — `FileSink` no longer stands in for the whole egress path, and `HttpSink` has talked to a real (in-process) endpoint. What's still open is the real-socket run, tracked above, not "who builds it."
8. **A relay/producer log partitioned by `shared_id`-at-record-time still isn't the whole fix.** E4 round 2 closed the leak for anything that reaches `Relay.record()` — every line in `findings.jsonl` is tagged with the Shared Session bound when it was recorded, and `flush()`/`resync()` send each group to its own session, never to whatever's currently bound. Round 3 found this does *not* cover a Finding still sitting in the **worker's own** write-ahead log at the moment of a re-join: that log's envelope carries no Shared Session identity, so a retried POST after `synapse-worker join <new_id>` is indistinguishable from a fresh Finding produced under the new session. Tracked in "What remains" above.
9. **The closed-loop E2E test covers the wire, not the routing decision.** `test_end_to_end.py` proves a Finding can cross all three packages over real (in-process) HTTP, but it does so via the producer endpoint's legacy single-`shared_id` path, not the `resolve_binding_for_agent` per-agent routing production actually calls. Passing E2E is not evidence the routing logic is exercised end-to-end — only unit tests cover that today.

## Not done

- No Codex adapter, no compaction. Both still parked; see "What remains."
- The A/B demo measurement (Plan B.8) has not started.
