# 008 — Retrieval failure becomes a typed exception and a 503, never an empty 200

## Question

`retrieval.py:109`'s `except Exception: return []` converts a dead model
backend into an empty findings list and an HTTP 200; the orchestrator then
tells the agent "Team memory has nothing relevant to that. (Checked — not
skipped.)" — a confident lie. `282fd07` named this "what made this silent
rather than loud" and deferred it because it changes the query contract. How
should "the model call failed" be distinguished from "nothing matched"?

## Options

**A. Typed exception (`RetrievalUnavailable`) raised out of
`query_findings`; the `/query` route maps it to
`503 {"error": "retrieval_unavailable", "provider", "detail"}`; the MCP tool
renders an explicit outage sentence.**
- Pros: impossible to ignore accidentally — the compiler-grade property a
  sentinel lacks; exactly one production caller exists (`api.py:725`,
  verified by grep), so the blast radius is one route plus the orchestrator's
  reader; a failed query stops before `mark_seen`, so the asker's watermark
  is not advanced by a query they never got; `/debug` gains a distinct
  `query_failed` feed event.
- Cons: every test that queries against a raising provider must be audited
  (`FakeProvider(scripts=[])` appears ~10 times in test_lifecycle.py alone);
  a 503 where a 200 used to be is a wire-contract change for any un-upgraded
  orchestrator — it will render the generic "Shared memory is unreachable
  right now (HTTPStatusError)", which is still strictly more honest than the
  empty-200 it replaces.

**B. Sentinel return (`None` means failed, `[]` means empty).**
- Pros: no exception plumbing; wire contract choosable per caller.
- Cons: `None` invites the next caller to `or []` it straight back into
  silence — the exact regression class this fix exists to close; type of the
  function becomes `list | None`, which every test touches anyway. Cost
  without the guarantee.

**C. Status field (`{"findings": [], "degraded": true}` on a 200).**
- Pros: softest wire change; old clients keep working.
- Cons: old clients keep working *silently wrong* — an un-upgraded
  orchestrator reads `findings: []` and renders the confident lie unchanged.
  A degraded flag on a 200 is exactly the shape of `deferred`-never-reaching-
  the-agent (FLOW.md §1.4), a known gap, reproduced on purpose.

## Transcript alignment

The demo's retrieval moment is the product: an agent asks and a teammate's
finding comes back credited. The transcript's behind-the-scenes view exists
so the audience "can correlate when this person did X, this happened on the
backend" — an empty 200 from a dead brain is uncorrelatable by construction,
while a 503 shows in the front view (agent says "outage") and the dev view
(`query_failed` feed event) simultaneously. The transcript also warns the
live portion must be "the most basic/robust interactions only": the worst
live outcome is not an error on screen, it is *everything looking fine while
the brain is empty* (PLAN W1's own words). C reproduces that; A is its
negation. So A.

## Decision

A. `class RetrievalUnavailable(RuntimeError)` in
`synapse_service/retrieval.py` carrying `provider_id` and the chained cause;
`query_findings` raises it where it returned `[]` on exception (the
`logger.exception` line stays; the genuinely-empty returns stay `[]`).
`api.query` catches it → 503 `{"error": "retrieval_unavailable",
"provider", "detail"}`, skips `mark_seen`, emits a `query_failed` feed
event. `orchestrator/server.py query()` checks 503+body before
`raise_for_status` (the `is_session_ended` pattern) and returns: "Shared
memory is DOWN, not empty … do NOT report 'no relevant findings'." Synthesis's
own swallow (`synthesis.py:363-366`) is explicitly out of scope: it already
carries an honest wire signal (`synthesized`/`deferred`) with its own
decision history. Tests asserting empty-on-failure flip to asserting the 503
(`test_cli_provider.py` docstring, the `/query`-reaching
`FakeProvider(scripts=[])` sites in test_lifecycle.py/test_api.py, one new
pin in test_retrieval.py).

### As-built notes

Two things the design anticipated turned out differently, recorded here
rather than silently:

1. **No existing test had to flip.** The audit of `FakeProvider(scripts=[])`
   sites found that none of them actually reaches `/query` — those tests
   exercise lifecycle routes and never rank. The suite was green on the
   contract change alone, so the cost the option-A "cons" priced in was not
   paid. The new coverage is therefore all additive:
   `packages/service/tests/test_retrieval_outage.py` (the route),
   three pins in `test_retrieval.py` (the function), and two in
   `packages/orchestrator/tests/test_tools.py` (the agent-facing sentence,
   plus a parametrized negative control proving an unrelated 5xx still gets
   the generic text).
2. **`/debug` needed a small change to actually show the event.** The feed
   panel filtered on `tag == "query"` and the page hides any entry whose tag
   has no filter chip, so a `query_failed` event would have been recorded and
   then rendered nowhere — the opposite of the intent. `debug.py` now
   interleaves both tags by timestamp, passes the real tag through to the
   page, and carries a `query_failed` chip and colour (the only red on the
   dashboard).

## How to undo

    git revert --no-edit $(git log --format=%H -1 --grep="w1: retrieval failure is a typed 503")

(One commit carries the contract change and its test flips; the subject line
is its grep key.) Reverting restores fail-closed-and-silent: empty list,
200, no watermark skip — and re-opens the masking `282fd07` documented.
