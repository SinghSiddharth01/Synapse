# TLDR — overnight 2026-08-06

**Final, written 09:55 PDT.** The run is over; nothing is left running on the
host (ports 8899/8787/18181 verified free, no synapse processes).

## The one-line summary

All 11 planned workstreams plus two adopted ones **shipped and merged to main**;
the suite grew **888 → 1335, green at every merge**, and the closing gate ran
the full suite + `rehearse_demo.py` **ALL BEATS PASS on the real default ports**.
The lifecycle arc ran clean **twice against a real stack on the real ports** —
that evidence is in `docs/overnight/w7-live-evidence.md`.

## What shipped (chronological, each with its decision file where one applies)

- **Checklists** (`10783f4`) — `DEMO-READINESS-CHECKLIST.md` rebuilt agentic-only; new `HUMAN-TODO.md`, deadline-ordered.
- **FLOW.md + audits** (`2074873`) — contribute-path/limits/AI-100 investigation, docs audit, install scope.
- **Slides** (`c1d768f`, decisions/010) — `slide-content-outline.md` (every claim sourced, barred-claims gate) + `presentation/` two-tab skeleton.
- **W6 stress/regression** (`d03547e` + `d6c48cd`, decisions/006) — the four team fixes now revert-verified; Haiku 4K pinned in the provider; `effort` gated off Haiku (was a live 400 waiting for the demo).
- **W2 multi-session** (`a7845c1`, decisions/001) — per-session bindings, `agent_session_id` through every tool, suppression by conversation / watermark by contributor. Two windows of one human are genuinely two participants — **proven live in the W7 arc**.
- **W8 install** (`9ed1361`) — `install.sh` + `install.ps1` + `install.bat`, doctor pre-flight, `secrets.example.jsonc`, README one-paste join. Its reviews caught 4 HIGHs pre-merge (missing ps1, PATH bug, doctor rc swallowed, key routing).
- **W10 docs site** (`9ed1361`, decisions/007+009) — MkDocs+Material, six rebuilt areas, `--strict` clean, consistency-reviewed against code.
- **W1 GenieX idle death** (`b460684`, decisions/005+008) — retrieval failure is a **typed 503, never an empty 200**; in-app seam supervisor (probe→strikes→kill→respawn, live-verified); boot preflights; `--npu --live` split config.
- **W7 live arc** (`c8baded`) — create→join→contribute→query→leave→rejoin→end, twice, real ports, claude-cli Haiku arm; reviewer verdict: "the evidence holds".
- **F1 contamination fix** (`3c132bf`) — the claude-cli arm was echoing the prompt's few-shot examples as real findings (6 of 9 in the live run!). Root cause: the flatten erased role structure. Fixed both halves + regression tests in the missing direction.
- **W5 arrival summary** (`64a422f`, decisions/004) — join now delivers purpose + members + accumulated + new-since on **both** join paths (tool result and connect-path briefing). The storyboard's awareness beat fires.
- **W4a brain page** (`a789db5`, decisions/003) — `/debug` is now the state of the brain (participants per conversation, working memory + revisions, latest-into-memory), old page at `/debug/log`. Nothing fabricated — missing data is *named as missing* on the page.
- **W3b limiter** (`6b22767`, decisions/002) — worker→provider bounded (4/tick, 1 concurrent, 64 deferred with backpressure, visible four ways, persistent backlog); `/query` finally charged to the key governor; key-pool headroom warning at boot.

## What broke (all contained, all documented)

1. **06:00 isolation incident** — my wave-2 workflows didn't actually grant agents isolated worktrees; they shared mine and cross-committed. Stopped, recovered everything, relaunched correctly. Cost ~40 min. Full account: LOG.md 06:00. Lesson memorized for future sessions.
2. **Transient API deaths** hit 4 agents mid-night (connection closed). The pipeline shape absorbed all of them — most dramatically W2, where the review caught the dead pass-2 and the fix agent implemented it.
3. **W4b (dashboard pages 2/3)** — not reached; explicitly expendable per the plan. The only planned item not done.

## Needs you (ranked)

1. **HUMAN-TODO.md top section** — Microsoft Form owner + 3× feedback survey (Fri noon PST, per-person) + this morning's demo-order email.
2. **`secrets.jsonc`: the `anthropic.api_key` is an EMPTY STRING** — you believed both keys were present; only `inference_cloud` is. Anything on the `--distiller anthropic` arm needs a real key; tonight's live work used the claude-cli arm instead.
3. **Slides** — build from `docs/overnight/slide-content-outline.md` (skeleton in `presentation/`). Morning order-of-operations is at the bottom of the outline. Fresh test count for S7/S13: **1335**, but re-run morning-of.
4. **AI-100 keys** — ~10 to hold 60s synthesis latency (ADR 0005), or use the honest one-key framing in outline S7.
5. **Review `decisions/` 001–010** — each names its revert command. The suppression re-key (001) partially reverses `6d6779b` — the one you should eyeball first.
6. **5-min cleanup** — stale worktrees + branches under `.claude/worktrees/` (origin refs are the truth), and stash `4630ba2` on `overnight/journal` (redundant; verify, drop).

## Where everything is

`git log overnight-20260806-start..origin/main` is the whole night. Journal:
`docs/overnight/{LOG.md,STATE.md,FLOW.md,decisions/,w7-live-evidence.md}`.
Branches per workstream on origin; tags `overnight-20260806-start` and
`demo-fallback` both pushed. Nothing was force-pushed all night.
