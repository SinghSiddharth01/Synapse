"""The storage seam. Narrow on purpose.

Plan C Task C.2 asks for three operations, and warns that this shape is a
deliberate first pass and where most of the system's future value lives. The
signatures below keep two options open that the plan's original three did not:

  * `candidates()` takes the text to search *for*, so the same call serves both
    readers that need judgement — synthesis asks with an incoming finding,
    retrieval asks with a teammate's question. Building one lookup rather than
    two is the difference between merges that only happen inside a batch and
    merges that happen at all.

  * `candidates()` takes a `top_k`, so swapping the in-memory lanes for an
    approximate index later changes no caller.

`rebuild()` exists to keep the central claim honest: every index in this package
is derived, and the log alone is sufficient. It is exercised by a test that
folds a log, rebuilds from scratch, and asserts the two are identical. If that
ever goes red, something has become a second source of truth.

Persistence is deliberately absent. Shared Memory is in-memory for the
hackathon; recovery is every orchestrator re-pushing its durable log, which is
safe precisely because appending a finding twice is inert (see fold.py).
"""

from __future__ import annotations

from dataclasses import dataclass, field

from synapse_contracts import Finding, FindingId

from synapse_service.fold import View, fold
from synapse_service.lanes import (
    DEFAULT_RECENT,
    DEFAULT_TOP_K,
    CandidateSet,
    Indexes,
    select,
)
from synapse_service.lexical import LexicalIndex
from synapse_service.log import (
    FindingAppended,
    Log,
    MarkedTrivial,
    Merged,
    TopicAssigned,
    TopicId,
    TopicSplit,
)
from synapse_service.semantic import (
    Embedder,
    HashingEmbedder,
    TopicIndex,
    VectorIndex,
)
from synapse_service.symbols import SymbolIndex

# A topic holding more than this many findings has stopped discriminating: it
# returns more candidates than the prompt can hold, which makes it worthless as
# a lane while still looking like it is working. That silent-degradation shape
# is why it is checked rather than trusted.
MAX_TOPIC_SIZE = 60
MAX_TOPIC_SHARE = 0.35


@dataclass(frozen=True)
class Appended:
    """Result of appending. `topic_founded` is the only thing owing a model call."""

    finding_id: FindingId
    topic_id: TopicId
    topic_founded: bool
    version: int
    """`Log.version` -- the ENTRY COUNT, internal. Nothing consumes this field
    today, which is exactly the condition under which someone wires it to a
    watermark later. It is not `SessionContext.memory_version`. If you need a
    number for a client, `store.get_context(sid).memory_version` is the one."""


