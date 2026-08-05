"""Candidate selection: five lanes, unioned.

The asymmetry that decides every choice in this module: **this is a recall
problem, not a precision problem.** A missed candidate is a merge that never
happens, silently and permanently — the follower never re-reads a position and
nothing reports the loss. A spurious candidate costs about fifty tokens and the
model says "not the same."

So the lanes are **unioned, never intersected**, and every knob is set toward
returning more rather than fewer.

Each lane catches something the others structurally cannot:

    symbols   a shared number-with-unit or identifier      dict lookup, no scan
    lexical   reworded, same vocabulary                    inverted index, no scan
    vector    paraphrase with no shared words              one pass over N
    topic     a decision that *governs* the finding        centroid lookup
    recent    too new to have settled anywhere             free

WHY RECIPROCAL RANK FUSION
--------------------------
The lanes produce scores on incomparable scales — a BM25 score of 8.4, a cosine
of 0.71 and a symbol rarity weight of 0.5 cannot be added or compared. The two
usual repairs are both bad here: normalising per lane makes the top result of a
lane that found nothing relevant look as strong as one that found the answer,
and hand-weighting the lanes bakes in an assumption about which lane matters
before anything has been measured.

RRF fuses on *rank* instead, which is scale-free and needs no tuning. Its one
real property for this design: a finding surfaced by several lanes rises above
one that a single lane loved. That is exactly the signal worth trusting when
each lane is individually weak.

Every candidate records which lanes surfaced it. That field is not decoration —
it is what makes **lane yield** measurable later (of the candidates a lane
surfaced, what fraction ended in an accepted merge), which is the only honest
answer to whether a lane earns its cost. The topic lane in particular should be
deletable on that evidence.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum

from synapse_contracts import Finding, FindingId

from synapse_service.fold import View
from synapse_service.lexical import LexicalIndex
from synapse_service.semantic import TopicIndex, VectorIndex
from synapse_service.symbols import SymbolIndex, extract

# RRF's damping constant. 60 is the value from the original paper and is
# deliberately not tuned: with five weak lanes and no measured corpus, a tuned
# constant would be fitted to noise.
RRF_K = 60

DEFAULT_TOP_K = 14
# How many recent ids are COLLECTED. Only `max(1, top_k // RESERVE_DIVISOR)` of
# them are ever used (see the reserved floor below), so at the default
# top_k=14 this constant is inert above 2: measured identical results at
# recent=2, 8 and 20, and different only at recent=1. Raising it does nothing;
# raising the FLOOR costs fusion slots that were measured as harmful (an
# unranked recency pick scores identically to a symbol lane's top entry under
# RRF, so eight of them displaced genuine matches and pushed noise to rank 4
# of 14). Whether 2 is right is a harness question -- Plan E Task E.10.
DEFAULT_RECENT = 8

# What fraction of the result a lane with a guaranteed floor may occupy: one
# slot in five each. Every reserved slot is one the fusion does not get, so the
# floor is deliberately small — it exists to stop a lane being shut out
# entirely, not to promote it.
RESERVE_DIVISOR = 5

# The topic lane's default, set by MEASUREMENT in Step 5 of this task and read
# -- never re-declared -- by SharedMemory.candidates, InMemoryStore.candidates,
# recall.measure, test_lanes.py and tests/test_vocabulary.py. One line changes
# the shipped behaviour, the assertions and the vocabulary document together.
DEFAULT_TOPIC_LANE = False


class Lane(StrEnum):
    SYMBOLS = "symbols"
    LEXICAL = "lexical"
    VECTOR = "vector"
    TOPIC = "topic"
    RECENT = "recent"


@dataclass(frozen=True)
class Candidate:
    finding: Finding
    score: float
    lanes: frozenset[Lane]
    shared_symbols: frozenset[str] = frozenset()

    @property
    def finding_id(self) -> FindingId:
        return self.finding.id

    def render(self) -> str:
        """One prompt line. Lane provenance is included on purpose.

        Telling the model *why* a candidate was surfaced — especially a shared
        symbol — is a strong, cheap hint that costs a few tokens per line.
        """
        lanes = " · ".join(sorted(lane.value for lane in self.lanes))
        marks = (
            f": {', '.join(sorted(self.shared_symbols))}" if self.shared_symbols else ""
        )
        return f"#{self.finding.id} {self.finding.text}  [{lanes}{marks}]"


@dataclass(frozen=True)
class CandidateSet:
    """What one new finding is compared against, plus what it cost to find."""

    for_text: str
    candidates: tuple[Candidate, ...]
    searched: int
    lanes_run: frozenset[Lane]

    def __len__(self) -> int:
        return len(self.candidates)

    def ids(self) -> tuple[FindingId, ...]:
        return tuple(candidate.finding_id for candidate in self.candidates)

    def coverage_line(self) -> str:
        """The one global signal in the prompt.

        It exists so "I found no match" is calibrated rather than confident: the
        model can tell the difference between having seen everything and having
        seen fourteen of three thousand. It replaced a table of topic counts,
        which could not be justified — nobody could name a decision that changes
        because the model knows a topic has 412 members, and we are certainly
        not sending 412 findings.
        """
        return (
            f"searched {self.searched} findings · "
            f"{len(self.lanes_run)} lanes · showing top {len(self.candidates)}"
        )


@dataclass
class Indexes:
    """Every derived structure the lanes read. All rebuildable from the log."""

    symbols: SymbolIndex
    lexical: LexicalIndex
    vectors: VectorIndex
    topics: TopicIndex

    def add(self, finding: Finding, sources: tuple[FindingId, ...] = ()) -> None:
        if sources:
            self.symbols.add_merged(finding.id, finding.text, sources)
        else:
            self.symbols.add(finding.id, finding.text)
        self.lexical.add(finding.id, finding.text)
        self.vectors.add(finding.id, finding.text)


def select(
    text: str,
    view: View,
    indexes: Indexes,
    *,
    top_k: int = DEFAULT_TOP_K,
    recent: int = DEFAULT_RECENT,
    exclude: frozenset[FindingId] = frozenset(),
    topic_lane: bool = DEFAULT_TOPIC_LANE,
) -> CandidateSet:
    """Run every lane over the current view and fuse the rankings.

    Only visible findings are eligible. That predicate lives in `fold.py` and is
    applied here once, so no lane and no caller has to remember it.
    """
    visible = frozenset(view.visible_ids)
    blocked = frozenset(exclude)

    def eligible(ranked: list[tuple[FindingId, float]]) -> list[FindingId]:
        return [
            finding_id
            for finding_id, _ in ranked
            if finding_id in visible and finding_id not in blocked
        ]

    ranked_by_lane: dict[Lane, list[FindingId]] = {
        Lane.SYMBOLS: eligible(indexes.symbols.search(text)),
        Lane.LEXICAL: eligible(indexes.lexical.search(text)),
        Lane.VECTOR: eligible(indexes.vectors.search(text)),
    }

    # OFF by default: measured at 0 partners and 0 uniquely at 422 findings
    # and at 2,022 (docs/STATE.md, "The topic lane is on notice"). A lane
    # returning a whole 40-member cluster into an RRF fusion is not free --
    # those members take rank credit that can outvote real matches. Kept
    # behind a flag rather than deleted because lane yield on a REAL corpus is
    # what decides, and that measurement is blocked on the fixture co-sign.
    if topic_lane:
        query_vector = indexes.vectors.embedder.embed(text)
        ranked_by_lane[Lane.TOPIC] = eligible(
            indexes.topics.search(query_vector, view.topic_of))

    # The recall hedge: the last M unconditionally, regardless of any score.
    # Recent work is disproportionately likely to be relevant, and a finding
    # that arrived a minute ago has had no chance to settle into a topic.
    #
    # It is kept OUT of the fusion and given a small reserved allocation
    # instead, because it is not a ranking. Measured on a 422-finding corpus,
    # letting it compete cost real recall: its top entry scores identically to
    # a symbol lane's top entry under RRF, so eight unranked recency picks
    # displaced genuine matches and pushed noise to rank 4 of 14. A hedge that
    # crowds out the thing it is hedging for is worse than no hedge.
    recent_ids = [
        finding_id
        for finding_id in reversed(view.visible_ids)
        if finding_id not in blocked
    ][:recent]
    ranked_by_lane[Lane.RECENT] = recent_ids

    fused: dict[FindingId, float] = {}
    lanes_of: dict[FindingId, set[Lane]] = {}
    for lane, ranking in ranked_by_lane.items():
        if lane is Lane.RECENT:
            continue
        for rank, finding_id in enumerate(ranking):
            fused[finding_id] = fused.get(finding_id, 0.0) + 1.0 / (RRF_K + rank + 1)
            lanes_of.setdefault(finding_id, set()).add(lane)

    query_symbols = extract(text)

    # Two lanes get a guaranteed floor rather than competing on fused rank, for
    # opposite reasons. Both were established by measurement, not taste.
    #
    # SYMBOLS is high-precision: a shared rare identifier is close to proof.
    # Measured on a 422-finding corpus, a pair sharing `1.9.4` — a symbol held
    # by exactly two findings — was pushed out of the top 14 by twelve
    # distractors that each scored on lexical *and* vector. RRF assumes its
    # inputs are independent; those two are not (they read the same surface
    # form), so their agreement double-counts one signal and outvotes real
    # evidence. Weighting the lanes would fix it too, but a weight tuned
    # against a synthetic corpus is fitted to noise. A floor is not a weight.
    #
    # RECENT is the opposite case: it is not a ranking at all, so letting its
    # unranked picks compete on rank crowded out the matches it exists to hedge.
    floor = max(1, top_k // RESERVE_DIVISOR)
    reserved: tuple[tuple[Lane, list[FindingId]], ...] = (
        (Lane.SYMBOLS, ranked_by_lane[Lane.SYMBOLS][:floor]),
        (Lane.RECENT, recent_ids[:floor]),
    )
    budget = top_k - sum(min(len(ids), floor) for _, ids in reserved)

    fused_ranked = sorted(fused.items(), key=lambda kv: (-kv[1], kv[0]))
    ordered = fused_ranked[:budget]
    chosen = {finding_id for finding_id, _ in ordered}

    for lane, ids in reserved:
        for finding_id in ids:
            if len(ordered) >= top_k:
                break
            lanes_of.setdefault(finding_id, set()).add(lane)
            if finding_id in chosen:
                continue
            ordered.append((finding_id, fused.get(finding_id, 0.0)))
            chosen.add(finding_id)

    # A reserved id that the fusion had ALREADY chosen consumed a budget slot
    # and then hit the `continue` above, so the result silently came back short
    # -- measured 12 of 14, and worst exactly when the shared symbol is common
    # and breadth matters most. Refill from the fusion remainder.
    for finding_id, score in fused_ranked:
        if len(ordered) >= top_k:
            break
        if finding_id in chosen:
            continue
        ordered.append((finding_id, score))
        chosen.add(finding_id)

    ordered.sort(key=lambda kv: (-kv[1], kv[0]))
    candidates = tuple(
        Candidate(
            finding=view.findings[finding_id],
            score=score,
            lanes=frozenset(lanes_of[finding_id]),
            shared_symbols=query_symbols
            & indexes.symbols.symbols_of.get(finding_id, frozenset()),
        )
        for finding_id, score in ordered
    )

    # `searched` is what coverage_line() reports so the model can tell "I saw
    # everything" apart from "I saw fourteen of three thousand". Counting
    # findings the asker was never allowed to see over-reports exactly the
    # suppression, and turns a calibrated statement into a confident one.
    return CandidateSet(
        for_text=text,
        candidates=candidates,
        searched=len(visible - blocked),
        lanes_run=frozenset(ranked_by_lane),
    )
