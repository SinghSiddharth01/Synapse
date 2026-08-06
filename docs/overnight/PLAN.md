# Overnight plan — 2026-08-06

Everything in the brain-dump, cleaned up, grouped, and sequenced for autonomous
execution. **Nothing here is implemented yet.** Review and correct in the
morning; I will have been working from it.

Artifacts you will find when you wake up, all under `docs/overnight/`:

| file | what it is |
|---|---|
| `PLAN.md` | this — the agreed scope |
| `TLDR.md` | **read this first.** One page: what shipped, what broke, what needs you |
| `STATE.md` | living status per workstream, updated as each lands |
| `LOG.md` | chronological, timestamped, appended as I go |
| `decisions/NNN-*.md` | one per A-vs-B call I made without you, incl. how to reverse it |
| `parked/*.md` | dead ends, with full state so they can be resumed cold |

---

## ⚠️ Conflict with tonight's shipped work — read first

**W2 (suppression scoping) reverses part of `6d6779b`.**

Tonight we re-keyed **both** the watermark and self-suppression from
`agent_session` to `contributor`, because a new Claude Code conversation is a new
`agent_session_id` and that silently reset your place on rejoin.

Your dump now says:

> different sessions running on the same host don't get suppressed under the same
> flag. Each suppression should only be limited to that session for that user.
> Other sessions by that user on the same machine should be allowed even if
> they're the same agent

Those are incompatible as a single key. Two windows for the same human must
**share** a watermark but **not** share suppression.

**My call, executing unless you say otherwise:** split them — which is the option
I offered originally and you declined in favour of reusing `contributor`. Your
new requirement forces it.

| concern | key | rationale |
|---|---|---|
| watermark (`last_seen`) | `contributor` | must survive a new conversation, else rejoin replays everything |
| suppression (`visible_to`) | `agent_session` | window A's findings must be visible in window B |

Cost: three call sites and a contract note. It restores the pre-`6d6779b`
suppression behaviour while keeping the rejoin fix. Documented in
`decisions/001-split-suppression-from-watermark.md`.

---

## Workstreams

Ordered by demo risk, not by size. Each is independently mergeable.

### W1 — Geniex idle death *(demo-critical)*

**Symptom:** after X minutes idle, the process is alive but the HTTP server stops
serving; the URL goes unreachable.

**Deliverable:** the app survives it without human intervention.
- Health probe on the model seam, with a liveness definition that distinguishes
  "slow" from "dead" (a hung socket is not a crash).
- Supervised restart from the app itself, with backoff and a cap, so a genuinely
  broken NPU doesn't become a restart loop on stage.
- Every restart is logged loudly — a silent self-heal that hides a dying box is
  worse than the crash.

**CI:** you asked for it if testable. It is, at the seam: a fake endpoint that
accepts a connection then stops responding, asserting the supervisor notices and
recovers. That does not prove the real Geniex bug, and the test will say so.

**Blockage risk:** medium — reproducing the real idle timeout may need the NPU
box. If so, I build and test the supervisor against the fake and park the
real-hardware confirmation.

**Models:** Opus (dev) · Fable (design of the liveness rule).

---

### W2 — Multiple concurrent agent sessions *(correctness + capability)*

⟨REVISED 2026-08-06 after clarification — this is now the second-largest
workstream, not a three-call-site fix.⟩

**The real requirement.** One agent invocation = one session. A second Claude
Code window on the same machine is a **different worker** with its own entry, and
neither window's findings are hidden from the other. The use case is two sessions
debugging the same problem from different angles and sharing what they learn.

**What blocks it today.** `binding_path_for_agent` writes
`bindings/<agent>.json` — **one file per Agent PRODUCT**, not per session. Its
own docstring cites Plan D's limitation: *"one active Agent Session per Agent
product per machine"*. So a second window overwrites the first's binding and
becomes the same participant.

**What unblocks it.** Tonight's lifecycle work already added explicit
`agent_session_id` (from `CLAUDE_CODE_SESSION_ID`, which *is* the transcript
stem). That is precisely the identity MCP itself cannot supply. The enabler
exists; it is not yet threaded through.

**Scope:**
1. **Per-session bindings** — `bindings/<agent>/<agent_session_id>.json`, with a
   migration path from the single-file layout.
2. **Every tool call carries its session** — extend the optional
   `agent_session_id` argument from create/join to `query` and `contribute`, so
   the orchestrator can tell which conversation is speaking on every call. Falls
   back to the current single-binding behaviour when absent, so nothing breaks.
3. **The awareness pack teaches agents to pass it**, or the capability exists and
   nobody uses it.
4. **Suppression on `agent_session`; watermark on `contributor`** — the split
   from the conflict box, now with a concrete reason beyond rejoin.
