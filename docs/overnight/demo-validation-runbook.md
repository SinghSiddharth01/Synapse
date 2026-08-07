# Demo validation runbook — humans in the loop

Ordered end-to-end validation for a successful, impactful demo. Do the phases in
order — each one is a dependency of the next. Every item has a **pass condition**;
if it fails, stop and fix before moving on. Written 2026-08-06 15:45 PDT against
main `14961cc` (suite 1335 green, cold-clone verified this morning).

Times assume: judging **Fri 1:00–4:15pm PST**, submission **Fri 12:00pm PST**.

---

## Phase 0 — Sanity (5 min, do first, repeat on the demo machine day-of)

- [ ] **0.1 Pull + full suite.** `git pull && uv run pytest -q`
      **Pass:** all green. Write the number down — it goes on slides S7/S13
      (placeholder "N tests passing" in the outline; 1335 as of this morning).
- [ ] **0.2 Doctor.** `uv run python scripts/doctor.py`
      **Pass:** 0 FAIL. A WARN on `secrets.jsonc` just means keys aren't in yet
      (Phase 1).
- [ ] **0.3 Ports clean.** Nothing on 8899/8787/18181. If occupied:
      `pkill -f serve_local; pkill -f synapse-service; pkill -f synapse-orchestrator; pkill -f local_model_server; pkill -f synapse-worker`
      (each separately — `pkill -f synapse-` misses the `uv run` wrappers).
      **Pass:** `lsof -nP -iTCP:<port> -sTCP:LISTEN` empty for all three.

## Phase 1 — Credentials & capacity (blockers for every live arm)

- [ ] **1.1 Anthropic key.** `secrets.jsonc` → `anthropic.api_key` is an
      **empty string** right now. Paste a real key if you want the
      `--distiller anthropic` arm. (The `claude-cli` arm needs no key — it uses
      the logged-in CLI.) **Pass:** doctor's secrets check goes green.
- [ ] **1.2 Cloud 70B answers.** One live synthesis round:
      `uv run python scripts/rehearse_demo.py --live`
      **Pass:** live beats complete; note the measured merge latency (12.6–52.8s
      observed in rehearsal — the slide S7 number).
- [ ] **1.3 AI-100 capacity decision.** One key ≈ 7 syntheses/hour; ~10 keys
      hold 60s latency (ADR 0005). **Pass:** either the keys are procured, or
      you commit to the honest one-key framing in the outline (S7): "synthesis
      lags visibly; findings stay queryable throughout."
- [ ] **1.4 NPU box ready.** `geniex` on PATH, `mcp==1.9.4` intact
      (`uv run python -c "import importlib.metadata as m; print(m.version('mcp'))"`),
      Python is native ARM64 (`platform.machine()` → ARM64). See
      docs/NPU-RUNBOOK.md + JOIN-WINDOWS.md for the traps.

## Phase 2 — Single-machine end-to-end (the demo's spine; stand-in arm first)

- [ ] **2.1 Deterministic rehearsal.** `uv run python scripts/rehearse_demo.py`
      **Pass:** `== ALL BEATS PASS ==`. (It now refuses to run if something
      already holds the ports — that guard protected you once already.)
- [ ] **2.2 The storyboard arc, by hand, once.** Boot
      `uv run python scripts/serve_local.py --purpose "<real purpose>"`, register
      MCP in a scratch project (`claude mcp add --transport http --scope project synapse http://127.0.0.1:8787/mcp`),
      then in a Claude Code window: create/confirm session → contribute real
      prose → query.
      **Pass:** the query answer is **your own prose**, attributed to you.
      **Contamination check (F1, fixed last night):** if findings mention
      *durable brokers, message queues, or template caching* you never typed —
      the prompt-echo bug is back; stop and flag it.
- [ ] **2.3 The join beat (the awareness moment).** Second Claude Code window
      joins the same session.
      **Pass:** the joining agent receives purpose + members + summary +
      new-since and **says it out loud** ("I have this context, ready to go").
      This is storyboard step 4 — the beat the demo is named for.
- [ ] **2.4 Two windows = two participants.** With both windows active, open
      `http://127.0.0.1:8899/debug` (the brain page).
      **Pass:** two rows for one human under PARTICIPANTS, each with its own
      conversation id; window A's findings are visible in window B's queries.
- [ ] **2.5 Rejoin keeps your place.** Leave in one window, open a *fresh*
      conversation, rejoin.
      **Pass:** no replay of everything; the briefing reports only what is new
      since you last looked.
- [ ] **2.6 Sequencing trap — do NOT end mid-demo.** `end_session` clears the
      boot binding `serve_local` wrote (known F2); after it the running stack
      reports unbound until a fresh create. **Pass:** the demo script has
      `end_session` as the *final* beat, nowhere earlier.
