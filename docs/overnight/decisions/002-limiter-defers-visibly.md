# 002 — The worker→provider limiter defers with a visible bound; it never sheds

## Question

FLOW.md §1.3 item 8: `WorkerLoop.tick` distils every surviving segment
sequentially with no per-tick cap and no ledger (`loop.py:340-374`), and
`run()` only sleeps between ticks (`loop.py:436-446`). Nothing bounds what one
worker asks of one provider. Paste a 5,000-line log into a conversation and one
tick issues however many model calls that segments into, back to back, for as
long as it takes; and the in-flight count is one only because that `for` loop
happens to await in sequence — `asyncio.gather` over the same list is a one-line
refactor away from N simultaneous calls into a single local NPU, which is the
"infinite sessions in parallel" failure the requirement names.

This is **not** the seam ADR 0005 governs. That one bounds how often the
*service* calls the *synthesis* model on a hosted key's token ledger. This one
is the other end of the pipeline: worker loop → `Distiller` → whichever provider
`SYNAPSE_DISTILLER` selected.

So: over the bound, what happens to the work?

## Options

**A. DEFER with a visible bound.** Cap calls per tick; the overflow waits in a
FIFO queue that is itself bounded, and at that bound the loop stops *reading*
new transcript bytes.
- Pros: nothing is lost, and deferral is *provably* safe at this specific seam
  because **the transcript on disk is the durable queue** — refusing to read
  leaves the bytes exactly where they are, behind the follower's offset
  (`follower.py:135-149`), for the tick that has room. The in-memory part of the
  backlog is small and persists next to the pending-events buffer, so a restart
  costs duplicated work rather than lost conversation, which is the ordering
  `loop.py`'s module docstring already commits to. Backpressure at the source is
  a real bound on memory, not a promise about it.
- Cons: the working memory lags under sustained load, and the lag is unbounded
  in *time* even though the queue is bounded in *size*. That has to be visible or
  it is indistinguishable from a dead provider.

**B. SHED beyond the bound** — drop the oldest (or newest) segment.
- Cons: a shed segment is indistinguishable, from every downstream vantage
  point, from a segment that distilled to nothing. `distil` already drops after
  two attempts (`distiller.py:173-177`) and triage already skips
  (`triage.py:96-147`), both of which log; a *third* silent disappearance in the
  same path, on the axis of load rather than content, gives an operator no way
  to tell "the conversation was boring" from "we were busy". This system's entire
  failure history is silent loss that looks like success — ADR 0005's whole
  context is twelve rounds of "findings landed, memory unchanged". Shedding
  reproduces that shape deliberately. And unlike the service side, where a
  deferred finding is already `upsert`ed and queryable (`api.py:492-499`), a shed
  *segment* was never distilled at all: there is nothing left of it anywhere.

**C. BLOCK — let the tick run as long as it takes, unbounded.**
- Pros: no new state, no new failure mode, zero diff.
- Cons: this is the status quo, and it is the thing being asked about. It also
  fails the second half of the requirement: blocking bounds nothing about
  concurrency, and a loop that cannot finish a tick cannot notice a re-join
  (`_sync_binding_from_disk` runs at the *top* of `tick`), cannot retry the
  producer's queue, and cannot report anything.

## Transcript alignment

The demo's whole claim is that the memory reflects what the team actually did.
A shed segment breaks that claim silently and permanently; a deferred one breaks
it temporarily and says so. The transcript's requirement that the live portion be
"the most basic/robust interactions only" points the same way: the worst live
outcome is not a slow tick on screen, it is everything looking fine while the
brain is quietly incomplete. B is that outcome by construction. A is its
negation, provided the bound is *readable* — which is why visibility is part of
the decision rather than a nicety attached to it.

## Decision

**A**, with the bound stated in four places at once.

`SeamLimiter` (`packages/worker/src/synapse_worker/limiter.py`) carries three
bounds, all config-driven from `[worker]` in `config/synapse.toml` with
`SYNAPSE_MAX_*` env overrides:

| bound | default | what it is | why that number |
|---|---|---|---|
| `max_calls_per_tick` | **4** | the RATE — segments admitted to the provider per tick | one distillation is ~10s on the NPU against a 30s poll interval, so ~3 is what a tick's own period pays for; 4 leaves headroom to catch up after a quiet stretch |
| `max_concurrent_calls` | **1** | the CEILING, held as a semaphore every worker→provider call in `loop.py` passes through | the shipped distiller arm is one local NPU serving one request at a time; the cloud arms genuinely do serve concurrent calls, which is why it is a number and not a constant |
| `max_deferred_segments` | **64** | the BACKLOG bound — the deferred queue is never filled past it, and while work is held back `tick()` reads no new transcript bytes | ~8 minutes of backlog at 4/tick / 30s: long enough to absorb a burst, short enough that the number is climbing visibly before it is hours |