5. **One orchestrator, N MCP clients.** No extra worker process — the
   orchestrator already runs as one HTTP server; it must stop assuming one
   binding.
6. Generalise across products, not just Claude Code — `AGENT_REGISTRY` already
   has Codex.

**Why this matters beyond correctness.** If it works, the workflow agents I run
tonight can talk to each other through Synapse — genuinely shared context between
agents working different angles of the same problem. **I will dogfood it:** once
it lands, later workstreams' agents contribute and query as distinct
participants, and whether that actually helps goes in `TLDR.md` as a finding
either way. That is also the strongest possible demo material.

**Dependency to note:** W4 Page 1 wants *"all our workers who joined, when they
left, where they are in their session"* — that is only meaningful once
participants are per-session. **W2 lands before W4 Page 1.**

**Blockage risk:** medium. The binding layout change touches worker,
orchestrator, and the pack. Mitigation: land per-session bindings behind
backward-compatible resolution first, so a half-finished state still runs.

**Models:** Fable (identity model + migration design) · Opus (dev) · full review
shape.

---

### W3 — Worker rate limiting + provider queueing

**Deliverable:** the worker bounds what it sends to a provider.
- Rate limit inbound work rather than accumulating an unbounded backlog.
- Bounded concurrency — no unbounded parallel provider calls.
- Shed or defer beyond the bound, visibly, rather than queueing forever.

**Paired investigation** — you asked three questions; these are answered in
writing as `docs/overnight/FLOW.md`, with file:line citations, not prose:

1. **The MCP `contribute` path** — end to end: what houses it, how it is wired,
   where the model call happens, what is bounded and what is not.
2. **Listener mode** — how output is batched/chunked today, and the actual limits
   in the current config (not the defaults in the code — the effective values).
3. **AI-100 config** — prompt size, context window, how much is spent on working
   memory, and where each number is set.

That investigation runs **first** because W6 depends on its answers.

**Blockage risk:** low for investigation, medium for the limiter (needs a policy
choice — see decisions).

**Models:** Fable (investigation, needs judgment) · Opus (limiter).

---

### W4 — Dashboard *(largest; demo-facing)*

Your requirement in one line: **the top level shows the state of the brain, not
the log.**

**Page 1 — Memory dashboard (default)**
- Workers: who is connected, joined/left, where they are in their session, last
  query time, health.
- Count of connected users.
- Working memory: current content, when last updated, last 5–10 revisions.
- Only top-level relevant state. Production-grade feel.

**Page 2 — Memory**
- The findings actually *in* memory now — what can be queried or appended to.
- Clean, filterable.

**Page 3 — History / raw**
- Everything the current dashboard shows today, moved here.

**Cross-cutting**
- Structured log rows, not raw JSON dumps: **author · text · contributed vs
  listened**.
- Every row **inspectable** — click to expand properties and values. Today it is
  flat text with no affordance.
- Basic filters (author, type, contributed/listened, time).
- Modern, polished, industry-standard layout. This may end up in the demo video.

**Constraint you set, which I will hold:** *don't invent new things* — build on
what the current plumbing already exposes. Where the data doesn't exist, I note
it rather than fabricating a metric.

**⟨SCOPE CUT 2026-08-06⟩ Page 1 only, and it does not get the night.** Your
words: the dashboard is *"a sink, not a source of any information"*. So:

- **W4a — Page 1 basics.** Runs after W2 (it needs per-session participants to
  have anything true to show). Ships or does not; either way it does not block.
- **W4b — Pages 2 and 3, filters, inspectable rows.** Only started once every
  other workstream is done and time remains. If the night runs out here, nothing
  of value is lost.

Splitting there because Page 1 is the only part that shows something we cannot
already see, and the rest is a nicer view of data we already have.

**Blockage risk:** low technically, high on scope — hence the cut.

**Models:** Fable (IA + visual direction) · Opus (implementation) · frontend
design skill.

---

### W5 — MCP arrival summary

**Problem:** a late joiner has no idea what has happened. Dumping everything is
wrong; the session may be hours old.

**Deliverable:** on join, a *summary* of what has accumulated, plus what is new
since. Add the endpoint if none exists. Summarisation may run on the AI-100
backend.

**Interaction with existing work:** the arrival briefing already exists
(`briefing.py`) and already carries a watermark. This is an upgrade to it, not a
new mechanism — I will extend rather than duplicate.

**Blockage risk:** low.

**Models:** Fable (what belongs in a summary) · Opus (endpoint + wiring).

---

### W6 — Stress testing: chunking and context bounds

**Deliverable:** tests that would have caught the context-growth bug, and would
catch the next one.
- Large inputs through the chunking path, asserting bounds hold.
- Context window growth is bounded and asserted, not merely observed.
- **Haiku capped at 4K context**, matching the other models — explicitly, not by
  default.
