# 010 — Slides and the demo site: build tonight, outline tonight, or leave for humans?

**Decided:** 2026-08-06 ~05:50 PDT, decision agent (protocol: PLAN.md:541-558).
**Trigger:** W11's wave-1 analysis found the deck + demo video are 5 of the 7 presentation minutes (demo-transcripts.txt:194-198) and no overnight workstream owns them. The agreed plan predates the finding; its nearest item, dashboard W4b, is explicitly expendable (PLAN.md:497, STATE.md). Submission is Fri 12:00pm PST; judging Fri 1:00–4:15pm.

## Question

Tonight's agents could produce the presentation assets nobody owns. Do we: (A) build a full HTML deck skeleton plus the two-tab website as a new late workstream; (B) write only a slide-by-slide content outline for humans to build tomorrow; (C) both, deck skeleton gated on every planned workstream merging first; (D) nothing — strictly out of agreed scope?

## Options

**A — deck skeleton + two-tab site tonight, new late workstream.**
- Pro: Claude-generated HTML slides are the team's *stated intent*, verbatim (transcripts:199-201) — this is not an agent's preference being smuggled in.
- Pro: tomorrow is the most time-poor day of the project (recording, editing, rehearsal, form, surveys — HUMAN-TODO.md:8-42); every deck-hour absorbed tonight is human runway returned.
- Pro: the rebuilt agentic checklist — already merged to main (10783f4) as the working checklist — lists "HTML slide deck" and "Two-tab site" under "Presentation assets (no workstream owns these)" with acceptance criteria (DEMO-READINESS-CHECKLIST.md:93-114). W11 itself classified this as agent work.
- Pro: a new top-level directory touches no code seam; cannot break the suite-green invariant.
- Con: the numbers the deck needs mostly do not exist yet — latency harness unrun, Beat 5 unre-verified, competitor check undone, power numbers barred outright. A "full" deck tonight is a deck full of placeholders, and a placeholder that looks finished is exactly the half-built-asset trap.
- Con: "don't put AI slop in the docs" (transcripts:122-123) is a standing instruction; an unreviewed generated deck is the nearest thing to that instruction's target.
- Con: 5am agent design taste is not the team's; visual polish may be redone regardless.

**B — content outline only.**
- Pro: small, safe, and unblocks humans regardless of every other outcome. It is judgment work over material tonight's agents uniquely have loaded (five transcript lenses, FLOW.md, ADR 0005, both checklists).
- Pro: an outline cannot be mistaken for a finished asset — zero confusion risk.
- Con: leaves the mechanical HTML work — the part the team explicitly wanted Claude to do — for the tightest hours of tomorrow.
- Con: under-uses a night with affordable Opus capacity while waves merge.

**C — both, skeleton gated on all planned workstreams merged first.**
- Pro: sequences code risk ahead of presentation polish.
- Con: the gate is wrong-shaped. The deck depends on no code workstream and shares no files with any of them, so the gate binds it to an unrelated condition. At 05:40 with W3b/W5/W7/W4a still queued behind five running workstreams (STATE.md), the gate realistically never opens before the user wakes — C is a B that promises an A. A gate that will not open is not caution; it is a commitment structured to fail.

**D — nothing, strictly out of scope.**
- Pro: purist reading of the agreed plan; zero new-asset risk; agents stay on code.
- Con: the plan's own "Not in scope tonight" list (PLAN.md:575-583) does not mention slides — they are *unowned*, not *excluded*. And the plan's own rule says W9 "runs continuously, feeding items into whichever wave fits" (PLAN.md:501-502): adopting a wave-1 finding into a late wave is the agreed mechanism working, not scope creep.
- Con: leaves 5 of 7 minutes owned by nobody, ~30 hours before judging, against the transcript's direct statement of intent.

## Transcript alignment

The transcript commits us to Claude-built HTML slides and a two-tab site; the only open variable is *when*:

- transcripts:199-201 — "Plan: build a single website with two tabs — one for the slide deck (HTML-based, built by having Claude generate animated HTML slides rather than a lower-fidelity PPT, since HTML gives full CSS/animation control)". Option A's exact artifact, named as the team's plan.
- transcripts:201-203 — "...and one for the 'Global View' / behind-the-scenes dev view, with the demo video embedded. Video should be placed at the END after slides give context, not before." Fixes the site structure and the video slot's position.
- transcripts:194-198 — the 7-minute budget: 1 min live intro / "5 minutes: recorded video and/or slides content" / 1 min live outro. The unowned gap is that middle 5.
- transcripts:209-228 — five slides named in the meeting: the efficiency graphic ("make it a graphic, not just a table", :211), use-case icon boxes (:216-219), parallelization architecture (:220-222), triage→distill→synthesize→retrieve pipeline (:223-226), competitive differentiation (:227-228). This is the outline's required spine.
- transcripts:214 — "we feed them the imagination, we don't leave it to their creativity" — argues for concrete, spelled-out slide content now; at minimum B.
- transcripts:122-123 — "don't put AI slop in the docs" — the honest counterweight. It constrains *how* anything is built tonight (transcript-committed text only, human review in the morning), not whether.
- transcripts:243-245, :170-172 — barred claims (no energy numbers; no topic-lane claim; never "one worker joins multiple sessions") — must be structurally excluded from anything built tonight.