⟨**CORRECTED** 2026-08-06, review pass⟩ The third row originally claimed only
the second half of that, and the code implemented only the second half.
`accepting_input()` is checked *before* the read and `read_new_lines` returns
everything from the offset to EOF, so one tick could drain an arbitrary number
of segments into the queue: measured at **196 segments against a bound of 64**
on a `--from-start` attach over a 200-turn transcript, rewriting 128 KB of
segment JSON every tick for the ~25 minutes it took to drain. Backpressure only
ever prevented the *next* read. The bound is now enforced at both ends —
`WorkerLoop` caps `Segmenter.drain(max_segments=…)` at the queue's remaining
headroom, and what does not fit stays in the segmenter, behind the same
persisted position in the transcript. Exact to within one turn, because a turn
is never split across ticks.

The read gate moved with it and had to: on queue depth alone it would now be
dead code, since `admit()` empties `max_calls_per_tick` off the queue every tick
and a queue filled exactly to its bound is back under the bound by the next
tick. The loop now asks whether *either* buffer still holds unconverted work
(`Segmenter.has_undrained_turns`), which is the question the gate was always
trying to ask.

**Visibility is the decision, not a garnish.** A `WARNING` naming the depth and
the bound when backpressure engages; an `INFO` on every deferral naming the
count and saying "NOT dropped"; a `limiter` stats event carrying
`{admitted, deferred, bound, backpressured}` for `/debug`;
`TickResult.deferred` / `.backpressured` in the tick summary the CLI prints —
which no longer says `"no change"` while work is queued, because "no change"
next to an undistilled backlog is the exact misreading this exists to prevent.

**Deferred segments persist.** They have already been drained out of the
segmenter and their bytes are already behind the follower's offset, so holding
them only in memory would be precisely the silent loss `loop.py`'s crash-safety
ordering forbids. `deferred-segments.json` is written atomically alongside
`pending-events.json`, restored on start, and drained **in full** by
`shutdown()` — the per-tick rate paces a loop that gets another tick, and
shutdown does not.

⟨**CORRECTED** 2026-08-06, review pass⟩ "Drained in full" was true only when
every distillation succeeded. `shutdown()` empties `self._deferred` up front,
and a segment whose `limiter.call` raised was logged and dropped; `_persist_
state()` then wrote `[]` over `deferred-segments.json`. So one provider death
during teardown — the model server stopped before the worker, the ordinary
kill-order in a demo — silently discarded the **entire** backlog, up to
`max_deferred_segments` segments whose transcript bytes are already behind the
follower's offset and which nothing will ever re-read. Measured: 4 turns
queued, provider dies after the first, `deferred = 3` before shutdown and
`0` in memory, `0` on disk, 1 finding landed. That is the silent-loss-that-
looks-like-success this workstream exists to negate, arriving inside the fix
for it. Failed segments are now re-queued and re-persisted, and the re-queue is
`WARNING`-level: an operator who believes shutdown drained everything is the
person this file is written for. Pinned by
`test_shutdown_requeues_what_the_provider_could_not_distil`, which asserts on
the file on disk and on a fresh loop restoring it.

### Two decisions carried in the same change

**The metering hole is closed, and the product question it was blocked on is
answered.** `build_app` wraps ONE provider object twice, so `synthesis_provider`
and `retrieval_provider` are two façades over one instance — one key, one hourly
ceiling — and `/query` was never charged. Re-verified at HEAD *after* W1 rewrote
the query path: the handler spans `api.py:672-798` and contains neither
`_record_spend()` nor `_affordable()`; `_record_spend` had exactly two call
sites, both on the synthesis path. Twenty queries and no pushes exhausted the
key while `_affordable()` still answered `(True, "")`.

`api.py:68-79` left this open because metering retrieval lets query traffic
defer synthesis, "a product decision and not a bug fix". **The decision is made
this way** because the alternative was never "synthesis keeps its budget" — one
key does not care which component drained it. The only real choice was between
deferring a merge *with a logged reason* and 429ing it *without one*. So
`_spend` becomes the KEY's ledger, tagged by component: the hourly ceilings sum
across everything, while merge pricing still reads only merges, because a run of
cheap ranking calls must not convince `_affordable` the next 70B round is cheap.
`/query` is **charged, never gated** — a spent budget must not become "shared
memory is unreachable", which is W1's 503 and means something else.

**Round-robin is KEPT.** The instruction was wire it or delete it, and the
grep says the premise is wrong. `AIC100Provider._post_rotating` is the *only*
POST helper `complete()` calls — both the `/chat/completions` branch
(`aic100.py:222`) and the `/completions` schema branch (`aic100.py:246`) — and
`test_key_pool_rotates_on_429` covers it. `SYNTHESIS_KEYS` is read by
`_affordable()` on every push and `test_more_keys_buy_more_rounds` covers that.
Both layers are live reads with live tests; deleting either removes a POST path
and a passing test to make a sentence in an ADR true.