- AI-100 backend stressed with large blobs.
- Haiku used as the local test provider so this is runnable here.

**Depends on:** W3's investigation output (`FLOW.md`) for the real current limits.

**Blockage risk:** low — this is exactly the kind of work that parallelises.

**Models:** Opus (tests) · Fable (deciding what "out of bounds" means per seam).

---

### W7 — Confirm lifecycle actually works live

`create_session` / `join_session` / `leave_session` / `end_session` are shipped
and unit-tested, and the four tools appeared in a live MCP session tonight — but
the **end-to-end arc has never been driven against a real running stack**.

Deliverable: drive it, record the result, fix what breaks.

**Constraint:** the rehearsal script hardcodes `8899`/`8787` and silently
measures whatever is already running — that is how test fixtures got into your
real session tonight. **I will not run it against a live stack.** Port-shifted
copy only. Fixing that hardcoding is folded in here.

**Blockage risk:** medium — needs a stack up. I control that on a shifted port.

---

### W8 — Install scripts, Windows + macOS *(your explicit agent pipeline)*

You specified the shape; I will follow it exactly:

1. **Scoping agent** — what the install scripts must cover, both OSes.
2. **Review agent** — compares the scope against the **demo transcript** to
   confirm alignment with the transcript's goals.
3. **Planner agent** — specs, test cases, acceptance criteria.
4. **Opus developer agents** — implementation, parallel where separable.
5. **Review → fix → verify agents**, as in the workflows we have been running.

**Includes:** the Windows Unicode encoder fix — special characters fail to decode
until an env var is set; that becomes part of the install rather than a footnote.

**Blockage risk:** low. Highest parallelism of any workstream.

---

### W9 / W11 — Transcript analysis and the two checklists

Both documents are now pulled: `docs/demo-transcripts.txt` (268 lines — notes
extracted from recording-1, recording-2, **and the hackathon rules PDF**) and
`docs/DEMO-READINESS-CHECKLIST.md`, freshly trimmed to 4 open items.

The transcript is short enough that parallelism buys **angles, not throughput**.
Five readers, each with a different lens, then one synthesiser:

| lens | looking for |
|---|---|
| narrative | what the demo must *show*, and every claim the slides commit us to |
| technical commitments | behaviour promised in the recording that must exist in code |
| hackathon rules | judging criteria and compliance obligations from the PDF |
| gap analysis | claimed vs what actually ships today, with file:line |
| todo extraction | every actionable item, each classified **agentic** or **human** |

**Two output files, per your instruction:**

- `docs/DEMO-READINESS-CHECKLIST.md` — **agentic only.** Things I can finish.
  Stays the working checklist.
- `docs/HUMAN-TODO.md` — **new.** Everything needing a person: hardware, power
  measurement, judging submissions, recording, anything needing a decision or a
  body in a room. Restored from the transcript rather than left dropped.

The current 4 open checklist items look predominantly *human* to me (power
measurement, A/B latency numbers, a real two-machine run, competitive due
diligence), so expect that file to move most of them across and the agentic
checklist to be repopulated from the transcript.

Also folds in: any transcript item that belongs to an existing workstream gets
routed there rather than duplicated, and the gap-analysis lens feeds W10's
consistency review.

**Blockage risk:** none. Pure read + write.

**Models:** Fable (all five lenses — every one of them is judgment) · Fable
(synthesis).

---

## ⚠️ Overlap with the other session's just-landed work

Pulling brought in four commits from the concurrent session that sit **directly
on top of W6's territory**:

| commit | overlaps |
|---|---|
| `7c42e96` fix(worker): split an event too big for the budget, and make the retry a retry | chunking — W6 |
| `7418a63` fix(config): honour the response_reserve, and stop timing out on a live push | context budget — W6 |
| `282fd07` fix(service): a synthesizer that speaks the endpoint GenieX actually serves | model seam — W1, W6 |
| `c077a51` fix(providers): a 400 that carries findings is not a 400 with nothing in it | provider error handling — W3 |

**W6 therefore starts by reading these four commits**, not by writing tests. Some
of what I planned to find may already be fixed, and duplicating it would be worse
than useless — it would produce contradictory tests. Same discipline for W1 and
W3.

---

### W10 — Documentation

**Publishing: keep it in-repo, do NOT use the GitHub wiki.** Reasoning, since
you asked:

A wiki is a *separate git repository*. Docs stop moving in the same commit as
the code they describe, they leave PR review, and they leave `grep`. This repo's
whole discipline is the opposite — dated evidence next to the thing it explains,
`CONTEXT.md` as canonical invariants, ADRs beside the decisions. **We hit exactly
this failure tonight:** `CONTEXT.md` asserted the opposite of shipped suppression
behaviour, and the fix was only safe because the doc and the code landed in one
commit. A wiki structurally prevents that.

