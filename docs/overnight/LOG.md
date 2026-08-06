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
- **08:06** — W1-v2 reported: dev complete on `overnight/w1-geniex` (927 tests,
  three clean commits: typed-503 retrieval contract, in-app seam supervisor,
  boot preflights + `--npu --live` split). Adversarial review: 13 findings, #1
  HIGH — `kill_port_owner` uses `lsof -ti :port`, which matches *clients* too,
  so a seam restart would SIGTERM the service mid-demo. Fix agent died on an
  API connection error; audit blocked by a transient classifier error.
  **Workflow resumed from cache** (fix + audit re-run live). Interim TLDR.md
  written and pushed to main (`0375b29`).
- **08:16** — **W6 merged** (`d03547e`): 1032 tests (+144 net). Superb dev
  report — every regression test revert-verified, Haiku 4K pin in the provider
  (decisions/006), degenerate-vs-overbudget distinguished with honest fixtures.
  Two caveats: (a) the live Haiku stress could NOT run — **the `anthropic` key
  in secrets.jsonc is an empty string** (offline bound-verification ran
  instead: 70/70 checks, chunking bound held with 0-headroom at the seam);
  (b) the fix agent died before applying confirmed review findings — incl.
  `output_config.effort` sent unconditionally by the Anthropic provider, which
  errors on Haiku 4.5 (the demo arm). Post-merge fix agent dispatched.
- **08:20** — **W2 merged** (`a7845c1`): **1080 tests.** The redundancy paid
  for itself: pass-2 (the workstream's core) died mid-flight and shipped
  nothing; the review caught "PASS-2 was never written" as blocking; the FIX
  agent then implemented all of it — agent_session_id on query/contribute/
  leave, one-orchestrator-N-conversations, suppression by agent_session with
  watermark by contributor, serve_local scope fix, pack + CONTEXT.md aligned.
  Two windows of one human are now two participants through every real hop.
- **08:25** — W5 (join-time arrival summary) and W4a (dashboard Page 1)
  launched — both were blocked on W2. W6 post-merge fix agent running.
  Still in flight: W1 resume, Integration (W8+W10 reviews).
- **08:27** — **Integration merged** (`9ed1361`): 1109 tests. The skipped
  reviews found 4 HIGHs — install.ps1 advertised everywhere but never written
  (dev2 had not run), a PATH bug failing fresh installs, the doctor's exit
  code discarded by a tee pipeline, a secrets key-routing drift. Fix stage
  wrote install.ps1 + install.bat and closed all of them; audit verified live:
  doctor rc 0, install.sh --doctor-only idempotent, mkdocs --strict clean,
  rehearse ALL BEATS PASS on shifted ports. W7 live arc launched on the real
  ports with the claude-cli Haiku arm.
- **08:35** — **W6 post-fix merged** (`d6c48cd`): 1125 tests. All six
  confirmed findings closed — incl. the effort-key gate (fail-closed, keeps
  structured outputs on Haiku) and reproducible stress-doc numbers (crc32
  seeds; the doc now names which rows moved and why).
- **08:39** — **W1 merged** (`b460684`): **1191 tests.** Typed-503 retrieval
  contract, in-app seam supervisor (probe/strikes/kill/respawn, loud logs),
  boot preflights, `--npu --live` split config, decisions/005 + 008. The
  review's HIGH (client-killing lsof) and the 45s-threshold misread were fixed
  pre-merge; audit resolved 4 real conflicts against W2's re-keying. **W3b
  launched** (last code workstream). Still running: W5, W4a, W7 live arc.
- **08:59** — **W7 live arc merged** (`c8baded`, 1191 green): the full
  lifecycle — create → join as a second participant with its own
  agent_session_id → contribute (real `claude -p` distillation, ~36–47s) →
  attributed query → leave → rejoin under a new session id → end — ran clean
  END-TO-END **twice, against the real stack on the real ports**. Evidence
  verbatim in `w7-live-evidence.md`; its reviewer's verdict: "the evidence
  holds." Suppression/watermark behaved exactly as decisions/001 specifies.
  **Major catch (F1): the claude-cli distiller echoed 6 of 9 findings from
  `v4-condense.toml`'s own few-shot examples**, attributed as real
  contributions — the existing contamination test guards only the inverse
  direction. Targeted fix agent dispatched (prompt hardening + parse-time
  example-echo guard + the missing-direction regression test). Smaller
  catches: end_session orphans serve_local's legacy boot binding (F2), a
  leave/end double-unbind message (F3) — both queued behind W5 (same file).
- **09:30** — **F1 contamination fix merged** (`3c132bf`, 1214 tests). Root
  cause was prompt ASSEMBLY, not the model: the claude-cli arm flattened the
  few-shot message list into one unlabeled transcript and then asked the model
  to "rewrite the session above" — the session above was the examples. Fix:
  the flatten now names every block (example input / wanted reply / the
  material to work on), and the distiller gained a parse-time example-echo
  guard (order-aware similarity vs the pack's own examples, threshold 0.60 in
  the measured 0.46–0.67 gap, <10-word exemption, loud reason=example_echo
  drop log). The 9 findings from the live evidence replay verbatim in tests:
  6 drop, 3 survive. Prompt bytes untouched — the 809-token calibrated
  overhead has zero headroom against the pinned 2787 segment budget.
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
