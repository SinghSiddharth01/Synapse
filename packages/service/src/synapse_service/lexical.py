"""The term-overlap lane: BM25 over an inverted index.

Catches rewording that reuses vocabulary — "the pool runs out of connections"
against "connections are exhausted in the pool". The symbol lane misses that
(no shared identifier), the vector lane catches it but costs an embedding call
per finding; this one is free and deterministic.

**It does not scan the log.** An inverted index only touches documents that
share at least one term with the query, so cost tracks query length and term
rarity, not corpus size. That is the property that makes it hold up when the log
is far larger than any context window.

BM25 rather than plain overlap for one reason that matters in this corpus: rare
terms get weighted higher. `qairt` appearing in two findings means something;
`the` does not. Stopwords are removed anyway, but the weighting keeps working
for domain terms no stopword list knows about.
"""

from __future__ import annotations

import math
import re
from collections import Counter, defaultdict
from dataclasses import dataclass, field

from synapse_contracts import FindingId

# Standard BM25 parameters. k1 controls how fast term frequency saturates, b how
# much document length is penalised. Findings are short and uniform in length,
# so b barely matters here; both are left at the textbook values rather than
# tuned against a corpus of two.
K1 = 1.5
B = 0.75

_TOKEN = re.compile(r"[a-z0-9_]+")

# Deliberately short. A long stopword list starts eating domain vocabulary —
# "does", "not" and "under" all carry meaning in a finding like "does not
# reproduce under load", and dropping them costs recall on exactly the
# qualifier-bearing findings that merging exists to preserve.
_STOPWORDS = frozenset(
    """
    a an the this that these those it its is are was were be been being
    of to in on at by for with from as and or but if then than so
    we our you your they their i me my
    """.split()
)


def tokenize(text: str) -> list[str]:
    return [
        token
        for token in _TOKEN.findall(text.lower())
        if len(token) > 1 and token not in _STOPWORDS
    ]


@dataclass
class LexicalIndex:
    """BM25 postings. Derived state; rebuilt by replaying the log."""

    postings: dict[str, dict[FindingId, int]] = field(
        default_factory=lambda: defaultdict(dict)
    )
    lengths: dict[FindingId, int] = field(default_factory=dict)

    @property
    def size(self) -> int:
        return len(self.lengths)

    @property
    def average_length(self) -> float:
        if not self.lengths:
            return 0.0
        return sum(self.lengths.values()) / len(self.lengths)

    def add(self, finding_id: FindingId, text: str) -> None:
        tokens = tokenize(text)
        self.lengths[finding_id] = len(tokens)
        for token, count in Counter(tokens).items():
            self.postings[token][finding_id] = count

    def search(
        self, text: str, *, exclude: frozenset[FindingId] = frozenset()
    ) -> list[tuple[FindingId, float]]:
        """Findings sharing vocabulary with `text`, best first."""
        query = tokenize(text)
        if not query or not self.lengths:
            return []

        n = len(self.lengths)
        avgdl = self.average_length or 1.0
        scores: dict[FindingId, float] = defaultdict(float)

        for token in set(query):
            holders = self.postings.get(token)
            if not holders:
                continue
            # Probabilistic IDF, floored at zero. A term in more than half the
            # corpus goes negative in the raw formula, which would let a common
            # word actively push a genuinely relevant finding down the ranking.
            idf = max(0.0, math.log(1.0 + (n - len(holders) + 0.5) / (len(holders) + 0.5)))
            if idf == 0.0:
                continue
            for finding_id, freq in holders.items():
                if finding_id in exclude:
                    continue
                length = self.lengths[finding_id]
                denominator = freq + K1 * (1 - B + B * length / avgdl)
                scores[finding_id] += idf * (freq * (K1 + 1)) / denominator

        return sorted(scores.items(), key=lambda kv: (-kv[1], kv[0]))