- [ ] **2.7 The two views on screen.** Brain page (`/debug`) as the Global
      View; raw feed at `/debug/log`. **Pass:** during 2.2–2.5 you can watch
      participants update, working-memory revisions appear, and
      latest-into-memory rows land — correlatable 1:1 with what the front
      windows are doing (the transcript's stated requirement).

## Phase 3 — Live model arms (what makes it real to judges)

- [ ] **3.1 claude-cli arm live sanity.**
      `serve_local --distiller claude-cli --claude-model haiku`, one contribute.
      **Pass:** ~35–50s distillation, findings are your prose, a sane count
      (~3 from a paragraph, not 9 — nine was the contamination signature).
- [ ] **3.2 NPU arm + supervisor (on the Snapdragon box).** `serve_local --npu`.
      Let it idle past the death window, or kill `geniex` by hand.
      **Pass:** the supervisor logs the death **loudly** and GenieX comes back
      without human help (restart N, RESTORED). This is W1's whole point and a
      strong robustness beat if asked. Note: supervisor was live-verified against
      the stand-in only — this is its first real-GenieX outing.
- [ ] **3.3 Beat 5 re-verify (gates slide S11).** Against the live 70B: the
      cross-contributor merge (two findings, different contributors, merging
      into one lineage). **Pass:** it reproduces → the "24 entries apart,
      unscripted" callout ships. Fail → soften to "in an earlier live rehearsal"
      and cut the number (per the outline's morning gate).
- [ ] **3.4 The two-machine run — the biggest untested surface.** Teammate
      laptop → host's service over real HTTP. Host boots and reads the printed
      teammate block; teammate pastes **one line** (`install.sh` / `install.ps1`
      one-liner with `--service-url/--shared-id/--contributor`).
      **Pass:** teammate lands in the session and the join beat (2.3) fires
      across machines. This has *never been attempted* — schedule real time,
      and if it fails, the single-machine demo is the rehearsed fallback.
- [ ] **3.5 `--npu --live` (only if using the split config).** It now exits
      loudly instead of silently dropping `--live`. **Pass:** you know which
      machine runs which half before stage time.

## Phase 4 — Demo assets & claims honesty

- [ ] **4.1 Build the deck from the outline.**
      `docs/overnight/slide-content-outline.md` is the single source of truth;
      skeleton in `presentation/`. Close or soften the **5 evidence-pending
      chips**: test count (0.1) · latency numbers (1.2) · **competitor check —
      slide S10 does not ship without it** · lint/badges/release · Beat 5 (3.3).
- [ ] **4.2 Barred-claims sweep over final assets.**
      `grep -riE 'energy|watt|power efficiency|topic lane|multiple sessions'`
      over the deck, captions, and README changes. **Pass:** zero hits. (No
      power numbers exist; topic lane ships OFF; one agent = one session.)
- [ ] **4.3 Record through `serve_local`, not `demo_local`.** `demo_local`
      still runs the pre-ADR-0005 800-token budget unless you export
      `INFERENCE_CLOUD_MAX_TOKENS/TIMEOUT` (known trap, outline production
      notes). Time-lapse the waits (120s idle flush + 60s debounce) with
      captions ("Claude is now doing X"). Record both views side by side.
- [ ] **4.4 Two-tab site final.** Video embedded LAST (after slides give
      context), Global View tab pointing at the real page, `TODO-HUMAN`
      placeholders gone.

## Phase 5 — Submission compliance (hard deadlines, Fri 12:00pm PST)

- [ ] **5.1 Microsoft Form submitted.** Assign the owner *now*.
- [ ] **5.2 Feedback survey × every team member** — a stated submission
      requirement, per-person; three humans must each act.
- [ ] **5.3 Repo requirements.** Personal repo (✅ verified), README roster +
      emails match the submitted team, from-scratch setup + run instructions
      (✅ cold-clone verified this morning), MIT license (✅). Don't transfer
      the repo before submission.
- [ ] **5.4 Fallback is real.** `demo-fallback` tag is on origin (✅); 8B cloud
      fallback config verified once live. Know the demo-order email slot and
      freeze changes accordingly.

## Phase 6 — Day-of pre-flight (30 min before your slot)

- [ ] **6.1** Phase 0 again on the demo machine, verbatim.
- [ ] **6.2** Warm boot the stack; brain page + presentation tabs pre-opened;
      MCP shows `synapse ✔ connected` in a fresh window.
- [ ] **6.3** Live portion = only the basic, robust beats (the team's own
      rule); everything fancy lives in the recording.
- [ ] **6.4** Know the kill/restart line cold (0.3) and the fallback tag
      checkout: `git checkout demo-fallback`.

---

**The order in one sentence:** prove the machine honest (0), unlock the live
arms (1), prove the story single-machine (2), prove it real (3), make the
assets match the truth (4), submit (5), and re-prove the machine the hour
before (6).
