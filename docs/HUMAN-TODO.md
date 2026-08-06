# Human TODO

Everything here needs a person: hardware, external submissions, recording, or a judgment
call. Moved out of `DEMO-READINESS-CHECKLIST.md` (now agentic-only) per the overnight plan
(W9/W11). Ordered by deadline. Today is **Thu 2026-08-06**; judging is **Fri 2026-08-07,
1:00–4:15pm PST**.

## Today — Thu 2026-08-06

- [ ] **Check inbox for the randomized demo-order email** — sent Thursday morning
      (demo-transcripts.txt:188-190). The slot time decides when the fallback freeze happens.
- [ ] **Chase the feedback survey now, not tomorrow.** Every team member must submit it by
      Friday noon and it is a stated *submission requirement*, per-person — three separate
      humans must act (demo-transcripts.txt:267-268). Zero trace of it anywhere in the repo.
- [ ] **Assign an owner for the Microsoft Form and start the video/slides production plan** —
      5 of the 7 presentation minutes are recorded content and none of it exists yet
      (demo-transcripts.txt:194-198).

## By Fri 2026-08-07, 12:00pm PST — hard deadlines

- [ ] **Submit via the Microsoft Form** (demo-transcripts.txt:267-268). Untracked in the repo
      until tonight; blocks everything else.
- [ ] **Feedback survey × 3 team members** (demo-transcripts.txt:268).
- [ ] **Personal-repo requirement — verified, keep it true** (demo-transcripts.txt:264-265).
      `SinghSiddharth01/Synapse` is personal, public, not a fork, MIT-licensed (checked via
      `gh api` 2026-08-06). Do not transfer it before submission. Confirm the README roster
      (README.md:103-109) matches the actually-submitted team.

## Before the Friday demo (1:00–4:15pm PST)

- [ ] **Record the demo video** — screen capture of both views, a person driving live Claude
      Code sessions (demo-transcripts.txt:159-163, 194-198).
- [ ] **Edit the video** — time-lapse the 2.5–3-minute pipeline waits (segment idle-flush 120s
      + synthesis debounce 60s, `config/synapse.toml:107,113`, `api.py:47`), captions over the
      sped-up parts ("Claude is now doing X") (demo-transcripts.txt:176-180).
- [ ] **Rehearse the 7-minute format** — 1 min live intro / 5 min video+slides / 1 min live
      outro (demo-transcripts.txt:194-198).
- [ ] **Decide the live portion** — keep any live interaction to the most basic/robust beats
      only (demo-transcripts.txt:183-184).
- [ ] **Prep the open-house round** — same assets working informally for roaming
      judges/families/managers; selection for the final six happens there
      (demo-transcripts.txt:204-206).
- [ ] **Real two-machine run** — worker → orchestrator → teammate-hosted service over real
      HTTP. Never attempted; all current e2e tests are in-process, zero real sockets
      (previous DEMO-READINESS-CHECKLIST, 2026-08-06). Needs a second physical machine; the
      plumbing audit stays agentic. Watch the `mcp==1.9.4` ARM64-Windows pin trap on the
      Snapdragon box (README.md:70).
- [ ] **A/B latency numbers on real hardware** — the harness is agentic; the NPU-box run
      (GenieX tokens/sec, local round-trip vs cloud) needs the device
      (demo-transcripts.txt:103-104; previous checklist "Not started").
- [ ] **Power measurement** — nothing measured; energy-efficiency claims stay OFF all slides
      until a real NPU power number exists (previous checklist;
      demo-transcripts.txt:243-244).
- [ ] **Competitive due diligence** — verify "nothing like this runs on local NPUs" before the
      differentiation slide ships; the transcript itself says "do a quick competitor check
      before final claim" (demo-transcripts.txt:110-113; previous checklist item).
- [ ] **AI-100 capacity: ~10 keys needed to hold 60s synthesis latency** (ADR 0005,
      `docs/adr/0005-the-synthesis-output-budget-is-derived.md`; recorded limit 20 req/hr,
      25k tok/hr ⇒ ~7 syntheses/hour per key, `config/synapse.toml`). Procure or pool keys
      before Friday, or accept visible synthesis lag on stage. A capacity fact, not a code
      change (docs/overnight/PLAN.md:75-76).
