# Overnight log — 2026-08-06

Chronological, timestamped (PDT), appended at every transition and merge.
Newest at the bottom.

- **03:55** — Session start. Read PLAN.md (f56d6f0). Recon: repo clean, in sync
  with origin/main, only tag `demo-fallback`.
- **03:56** — Baseline full suite on f56d6f0: **888 passed**, 1 warning, 22s.
  Green starting point confirmed.
- **03:57** — Tagged `overnight-20260806-start` at f56d6f0, pushed. Control
  worktree entered; journal lives on branch `overnight/journal`.
- **03:59** — **Wave 1 launched** (workflow `wf_04d720d1-4f2`, 12 agents):
  W11 five transcript lenses → Fable synthesis; W3a three-part flow
  investigation; W10a docs audit; W8a install scoping → transcript review.
  All read-only. Models: Opus for lenses/investigation/audit, Fable for the
  one synthesis step.
- **05:13** — **Wave 1 complete.** 11/12 agents (narrative lens died on a
  connection error; its ground is covered by the other four). 74 minutes,
  2.9M subagent tokens, 627 tool calls. Headline findings:
  - Storyboard steps 1/2/4/5 (slash command, presentation view, join-time
    briefing with purpose, background query hook) have **no implementation**.
    W5 retargets to join-time delivery; new checklist items own the rest.
  - The documented demo path is structurally silent: `serve_local` writes an
    `as-<contributor>` binding the freshness hook can never match, and spawns
    no worker. Raises W2's priority — per-session bindings fix the identity
    mismatch.
  - `--npu --live` silently drops `--live` (the flag pair a presenter would
    reach for). Routed to W1.
  - `demo-fallback` tag existed only locally — **pushed at 05:18**, the
    "rehearsed fallback" claim is recoverable from GitHub again.
  - Submission logistics (Microsoft Form, 3× feedback survey) have zero repo
    trace and are due Fri noon PST → top of HUMAN-TODO.md.
  - W8a scope verdict from its own reviewer: NEEDS-CHANGES, 8 concrete
    corrections (doctor trimmed to pre-flight, one-paste joiner command,
    `--live` in the arm table, never pull by default…) — feeds W8b.
- **05:20** — FLOW.md assembled from the three W3a investigations (195 lines,
  file:line cited). Audit + scope artifacts parked under `docs/overnight/`.
  Worktrees created for W1, W2, W6, W8, W10, W11 on `overnight/*` branches.
- **05:25** — W11 checklists committed on `overnight/w11-transcript`, merging
  to main. **Wave 2 launching**: W1, W2, W8b, W6, W10b as five parallel
  workflows, each with the review → adversarial-verify → fix → audit shape.
- **05:30** — W11 merged to main (`10783f4`, 888 green). Wave 2 running; W7
  launched behind it. Journal (FLOW.md + wave-1 artifacts) merged to main
  (`2074873`).
- **05:45** — Decision 010 (slides): outline tonight + hard-scoped skeleton.
  Fable outline agent → `slide-content-outline.md` on main (`c2ca4b9`);
  skeleton agent → `presentation/index.html` + `deck.html` merged (`c1d768f`,
  888 green, barred-claims grep clean).
- **06:00** — ⚠ **INCIDENT: workflow agents shared my control worktree.**
  Root cause: my wave-2 workflow scripts *told* agents they had isolated
  worktrees but never set `isolation: 'worktree'` in the agent options, so
  every workflow agent inherited this session's worktree — switching branches
  and committing under one another. Symptoms found: my worktree left on a W7
  branch; W8/W10/W7 commits interleaved on one lineage pushed to two branch
  refs; two W8/W10 partial commits (`37b456b`, `b44c58b`) reached main inside
  my `c2ca4b9` push without their review stages; `decisions/010` missed main;
  a stash (`4630ba2`, "pre-switch stash for w10 assembler") left on my branch;
  nested worktrees created inside mine. **All six workflows stopped at 06:00.**
  No suite breakage: main (`c1d768f`) verified 888 green by the skeleton agent
  in a clean worktree. Nothing force-pushed; nothing lost except W1's
  uncommitted dev work and the un-run review stages.
- **06:05** — Recovery: stage outputs (W1+W2 designs, W6 coverage audit, W8
  spec, W10 writer outputs) extracted from workflow journals; W6's lone
  unpushed commit `ee384e1` preserved to `origin/overnight/w6-stress`; my
  worktree restored to `overnight/journal`; stray processes killed, demo
  ports verified free.
- **06:10** — **Relaunch with real isolation** (every agent now gets its own
  harness worktree + detached-HEAD checkout, push-by-ref only):
  `w1-geniex-v2` (wf_4c6491eb, full redo from recovered design) ·
  `integration-w8-w10-w7` (wf_efecf617, runs the skipped reviews over the
  interleaved chain, then merges) · `w2-multisession-v2` (wf_54298c1f,
  continues from preserved pass-1 tip `40b0744`) · `w6-stress-v2`
  (wf_581ae566, continues from `ee384e1`). The un-reviewed commits already on
  main (`37b456b`, `b44c58b`) are covered by the integration reviews rather
  than reverted — they were green and coherent, and their finishing stages run
  against the merged state.
