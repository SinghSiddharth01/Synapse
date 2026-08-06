# Demo readiness checklist — agentic

Rebuilt overnight 2026-08-06 (W11) from `docs/demo-transcripts.txt` + five lens reports.
**Agentic items only** — anything needing a person (hardware, NPU-box measurements, the
two-machine run itself, submissions, recording, competitive due diligence) moved to
`docs/HUMAN-TODO.md`. Of the previous 4 items, only the latency *harness* and the
cross-machine *plumbing audit* halves were agentic; they stay below. Items owned by an
overnight workstream (W1–W10) are routed there, not duplicated here.

## Storyboard mechanisms — no workstream owns these (demo-transcripts.txt §6)

- [ ] **`/create` slash command.** Step 1 (transcripts:130-132) expects a slash command; only
      the MCP tool exists (`orchestrator/server.py:653`) and the `/mcp__synapse__start` prompt
      was deleted for plan-conformance (STATE.md:78). Ship a `packs/claude-code/commands/`
      entry (or restore the prompt) that mints a session and prints id + URL.
      *Accept:* typing the command in a fresh Claude Code session creates a session and prints its id and an openable URL.
- [ ] **Presentation view page.** Step 2 (transcripts:133-137) has zero implementation — no
      welcome page, no session-scoped route; only dev `/debug` pages exist
      (`service/debug.py:158-159`, `worker/debug_server.py:24`). Serve a session-bound page
      stating the purpose in natural language.
      *Accept:* opening `<service>/s/<shared_id>` (or equivalent) shows the session purpose, welcome-styled, without /debug.
- [ ] **Decision-propagation beat.** transcripts:155-157 wants a scripted high-confidence
      stakeholder DECISION visibly reaching agent #2. `FindingType.DECISION` exists
      (`contracts/schemas.py:76`) but rides generic untyped ranking (`service/lanes.py:171-200`).
      Script the beat so a query demonstrably surfaces it with attribution.
      *Accept:* rehearsal log shows agent #2's query returning the decision, attributed to agent #1.
- [ ] **Worker #1's scripted content** (transcripts:136-137) — 2-3 pre-scripted prompts so
      memory has real content before the join, via the agent-facing path (today's demo assets
      are all direct HTTP: `demo_local.py:566-628`, `demo-script.md` §B).
      *Accept:* running the script leaves ≥3 findings in a live session through the MCP/worker path, not curl.
- [ ] **Teammate #2 pre-crossover beat** (transcripts:150-154) — one beat of #2's own work
      before the reveal so it doesn't read as a gimmick.
      *Accept:* demo script contains the beat and it rehearses cleanly.

## Known-broken on the documented demo path

- [ ] **Freshness hook is structurally silent under `serve_local`.** Binding is written with
      `agent_session_id="as-{contributor}"` (`serve_local.py:414-416`), which can never equal
      Claude Code's real session id, so the hook returns before any network call
      (`freshness_pointer.py:518-520`).
      *Accept:* on the JOIN.md path, a memory-version bump makes the pointer line appear in a live Claude Code session.
- [ ] **`new_since` is always the full memory version.** Hook hits `/watermark` under the
      transcript uuid but `last_seen` advances only under `contributor` (`api.py:656`, `:733`).
      Wrong number on camera whenever the hook speaks.
      *Accept:* after one query, the next pointer reports the true delta, not the total version.
- [ ] **`--npu --live` silently drops `--live`.** `serve_local.py:288-302` consults `--live`
      only in the non-`--npu` branch — the flag pair a presenter would reach for degrades with
      no warning.
      *Accept:* `--npu --live` either runs the split config or exits loudly naming the two-machine requirement.
- [ ] **`secrets.example.jsonc` does not exist** though `.gitignore:6-8` claims it is committed —
      a fresh cloner on the live path has no credential template.
      *Accept:* file exists with a placeholder for every credential `serve_local.py`/`local_model_server.py` reads.

## Rubric line items (named in the PDF, all cheap)

- [ ] **Lint job in CI.** Rubric names "linters/PEP8" (transcripts:120); `ci.yml` runs pytest
      only, no ruff/flake8 config anywhere in `pyproject.toml`.
      *Accept:* `ruff check` green locally and as a CI job on main.
- [ ] **README badges + a tagged release.** "dynamic GitHub badges (build passing,
      release-ready)" (transcripts:120-121); zero badges, `gh release list` empty.
      *Accept:* README renders a live CI badge and a release badge that both resolve.
- [ ] **Repo description + homepage fields.** Both null on GitHub; `gh repo edit`, 2 minutes.
      *Accept:* `gh repo view` shows a non-empty description.
