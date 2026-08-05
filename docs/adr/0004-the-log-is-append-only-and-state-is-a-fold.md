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
