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
session's content fingerprint — `(log entry count, memory_version)` — so a
cached entry is only reachable while nothing has happened to the session. The
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
**where the summary is delivered**: in the tool result, which is the one surface
an agent has just been handed and will speak from.

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