The direction the team would have gone: they already said it — Claude builds the HTML deck. They would not have said "but only after the rate limiter merges."

## Decision

**B immediately, then a hard-scoped A. C's gate rejected; D rejected.**

1. **Outline first — the load-bearing artifact.** Fable writes `docs/overnight/slide-content-outline.md` (~20-30 min): slide-by-slide, mapped 1:1 to the rubric sections (transcripts:95-123) and the five named slides (:209-228), following the claim inventory already assembled in DEMO-READINESS-CHECKLIST.md:95-109. Every claim quoted from the transcript with line refs. Every number either sourced to a repo artifact (test count re-run before use, ADR 0005 capacity fact, FLOW.md effective limits) or marked `EVIDENCE PENDING: <checklist item>`. Barred claims listed at the top as a hard exclusion list. Humans can build the entire deck from this file alone.
2. **Skeleton second, strictly cut (scope below).** One Opus agent, ~45 min, builds the two-tab shell and deck skeleton *from the outline* — no content invented, every pending number an unmissable styled chip. It cannot confuse tomorrow's humans because nothing in it pretends to be finished and the outline remains the single source of truth.
3. **Sequencing rule:** if runway or capacity collapses, the skeleton is dropped and the outline stands alone. B is the floor and is never sacrificed for A.
4. `HUMAN-TODO.md`'s "start the video/slides production plan" item (HUMAN-TODO.md:15-17) gets a pointer to both artifacts; `TLDR.md` indexes this decision.

Why not full A: the deck's content is blocked on evidence tonight cannot produce and partly must not fake (latency numbers, Beat 5 re-verify, competitor check) — "full deck" is not honestly achievable tonight by anyone; only skeleton + outline is. Why not C: its gate binds the deck to workstreams it shares no files with, and at 05:40 it is known-unreachable before morning. Why not D: PLAN.md:501-502 makes W9 adoption the agreed mechanism, the merged agentic checklist already carries these items, and the transcript names the artifact.

## Scope cut — build exactly these files, nothing more

One Opus agent, ~45 minutes, branch `overnight/slides-skeleton`, merges when the two acceptance lines pass. Inputs: `docs/overnight/slide-content-outline.md` (must exist first), `docs/demo-transcripts.txt`, `docs/DEMO-READINESS-CHECKLIST.md:93-126`.

1. **`presentation/index.html`** — the two-tab shell. Tab "Deck" iframes `deck.html`. Tab "Global View" holds (a) a placeholder panel naming the live global-view URL as `TODO-HUMAN` (linking the existing `/debug` as the labeled stand-in), and (b) a `<video>` element with poster placeholder, positioned last per transcripts:202-203, inside a visibly-styled `TODO-HUMAN: demo video drops here` frame. Inline CSS/JS only, zero external requests, opens from `file://` and `python -m http.server`.
2. **`presentation/deck.html`** — standalone keyboard-navigable deck (arrow keys, one `<section class="slide">` per slide, CSS-only transitions). Slide text comes *only* from the outline; each slide carries an HTML comment citing its transcript lines. Every unsourced number renders as a warning-styled `EVIDENCE PENDING: <checklist item>` chip. Barred claims appear nowhere, including as placeholders.

Explicitly NOT tonight: storyboard screenshots (checklist :113-114 stays open), animation polish beyond CSS transitions, any invented metric or benchmark, the video file itself, a real Global View implementation, MkDocs/Pages integration (do not place under `docs/` — that is W10b's directory — and do not name the folder `site/`, MkDocs' default build output), README changes, npm or any build tooling.

Acceptance: (1) `presentation/index.html` opens in a browser showing both tabs with the video slot last; (2) `grep -riE 'energy|watt|power efficiency|topic lane|multiple sessions'` over `presentation/` hits nothing on the barred list, and every number in `deck.html` is repo-traceable or inside an EVIDENCE PENDING chip.

## How to undo

- Whole decision: `git revert` the merge commit of `overnight/slides-skeleton` — new files only, nothing depends on them, reverts clean. Delete `docs/overnight/slide-content-outline.md` in the same commit if the outline is also unwanted.
- The expected partial undo (keep outline, drop skeleton): `git rm -r presentation/` — the outline references nothing inside `presentation/`, so it stands alone.
- Nothing else moves: no code path, no config, and no doc outside `docs/overnight/` is touched by this decision.
