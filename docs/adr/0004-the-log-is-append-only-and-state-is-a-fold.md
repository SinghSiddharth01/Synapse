# 4. The log is append-only; current state is a fold

**Status:** Accepted (2026-08-05)
**Amends:** [ADR 0002](./0002-semantic-merge-and-tombstones.md) — its *intent* stands unchanged; its *mechanism* is superseded.

## Context

ADR 0002 established that a semantic merge writes a new Finding and the originals become tombstones: `merged_into` set, text and Attribution retained, excluded from retrieval. That decision was right and nothing here reverses it.

Building it surfaced a hole in *how* it was to be implemented. `merged_into` and `status` are service-written fields living on the same record as producer-written fields (`text`, `type`, `attributions`, `ts`). And Plan D.4 makes wholesale `resync` the explicit recovery path: orchestrators retain their durable logs after ack and re-push everything, because Shared Memory is in-memory and a service restart would otherwise wipe it for everyone.

A re-pushed finding necessarily carries `merged_into=None` and `status=KEPT`. The producer never had a verdict to send — it does not know the finding was merged away, and by the egress rule it never will.

So under upsert-by-id semantics, the recovery path silently undoes synthesis:

> Aditya's finding #41 merges with Akhil's #58 into #59. Aditya's orchestrator restarts and resyncs its log. #41 is written again with its verdict fields at their defaults. #41 is now retrievable, alongside #59 which already contains it. The duplicate the merge removed is back, and so is every other merged-away finding in the session.

Nothing errors. Nothing appears in a log. The store quietly refills with the noise it had curated away.

The available fix under upsert is **field-scoped writes** — the store must know which fields belong to which writer and merge accordingly. That works, and it is a rule every future write path has to remember, in a system where forgetting it produces no error.

## Decision

**The log is append-only. Nothing is ever modified or removed.**

Current state — what is visible, what superseded what, which topic a finding belongs to — is **derived by folding the log in order**, and is never stored as a mutable field on a record.

Four entry kinds, all of the same shape: a fact that happened, at a position.

| Entry | Meaning |
|---|---|
| `FindingAppended` | a producer pushed this finding |
| `Merged` | synthesis judged these sources the same and wrote this result |
| `TopicAssigned` | this finding joined this topic |
| `TopicSplit` | this topic stopped discriminating and became these two |

A finding is absent from the view when a *later* entry supersedes it. First write of an id wins; subsequent appends of the same id are inert.

The resync above now changes nothing: entry #73 re-appends #41, but the `Merged` entry at #59 is still in the log and still earlier, so the fold still drops #41. **A rule people have to follow became a property the structure guarantees.**

## Consequences

**ADR 0002 is preserved in full at the level that matters.** Originals are retained and readable, `RETRIEVABLE` is defined in exactly one place, a bad merge is reversible, and references follow supersession forward. All three of 0002's justifications for tombstones-over-deletes hold identically. Only the representation changed: a derived property rather than a written field.

**One mechanism now serves three jobs.** Merges supersede findings, topic splits regroup them, and (if pinning is ever built) promotion would work the same way. Each is an appended event resolved in the same fold. There is no second pattern to learn.

**`Finding.merged_into` and `Finding.status` are not written by the service.** This is the one genuine loose end and it is a **team decision, not a resolved one**:

- *Option A* — the ingest API projects them onto findings on egress, computed from the fold. The contract is unchanged, existing consumers keep working, and the fields become a serialization detail.
- *Option B* — the fields leave `Finding` and supersession is exposed as its own thing.

Option A is the lower-risk default and is what a reviewer should assume unless the team says otherwise. Until it is built, treat those two fields as **undefined on anything the service returns**, and read visibility from the view.

**"Idempotent upsert by `Finding.id`" (Plan C.3, Plan D.4) becomes "append; the fold takes the first write of an id."** The externally visible property Plan C.3 asked for is unchanged and still tested: a replayed POST with identical ids is a no-op. What changed is that it is now true by construction rather than by careful write logic.

**Reads that need current state must fold.** Cached and invalidated by log version, so in practice this is one pass on write rather than per read. The cost is real but small, and the cache is disposable by definition — if it is ever wrong, discard and fold again.

**The log is the only thing that has to survive.** Every index in `synapse_service` — symbols, BM25 postings, vectors, topic centroids — rebuilds from a replay. `Store.rebuild()` exists to keep that claim honest and is covered by a test that folds, rebuilds from scratch, and asserts the two are identical. If that test ever goes red, something has become a second source of truth while pretending to be a cache.

## Alternatives considered

**Field-scoped upsert.** Keep records mutable; teach the store which fields belong to producers and which to the service. Correct, and roughly the same amount of code. Rejected because it is a discipline rather than a property: it must be re-applied at every future write path, and the failure mode when someone forgets is silent, delayed, and looks like a synthesis quality problem rather than a storage bug.

**Merge only in the Working Memory, keep the log raw.** Already rejected in ADR 0002 — retrieval reads the log, so teammates would see both near-duplicates.

**Event sourcing with periodic snapshots.** The natural next step if folding ever becomes expensive. Deliberately not built: at hackathon scale the fold is microseconds, and a snapshot is a second source of truth that has to be invalidated correctly. Recorded as the scaling move, not taken.

## Follow-up

Deciding Option A vs Option B above, and — if A — building the projection at the ingest boundary rather than inside the store, so that the store keeps having exactly one way to represent supersession.

---

## Amendment (2026-08-05) — Siddsing, during the Plan E integration

The decision above is **adopted**. Everything below corrects the reasoning that
supports it and closes the one question it left open. The original text is
unedited and its Status is unchanged; this section is separately attributed
because the argument above is its author's and the corrections here are not.

