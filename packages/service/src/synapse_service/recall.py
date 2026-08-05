"""Candidate recall: does the true partner reach the model at all?

For every finding with a known duplicate, ask the store for candidates and check
whether the partner is among them. That is the whole measurement, and it needs
no model, no network and no key — which is why it can be a test rather than an
eval, and why it should be the first number this track produces.

Recall is reported three ways, because a single aggregate hides the thing you
need to know:

    overall         did the partner make top-K
    by band         which *kind* of duplicate is being missed
    by lane         which lane actually surfaced the partner  (lane yield)

Lane yield is the number that decides whether a lane earns its cost. A lane that
fires constantly and never supplies the partner is machinery, not retrieval —
the topic lane in particular should be deletable on this evidence, and the
design is written so that deleting it changes nothing else.

The measurement is only as good as the corpus, and the corpus that exists today
is synthetic and written by the same author as the lanes. See corpus.py. Treat
these numbers as a regression guard and nothing more.
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field

from synapse_service.corpus import Band, CorpusEntry
from synapse_service.lanes import DEFAULT_TOP_K, Lane
from synapse_service.memory import SharedMemory


@dataclass(frozen=True)
class Probe:
    """One finding's result: was its partner retrieved, and by what?"""

    finding_id: str
    partner_id: str
    band: Band
    found: bool
    rank: int | None
    lanes: frozenset[Lane]


@dataclass
class RecallReport:
    top_k: int
    corpus_size: int
    probes: list[Probe] = field(default_factory=list)

    @property
    def overall(self) -> float:
        if not self.probes:
            return 0.0
        return sum(1 for probe in self.probes if probe.found) / len(self.probes)

    def by_band(self) -> dict[Band, float]:
        buckets: dict[Band, list[bool]] = defaultdict(list)
        for probe in self.probes:
            buckets[probe.band].append(probe.found)
        return {
            band: sum(results) / len(results) for band, results in buckets.items()
        }

    def by_lane(self) -> dict[Lane, int]:
        """How many partners each lane surfaced. Lanes overlap, so this sums high.

        A lane at zero found nothing that mattered, whatever else it was doing.
        """
        counts: dict[Lane, int] = {lane: 0 for lane in Lane}
        for probe in self.probes:
            for lane in probe.lanes:
                counts[lane] += 1
        return counts

    def unique_to(self, lane: Lane) -> int:
        """Partners *only* this lane found. The real case for keeping it."""
        return sum(
            1 for probe in self.probes if probe.found and probe.lanes == {lane}
        )

    def format(self) -> str:
        lines = [
            f"candidate recall @ K={self.top_k}   corpus={self.corpus_size} findings",
            f"  overall            {self.overall:6.1%}  "
            f"({sum(1 for p in self.probes if p.found)}/{len(self.probes)})",
            "",
            "  by band",
        ]
        for band, score in sorted(self.by_band().items()):
            lines.append(f"    {band.value:<12}     {score:6.1%}")

        lines += ["", "  by lane (partners surfaced · unique to that lane)"]
        counts = self.by_lane()
        for lane in Lane:
            lines.append(
                f"    {lane.value:<12}     {counts[lane]:>3}  ·  "
                f"{self.unique_to(lane):>3}"
            )
        return "\n".join(lines)


def measure(
    entries: list[CorpusEntry], *, top_k: int = DEFAULT_TOP_K, shared_id: str = "recall"
) -> RecallReport:
    """Load the corpus into a store, then probe every finding that has a partner.

    Every finding is loaded first, so each probe searches a complete corpus
    rather than only what happened to arrive before it. Retrieval at merge time
    is against the whole log too — measuring against a partial one would flatter
    the lanes for the same reason a time-window candidate set does.
    """
    store = SharedMemory(shared_id=shared_id)
    for entry in entries:
        store.append(entry.finding)

    report = RecallReport(top_k=top_k, corpus_size=len(entries))

    for entry in entries:
        if entry.partner_id is None or entry.band is None:
            continue

        result = store.candidates(
            entry.finding.text,
            top_k=top_k,
            exclude=frozenset({entry.finding.id}),
        )
        ids = result.ids()
        found = entry.partner_id in ids

        lanes: frozenset[Lane] = frozenset()
        rank: int | None = None
        if found:
            rank = ids.index(entry.partner_id)
            lanes = result.candidates[rank].lanes

        report.probes.append(
            Probe(
                finding_id=entry.finding.id,
                partner_id=entry.partner_id,
                band=entry.band,
                found=found,
                rank=rank,
                lanes=lanes,
            )
        )

    return report
