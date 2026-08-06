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
