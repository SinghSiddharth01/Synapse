"""Folding the log into the current view.

"Visible" is not a field on a finding. It is a property of the log as a whole,
recomputed the same way every time: replay entries in order, and a finding drops
out when a later entry supersedes it.

That distinction is the whole reason a worker's resync is harmless. Under a
stored flag, the resend carries `merged_into=None` and un-hides everything. Here
the resend is a later entry that adds no information, while the merge entry that
superseded the finding is still sitting in the log — so the fold produces the
same view it did before.

The result is a cache. Recompute it, keep it warm incrementally, or throw it
away and fold again; it is never a second source of truth.

Four things resolve here, all by the same mechanism:

    supersession   a finding named as a source by a later Merged entry
    topic          the last TopicAssigned, then any TopicSplit reassignment
    trivia         synthesis marked the finding as restating an action
    termination    a SessionEnded entry closed the Shared Session  ⟨2026-08-06⟩

RETRIEVABLE  ==  not superseded and not marked trivial.  Defined once, here.
No consumer outside this module is given the raw entry list, because a
predicate re-implemented at a call site is one that eventually gets it subtly
wrong and surfaces something that was merged away.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from synapse_contracts import Finding, FindingId

from synapse_service.log import (
    Entry,
    FindingAppended,
    Log,
    MarkedTrivial,
    Merged,
    SessionEnded,
    TopicAssigned,
    TopicId,
    TopicSplit,
)

# A merge whose result is itself later merged forms a chain. Chains are legal
# and expected (three people converge over an afternoon), but a malformed log
# could contain a cycle, and a fold that hangs takes the whole service with it.
# Depth is capped rather than cycle-detected-only so that a pathological-but-
# acyclic chain also terminates.
MAX_SUPERSESSION_DEPTH = 64


class SupersessionCycleError(RuntimeError):
    """A finding is, transitively, its own successor. The log is malformed."""


@dataclass(frozen=True)
class View:
    """What Shared Memory currently looks like. Entirely derived from a Log."""

    shared_id: str
    version: int
    findings: dict[FindingId, Finding]
    visible_ids: tuple[FindingId, ...]
    superseded_by: dict[FindingId, FindingId]
    topic_of: dict[FindingId, TopicId]
    members_of: dict[TopicId, tuple[FindingId, ...]] = field(default_factory=dict)
    trivial: frozenset[FindingId] = frozenset()

    # The Contributor who closed this Shared Session, or None while it is open
    # ⟨2026-08-06, session lifecycle spec⟩. Derived like everything else here:
    # `SessionContext.status` is this field read through the store, never a
    # written flag. Kept as the id rather than a bool because the 403 on
    # `POST /end` and any later audit both want to name someone.
    ended_by: str | None = None

    def visible(self) -> list[Finding]:
        """Retrievable findings, in log order. The only list consumers get."""
        return [self.findings[fid] for fid in self.visible_ids]

    def is_visible(self, finding_id: FindingId) -> bool:
        return finding_id in self.visible_ids

    def resolve(self, finding_id: FindingId) -> FindingId:
        """Follow supersession forward to the finding that survives.

        A `Conflict` holds ids, and the finding it names may have been merged
        away since. Rather than dangling, it follows the chain to whatever now
        carries that meaning.
        """
        seen: set[FindingId] = set()
        current = finding_id
        for _ in range(MAX_SUPERSESSION_DEPTH):
            if current not in self.superseded_by:
                return current
            if current in seen:
                raise SupersessionCycleError(
                    f"{finding_id!r} is transitively its own successor "
                    f"(cycle at {current!r})"
                )
            seen.add(current)
            current = self.superseded_by[current]
        raise SupersessionCycleError(
            f"supersession chain from {finding_id!r} exceeded "
            f"{MAX_SUPERSESSION_DEPTH} hops; treating as malformed"
        )


def fold(log: Log) -> View:
    """Replay a log into its current view. Pure, deterministic, no model."""
    findings: dict[FindingId, Finding] = {}
    superseded_by: dict[FindingId, FindingId] = {}
    topic_of: dict[FindingId, TopicId] = {}
    trivial: set[FindingId] = set()
    order: list[FindingId] = []
    ended_by: str | None = None

    for entry in log:
        # Termination comes back as a RETURN VALUE rather than through a
        # mutable accumulator like the four above it, because it is a scalar:
        # threading it through a one-element list purely to keep `_apply`'s
        # signature uniform would hide "first end wins" inside `_apply`, where
        # the reader looking for it would not think to check. `is None` here
        # is that rule, stated once, in the open.
        closed_by = _apply(entry, findings, superseded_by, topic_of, trivial, order)
        if ended_by is None:
            ended_by = closed_by

    # RETRIEVABLE, defined once, here:
    visible_ids = tuple(
        fid
        for fid in order
        if fid not in superseded_by and fid not in trivial
    )

    members: dict[TopicId, list[FindingId]] = {}
    for fid in visible_ids:
        topic = topic_of.get(fid)
        if topic is not None:
            members.setdefault(topic, []).append(fid)

    return View(
        shared_id=log.shared_id,
        version=log.version,
        findings=findings,
        visible_ids=visible_ids,
        superseded_by=superseded_by,
        topic_of=topic_of,
        members_of={t: tuple(ids) for t, ids in members.items()},
        trivial=frozenset(trivial),
        ended_by=ended_by,
    )


def _apply(
    entry: Entry,
    findings: dict[FindingId, Finding],
    superseded_by: dict[FindingId, FindingId],
    topic_of: dict[FindingId, TopicId],
    trivial: set[FindingId],
    order: list[FindingId],
) -> str | None:
    """Fold one entry into the accumulating state.

    Returns the Contributor named by a `SessionEnded` entry and None for every
    other kind — see `fold()` for why termination is not accumulated in place.
    """
    if isinstance(entry, FindingAppended):
        _record(entry.finding, findings, order)

    elif isinstance(entry, Merged):
        _record(entry.result, findings, order)
        for source in entry.sources:
            # Last merge wins if a source is somehow claimed twice. It cannot
            # be un-superseded, which is the property that makes a resend inert.
            superseded_by[source] = entry.result.id

    elif isinstance(entry, MarkedTrivial):
        trivial.update(entry.finding_ids)

    elif isinstance(entry, TopicAssigned):
        topic_of[entry.finding_id] = entry.topic_id

    elif isinstance(entry, TopicSplit):
        for finding_id, new_topic in entry.assignments:
            topic_of[finding_id] = new_topic

    elif isinstance(entry, SessionEnded):
        return entry.ended_by

    return None


def _record(
    finding: Finding, findings: dict[FindingId, Finding], order: list[FindingId]
) -> None:
    """First write of an id wins; later identical appends are inert.

    This is the resend path. A worker replaying its durable log re-sends
    findings the service already holds, and the copy it sends carries the
    service-written fields at their defaults because the worker never had a
    verdict. Ignoring the duplicate — rather than overwriting — is what stops
    a resync from undoing every merge in the session.
    """
    if finding.id in findings:
        return
    findings[finding.id] = finding
    order.append(finding.id)
