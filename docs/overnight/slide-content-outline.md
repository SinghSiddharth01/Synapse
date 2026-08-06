# Slide content outline — build the whole deck from this file

Written overnight 2026-08-06 (~05:55 PDT), per decision 010
(`docs/overnight/decisions/010-slides-and-demo-site.md`). Every claim carries a source:
`transcripts:N` = `docs/demo-transcripts.txt` line N; repo paths are repo-relative;
`⚠ EVIDENCE PENDING` chips name the exact checklist item that must close before the
claim goes on a slide. **No number in this file may be replaced by a nicer-sounding one.**

---

## ⛔ HARD GATE — claims barred from every slide, caption, and doc

Run this list against the finished deck before recording. If any appear, remove them —
they are barred by the team's own decisions, not by caution:

1. **No energy/power/watt numbers, ever.** Nothing was measured (transcripts:243-244;
   HUMAN-TODO.md "Power measurement" — still open). "Energy efficiency" may appear only
   as a *rubric axis we address architecturally* (always-on work moved off CPU/GPU onto
   the NPU), never as a measured claim.
2. **No topic-lane retrieval claim.** The topic *lane* measured zero yield and ships
   OFF (`service/lanes.py:79`; CONTEXT.md "Topic" entry; transcripts:244-245). Topic
   **labels** in the arrival briefing are the only thing that may be mentioned.
3. **Never "one worker joins multiple sessions."** One agent joins one Shared Session
   at a time; the demo uses multiple agents instead (transcripts:170-172;
   `packs/claude-code/INSTALL.md:69-71`).
4. **Never "requires Snapdragon" / "only works on Snapdragon."** Agreed framing is
   versatile-plus-assist (transcripts:50-58). See slide S8 for the exact wording.
5. **No stale test count.** Write "N tests passing" as a placeholder and re-run
   `uv run pytest -q` morning-of for the true number (transcripts:234-235 quoted 526 on
   08-05; the suite grew overnight; any number typed tonight is wrong by morning).
6. **Session-history list = roadmap gesture only**, not a built feature
   (transcripts:165-168).
7. **No AI slop.** Every on-slide sentence below is terse and human-voiced; keep it
   that way (transcripts:122-123).

Grep sweep before shipping: `grep -riE 'energy|watt|power efficiency|topic lane|multiple sessions'` over the deck must hit nothing.

---

## Deck map — 1:1 to the rubric (transcripts:95-123)

| Rubric section | Pts | Slides |
|---|---|---|
| Technical Implementation | 40 | S4 pipeline · S5 parallelization · S6 efficiency graphic · S7 numbers/profiling · S8 Snapdragon story |
| Application Use-Case & Innovation | 25 | S2 problem · S3 value prop · S9 use-case boxes · S10 differentiation · S11 the aha moment |
| Deployment & Accessibility | 20 | S12 one-command install / runs anywhere |
| Presentation & Documentation | 15 | S13 engineering quality (plus the deck itself, the README, the docs site) |
| — narrative | — | S1 title · S14 roadmap & close |

The five slides named by name in the meeting (transcripts:209-228): efficiency graphic → S6, use-case icon boxes → S9, parallelization architecture → S5, pipeline → S4, competitive differentiation → S10.

---

## S1 — Title

**On-slide:**
> **Synapse**
> Shared working memory for AI-assisted teams.
> Your agent stops working alone.

**Visual:** wordmark + one connecting-nodes motif. No hardware logos yet — Snapdragon enters at S8 on our terms.
**Source:** README.md:1-3.
**Speaker note:** one breath, then straight to the problem.

## S2 — The problem *(Use-Case 25)*

**On-slide:**
> Every engineer has a coding agent. Every agent is blind to the team.
> - **Duplication** — agents redo each other's exploration and debugging.
> - **Asymmetry** — decisions, findings, dead ends don't propagate; a human has to copy-paste them around.

**Visual:** two developers, two agent windows, identical work happening twice — a red mirrored loop.
**Speaker note (the anecdote):** systems engineer writes MATLAB, software engineer ports to C++; every bug found on one side needs a manual message before the other's agent knows. Synapse removes the relay step (transcripts:17-20).
**Sources:** duplication + asymmetry framing transcripts:10-15; README.md:5-7.