@dataclass
class SharedMemory:
    """One Shared Session's memory: an append-only log plus derived indexes.

    Paired with the registry (`InMemoryStore`) that holds one of these per
    Shared Session alongside the Working Memory prose on `SessionContext`,
    this is CONTEXT.md's **Shared Memory**.
    """

    shared_id: str
    purpose: str = ""
    embedder: Embedder = field(default_factory=HashingEmbedder)
    log: Log = field(init=False)
    indexes: Indexes = field(init=False)
    _view: View | None = field(default=None, init=False, repr=False)

    def __post_init__(self) -> None:
        self.log = Log(shared_id=self.shared_id)
        self.indexes = _fresh_indexes(self.embedder)

    # ---- writes -----------------------------------------------------------

    def append(self, finding: Finding, *, is_new: bool | None = None) -> Appended:
        """Add a finding. Idempotent by id, because the resend path demands it.

        `is_new` lets a BATCHING caller pass the answer it already has.
        `InMemoryStore.upsert` folds once for the whole push and then tells
        each append; leaving it None makes this method ask `self.view()`
        itself, and since every append invalidates the fold cache that is one
        fold PER FINDING -- O(N**2) in log entries over a batch. Correct
        either way, and the batch path is the one the route uses.

        The authority is the folded view, never an index. Reading
        `self.indexes.vectors.vectors` here made the duplicate guard
        untestable: deleting the guard left 75 tests green, because the index
        and the log happened to agree.
        """
        if is_new is None:
            is_new = finding.id not in self.view().findings
        if not is_new:
            # A resend. Record that it happened; index nothing; and DO NOT
            # FOLD. This branch runs N times per push, not zero: api.py:71
            # upserts and then synthesis.py:131 upserts the SAME list again
            # (see api.py's rewritten comment). Reading `self.view()` here to
            # fill `topic_id` re-folded a log the previous append had just
            # invalidated -- one fold per duplicate, which is precisely the
            # O(N**2) the `is_new` hint exists to remove, left in place on the
            # one path that always takes it.
            #
            # `topic_id` is empty because NOTHING consumes `Appended` (see
            # __init__.py, and Task 5's docstring on `.version`). If anything
            # ever does, read it from the CALLER's batch view -- do not fold
            # here.
            self.log.append(FindingAppended(finding=finding))
            self._view = None
            return Appended(finding_id=finding.id, topic_id="",
                            topic_founded=False, version=self.log.version)

        self.log.append(FindingAppended(finding=finding))
        return self._index_and_assign(finding)

    def merge(self, result: Finding, sources: tuple[FindingId, ...]) -> Appended:
        """Record that `sources` mean the same thing and `result` captures both.

        The sources are not touched. They stop appearing in the view because
        this entry is later in the log — which is what lets a bad merge be
        reversed and what stops a resync from resurrecting them.
        """
        self.log.append(Merged(result=result, sources=sources))
        return self._index_and_assign(result, sources=sources)

    def mark_trivial(self, finding_ids: tuple[FindingId, ...]) -> None:
        """Record synthesis's trivia verdict. Nothing is written onto the
        findings; they leave the view because this entry exists."""
        if not finding_ids:
            return
        self.log.append(MarkedTrivial(finding_ids=finding_ids))
        self._view = None

    def split_topic(self, topic_id: TopicId) -> tuple[TopicId, TopicId]:
        """2-means a collapsed topic. Appends the split; re-tags nothing."""
        into, assignments = self.indexes.topics.split(
            topic_id, self.indexes.vectors.vectors
        )
        self.log.append(
            TopicSplit(topic_id=topic_id, into=into, assignments=assignments)
        )
        self._view = None
        return into

    # ---- reads ------------------------------------------------------------

    def view(self) -> View:
        """The current view, folded. Cached until the next write."""
        if self._view is None or self._view.version != self.log.version:
            self._view = fold(self.log)
        return self._view

    def candidates(
        self,
        text: str,
        *,
        top_k: int = DEFAULT_TOP_K,
        recent: int = DEFAULT_RECENT,
        exclude: frozenset[FindingId] = frozenset(),
    ) -> CandidateSet:
        """The one lookup. Synthesis passes a finding; retrieval passes a query."""
        return select(
            text,
            self.view(),
            self.indexes,
            top_k=top_k,
            recent=recent,
            exclude=exclude,
        )

    def unhealthy_topics(self) -> list[TopicId]:
        """Topics that have stopped discriminating and should be split."""
        return [
            health.topic_id
            for health in self.indexes.topics.health(self.indexes.vectors.vectors)
            if health.is_collapsed(
                max_share=MAX_TOPIC_SHARE, max_size=MAX_TOPIC_SIZE
            )
        ]

    # ---- derivation -------------------------------------------------------

    def rebuild(self) -> None:
        """Discard every index and recompute from the log alone.

        The claim this defends: the log is the only thing that has to survive.
        Anything that does not come back from a replay was never derived state,
        it was a second source of truth hiding as a cache.
        """
        entries = list(self.log.entries)
        self.log = Log(shared_id=self.shared_id)
        self.indexes = _fresh_indexes(self.embedder)
        self._view = None

        for entry in entries:
            if isinstance(entry, FindingAppended):
                self.append(entry.finding)
            elif isinstance(entry, Merged):
                self.merge(entry.result, entry.sources)
            elif isinstance(entry, TopicSplit):
                self.split_topic(entry.topic_id)
            elif isinstance(entry, MarkedTrivial):
                self.mark_trivial(entry.finding_ids)
            # TopicAssigned is re-emitted by append/merge and is not replayed
            # directly; replaying it would double-count centroid membership.

    # ---- internals --------------------------------------------------------

    def _index_and_assign(
        self, finding: Finding, sources: tuple[FindingId, ...] = ()
    ) -> Appended:
        self.indexes.add(finding, sources=sources)
        vector = self.indexes.vectors.vectors[finding.id]
        topic_id, founded = self.indexes.topics.assign(finding.id, vector)
        self.log.append(
            TopicAssigned(
                finding_id=finding.id, topic_id=topic_id, founded=founded
            )
        )
        self._view = None
        return Appended(
            finding_id=finding.id,
            topic_id=topic_id,
            topic_founded=founded,
            version=self.log.version,
        )


def _fresh_indexes(embedder: Embedder) -> Indexes:
    return Indexes(
        symbols=SymbolIndex(),
        lexical=LexicalIndex(),
        vectors=VectorIndex(embedder=embedder),
        topics=TopicIndex(),
    )
