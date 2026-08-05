"""The vector and topic lanes.

Two lanes, one substrate. Every finding is embedded once when it is appended,
and both lanes read those vectors:

    vector   cosine of the query against every finding. The only lane that
             touches all N — but as one matmul-shaped pass, which stays
             comfortable to roughly a million records. There is no vector
             database here; an approximate index is a drop-in later that
             changes no interface.

    topic    which cluster a finding belongs to, so that a *decision* can be
             retrieved for a *finding* it governs. "Don't tune pool size, it's
             not the cause" shares no vocabulary and no embedding neighbourhood
             with "fails above 40 ms under load", but it is the finding most
             likely to stop a wrong merge. Nothing else in the system can reach
             it.

TOPICS: GEOMETRY DECIDES, A MODEL ONLY NAMES
--------------------------------------------
An earlier design had the on-device 4B emitting a topic label per finding. That
was wrong: it is the least reliable component in the system, it sees one segment
at a time with no view of the session's vocabulary, and it would fragment
`pooling` / `conn pool` / `connection pooling` within an hour.

The fix is not a bigger model, it is splitting a job that was never one job.
Membership is a cosine against existing centroids — deterministic, drift-free,
no model. Naming is one model call when a cluster is *born*, not per finding,
and it rides along on the merge call as an extra output field.

So **retrieval uses centroids, not names.** A bad label makes the prompt read
oddly and changes nothing about what is retrieved. The unreliable component sits
in cosmetics, where its failures are visible and harmless.

WHAT THE OFFLINE EMBEDDER DOES AND DOES NOT MEASURE
---------------------------------------------------
`HashingEmbedder` is deterministic, dependency-free, and runs in CI. It hashes
tokens into a fixed space, so it has real signal for shared vocabulary and
**no signal at all for paraphrase** — which is precisely the case the vector
lane exists to catch.

That means recall measured against it is a measurement of the *plumbing*, not of
the lane. Numbers from `recall.py` under this embedder are a lower bound and a
regression guard; they are not evidence the vector or topic lanes work. Saying
otherwise would be the same error as reporting `verbatim_overlap = 0.00` as
proof of privacy.
"""

from __future__ import annotations

import hashlib
import math
from dataclasses import dataclass, field
from typing import Protocol, Sequence, runtime_checkable

from synapse_contracts import FindingId

from synapse_service.lexical import tokenize
from synapse_service.log import TopicId

Vector = tuple[float, ...]


@runtime_checkable
class Embedder(Protocol):
    """Anything that turns finding text into a unit-norm vector.

    Kept as a protocol so the real embedding model (bge via the existing
    OpenAI-compatible provider) drops in behind the same call that the offline
    hashing stand-in satisfies.
    """

    @property
    def dimensions(self) -> int: ...

    def embed(self, text: str) -> Vector: ...


@dataclass(frozen=True)
class HashingEmbedder:
    """Deterministic bag-of-tokens hashing. Offline, no dependencies, no model.

    Signal for shared vocabulary; none for paraphrase. See the module docstring
    before reading anything into a number produced with this.
    """

    dims: int = 256

    @property
    def dimensions(self) -> int:
        return self.dims

    def embed(self, text: str) -> Vector:
        buckets = [0.0] * self.dims
        for token in tokenize(text):
            digest = hashlib.blake2b(token.encode("utf-8"), digest_size=8).digest()
            index = int.from_bytes(digest[:4], "big") % self.dims
            # Signed contribution, so unrelated tokens landing in one bucket
            # cancel as often as they reinforce instead of always adding.
            sign = 1.0 if digest[4] & 1 else -1.0
            buckets[index] += sign
        return normalise(tuple(buckets))


def normalise(vector: Sequence[float]) -> Vector:
    magnitude = math.sqrt(sum(component * component for component in vector))
    if magnitude == 0.0:
        return tuple(vector)
    return tuple(component / magnitude for component in vector)


def cosine(a: Sequence[float], b: Sequence[float]) -> float:
    """Dot product of two unit vectors. Both sides are normalised on store."""
    return sum(x * y for x, y in zip(a, b))


def mean(vectors: Sequence[Vector]) -> Vector:
    if not vectors:
        return ()
    dims = len(vectors[0])
    total = [0.0] * dims
    for vector in vectors:
        for i, component in enumerate(vector):
            total[i] += component
    return normalise([component / len(vectors) for component in total])


@dataclass
class VectorIndex:
    """Embeddings for every finding. Derived; rebuilt by replaying the log."""

    embedder: Embedder
    vectors: dict[FindingId, Vector] = field(default_factory=dict)

    def add(self, finding_id: FindingId, text: str) -> Vector:
        vector = self.embedder.embed(text)
        self.vectors[finding_id] = vector
        return vector

    def search(
        self, text: str, *, exclude: frozenset[FindingId] = frozenset()
    ) -> list[tuple[FindingId, float]]:
        query = self.embedder.embed(text)
        scored = [
            (finding_id, cosine(query, vector))
            for finding_id, vector in self.vectors.items()
            if finding_id not in exclude
        ]
        # Negative similarity is anti-correlation, never a candidate.
        return sorted(
            (pair for pair in scored if pair[1] > 0.0),
            key=lambda kv: (-kv[1], kv[0]),
        )


@dataclass
class Topic:
    topic_id: TopicId
    centroid: Vector
    members: list[FindingId] = field(default_factory=list)
    name: str = ""

    @property
    def size(self) -> int:
        return len(self.members)


