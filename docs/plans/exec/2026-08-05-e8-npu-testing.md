# E8 — NPU Testing Plan (X Elite box)

> **For the Claude Code session on Aditya's machine:** execute this plan task
> by task, ticking checkboxes and committing where a step says commit. The
> detailed how-to for every step is [`docs/NPU-RUNBOOK.md`](../../NPU-RUNBOOK.md) —
> this plan is the gate sequence and the record-keeping. **A red gate stops
> the plan**: report it (Task 8's template) rather than pushing past it.

**Goal:** prove the merged system (main @ `25b4e81`+, 717 tests) on the real
NPU, produce the two measurements only this box can, and land every result in
git so the team sees them without asking.

**Ground rules:** ARM64 interpreter pin on every `uv` command · `mcp==1.9.4`
stays pinned · never commit keys · commit results to a branch
`npu-testing-aug6` and push it (do not push to main from this plan — the
capability-record change ripples into segment sizing and gets a review).

---

### Task 1 — sync gate

- [ ] `git pull` on main; note the HEAD short-sha here: ______
- [ ] `uv sync --python "$env:LOCALAPPDATA\Programs\Python\Python312-arm64\python.exe"` resolves clean
- [ ] `uv run python -c "import platform; assert platform.machine() == 'ARM64'"` passes

### Task 2 — suite gate (the Windows-portability gate)

- [ ] `uv run pytest -q` → expect **717 passed**
- [ ] If red: STOP. Capture the failure list verbatim into the Task 8 report — post-Aug-4 code has never run on Windows and this is exactly what the gate exists to catch. Do not proceed to hardware with a red suite.

### Task 3 — NPU + corpus gate

- [ ] `geniex serve` boots; `GET localhost:18181/v1/models` lists the bundle
- [ ] `uv run python scripts/run_npu_eval.py` — canary PASSES (if it fails: `geniex model set-type <model> llm`, retry once, else stop and report)
- [ ] Record in the report: schema-valid rate · decode tok/s · every LEAKED IDENTIFIERS line · any VOIDed pack/fixture pairs
- [ ] Commit the harness output: save to `.measurements/npu-eval-aug6.log`, copy the summary table into the Task 8 report

### Task 4 — the live on-device loop

- [ ] Four terminals per runbook Phase 3 (service in fake mode first)
- [ ] Open a real Claude Code session in any repo on this box and work normally for ~10 minutes — that session is the test input
- [ ] Gate: one finding traced end to end — transcript → triage keep (copper dashboard) → NPU-now ticking → push → service log tail (teal dashboard) → retrievable via `query` from a different `agent_session`
- [ ] Confirm suppression: the producing session's own query returns nothing (invariant 3 — correct, not a bug)
- [ ] Record: rough wall-clock per segment on the NPU, triage keep/skip counts from the dashboard, anything surprising in the `llm` previews

### Task 5 — real 70B synthesis (optional, budget-aware)

- [ ] Get the Indonesian env trio from Siddsing (never committed); restart only the service with it
- [ ] Re-run ~5 minutes of Task 4; gate: a `Merged` entry appears in the teal log tail from real 70B verdicts
- [ ] Budget: ~20 requests/hour/key — stop at the first merge observed

### Task 6 — the two numbers (the reason this box matters)

- [ ] **Prefill:** measure `prefill_toks_per_sec` per runbook Phase 4 (seed-pinned increasing-length prompts, slope of latency vs prompt_tokens). Update `config/synapse.toml`'s capability record, delete the `PROVISIONAL` marker, note method + raw datapoints in the commit message. **Commit.**
- [ ] **Power:** sustained distillation on `-c npu` vs `-c cpu`, whatever telemetry exists (runbook Phase 4). Record watts-or-proxy for both, method and caveats included. Add to the Task 8 report and `docs/STATE.md`'s power line. **Commit.**
- [ ] If power is genuinely unmeasurable on this box, write THAT down with what was tried — "still unmeasured, here is why" beats silence, and the no-efficiency-claims rule stays in force either way.

### Task 7 — Codex provenance (10 min, if `codex` is installed)

- [ ] One real Codex session; `synapse-worker status` detects it; its rollout parses with zero unknown-variant warnings beyond the documented ones
- [ ] Update `fixtures/raw_lines/codex/README.md`'s "Residual risk" section: source-confirmed → live-transcript-confirmed (or record the mismatch found). **Commit.**

### Task 8 — report and push

- [ ] Append the filled report below to `docs/STATE.md` under a `⟨NPU testing, Aug 6⟩` heading; push branch `npu-testing-aug6`; tell the team

```
NPU TESTING REPORT — <date>, X Elite
main sha tested: ______
T2 suite:        717? ______  (failures verbatim if red)
T3 eval:         schema-valid ____ · decode tok/s ____ · leaks: ____
T4 live loop:    end-to-end trace ✓/✗ · s/segment ____ · keep/skip ____
T5 70B merge:    observed ✓/✗ (entry: ______)
T6 prefill:      ____ tok/s (was PROVISIONAL 250.0) · committed ✓/✗
T6 power:        npu ____ vs cpu ____ (method: ______) — or why not
T7 codex:        live-confirmed ✓/✗
Surprises:       ______
```
