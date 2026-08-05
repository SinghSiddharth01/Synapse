# NPU Bring-Up Runbook — X Elite box

**For Aditya's machine.** Open Claude Code in this repo on the X Elite and say:
*"follow docs/NPU-RUNBOOK.md, phase by phase, and report each gate."* Every
phase has an expected output and a fallback — do not skip a red gate.

**Readiness, honestly:** the code is NPU-ready (the worker ran end-to-end on
this box's NPU on 2026-08-04), but everything merged since — triage,
compaction, the re-join envelope, the brain, dashboards, CodexSource — has
only ever executed on macOS and Linux CI. Phase 1 exists to catch any
Windows-specific breakage before it wastes NPU time.

**The two environment traps (both cost real time before — do not rediscover them):**
- `uv`'s managed interpreter is x86_64-under-Prism. **Always** pin:
  `uv sync --python "$env:LOCALAPPDATA\Programs\Python\Python312-arm64\python.exe"`
- `mcp` is pinned to `1.9.4` in the lockfile (newer pulls `cryptography`,
  which has no ARM64-Windows wheel). Do not "upgrade" it.

---

## Phase 0 — pull and sync (5 min)

```powershell
cd <repo>; git pull
uv sync --python "$env:LOCALAPPDATA\Programs\Python\Python312-arm64\python.exe"
```
**Gate:** sync resolves cleanly. If `cryptography`/Rust errors appear, the
interpreter pin above was skipped.

## Phase 1 — the suite, offline (2 min)

```powershell
uv run pytest -q
```
**Gate: 717 passed.** Green on macOS and Linux CI at `26a428a`+ — if anything
is red here it is Windows-specific (paths, subprocess quirks) in post-Aug-4
code. **Stop and report the failures; do not proceed to hardware.** This is
the whole point of the phase.

## Phase 2 — NPU alive + the new corpus (15 min)

```powershell
geniex serve          # terminal 1 — qairt bundle; llama_cpp GGUF is the fallback
uv run python scripts/run_npu_eval.py    # terminal 2
```
New since your Aug-4 run: the corpus is **8 fixtures** (was 2), prompt pack
defaults to `v4-condense`, the harness prints a **LEAKED IDENTIFIERS** column
and VOIDs contaminated pairs, and `fixtures/triage.json` records per-fixture
keep/skip intent (two entries are ACCEPTED FALSE POSITIVES — that is by
design, read the notes).

**Gate:** canary passes (it gates the corpus — a canary failure means the
model is mistyped: `geniex model set-type <model> llm`). **Record:**
schema-valid rate, decode tok/s, any leaked identifiers, per-fixture scores.

## Phase 3 — the live loop, on-device (the real thing)

Worth ten minutes first, on this box or any other: `uv run python
scripts/demo_local.py` runs these same processes with a model stand-in where
GenieX would be, so you see the intended shape of the dashboards and the merge
before any of it depends on the NPU. It measures nothing — that is what the
rest of this runbook is for.

Four terminals + a browser:

```powershell
# T1: the NPU model
geniex serve
# T2: the service — fake verdicts, or the real 70B (env trio below)
uv run synapse-service
# T3: the local orchestrator
uv run synapse-orchestrator --port 8787 --service-url http://127.0.0.1:8899 --state-dir .synapse
# T4: create a session, join, run
$SID = (curl -s -Method POST http://127.0.0.1:8899/v1/sessions -ContentType application/json -Body '{"purpose":"npu bring-up","created_by":"aditya"}' | ConvertFrom-Json).shared_id
uv run synapse-worker join $SID
uv run synapse-worker run --interval 15 --debug-port 8790
```

Then **open a real Claude Code session in any repo on this box and just
work** — that session *is* the test input. The worker detects and tails it
(`join` binds the live transcript; two windows of the same product is the
documented ambiguity, keep one). Set `SYNAPSE_SINK=http` (or
`sink = "http"` in config) so findings flow to the orchestrator rather than
the file sink.

Open side by side:
- `http://127.0.0.1:8790/debug` — the copper page: **NPU-now should tick
  live** while a segment distils; triage skips appear with reasons; `llm`
  entries expand to real prompt/output previews.
- `http://127.0.0.1:8899/debug` — the teal page: `FindingAppended` entries,
  then `Merged` the moment synthesis reconciles anything.

**Gate:** a finding you can trace end to end — transcript line → triage keep
→ NPU distil (watch the seconds) → push → service log tail → retrievable by
`POST /v1/sessions/$SID/query` from a *different* `agent_session` (your own
session's findings are suppressed for you — that is invariant 3 working, not
a bug).

**Real 70B synthesis** (instead of fake): set the trio before starting T2 —
`INFERENCE_CLOUD_BASE_URL` = the Indonesian instance, `INFERENCE_CLOUD_API_KEY`
= the Indonesian key (ask Siddsing — never committed), `INFERENCE_CLOUD_MODEL`
= `Llama-3.3-70B`. Budget ~20 requests/hour/key; `INFERENCE_CLOUD_API_KEYS`
(comma-separated) rotates a pool automatically once more keys are activated.

## Phase 4 — the two numbers still owed (this box is the only place they exist)

1. **`prefill_toks_per_sec`** — currently a PROVISIONAL guess of `250.0` in
   `config/synapse.toml`, and it sizes every segment. Measure it (seed-pinned
   prompts of increasing length through `geniex serve`, slope of
   prompt_tokens vs latency), fix the capability record, commit.
2. **Power.** The NPU placement rests on contention-and-power, and the power
   half has never been measured. NPU vs CPU (`geniex serve -c cpu`) sustained
   distillation, whatever telemetry the box offers. Until this number exists,
   nobody claims efficiency on stage — that rule is already written down.

## Phase 5 — Codex provenance (10 min, if `codex` CLI is installed)

`CodexSource` is confirmed against openai/codex's source and test literals —
not yet against a live transcript (`fixtures/raw_lines/codex/README.md`,
"Residual risk"). Run one real Codex session, then check the worker detects
it (`synapse-worker status`) and parses its rollout cleanly. That upgrades
the adapter's provenance to the same standard as Claude Code's.

## Fallback ladder (unchanged, written before you need it)

distiller: `qairt` bundle → GenieX `llama_cpp` GGUF (same server) →
Mac-side dev loop. synthesizer: Indonesia 70B → `.com` 8B → fake-scripted
(`scripts/_rehearsal_service.py`). The demo runbook is `docs/demo-script.md`;
`scripts/rehearse_demo.py` asserts every beat and also runs on this box.
