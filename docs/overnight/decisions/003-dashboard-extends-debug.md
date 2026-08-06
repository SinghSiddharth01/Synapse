# 003 — The dashboard extends `/debug`; the brain page takes the top level

**Status:** Decided, design only (2026-08-06). Implementation is W4a.
**Workstream:** W4a — Dashboard Page 1 (Memory dashboard)
**Author:** decision agent, per the Decision Agent Protocol (PLAN.md)

## Question

The requirement is one line: **"the top level shows the state of the brain, not
the log."** Three sub-questions follow from it, and they are separable:

1. **Where does the page live** — a new app (React/Vite, its own build, its own
   port), or the existing server-rendered `/debug` surface in
   `packages/service/src/synapse_service/debug.py`?
2. **Which URL is the brain page** — `/debug` itself (displacing today's page),
   or a sibling like `/debug/brain` that leaves `/debug` alone?
3. **Working Memory has no revision history anywhere in the system.** Page 1
   asks for "the last 5–10 revisions". Omit and note, or retain them?

## Options

### Q1 — a new app, or extend `/debug`

#### Option A — a new frontend app

**Pros**
- Component libraries, a real router, and the polish ceiling is higher.
- Pages 2 and 3 (W4b) would have somewhere to grow into.

**Cons**
- A build step, a `node_modules`, a second process to start, and a second thing
  that can fail on a demo machine two days out. `scripts/serve_local.py` and
  `scripts/demo_local.py` currently start **two** processes and print two URLs;
  this adds a third with its own failure surface.
- It cannot be served from the same listener, so it needs CORS or a proxy — and
  the service's debug surface is deliberately localhost-shaped and gated
  (`build_app(debug=False)` exists precisely so `/debug` does not ride along on
  a `0.0.0.0` listener; `api.py:148-166`).
- The constraint the user set is *"don't invent new things — build on what the
  plumbing already exposes"*. Every datum this page needs is already computed
  inside `InMemoryStore` and `Feed`. A new app would spend its budget on
  transport and build tooling, not on the page.
- It breaks the one property the existing pages have that a demo actually needs:
  **zero external requests**, whole page in one inlined document.

#### Option B — extend `/debug` (server-rendered, inline CSS/JS) ✅

**Pros**
- The pattern already exists and already works: `debug.py` serves one inlined
  HTML document plus one JSON endpoint polled every second, with a dark theme
  keyed to the device boundary (teal = service/cloud, copper = worker) and no
  external asset of any kind (`<link rel="icon" href="data:,">`, inline
  `<style>`, inline `<script>`).
- Every data source is already inside the process: `store.log_entries`,
  `store.view`, `store.get_context`, `store.get_session`, `store.last_seen`,
  and the `Feed`/`CallLog` that `api.py` already writes into.
- It inherits the `debug=False` off switch for free — a new app would need its
  own.
- It inherits the read-only-by-construction property: routes mounted GET-only,
  no writes, no provider calls (`debug.py:1-16`).

**Cons**
- HTML-in-a-Python-string. Real, and already the house style for two pages; the
  existing JS test (`test_service_debug_page_js.py`) extracts the real `<script>`
  and runs it under Node, so this is testable rather than merely tolerated.
- Ceiling on polish is lower than a component framework's. Accepted: the ask is
  "production-grade feel", and a dense monospace operations console is a
  legitimate and achievable target for that.

### Q2 — which URL the brain page takes

#### Option A — `/debug/brain`, leaving `/debug` untouched

**Pros:** zero edits to existing tests; nothing moves under anyone's feet
mid-night.
**Cons:** it does not satisfy the requirement. *"The top level shows the state
of the brain"* — a brain page one click below the top level is the exact thing
the sentence rules out. It also books a second URL move later, when W4b makes
today's page into Page 3.

#### Option B — `/debug` becomes the brain page; today's page moves verbatim to `/debug/log` ✅

**Pros**
- It is the requirement, literally. The default page is what an audience sees
  when someone types the URL in the demo video.
- It lands the IA the user described in one step rather than two: Page 1 at
  `/debug`, Page 3 at `/debug/log`, Page 2 (`/debug/memory`) reserved for W4b.
- The moved page is **byte-identical** — the same `_PAGE` constant, the same
  script, mounted at a different path. Nothing about it is rewritten, so nothing
  about it can regress.
- Everything outside the tests that touches `/debug` uses it as a **readiness
  probe or a printed URL**, not as content: `serve_local.py:361,375,377`,
  `demo_local.py:461,518,523,655,656`, `rehearse_demo.py:233,356`,
  `demo_say.py:125,183`. All of them keep working unchanged, because `/debug`
  still answers 200 with HTML.

**Cons**
- Two existing test lines change URL: `test_debug.py::test_debug_page_has_
  required_ids` and `test_service_debug_page_js.py:64`, both `client.get("/debug")`
  → `client.get("/debug/log")`. Both are one-line edits against a page whose
  content did not change.
- Anyone's muscle memory for `/debug` now lands on the new page. That is the
  intent, and both pages carry a nav strip linking to the other.

### Q3 — Working Memory revisions, which do not exist

`SessionContext.working_memory` is one mutable string. `store.set_context`
(store.py:326) overwrites it; its only caller in the tree is `synthesis.py:469`.
The append-only log has **six** entry kinds and none of them carries the prose
(`log.py:167`). There is no history, anywhere, at any layer. So:

#### Option A — omit the revisions panel, note the absence

**Pros:** strictly no new retention; the purest reading of "don't invent new
things".
**Cons:** deletes a named Page-1 requirement outright, and the requirement is
not unreasonable — the rewrites genuinely happen, several times a session, and
watching the memory *change* is the single most legible thing this page can show
an audience. "The state of the brain" with no sense of movement is a snapshot.

#### Option B — retain the rewrites that already happen, debug-side, bounded ✅

A `WorkingMemoryLog` in `debug.py`: a ring of at most **10** revisions per
Shared Session, appended by `api._record_synthesis_feed` — which already reads
`store.get_context(sid)` immediately after every merge — and **only when the
text actually changed**. One object, constructed next to `Feed()` and `CallLog()`
in `build_app`, `None` when `debug=False`, exactly like both of them.

**Pros**
- It does not fabricate a metric. Every row is a real rewrite that really
  happened, with the real version number and the real observation time. Nothing
  is inferred, averaged, or modelled.
- It is the same move `Feed` itself already is, and `debug.py:1-16` already
  states the precedent out loud: the Finding Log has no concept of an LLM call
  or a teammate's question, *"and those are exactly what an instrumented
  dashboard needs to show"*. A working-memory rewrite is the third member of
  that set.
- Bounded and small: working memory is capped at 500 words by
  `SynthesisBudget` (`synthesis.py:82`, `MAX_WM_WORDS`), so ten revisions is a
  few tens of KB per session, in a process that already retains 200 LLM
  prompt/output previews.
- Cost is ~4 lines in `api.py` plus the class. No store change, no log change,
  no product-path change, nothing retained when `debug=False`.

**Cons**
- It is debug-only state, so it is empty after a service restart and empty for a
  session whose merges happened in another process. Accepted, and **the page
  must say so** rather than render an empty panel that reads as "nothing has
  happened": with no revisions the panel says the history is not retained across
  a restart, and the current text is still shown.
- A second place that knows when working memory changed. Mitigated by it being
  a pure observer at the one call site that already exists for exactly this.

## Transcript alignment

`docs/demo-transcripts.txt:159-168` asks for two views recorded side by side —
the "Front"/UX view, and a "behind the scenes / dev / **global view**" showing
*"services, active sessions, active workers, what's happening on the backend"*,
**correlatable 1:1** with the front view. Three consequences, and each one
points the same way:

- **Correlatable 1:1** means the page has to be true at the moment it is
  filmed. That is an argument for deriving everything from the store and the
  feed on each poll and for inventing no metric that cannot be checked against
  the other view — a fabricated "health: 98%" is worse than useless in a
  side-by-side.
- **"Active workers"** is the roster. The service never sees a worker (the
  orchestrator is the single egress, CONTEXT.md), so what the page can honestly
  show is **participants: one row per Agent Session**, which is exactly what W2
  made real tonight. The word in the UI is "conversation", not "worker", and the
  page says why.
- The transcript's own explicit anti-pattern — *"NEVER claim one worker can join
  multiple sessions"* — is why the roster is scoped to the selected Shared
  Session and never aggregates across them.

The session-history ask in the same passage ("mostly closed/past sessions, one or
two open") is the presentation GUI's, not this page's; what this page owes it is
the honest `active`/`ended` status per session, which is Q1's field below.

## Decision

**Option B on all three.**

1. **Extend `/debug`.** No new app, no build step, no external request. The page
   is server-rendered from `packages/service/src/synapse_service/debug.py` with
   inline CSS and inline JS, in the existing teal dark theme, polling one JSON
   endpoint on the same listener.
2. **`/debug` is the brain page.** Today's page moves verbatim to `/debug/log`;
   both carry a nav strip (`Brain · Log`, with `Memory` arriving in W4b). The
   only edits to existing behaviour are the two test URLs listed above.
3. **Working memory revisions are retained**, debug-side, bounded to 10 per
   session, deduplicated on unchanged text, and the panel states plainly when the
   history is empty.

The new page reads a **new** endpoint, `/debug/brain.json`, rather than growing
`/debug/stats.json`. `stats.json` carries `log_tail` (up to 200 entries) and the
whole `CallLog` (up to 200 records with prompt and output previews); the brain
page renders none of that and would be downloading it once a second. It is also
parsed today by three scripts (`demo_say.py:73`, `demo_local.py:286,359`,
`rehearse_demo.py:285,310`), and a page-shaped payload is a page's to change.

Two **additive** fields do land on `stats.json`, because the routing note asked
whether they were cheap and they are: `status` (from `ctx.status.value` — the
session is already folded for status on every `get_context` call, and the
dashboard was the one reader that could not see the answer) and `created_by`.
Nothing is removed or renamed, so every existing consumer is unaffected.

**What the page will NOT show, because the data does not exist** — recorded here
so the omissions are decisions rather than oversights, and detailed with
citations in `docs/overnight/w4a-page1-spec.md`:

- **Join and leave *times*.** Membership is a list on `SynapseSession`, mutated
  by `add_member`/`remove_member`; `store.remove_member`'s own docstring says
  membership has never been in the log and explains why making only the removal
  an entry would be worse. Joined/left is shown as **state**, derived truthfully
  (a Contributor with findings in the log but no longer in `members` has left);
  the times are omitted and the page says so.
- **Health, in the sense of a heartbeat or a liveness check.** There is none —
  the service is plain HTTP and holds no connection registry, so "who is
  connected" is not a question it can answer. The page shows **recency of last
  observed activity** with explicit thresholds, labelled as such, and never the
  word "connected" for a state nobody measures.
- **Per-conversation last-query time**, unless the one-line addition is taken:
  `api.query`'s feed event records `asked_by=contributor` (api.py:760) and
  discards `asking_session`, which is in scope two lines above. Adding
  `asked_by_session=asking_session` makes the roster's "last query" per
  conversation instead of per person. If it is not taken, the column falls back
  to the contributor's own last query and is labelled per-person.

## Why — the failure this avoids

The failure mode for a dashboard is not that it breaks; it is that it renders a
confident number nobody can check. `decisions/006` names the same shape in
another layer — *"a cap that is present, plausible, and inert is worse than an
absent one, because it answers the question with a confident yes"*. A "workers
online: 3" tile computed from a member list with no timestamps and no heartbeat
is that failure, on camera, in a view whose stated purpose is to be
**correlatable 1:1** with what the audience just watched a human do.

So the rule this page is built on: every element names its source, and anything
without one is either omitted or rendered as an explicit absence. That is also
why the revision panel is worth its four lines — the alternative was not "no
revisions", it was the temptation to show *something* in that space.

## How to undo it

Nothing here touches the product path, the store, the log, or any contract, so
the undo is deletion:

1. Delete `/debug/brain` and `/debug/brain.json` from `debug_routes`, and the
   `_BRAIN_PAGE` constant, the roster/revision derivation helpers, and the
   `WorkingMemoryLog` class from `debug.py`.
2. Re-mount the existing page at `/debug` (one string in the `Route(...)` list)
   and revert the two test URLs to `/debug`.
3. Remove `working_memory_log` from `build_app` and the one `wm_log.record(...)`
   call plus `asked_by_session=` from `api.py`.
4. Optionally keep `status`/`created_by` on `stats.json` — they are two additive
   fields with no dependency on any of the above.

`test_debug.py`'s existing suite passes before and after, which is the check
that the undo is clean.