**Recommendation:** stay in `/docs`, and if you want a formal published face, add
**GitHub Pages** built from `/docs`. Same files, versioned, PR-reviewable, and it
renders as a real site. If a wiki is genuinely wanted for discoverability,
*generate* it from `/docs` in CI so the repo stays the single source of truth —
never hand-edit it. Recorded as `decisions/007-docs-in-repo-not-wiki.md`.

**Pipeline:**
1. **Audit agent** — inventory every doc; flag stale, contradicted-by-code, or
   orphaned. Specifically hunt the `CONTEXT.md` failure class: a doc asserting
   something the code no longer does.
2. **Writer agents, parallel by area** — README and first-run story · architecture
   (current, not aspirational) · service HTTP reference · MCP tool reference
   (now ten tools, documented nowhere in one place) · troubleshooting ·
   contributor guide.
3. **Consistency reviewer** — cross-checks every claim against code, with
   file:line. This is the agent that would have caught `CONTEXT.md`, and it is
   the one worth keeping permanently.

**Blockage risk:** very low. Highest parallelism of anything here, and no
dependency on a running stack.

**Models:** Fable (audit + consistency review, needs judgment) · Opus/Sonnet
(writers).

---

## How I will work while you sleep

**Branch per workstream, merge when green.** Not direct to main. Another session
is also committing there, and tonight our two workstreams got tangled into each
other's commits twice. Branches make every piece independently reviewable and
revertable — which is your "if it's redoable, do it that way".

**Ordering** ⟨revised — W4 demoted, W2 promoted⟩:

| wave | workstreams | why |
|---|---|---|
| 1 | **W11 transcript** (5 lenses) · **W3 investigation** · **W10 docs audit** · **W8 scoping** | all pure-read, zero dependencies, start everything at once |
| 2 | **W1 Geniex** · **W2 multi-session** · **W8 dev** · **W10 writers** | different subsystems, fully parallel |
| 3 | **W5** · **W6** · **W7** · **W3 limiter** | W6 reads the other session's four commits first; W7 needs a stack on a shifted port |
| 4 | **W4a** dashboard Page 1 | needs W2's per-session participants to show anything true |
| 5 | **W4b** — *only if time remains* | explicitly expendable |

W11 lands first on purpose: it may add or re-prioritise items in every later
wave, and discovering that at 3am is cheaper than at 7am.

W9 (demo-readiness sweep) runs continuously, feeding items into whichever wave
fits.

**Model assignment.** Fable 5 orchestrates and takes frontier/judgment work.
Opus for development. Smaller models for mechanical passes. Every workstream gets
the review → adversarial-verify → fix → audit shape we used tonight, because it
caught real defects both times.

**Decision rules you gave me, which I will apply literally:**

1. Easy and modular → take the modular option, so tomorrow you can swap it.
2. Big A vs B → my recommended choice, fully documented, with the reversal noted.
3. Prefer whatever is redoable in git.
4. Dead end → **park it**: write `parked/<name>.md` with full state, move on. No
   workstream blocks another.
5. Never wait on you. Anything needing you goes in `TLDR.md` under "needs your
   call" and I proceed with the reversible option meanwhile.

**Logging cadence.** `LOG.md` appended at every workstream transition and every
merge. `STATE.md` rewritten to current truth at the same points. `TLDR.md`
written last, and rewritten if anything material changes after.

---

## Things I already know I will have to decide without you

Listed now so nothing is a surprise. Each gets a `decisions/` file.

| # | decision | leaning |
|---|---|---|
| 001 | Split suppression from watermark (see conflict box) | split; suppression → `agent_session` |
| 002 | Rate limiter policy: shed vs defer vs block | defer with a visible bound; shedding loses work silently |
| 003 | Dashboard stack — extend current `/debug` vs new app | extend, per your "don't invent too many new things" |
| 004 | Arrival summary computed where — service, or AI-100 | service-side, cached; AI-100 only if quality demands |
| 005 | Geniex supervisor lives in app vs a wrapper script | in-app, so it works for teammates who never read the runbook |
| 006 | Haiku 4K cap enforced in provider vs config | provider, with config override — config alone is too easy to miss |

---

## Not in scope tonight

- Service-side log persistence. It is the real fix behind two stopgaps
  (`ended.py`, `session_meta.py`) but it is a substantial change and not
  demo-blocking.
- The `_warn_if_identity_was_not_taken` concurrency race — reported, deliberately
  not repaired, correct as-is.
- Removing the two stale `created_by: "resync"` mock bodies in `test_cli.py`.
  Cosmetic; I will sweep it if a workstream lands nearby.