What is dead is the **pool**, which holds one entry — a configuration fact, not
code. And the live part is worse than dead code, because it lies:
`SYNAPSE_SYNTHESIS_KEYS=10` multiplies the governor's ceiling by ten whether or
not ten keys exist, and the excess does not defer, it 429s inside a provider
whose one-key pool cannot rotate out of it. That is exactly "the next person
concludes there is more headroom than there is", sitting next to a new rate
limiter. So `build_app` compares the two numbers at boot and warns with the
arithmetic spelled out. ADR 0005's "dead code at both layers" paragraph is
corrected in place rather than left to mislead the next reader.

### As-built notes

1. **The concurrency ceiling defaults to 1 and the loop is still sequential,
   and that is not decoration.** Every worker→provider call in `loop.py` goes
   through `limiter.call()`, so "one at a time" is a *checked* property rather
   than a consequence of how the `for` loop is written — which is the thing the
   requirement was actually about. The loop was deliberately **not**
   parallelised: the shipped arm is one NPU, and `StatsBuffer.current` is a
   single slot (`stats.py:48-56`) that would report in-flight work dishonestly
   above 1. Raising the bound is a knob for the cloud arms, and the ceiling is
   proven to hold at 2 and 5 by test.
2. **Verified by mutation, not by reading the assertions.** The ADR-0005 trap
   was a test that asserted against a hand-written `finish_reason: "length"` the
   host never sends and passed throughout the outage it predicted. So the bounds
   were checked by breaking them: neutering `admit()`, the semaphore and
   `accepting_input()` fails 14 of the 21 limiter tests on numbers
   (`20 == 1`, `6 == 2`, `[6,0,0] == [2,2,2]`); removing the `on_usage` hook
   fails 6 of the 8 metering tests. The survivors in each case are the
   deliberate negative controls.
3. **`TickResult.segments` changed meaning** from "drained this tick" to
   "admitted this tick". That is the honest reading — it counts what actually
   cost a model call — and no existing assertion moved, because every one of
   them is on a value of 0 or 1.

## How to undo

Three commits, newest first:

    git revert --no-edit 9dc4c47 3b60424 ec1d237

- `ec1d237` — the limiter. Reverting restores the unbounded seam: one tick
  distils every drained segment, and the concurrency ceiling goes back to being
  an accident of control flow. `.synapse/deferred-segments.json` is left behind
  and simply stops being read; delete it, or leave it, but note that anything
  still queued in it at revert time **is** lost, because nothing else knows
  those bytes were consumed. Drain first (`synapse-worker` shutdown flushes the
  backlog in full) before reverting on a live tree.
- `3b60424` — the metering fix. Reverting re-opens the hole: `/query` stops
  charging, `_spend` goes back to two-tuples, and 20 queries against one key
  once again leave `_affordable()` answering True into a 429. If you only want
  the *policy* back (retrieval unmetered) without losing the ledger's component
  tags, delete the `on_usage=_record_query_spend` argument at the
  `query_findings` call site and drop `test_query_metering.py`; everything else
  in that commit is inert without it.
- `9dc4c47` — the key-pool warning and the ADR/architecture corrections.
  Reverting removes one boot-time log line and restores two documents' claim
  that round-robin is dead code. Nothing behavioural depends on it. If you
  revert this one alone, keep the ADR correction: the code it describes is
  unchanged either way, and the sentence is simply false.

⟨**Added** 2026-08-06, review pass⟩ A fourth commit sits on top of those three,
fixing four defects the review found in them. It is the one to revert **last**
and the one you almost certainly do not want to revert alone — each hunk
restores a specific bug, so partial reverts are listed rather than a single SHA:

- *the `/query` double-charge* (`retrieval.py`) — reverting merges the
  `result.data.get(...)` back inside the provider `try`, so a schema-failing
  ranking books two ledger entries again and burns 4 of the key's 20
  requests/hour for one query. Pinned by
  `test_an_unparseable_ranking_is_charged_exactly_once`.
- *the shutdown re-queue* (`loop.py`) — reverting drops the `failed` list, and
  a provider that dies during teardown discards the whole backlog again.
- *the drain cap* (`loop.py` + `segmenter.py`) — reverting restores the
  overshoot (196 against a bound of 64). If you revert this hunk you **must**
  also revert the `has_undrained_turns` half of the read gate in `tick()`,
  or backpressure engages on a condition nothing clears. The two are one
  change; `max_segments=None` at the `drain()` call site is the smaller
  version if you only want the old sizing back.
- *the docstring/table corrections* — inert. Reverting only restores claims the
  code does not honour.
