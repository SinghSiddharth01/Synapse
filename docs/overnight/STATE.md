# Overnight state — per workstream

Rewritten to current truth at every transition. Times PDT, 2026-08-06.
Last rewrite: **08:32**.

| ws | what | status | where | notes |
|---|---|---|---|---|
| W11/W9 | transcript → two checklists | **✅ merged** | `10783f4` | HUMAN-TODO.md deadline-ordered |
| W3a | flow investigation | **✅ merged** | `2074873` | FLOW.md |
| W10a | docs audit | **✅ merged** | `2074873` | |
| W8a | install scope + review | **✅ merged** | `2074873` | |
| slides | outline + skeleton | **✅ merged** | `c2ca4b9`+`c1d768f` | decisions/010 |
| W6 | stress/regression | **✅ merged** | `d03547e` | 1032 tests at merge; 4K pin (decisions/006); post-merge fix agent running for confirmed review findings (incl. the `output_config.effort`-on-Haiku bug) |
| W2 | multi-session | **✅ merged** | `a7845c1` | 1080 tests; per-session bindings + agent_session_id on all tools + suppression/watermark split (decisions/001). Pass-2 died mid-flight; the review caught it, the fix agent implemented it — the pipeline shape worked. |
| W8b | install scripts | **✅ merged** | `9ed1361` | install.sh + install.ps1 + install.bat + doctor; reviews found 4 HIGHs (missing ps1, PATH bug, doctor rc discarded, secrets drift) — all fixed pre-merge; doctor rc 0; ALL BEATS PASS on shifted ports |
| W10b | docs site | **✅ merged** | `9ed1361` | six areas + mkdocs --strict clean; consistency-reviewed |
| W7 | lifecycle E2E | dev ✅ merged · **live arc running** (wf_a7b121a5) | → overnight/w7-live | real ports 8899/8787, claude-cli Haiku arm (API key empty — see below) |
| W1 | GenieX idle death | **🔄 fix+audit re-running** (resumed) | overnight/w1-geniex | dev done, 927 on branch; review found HIGH `kill_port_owner` client-kill bug; merge pending |
| W5 | arrival summary at join | **🔄 running** (wf_f946e3d5) | → overnight/w5-arrival | join_session returns purpose + summary + new-since |
| W4a | dashboard Page 1 | **🔄 running** (wf_5be786e9) | → overnight/w4a-dashboard | extends /debug (decisions/003) |
| W3b | worker rate limiter | queued — after W1 merges | — | |
| W4b | dashboard Pages 2+3 | expendable | — | unlikely to be reached |

**Main:** `9ed1361` (via `4c495be` journal) — **1109 tests green.** Suite growth
tonight: 888 → 1109. Every merge gated on a full green run.

**Finding for Sid:** `secrets.jsonc`'s `anthropic.api_key` is an **empty string**
(the inference_cloud block is fine). The live Haiku stress and any
`--distiller anthropic` path need a real key; tonight's live work uses the
claude-cli arm instead. Neither the doctor nor serve_local named this clearly
before — the doctor now warns on empty blocks.

**Morning cleanup for Sid** (5 min): stale worktrees under `.claude/worktrees/`
(`w1-geniex`, `w2-multisession`, `w6-stress`, `w8-install`, `w10-docs`,
`w11-transcript`, `agent-*`, `wf_*`) + their stale local branches — origin refs
are the truth. Stash `4630ba2` on `overnight/journal` looks redundant — verify,
then drop.