@dataclass(frozen=True)
class TopicHealth:
    """Both failure directions of one threshold, measured without a model.

    Collapse and fragmentation are the same knob set wrong in opposite
    directions. Neither raises; both are visible here. Note that detection
    assumes something is *watching* — there is no alerting anywhere in this
    design, which is a named gap rather than an oversight.
    """

    topic_id: TopicId
    size: int
    spread: float
    share: float

    def is_collapsed(self, *, max_share: float, max_size: int) -> bool:
        """Stopped discriminating: it returns more than a prompt can hold."""
        return self.size > max_size or self.share > max_share


@dataclass
class TopicIndex:
    """Centroid clustering over finding vectors. No model in the decision path."""

    threshold: float = 0.45
    topics: dict[TopicId, Topic] = field(default_factory=dict)
    _counter: int = 0

    def assign(self, finding_id: FindingId, vector: Vector) -> tuple[TopicId, bool]:
        """Join the nearest topic above threshold, or found a new one.

        Returns `(topic_id, founded)`. `founded` is what tells the caller a
        naming call is owed — the only point at which a model is involved in
        topics at all.
        """
        best_id, best_score = None, self.threshold
        for topic in self.topics.values():
            score = cosine(vector, topic.centroid)
            if score > best_score:
                best_id, best_score = topic.topic_id, score

        if best_id is None:
            topic_id = self._next_id()
            self.topics[topic_id] = Topic(
                topic_id=topic_id, centroid=vector, members=[finding_id]
            )
            return topic_id, True

        topic = self.topics[best_id]
        topic.members.append(finding_id)
        # Incremental centroid update: cheaper than re-averaging, and exact.
        n = len(topic.members)
        topic.centroid = normalise(
            [
                (c * (n - 1) + v) / n
                for c, v in zip(topic.centroid, vector)
            ]
        )
        return best_id, False

    def health(self, vectors: dict[FindingId, Vector]) -> list[TopicHealth]:
        total = sum(topic.size for topic in self.topics.values()) or 1
        report = []
        for topic in self.topics.values():
            members = [vectors[m] for m in topic.members if m in vectors]
            spread = (
                1.0 - sum(cosine(v, topic.centroid) for v in members) / len(members)
                if members
                else 0.0
            )
            report.append(
                TopicHealth(
                    topic_id=topic.topic_id,
                    size=topic.size,
                    spread=spread,
                    share=topic.size / total,
                )
            )
        return sorted(report, key=lambda h: -h.size)

    def split(
        self, topic_id: TopicId, vectors: dict[FindingId, Vector]
    ) -> tuple[tuple[TopicId, TopicId], tuple[tuple[FindingId, TopicId], ...]]:
        """2-means a collapsed topic into two. Deterministic seeding.

        Returns the two new ids and the per-finding reassignment, which the
        caller appends as a `TopicSplit` entry. **No finding is re-tagged** —
        the split re-maps the grouping and the fold resolves it, exactly like a
        merge.
        """
        topic = self.topics[topic_id]
        members = [m for m in topic.members if m in vectors]
        if len(members) < 2:
            raise ValueError(f"cannot split {topic_id!r}: {len(members)} member(s)")

        # Farthest-first seeding rather than random: the split must be
        # reproducible from the log, and `Math.random` in a fold is a bug.
        first = min(members)
        seed_a = max(members, key=lambda m: (-cosine(vectors[first], vectors[m]), m))
        seed_b = max(members, key=lambda m: (-cosine(vectors[seed_a], vectors[m]), m))

        centre_a, centre_b = vectors[seed_a], vectors[seed_b]
        groups: tuple[list[FindingId], list[FindingId]] = ([], [])
        for _ in range(10):
            groups = ([], [])
            for member in members:
                target = 0 if cosine(vectors[member], centre_a) >= cosine(
                    vectors[member], centre_b
                ) else 1
                groups[target].append(member)
            if not groups[0] or not groups[1]:
                # Degenerate: everything landed on one side. Halve by distance
                # to keep the split meaningful rather than returning a no-op.
                ordered = sorted(members, key=lambda m: (-cosine(vectors[m], centre_a), m))
                middle = len(ordered) // 2
                groups = (ordered[:middle], ordered[middle:])
                break
            new_a = mean([vectors[m] for m in groups[0]])
            new_b = mean([vectors[m] for m in groups[1]])
            if new_a == centre_a and new_b == centre_b:
                break
            centre_a, centre_b = new_a, new_b

        id_a, id_b = self._next_id(), self._next_id()
        self.topics[id_a] = Topic(id_a, mean([vectors[m] for m in groups[0]]), groups[0])
        self.topics[id_b] = Topic(id_b, mean([vectors[m] for m in groups[1]]), groups[1])
        del self.topics[topic_id]

        assignments = tuple(
            (member, id_a if member in set(groups[0]) else id_b) for member in members
        )
        return (id_a, id_b), assignments

    def search(
        self,
        vector: Vector,
        topic_of: dict[FindingId, TopicId],
        *,
        exclude: frozenset[FindingId] = frozenset(),
        limit: int = 40,
    ) -> list[tuple[FindingId, float]]:
        """Findings in the nearest topic to `vector`.

        Deliberately returns a whole cluster rather than a ranked slice of one:
        the point of this lane is reaching findings that no *text* similarity
        would surface, so ranking them by text similarity again would undo it.
        Members come back in log order, capped.
        """
        if not self.topics:
            return []
        nearest = max(
            self.topics.values(), key=lambda t: (cosine(vector, t.centroid), t.topic_id)
        )
        if cosine(vector, nearest.centroid) <= 0.0:
            return []
        members = [
            m
            for m in nearest.members
            if m not in exclude and topic_of.get(m) == nearest.topic_id
        ]
        return [(m, 1.0) for m in members[:limit]]

    def _next_id(self) -> TopicId:
        self._counter += 1
        return f"t{self._counter:04d}"
