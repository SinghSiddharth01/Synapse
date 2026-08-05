"""The append-only log. The only thing in Shared Memory that is true.

Everything else in this package — the current view, the symbol index, the BM25
postings, the vectors, the topic centroids — is *derived* and rebuilds by
replaying this log. If the log survives, nothing is lost.

The single rule: **append is the only write operation.** There is no update and
no delete, anywhere, for any reason. That is not an audit preference, it is what
makes three separate problems disappear:

  1. A worker retries on 5xx and re-sends findings it already sent. Under
     update-in-place semantics that resend carries `status`/`merged_into` at
     their defaults (the worker never had a verdict to send), so a whole-object
     write resurrects every merged-away finding in the session — silently. Here
     the resend is just a later entry, the merge entry is still earlier in the
     log, and the fold still drops the original. Safe by construction rather
     than by a field-scoping rule somebody has to remember.

  2. A merge is a small model's judgement call and the only irreversible act in
     the system. Appending the merged finding while leaving both originals in
     place means all three versions coexist: you can read what each author
     actually wrote, check the merge against its own sources, and reverse it.

  3. Conflicts reference finding ids. Nothing dangles, because nothing is ever
     removed.

Five entry kinds, and they are deliberately all the same shape of thing — a
fact that happened, at a position. `Merged`, `MarkedTrivial`, `TopicAssigned`
and `TopicSplit` are not mutations dressed up as events; supersession, trivia
and topic membership are all *computed* from their presence during the fold
(see fold.py).

See docs/adr/0002-semantic-merge-and-tombstones.md and Plan C Task C.2.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Iterable, Iterator, Literal, Union

from synapse_contracts import Finding, FindingId

TopicId = str


@dataclass(frozen=True)
class FindingAppended:
    """A producer pushed a finding. The base fact everything else refers to."""

    finding: Finding
    kind: Literal["finding"] = "finding"

    @property
    def finding_id(self) -> FindingId:
        return self.finding.id


@dataclass(frozen=True)
class Merged:
    """Synthesis judged `sources` to mean the same thing and wrote `result`.

    `result` is a *new* finding carrying every source's attribution. The sources
    are untouched — they stop appearing in the view because this entry exists
    later in the log, not because anything was written onto them.
    """

    result: Finding
    sources: tuple[FindingId, ...]
    kind: Literal["merged"] = "merged"


@dataclass(frozen=True)
class MarkedTrivial:
    """Synthesis judged these findings to restate an action without insight.

    The FIFTH kind (adr/0004 Amendment A3, 2026-08-05). The original four
    cannot express the trivia verdict, and the fold's old visibility rule read
    `Finding.status` -- a producer-writable field that nothing in this package
    ever wrote, and that adr/0004 itself tells readers to treat as undefined.

    Durability judgment has exactly two homes (`adr/0003`): triage upstream and
    this verdict downstream. This entry kind is what keeps the second one from
    being deleted by the move to an append-only log.
    """

    finding_ids: tuple[FindingId, ...]
    kind: Literal["marked_trivial"] = "marked_trivial"


@dataclass(frozen=True)
class TopicAssigned:
    """Which topic a finding landed in, recorded at the moment it landed.

    Read by `fold()` to build `View.topic_of`, which is what `View.members_of`
    and every topic label are derived from.

    ⟨CORRECTED 2026-08-05⟩ This entry is NOT replayed by `SharedMemory.rebuild()`
    -- `rebuild()` re-derives it, by calling `append`/`merge` again and letting
    `_index_and_assign` emit a fresh one (see the comment at the tail of
    `rebuild()`). So a label's stability across a rebuild rests on the REPLAY
    ORDER being identical, not on this entry being read back. The docstring
    previously argued "replaying the decision is cheap; recomputing it is not",
    which describes a design this module does not have. Making `rebuild()`
    actually replay it is a behaviour change and is out of scope for Aug 7; the
    day assignment stops being deterministic -- a real `Embedder`, or membership
    pruning -- it stops being out of scope.
    """

    finding_id: FindingId
    topic_id: TopicId
    founded: bool = False
    kind: Literal["topic_assigned"] = "topic_assigned"


@dataclass(frozen=True)
class TopicSplit:
    """A topic stopped discriminating and was split by 2-means.

    **No finding is re-tagged.** The split re-maps the *grouping*, and current
    membership resolves through the split chain during the fold — exactly the
    same mechanism as merges. A topic that covers a large fraction of the log
    returns more candidates than K, which makes it useless as a lane; splitting
    is the repair. See topics documentation in the design memo, Part III.
    """

    topic_id: TopicId
    into: tuple[TopicId, TopicId]
    assignments: tuple[tuple[FindingId, TopicId], ...]
    kind: Literal["topic_split"] = "topic_split"


Entry = Union[FindingAppended, Merged, MarkedTrivial, TopicAssigned, TopicSplit]


@dataclass
class Log:
    """An ordered, append-only sequence of entries for one Shared Session.

    Order is the log's own position, not wall-clock time. Three laptops cannot
    agree on a clock, but they all append through one service, so position is
    the only ordering that is actually total. Timestamps stay on the findings
    for display and are never used to decide precedence.
    """

    shared_id: str
    entries: list[Entry] = field(default_factory=list)

    def append(self, entry: Entry) -> int:
        """Append one entry. Returns its position, which is its identity."""
        self.entries.append(entry)
        return len(self.entries) - 1

    def extend(self, entries: Iterable[Entry]) -> None:
        for entry in entries:
            self.append(entry)

    def __len__(self) -> int:
        return len(self.entries)

    def __iter__(self) -> Iterator[Entry]:
        return iter(self.entries)

    @property
    def version(self) -> int:
        """The entry count. Monotonic, total, and INTERNAL.

        Its only job is invalidating the fold cache in `SharedMemory.view()`.

        It is **not** `SessionContext.memory_version` and must never be reported
        as a watermark. `memory_version` counts VERDICT ROUNDS APPLIED -- it is
        bumped once at the end of every structurally-valid synthesis verdict,
        merges or not -- and that is what `/findings`, `/synthesize` and
        `/watermark` report. Taking this number instead would make
        `synthesized` True on every push including a pure replay, and turn
        `new_since` into a count of log entries (2+ per finding).
        """
        return len(self.entries)

    def findings(self) -> Iterator[Finding]:
        """Every finding ever appended, including superseded ones, in order.

        Callers that want what is currently *visible* want fold.py, not this.
        This exists for index rebuilds, which must see superseded findings too:
        a merge can be reversed, and an index that never saw the originals
        could not serve them afterwards.
        """
        for entry in self.entries:
            if isinstance(entry, FindingAppended):
                yield entry.finding
            elif isinstance(entry, Merged):
                yield entry.result

    def created_at(self) -> datetime | None:
        for finding in self.findings():
            return finding.ts
        return None
