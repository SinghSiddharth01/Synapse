# Overnight state — per workstream

Rewritten to current truth at every transition. Times PDT, 2026-08-06.
Last rewrite: **09:50**. One workstream still running (W3b).

| ws | what | status | merged at | suite | notes |
|---|---|---|---|---|---|
| W11/W9 | transcript → two checklists | ✅ | `10783f4` | 888 | HUMAN-TODO.md deadline-ordered |
| W3a | flow investigation (FLOW.md) | ✅ | `2074873` | — | file:line cited |
| W10a | docs audit | ✅ | `2074873` | — | |
| W8a | install scope + adversarial review | ✅ | `2074873` | — | |
| slides | outline + two-tab skeleton | ✅ | `c1d768f` | 888 | decisions/010; outline = source of truth |
| W6 | regression cover + 4K pin + stress | ✅ | `d03547e` + postfix `d6c48cd` | 1125 | decisions/006; effort-key gate; crc32 seeds |
| W2 | multi-session | ✅ | `a7845c1` | 1080 | decisions/001; two windows = two participants, proven live in W7 |
| W8b | install.sh/.ps1/.bat + doctor | ✅ | `9ed1361` | 1109 | 4 HIGHs fixed pre-merge; ALL BEATS PASS shifted |
| W10b | MkDocs site, six areas | ✅ | `9ed1361` | 1109 | --strict clean; consistency-reviewed |
| W1 | GenieX idle death | ✅ | `b460684` | 1191 | decisions/005+008; typed 503; supervisor live-verified kill→respawn |
| W7 | live lifecycle arc | ✅ | `c8baded` | 1191 | **ran clean twice on real ports**; evidence verified; caught F1 |
| F1 | few-shot echo contamination | ✅ | `3c132bf` | 1214 | root cause: claude-cli prompt flatten; parse-time guard + marked blocks |
| W5 | arrival summary at join | ✅ | `64a422f` | 1265 | decisions/004; beat fires on BOTH join paths |
| W4a | dashboard Page 1 (brain page) | ✅ | `a789db5` | 1299 | decisions/003; `/debug` = brain, old page at `/debug/log`; nothing fabricated |
| W3b | worker rate limiter + /query metering | **🔄 running** (wf_4f1ccc2d) | — | — | decisions/002 pending |
| W4b | dashboard Pages 2+3 | not reached | — | — | explicitly expendable; parked by runway, not by failure |

**Main:** `2e57f6f` · suite **1299 green** at the last workstream merge.
Growth tonight: **888 → 1299** (+411), every merge gated on a full green run.

**Live evidence:** `docs/overnight/w7-live-evidence.md` — the full lifecycle arc
twice against the real stack on 8899/8787 (claude-cli Haiku arm), reviewer
verdict "the evidence holds". Ports verified free afterwards.

**Needs Sid (full list in TLDR/HUMAN-TODO):** anthropic key in secrets.jsonc is
an empty string; Microsoft Form + 3× survey today; slide production from the
outline; AI-100 keys (~10 for 60s latency).

**Morning cleanup** (5 min): stale worktrees under `.claude/worktrees/`
(`w1-geniex`, `w2-multisession`, `w6-stress`, `w8-install`, `w10-docs`,
`w11-transcript`, `agent-*`, `wf_*`) + stale local branches — origin refs are
the truth. Stash `4630ba2` on `overnight/journal` (redundant-looking; verify,
drop).
