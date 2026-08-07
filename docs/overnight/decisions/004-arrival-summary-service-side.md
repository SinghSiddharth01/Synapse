# 004 — The arrival summary is computed service-side and cached, with no model call

**Status:** Decided, implemented (2026-08-06)
**Workstream:** W5 — MCP arrival summary
**Author:** decision agent, per the Decision Agent Protocol (PLAN.md)

## Question

W5's deliverable is: *on join, a summary of what has accumulated, plus what is
new since* — and PLAN.md adds *"summarisation may run on the AI-100 backend."*

Two independent questions hide inside that sentence, and they have to be
answered together because the answer to one constrains the other:

1. **Who assembles the summary** — the SERVICE (which holds the Finding Log,
   the topics, the working memory and every asker's watermark) or the
   ORCHESTRATOR (which holds the MCP connection and would have to fetch the raw
   material to compose it)?
2. **Is it MODEL-GENERATED** — an AI-100 call that writes prose about the
   session — or assembled deterministically from what is already stored?

The join path is the constraint. `join_session` is a tool call a human is
sitting in front of, waiting for; whatever this costs, they pay it while
watching a cursor.

## Options

### Option A — orchestrator-side assembly, no model

Fetch findings and the watermark, compose the text in `briefing.py`.

**Pros**
- No new service route; `briefing.py` already composes agent-facing text and
  already has the cleaning and capping machinery.
- The orchestrator is the single egress, so nothing new crosses a trust
  boundary.

**Cons**
- **It needs data no route serves.** "What has accumulated" is the whole
  visible log, grouped; `/query` returns a RANKED, top-k, model-filtered slice
  answering a question, and `/watermark` returns counts. Getting the raw
  material to the orchestrator means either a new route that dumps the log
  (which is the "dumping everything is wrong" failure, just moved one hop) or
  several round trips per join.
- **The watermark slice cannot be computed there at all.** "What is new to
  this person" is `store.seen_count` — service-side state, per contributor,
  with no wire representation. The orchestrator would be inferring it from a
  version delta, which is exactly the inference that is wrong (see below).
- Suppression (invariant 3) is defined in `retrieval.visible_to` and applied by
  the service. A second implementation in the orchestrator is a second answer
  to "what may this asker see", and the two would drift.

### Option B — service-side assembly, no model call ⟵ **chosen**

A `GET /v1/sessions/{sid}/arrival` route; `arrival.compute` assembles both
sections from the store; the orchestrator fetches the rendered text and returns
it inside `join_session`'s result.

**Pros**
- Everything it needs is already there and already correct: the fold, the topic
  labels, `SessionContext.working_memory`, the conflicts, and both watermarks.
- Suppression stays defined once. `arrival.compute` calls the same
  `visible_to` that `/query` and `/watermark` call, so a joiner's counts cannot
  disagree with the counts the briefing quotes at them.
- Answers in single-digit milliseconds, and cannot fail a join.
- Cacheable, because the service knows when its own content changed.

**Cons**
- A new route on the service's public surface, which is one more thing to
  version and one more thing an un-upgraded orchestrator can 404 on. Mitigated
  by the fetch failing open to None (a join that worked stays a join that
  worked).
- The service now composes AGENT-FACING PROSE, which it did not before —
  everything it returned was structured. Accepted, and bounded: the response
  carries both, `text` and the structured fields, so a future consumer that
  wants to render its own is not blocked by this one.

### Option C — service-side, summarised by a model (AI-100 or Haiku)

**Pros**
- Genuinely fluent prose, and a model can compress twelve findings into two
  sentences in a way no grouping rule can.
- It is what PLAN.md left open.

**Cons**
- **The model-written summary already exists.** `SessionContext.working_memory`
  IS the model's summary of this session, rewritten by `Synthesizer.merge` on
  every verdict round. Calling a model to summarise the session would be
  summarising a summary — spending scarce tokens to restate something already
  paid for and already stored.
- **The budget is not there.** `api.py`'s measured ceilings are 20 requests and
  25,000 tokens per hour PER KEY, and synthesis already does not fit inside
  them (`MERGE_MIN_INTERVAL_S`, `_affordable`, and the whole governor exist for
  that reason). Retrieval shares the same key and is already un-metered — the
  gap `api.py`'s comment calls out by name. Putting a third un-metered consumer
  on the join path means the demo's most-watched beat competes with synthesis
  for the same 25,000 tokens.
- **It puts a model on the critical path of joining.** Every failure mode of a
  laptop-local or hosted model — down, rate limited, slow, degenerate — becomes
  a failure mode of the join. Fail-open would leave the join succeeding with no
  summary, which is the state this workstream exists to end.
- Non-deterministic, so the demo's most rehearsed beat is the one that cannot
  be rehearsed.

## Transcript alignment

The storyboard (docs/demo-transcripts.txt:139-154) asks for the briefing to be
*"handed to Claude (not shown raw to the user) — Claude itself synthesizes it
into a short natural-language summary and tells the user 'I have this context,
ready to go'."*

Read carefully, that settles it: **the natural-language summarisation the demo
wants is done by the JOINING AGENT, not by the service.** There is already a
model on this path — the one the human is talking to — and it is the one whose
output the audience sees. A service-side model call would produce prose that is
then re-summarised by Claude before anybody reads a word of it. So the service's
job is to hand over *material*, faithfully and fast, and `join_session`'s result
says so in as many words ("summarise it for the user IN YOUR OWN WORDS").

PLAN.md's *"summarisation may run on the AI-100 backend"* is a permission, not a
requirement, and this record is the deviation being stated rather than taken
silently.

## Decision

**Option B.** `GET /v1/sessions/{sid}/arrival`, assembled by
`synapse_service.arrival.compute`, memoised by `SummaryCache`, fetched by
`briefing.fetch_arrival_summary` and returned inside `join_session`'s tool
result. **No provider is constructed anywhere on this path** — pinned by
`test_compute_needs_no_provider_at_all`.

Three consequences worth stating out loud:

**The two sections are separate, and the second one is allowed to be empty.**
For a first-ever joiner "new since" is empty by construction, and it says so in
one sentence rather than reprinting the backlog under a second heading. That
duplication is the same "dumping everything" failure at half scale.

**"New since" counts ARRIVALS, not versions.** `store.last_seen` holds a
`memory_version`, which counts verdict rounds; a finding carries no version
stamp and is queryable the instant it is pushed, whole rounds before a bump
covers it. So a version-derived list would omit precisely the findings a joiner
most wants — the ones that just landed. `store.seen_count` is a second
watermark counting findings ever recorded, keyed by CONTRIBUTOR exactly as
`last_seen` is (decisions/001), so a second window is the same person.
`store.has_looked` is the third piece: "first look ever" is not `last_seen == 0`,
which is also what someone returning to an unmoved session gets, and only the
first of those may be told "everything here is new to you".

**Cache invalidation is structural, not hooked.** The key contains the
session's content fingerprint — `(log entry count, memory_version, members)` —
so a cached entry is only reachable while nothing has happened to the session.
⟨The member tuple was added by the review below; the first two were not enough,
because membership is in neither the log nor the version.⟩ The
rejected alternative was an explicit `invalidate(sid)` called from
`push_findings` and `synthesize`: correct today, and wrong the first time a
route mutates a session and forgets to call it. That is the failure mode
`api._unavailable`'s own docstring describes for the liveness gate, and the
reason that gate is one function too. The key also carries the asker's identity
and watermark, because suppression is per conversation and the new-since slice
is per person.

## Why — the failure this avoids

W5 was originally scoped as an upgrade to text. The wave-1 analysis found the
real defect was PLUMBING: `briefing.py` composes `instructions`, and
`create_initialization_options()` reads that at MCP **connection init**, not at
join. An agent that connected before it joined — the storyboard's exact
ordering, and the ordinary one for anybody joining mid-conversation — was
briefed about no session at all, while `join_session`'s result carried no memory
content to make up for it. The join was *silent*: nothing to read, therefore
nothing to say, therefore no awareness moment, no matter how good the summary
text was.

Every option above would have failed identically had it only improved the
briefing string. The decision that matters as much as service-vs-model is
**where the summary is delivered**: on the surface the arriving agent is
actually handed, with a directive to speak from it. There turned out to be two
such surfaces, not one — see the amendment below.

## Amendments (2026-08-06, adversarial review)

The decision above stands unchanged — service-side, no model call — but the
review found the delivery argument in the section directly above this one was
still half wrong, and three defects underneath it. All five are fixed; none of
them changed the option chosen.

**The delivery surface was still the wrong one.** "In the tool result, which is
the one surface an agent has just been handed" is true of the in-conversation
join and false of the join the docs actually describe. `docs/JOIN.md` step 3
has the teammate run `scripts/serve_local.py`, which POSTs the member and writes
`bindings/claude-code.json` itself, then starts the orchestrator; step 4 points
Claude Code at an orchestrator that is *already bound*, so `join_session` — the
only place the body was delivered — is never called. The awareness pack says so
in its own words ("joined before this conversation connected"). So there are now
two delivery surfaces, not one: `join_session`'s result for the
in-conversation join, and MCP `instructions` for the pre-bound one, both
carrying the same `/arrival` body and both carrying a directive to relay it.
`briefing.compose_instructions` is the second; `build_briefing` is untouched
beneath it, so a body that cannot be fetched cannot downgrade a headline that
was already true.

**The global cap was eating the half W5 exists to add.** The per-part caps sum
to about twice `MAX_ARRIVAL_CHARS`, and `render` truncated the concatenation
from the END — so the growable content sat in the middle and the NEW SINCE
section paid for all of it. At the bounds the heading itself disappeared. The
sections are now composed against budgets: ACCUMULATED's fixed part first
against what is left over `MIN_NEW_SECTION_CHARS`, then the whole new section,
then ACCUMULATED's bullets into the remainder — dropping whole bullets with an
explicit "and N more" rather than cutting a sentence in half.

**The watermark was taken after the model call.** `/query` computed
`mark_seen`'s position once ranking returned, and ranking is a model call
docs/FLOW.md measures at 12.6–52.8 seconds. Everything a teammate pushed inside
that window was marked seen by an asker who was never shown it — and because
`seen_count` is an arrival index rather than a self-correcting version delta,
those findings were dropped from that person's NEW SINCE slice permanently. The
position is now snapshotted before the await (`store.read_position`,
`mark_seen(at=)`).

**A rejoiner was told the wrong team.** Covered in the fingerprint note above.

**Two doc surfaces quoted a cap that had moved.** Both now name the constant
rather than a number.

## How to undo it

The two commits are independent and revert cleanly in either order.

To drop the join-time delivery but keep the route (leaves the briefing's purpose
and members in place):

    git log --oneline -- packages/orchestrator/src/synapse_orchestrator/briefing.py
    git revert <orchestrator-sha>          # "deliver the arrival summary at JOIN"

To drop the service-side route and everything under it:

    git log --oneline -- packages/service/src/synapse_service/arrival.py
    git revert <service-sha>               # "the arrival summary — accumulated, plus new since"
    uv run pytest -q                       # packages/service/tests/test_arrival.py goes with it

To keep the shape and move the prose to a model instead (Option C), the seam is
`arrival.render`: it takes an `ArrivalSummary` and returns a string, and nothing
else in the module composes text. Replacing its body with a provider call is the
whole change — `compute` already assembles the structured material a prompt
would need, and `SummaryCache` already means one model call per (session,
content, asker) rather than one per join.