### A1 — retarget the Context

**The motivating bug is FALSE against `main`.** The Context's resync scenario
("#41 is written again with its verdict fields at their defaults ... the
duplicate the merge removed is back") is true of a whole-object upsert
(`table[f.id] = f`). `main` never shipped one:
`packages/service/src/synapse_service/store.py:58` is `if finding.id not in
table:` — FIRST-WRITE-WINS — and the module docstring names that rule and gives
exactly this scenario as its reason. Pinned three ways:
`test_upsert_is_first_write_wins`, `test_replayed_original_never_clobbers_a_tombstone`
(both `packages/service/tests/test_store.py`), and
`test_replayed_push_is_a_noop_and_skips_the_model` (`test_api.py`), which shows
that at the route a replay never even reaches the model.

**Read as written, the Context invites someone to rewrite synthesis two days
before a demo to close a hole that is not open. Do not.** Retarget it at the
case that IS open — **restart** — plus the three arguments the decision is
actually carried by:

1. **Property beats discipline, and we are about to add write paths.**
   First-write-wins must be re-applied at every future write path and fails
   silently. This week adds `supersede`, `mark_trivial` and a projection.
2. **It closes the producer-forged-verdict hole for free.** `api.push_findings`
   runs only `Finding.model_validate`, and the Relay POSTs to the service
   directly, so today a FIRST push carrying `merged_into="x"` or
   `status=trivial` lands and is excluded from retrieval forever, uncorrectable
   because upsert ignores known ids. Under the fold, **visibility stops reading
   producer-writable fields at all**, and the read accessors normalise them back
   to `KEPT` / `None` on the way out. Neither review claimed this; it is the
   strongest argument this ADR has and it was not in it. Pinned by
   `test_a_forged_verdict_on_ingest_has_no_effect_on_visibility`.
3. **It is the enabling step for durability.** A log is
   `for entry in entries: write(json)`. Mutable cross-referenced state is not.

### A2 — the order argument is wrong; the property is stronger than claimed

The Decision says the resync is inert because the `Merged` entry "is still
earlier" in the log. **Order is irrelevant to that conclusion:** `fold`
accumulates `superseded_by` across every entry and filters once at the end
(`fold.py:107-114`), so a `Merged` entry appearing *after* the re-append
suppresses just as well. The conclusion stands; the reasoning does not.

### A3 — a fifth entry kind, `MarkedTrivial`

The four kinds cannot express synthesis's trivia verdict, yet `fold.py:113`
reads `findings[fid].status is FindingStatus.KEPT` — a producer-writable field
nothing in this branch ever writes, and the same field this ADR tells readers to
treat as undefined. A fifth kind is added:

```python
@dataclass(frozen=True)
class MarkedTrivial:
    finding_ids: tuple[FindingId, ...]
    kind: Literal["marked_trivial"] = "marked_trivial"
```

Visibility becomes `fid not in superseded_by and fid not in trivial`, and the
fold stops reading `Finding.status` entirely. **This is what preserves
`adr/0003`:** durability judgment has exactly two homes — triage upstream (Plan
A.5b) and synthesis's trivia verdict downstream — and adopting the four kinds
as-is would have deleted one of them. Pinned by
**`test_marked_trivial_round_trips_through_rebuild`** (`test_fold.py`).

### Option A, closed

The Consequences leave Option A (project on egress) vs Option B (drop the fields
from the contract) open, calling it "a team decision, not a resolved one."
**Decided: Option A, 2026-08-05.** Option B is a three-track contract break two
days before the demo, for no gain.

The Follow-up asks for the projection "at the ingest boundary rather than inside
the store." **We deviate, deliberately**, and put it in the store's read
accessors (`get`, `all_findings`, `retrievable`, `candidates`): the store is the
only component holding the View, so it is the narrowest place the projection can
live **where no caller can forget it** — and it is what lets ~380 existing tests
keep passing unchanged, including every `test_synthesis.py` assertion of the
form `f.merged_into == syn.id`. That is not a convenience; it is the regression
guard for the whole swap. The projection is a **deep** copy
(`model_copy(deep=True)`): a shallow one shares `attributions`/`refs`/
`merged_from` with the record inside the fold, which is the same class of
mutation-through-reference this whole change exists to remove.

### One correction of vocabulary, so the next reader does not inherit it

⟨CORRECTION, corrected 2026-08-05⟩ `SessionContext.memory_version` counts
**verdict rounds applied**. ⟨CORRECTION, corrected 2026-08-05⟩ The gloss "merges completed", which appears in the
design memo and in Plan E, is wrong: `synthesis.merge` calls `bump_version`
once at the end of every structurally-valid verdict, `"merges": []` included
(`synthesis.py:273`), and `test_full_flow_push_watermark_query` pushes one
finding under a no-op verdict and asserts `memory_version == 1`. `/findings`'s
`synthesized: true` therefore means "a verdict round was applied". This is
stated here because a second counter (`Log.version`, the entry count, internal
to `SharedMemory` and used only for fold-cache invalidation) is being carefully
distinguished from it in the same week.

### A5 — a 404 from the service is retryable, not terminal

⟨CORRECTION, corrected 2026-08-05⟩ Plan E.9's first-failing-test list says "a
404 from `_post` is **terminal** — the Relay does not re-attempt it". The
execution deliberately inverts that clause. The overwhelmingly likely 404 in
this system is *"the service restarted and no longer knows this session"* —
which **stops being true the moment `cmd_resync` recreates it** through
create-or-return. Dropping those findings converts a self-healing case into one
that needs a human to type a command mid-demo. **Terminal is 400–499 excluding
404**; a 404 stays queued and flushes itself. The logging improvement, which is
the part that helps on stage, is unchanged.