## S3 — The idea *(Use-Case 25)*

**On-slide:**
> "It's like you join any Google Doc together" — but for coding agents.
> - **Save time** — never re-research what a teammate's agent already found.
> - **Save tokens** — never re-run the same exploration twice.

**Visual:** the two red loops from S2 collapse into one shared memory node feeding both agents.
**Speaker note:** say it both ways — product ("your team doesn't repeat itself") and technical ("deduplication is an optimization: we triage and distill before anything touches an LLM") (transcripts:39-43). That bridge sentence is the handoff into the 40-pt technical section.
**Sources:** quote transcripts:22-23; pillars transcripts:34-37; dedup-as-optimization transcripts:39-43.

## S4 — How it works: triage → distill → synthesize → retrieve *(Technical 40; named slide, transcripts:223-226)*

**On-slide (pipeline, four stages, annotated):**
> **Triage** — deterministic, no LLM. Decides what's worth processing at all.
> **Distill** (edge, NPU) — a small local model compresses raw work into structured Findings. Raw transcript never leaves the device.
> **Synthesize** (cloud, AI-100) — a 70B model merges everyone's findings into one working memory: dedup, conflicts, lineage.
> **Retrieve** (MCP) — agents pull on demand, natural language. Nothing injected unprompted.
> *Not everything goes to an LLM — triage and ranking are deterministic. That is the optimization.*

**Visual:** left-to-right pipeline; the Triage stage visibly filters (most input stops there); a lock icon on the device boundary for the privacy line.
**Speaker note:** finding types = learnings, decisions, dead ends, open questions, each tagged contributor + time (transcripts:83-86). Retrieval is pull-only — "your agent becomes aware" is a query, not an injection (transcripts:89-90).
**Sources:** transcripts:80-90, 223-226; deterministic triage `packages/worker/src/synapse_worker/triage.py:96-147` (via docs/overnight/FLOW.md §2); privacy line README.md:17; shipped local model = Qwen3-4B-Instruct W4A16 (`config/synapse.toml`, FLOW.md limits table).

## S5 — Parallelization: nothing waits *(Technical 40; named slide, transcripts:220-222)*

**On-slide:**
> The listener runs off your critical path.
> - You keep coding; the NPU distills in the background.
> - Findings are queryable the instant they land — synthesis catches up behind them.
> - The round-trip hides inside the pause a human takes between prompts anyway.

**Visual:** two horizontal timelines (developer / Synapse) showing overlap — compute and communication concurrent, not serial. This is the direct answer to the rubric's latency line.
**Speaker note:** "human latency is actually a benefit" — the system completes within natural human think-time, so it never needs to be the bottleneck (transcripts:179-182). *Honesty caution for whoever narrates:* the passive worker path is genuinely background; the explicit `contribute()` tool call is synchronous by design (FLOW.md §1.1). Present the passive path as the parallelization story.
**Sources:** transcripts:101-105, 176-184, 220-222; findings-queryable-before-synthesis `packages/service/src/synapse_service/api.py:492-499` (FLOW.md hop 7); debounce `api.py:500-510`.

## S6 — Every axis of efficiency *(Technical 40; named slide — "make it a graphic, not just a table", transcripts:211)*

