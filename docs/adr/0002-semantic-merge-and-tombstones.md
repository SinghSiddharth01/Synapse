# 2. Semantic merge produces a new Finding; originals become tombstones

**Status:** Accepted (2026-08-03). **Mechanism superseded by [ADR 0004](./0004-the-log-is-append-only-and-state-is-a-fold.md) (2026-08-05)** — every decision and consequence below still holds; only *how* a tombstone is represented changed. It is a derived condition (a later `Merged` entry names the finding as a source), not a `merged_into` field written onto the original. Read this ADR for the reasoning, then 0004 for the representation.

## Context

Synthesis must reconcile findings that different Contributors reached independently. "Deduplicate" was in the design from the start, but the word hid a decision nobody had made: what happens to the two findings.

The obvious reading — mark one a duplicate of the other and keep the first — loses information the product exists to preserve:

> Aditya: *"the timing window is 40 ms"*
> Akhil: *"it fails when the delay exceeds ~40 ms **under load**"*

Discard-one drops "under load" permanently. Pooling those halves is the entire value proposition.

A second reading — rewrite one original in place with the merged text and delete the other — is simpler and keeps the log small. But it leaves an id pointing at text its author never wrote, and `Conflict` references ids.

## Decision

Two Findings judged semantically the same produce a **new** Finding: `provenance: synthesized`, carrying **every** source's Attribution and `merged_from` lineage.

The originals become **tombstones** — `merged_into` set, text and Attribution retained, excluded from retrieval. `RETRIEVABLE == merged_into is None and status is KEPT`.

Semantic sameness is synthesis's judgement, never an id comparison. Ids exist for idempotent ingest and for referencing, not for deciding whether two Findings mean the same thing.

## Consequences

**Tombstones rather than deletes are a correctness requirement, not an audit preference.** Three independent reasons, any one of which is sufficient:

1. **Idempotency.** Producers retry on 5xx, so duplicate delivery is expected. Ingest upserts by `Finding.id`. If a merged-away finding is deleted, a retry finds no such id and re-inserts a finding that has already been merged — recreating the duplicate the merge removed.
2. **References.** `Conflict` holds `FindingId`s. Deletion dangles them; a tombstone lets them follow `merged_into` forward.
3. **Fallibility.** The merge is judged by an 8B model we have documented as expecting ~0.5–0.7 quality, performing the only irreversible action in the system. A bad merge on a delete-based design silently destroys a teammate's insight, in a product whose pitch is that nothing a teammate learned gets lost.

**Cost.** Retrieval must filter rather than read the log wholesale, and every consumer must respect the retrievable predicate. The Finding Log grows monotonically — acceptable at this scale, and a retention policy is a later concern.

**Attribution went plural** as a direct consequence: `Finding.attributions: list[Attribution]`. That in turn narrowed awareness suppression, which now applies only when *every* Attribution is the asking agent's own Agent Session — so a Synthesized Finding carrying a teammate's contribution is always shown.

## Alternatives considered

**Rewrite one original in place, delete the other.** Proposed on the grounds that history is not useful here — correct on its own terms, and it is why we retain a tombstone rather than a full superseded record. Rejected as a mechanism because of the three consequences above.

**Merge only in the Working Memory, keep the Finding Log raw.** Truest archive and simplest synthesis, but retrieval reads the Log, so teammates would receive both near-duplicates — defeating the merge.

## Follow-up

Surfacing tombstoned text dynamically when merge confidence or similarity is low, rather than hiding it unconditionally, is recorded as a stretch goal.