- [ ] **Push `demo-fallback`.** Tag exists locally; `git ls-remote origin` shows only
      `overnight-20260806-start`. The "rehearsed fallback" maturity claim (transcripts:240-242)
      is currently not recoverable from GitHub by anyone else.
      *Accept:* `git ls-remote origin` lists `demo-fallback` and its tree passes the fake-mode rehearsal.

## Evidence for the 40-pt technical section

- [ ] **A/B latency harness + software-side numbers** (kept from previous checklist — harness
      half is agentic). Drive the fixture corpus through the stand-in and AIC-100 arms via the
      existing `CallLog` (`providers/recording.py:36,144`); emit round-trip ms + tok/s per arm.
      NPU-box run itself → HUMAN-TODO.
      *Accept:* a script writes a per-arm latency/throughput table into `.measurements/`.
- [ ] **Verify `instructions` reaches Claude's context.** `verify_instructions.py:19-31` proves
      the wire, not the injection; the whole briefing mechanism rests on this assumption
      (`server.py:100-104`). Cheapest high-value pre-demo check.
      *Accept:* a live Claude Code session demonstrably knows a sentinel present only in the briefing text.
- [ ] **Re-verify Beat 5 reproduces against Llama-3.3-70B** (transcripts:236-239, 246-250) —
      the unscripted cross-contributor merge, 24 entries apart.
      *Accept:* live rehearsal log shows the merge; slide callout added only if it reproduces.
- [ ] **8B cloud fallback config verified** (transcripts:239-240) — the named fallback if 70B
      capacity is unavailable.
      *Accept:* one rehearsal beat passes against the 8B endpoint config.
- [ ] **Retrieval-quality eval** (transcripts:259-261) — only informally spot-checked; an
      accepted demo-time risk an eval shrinks.
      *Accept:* eval script grades every canned demo query hit/miss; all demo-script queries hit.
- [ ] **Cross-machine plumbing audit** (kept from previous checklist; the run itself is human):
      bind addresses, host/port config, URL generation for a non-localhost service.
      *Accept:* written audit with file:line; no 127.0.0.1 assumption left on any cross-machine seam.

## Presentation assets (no workstream owns these)

- [ ] **HTML slide deck** (animated HTML, not PPT — transcripts:199-202), sections mirroring the
      100-pt rubric (:95-123). Content the transcript commits us to: problem = duplication +
      asymmetry (:5-15) · MATLAB→C++ anecdote (:17-20) · "join any Google Doc together" quote
      (:21-23) · SAVE TIME / SAVE TOKENS pillars (:32-37) · dedup-as-optimization said both ways
      (:39-43) · Snapdragon positioning per agreed wording, "current MVP" not "the demo"
      (:48-64) · NPU 4B + AI-100 70B as one story, GenieX as leveraged infra (:65-71) ·
      multi-backend versatility without demoting Snapdragon (:72-75) · architecture with the
      privacy line "raw transcript never leaves the device" (:80-90) · division-of-labor line
      verbatim (:91-92) · parallelization graphic, not serial (:101-105, :220-222) ·
      not-everything-goes-to-an-LLM (:223-226) · 5-6 use-case icon boxes (:107-112, :216-219) ·
      PM/defense as opt-in call-outs (:218-219) · single efficiency graphic, not a table
      (:211-215) · human-latency-is-a-benefit line (:179-182) · roadmap slide parking
      agent-to-agent (:25-29) · profiling slide fed by the latency harness (:103-104) · risks
      slide (:253-263).
      *Accept:* deck opens in a browser, sections map 1:1 to the rubric, every number traces to a repo artifact.
- [ ] **Two-tab site** — slides tab + Global View tab, demo-video slot placed *after* the
      slides (transcripts:199-203).
      *Accept:* one URL serves both tabs with the video embed ordered last.
- [ ] **Storyboard screenshots for slides** (transcripts:126-128).
      *Accept:* a screenshot per storyboard beat exists in the deck assets.

## Copy honesty audit (claims the team barred)

- [ ] No energy-efficiency numbers anywhere in slides/docs (transcripts:243-244).
- [ ] Topic *lane* never claimed — labels in the briefing only; lane ships OFF
      (transcripts:244-245; `service/lanes.py:79`).
- [ ] Nothing implies one worker joins multiple sessions (transcripts:170-172;
      `packs/claude-code/INSTALL.md:69-71` already states the constraint).
- [ ] Session-history list framed as roadmap gesture, not built feature (transcripts:165-168).
- [ ] Test-count claim re-run before use, never restated stale (transcripts:234-235).
- [ ] No AI slop in README/docs (transcripts:122-123).
      *Accept (all):* grep sweep over docs/ + deck finds zero barred claims; stale counts refreshed.