**On-slide — one graphic, four spokes (rubric's own axes), each with its mechanism:**
> **Resource utilization** — always-on distillation on the Hexagon NPU; CPU/GPU stay free for your compile/test loop.
> **Optimization** — deterministic triage before any model call; bounded prompts however big the log grows (candidates 20, retrieval top-K 14); fixed-cost working memory rewritten per merge.
> **Latency** — background pipeline hidden behind human think-time (S5); merges debounced, never blocking.
> **Energy efficiency** — always-on work moved to the NPU, the silicon built for it. *(architectural claim only — no measured numbers, see hard gate #1)*

**Visual:** radial/quadrant graphic, icon per axis, one-line mechanism under each. Explicitly NOT a table. "We feed them the imagination, we don't leave it to their creativity" (transcripts:214) — spell each mechanism out.
**Sources:** axes = rubric transcripts:99-105; NPU-off-critical-path README.md:25; CANDIDATE_WINDOW=20 `synthesis.py:34`, TOP_K=14 `api.py:39` (FLOW.md §1.2); fixed-cost working memory CONTEXT.md "Working Memory"; derived synthesis budget ADR 0005.

## S7 — The numbers *(Technical 40 — the profiling slide, transcripts:103-104)*

**On-slide (only what is true tonight; chips for the rest):**
> - Cloud merge round-trip, measured live: **12.6–52.8 s** against Llama-3.3-70B.
> - One AI-100 key sustains **~6–7 syntheses/hour** (25k tokens + 20 requests/hour); **~10 keys** hold 60-second merge latency under continuous load. We meter spend and defer, we don't fall over.
> - **N tests passing** ⚠ *re-run `uv run pytest -q` morning-of; do not reuse any earlier count*
> - ⚠ EVIDENCE PENDING: "A/B latency harness + software-side numbers" (checklist) + "A/B latency numbers on real hardware" (HUMAN-TODO) — local round-trip vs cloud, GenieX tokens/sec.

**Visual:** big-number cards; the pending cards visibly greyed with the chip label so nobody mistakes them for done.
**Speaker note:** the capacity line is a *strength* framed honestly — the spend governor (rolling-hour token ledger, prices the next round at the max of the last five, charges failed rounds too) is real engineering, cite it if asked. If the keys aren't procured by Friday (HUMAN-TODO), say "synthesis lags visibly on one key; findings stay queryable throughout" — which is true and demonstrable.
**Sources:** 12.6–52.8 s measured latencies `packages/orchestrator/src/synapse_orchestrator/relay.py:128-143` (FLOW.md hop 8); capacity math `docs/adr/0005-the-synthesis-output-budget-is-derived.md` (Consequences: 240k tokens/hour needed at 60 s sustained vs 25k available per key; governor ~6 rounds/hour on one key; ~7 needed at observed load) + HUMAN-TODO.md AI-100 capacity item; governor `api.py:80-86, 238-261`.

## S8 — Why Qualcomm: the division of labor *(Technical 40 + the messaging slide)*

**On-slide:**
> "Edge distillation on Snapdragon plus cloud synthesis on Cloud AI 100 is the division of labor this hardware was designed for."
> - **Edge:** 4B-class models locally on the Hexagon NPU via GenieX — already optimized for the NPU; raw work never leaves the device.
> - **Cloud:** Llama-3.3-70B on Cloud AI 100 for cross-team synthesis.
> - **Versatile by design:** runs anywhere; if Qualcomm hardware is available, it gets measurably better and cheaper.

**Visual:** device-and-cloud split diagram; Snapdragon X Elite badge on the edge half, Cloud AI 100 on the cloud half; a small "any backend" strip underneath (CPU/GPU/NPU/other vendors) so Snapdragon reads as first-class, not a cage.

**Speaker notes — the agreed messaging strategy, follow it exactly (transcripts:46-75):**
- Never "requires Snapdragon" / "only on Snapdragon" — don't downgrade the product to sell the sponsor (transcripts:50-51).
- Don't oversell as frontier either. The team's own analogy: *"We're in Maruti's company [Qualcomm/edge], not Ferrari's [frontier cloud models] — you can still do real things within the constraints, cost-effectively."* (transcripts:52-54)
- The pitch order, verbatim: *"We start with Snapdragon, then Snapdragon Flow, then sell at the end — assists with Snapdragon but if not available you can still use it."* (transcripts:56-58)
- Judges weigh open-source viability beyond Snapdragon — don't box the story in (transcripts:59-60).
- Honest current status: the meeting agreed to say *"the current implementation requires a Snapdragon device"* about the MVP, honestly, while the product's future does not (transcripts:61-64) — call it "the current product, our working MVP," never "the demo." **Tonight's repo makes the stronger claim simply true** — see S12; if asked, say the versatile path already exists in code.
- GenieX is *leveraged* infra ("already optimized for the NPU"), not something we built — cite it that way (transcripts:70-71).
- The meeting also claimed 8B models run locally on the NPU (transcripts:66-67); the shipped distiller is Qwen3-4B (`config/synapse.toml`) — keep 4B on the slide, 8B only as spoken capability claim if the team stands behind it.

**Sources:** division-of-labor line transcripts:91-92 = README.md:27 (verbatim in both); NPU/AI-100 pairing transcripts:65-71; versatility transcripts:72-75; 70B on AIC100 `packages/providers/src/synapse_providers/aic100.py:34,151`.

## S9 — What you'd use it for *(Use-Case 25; named slide, transcripts:216-219)*

**On-slide — six icon boxes, one line each:**
> 🐛 **Debugging together** — one teammate's dead end is everyone's dead end.
> 🔧 **Feature work** — parallel building without parallel re-discovery.
> 🎨 **Design & brainstorming** — decisions propagate the moment they're made.
> 📣 **Status sharing** — your agent already knows what the team did today.
> 🧪 **Lab ↔ dev** — on-target context meets code context in one memory.
> 🔀 **Asymmetric teammates** — MATLAB author + C++ porter, no manual relay.

**Visual:** 2×3 icon grid; equal-weight boxes. Small footer: *"PM visibility · enterprise/defense — opt-in visibility call-outs"* kept deliberately quiet (transcripts:218-219).
**Sources:** transcripts:107-112, 216-219; lab-vs-dev README.md:51; MATLAB→C++ transcripts:17-20.

## S10 — Nothing else does this here *(Use-Case 25; named slide, transcripts:227-228)*

**On-slide:**
> Shared-memory systems exist. Agent frameworks exist.
> **Nothing combines team-shared agent memory with local NPU inference.**
> ⚠ EVIDENCE PENDING: "Competitive due diligence" (HUMAN-TODO) — the transcript itself orders "a quick competitor check before final claim" (transcripts:113). This slide DOES NOT SHIP until that check is done.

**Visual:** 2×2 positioning quadrant (shared memory ↔ private; cloud-only ↔ local/NPU) with Synapse alone in its quadrant — but only after the check confirms the neighbors are empty.
**Sources:** claim transcripts:110-113; pending item HUMAN-TODO.md "Competitive due diligence."

## S11 — The moment it works *(Use-Case 25 — the demo's aha, sets up the video)*

**On-slide:**
> A teammate joins late. Their agent is briefed on arrival.
> They start their own task — and before doing any work, their agent says:
> *"Sid already ruled this out."*
> In live rehearsal, the system merged two findings from different contributors — **24 entries apart** — unscripted.

**Visual:** two terminal screenshots side by side: the join briefing, and the proactive "already ruled out" surface. (Storyboard screenshots are a checklist item — "Storyboard screenshots for slides" — capture during tomorrow's recording.)
**Speaker note:** this is the asymmetry-closing claim with genuine evidence behind it. **Morning gate:** the checklist requires Beat 5 to be re-verified against Llama-3.3-70B before this callout ships ("Re-verify Beat 5 reproduces" — checklist). If it doesn't reproduce, soften to "in an earlier live rehearsal" and cut the number from the slide.
**Sources:** storyboard beats transcripts:139-154 (join briefing :139-143, aha beat :144-149, don't-make-it-instant note :150-154); unscripted merge transcripts:236-240; decision-propagation beat transcripts:155-157 (checklist item, script it into the video).

## S12 — Runs anywhere. One command. *(Deployment 20)*

**On-slide:**
> ```
> git clone … && uv sync
> uv run python scripts/serve_local.py --purpose "fix the flaky auth test"
> claude mcp add --transport http --scope project synapse http://127.0.0.1:8787/mcp
> ```
> A teammate joins with one command and a session id.
> **Five interchangeable model providers behind one seam** — NPU (GenieX), Cloud AI-100, Anthropic API, Claude CLI, offline stand-in. No NPU? No cloud key? It still runs, end to end, on any machine.

**Visual:** terminal block + a provider-seam diagram: one `ModelProvider` interface, five plugs.
**Speaker note:** this is *stronger than what the recording claimed*. The meeting honestly conceded "the current implementation requires a Snapdragon device" (transcripts:61-64) — tonight's code has outgrown that concession: the stand-in makes the full pipeline runnable anywhere (README.md:60), and the five providers are real, shipped code. Say the true, stronger thing and cite it. Demo plan: show the one-command install live as the joining teammate's first-time experience (transcripts:115-117).
**Sources:** commands README.md:64-88; runs-without-hardware README.md:60; providers `packages/providers/src/synapse_providers/__init__.py` (AIC100Provider, AnthropicProvider, ClaudeCliProvider, FakeProvider, NPUProvider — verified tonight); join flow README.md:86.

## S13 — Built like we mean it *(Presentation & Documentation 15)*

**On-slide:**
> - **N tests passing** ⚠ *re-run `uv run pytest -q` morning-of*
> - Append-only log, derived state, budgets derived not hard-coded — 5 ADRs documenting real decisions and one real 2am outage.
> - Rehearsed fallback: tag `demo-fallback`, pushed to origin. Plan B is real, not hypothetical.
> - ⚠ EVIDENCE PENDING: "Lint job in CI" + "README badges + a tagged release" (checklist — both cheap, do before recording)

**Visual:** repo screenshot with badges (once they exist) + the ADR file list.
**Speaker note:** the fallback line is a maturity signal, use it if asked about robustness (transcripts:240-242). `demo-fallback` verified on origin tonight (`git ls-remote origin` → `refs/tags/demo-fallback`), which closes the checklist's "Push `demo-fallback`" item — the claim is now recoverable from GitHub by anyone. 8B cloud fallback config is the named capacity fallback (transcripts:239-240) — verify before claiming aloud (checklist "8B cloud fallback config verified").
**Sources:** ADRs `docs/adr/0001–0005`; docs-site decision `docs/overnight/decisions/009-mkdocs-material-for-pages.md`; no-AI-slop instruction transcripts:122-123 (governs the deck, doesn't go on it).

## S14 — Where this goes *(close)*

**On-slide:**
> Today: humans collaborate through their agents' shared memory.
> Next: sessions that persist and accumulate · a browsable session history · agent-to-agent collaboration on top of the same memory.
> **Your agent stops working alone.**

**Visual:** three-step horizon graphic; the demo-video splash follows this slide.
**Speaker note:** everything on this slide is explicitly parked vision, not current capability (transcripts:25-29 — "we can't sell all this now… but we can list it as potential use cases"); session-history list is a roadmap *gesture* (transcripts:165-168; show mostly-closed past sessions in the GUI mock, claim nothing). Stretch goals README.md:43-47.

---

## Production notes for tomorrow (9am crew)

**The 7 minutes (transcripts:194-198):** 1 min live human intro → 5 min recorded video and/or slides → 1 min live human outro. Slides give the concept, **video goes at the END** — the audience needs the concept before the mechanism (transcripts:202-203). Suggested split of the recorded 5: ~2:30 slides (S2–S12 core; S1/S13/S14 can be near-instant), ~2:30 video.

**Recording the video (transcripts:176-184):** Claude Code turns take 2–3+ minutes — pre-record and **time-lapse the waits**, captioned over the sped-up parts ("Claude is now doing X") so it reads as work, not dead air. The honest talking point while it speeds by: the round-trip hides behind human think-time (transcripts:179-182). Record both views side by side — front/UX view and behind-the-scenes global view, correlatable 1:1 (transcripts:159-163). Any *live* portion: only the most basic, robust interactions (transcripts:183-184). Pipeline waits to time-lapse: segment idle-flush 120 s + synthesis debounce 60 s (`config/synapse.toml:107,113`, `api.py:47-48` — via HUMAN-TODO.md).

**One config trap before recording:** `scripts/demo_local.py` does not set `INFERENCE_CLOUD_MAX_TOKENS`/`TIMEOUT`, so it runs the pre-ADR-0005 800-token/60s budget; only `scripts/serve_local.py:344-345` carries the 1600/180s fix (FLOW.md §3, flagged discrepancy). Record through `serve_local.py` or export the two env vars first.

**Morning order of operations:** (1) `uv run pytest -q` → real test count into S7/S13; (2) re-verify Beat 5 → S11 number ships or softens; (3) competitor check → S10 ships or dies; (4) lint CI + badges + release tag → S13 chips close; (5) screenshots during the recording → S11.

**Pointers:** decision record `docs/overnight/decisions/010-slides-and-demo-site.md` · claim inventory `docs/DEMO-READINESS-CHECKLIST.md` ("Presentation assets" + "Copy honesty audit") · human items `docs/HUMAN-TODO.md` · deck skeleton, if the overnight agent built it, at `presentation/index.html` + `presentation/deck.html` on branch `overnight/slides-skeleton` — the skeleton renders this outline; **this file stays the single source of truth.**
