"""The symbol lane: exact matches on numbers, identifiers, paths and keys.

Two findings that both mention `40 ms`, or both mention `default_pool_size`, or
both name `gateway.yaml`, are very likely about the same thing. This lane is a
dict lookup over an inverted index — it never scans the log, never calls a
model, and it is the lane that catches the flagship case.

Its failure mode is a missed pattern: one retrieval path lost, quietly, with no
other damage. That is about as benign as failure gets, which is the argument for
leaning on this lane as hard as possible before reaching for anything cleverer.

WHERE EXTRACTION HAPPENS IS A PRIVACY BOUNDARY
----------------------------------------------
Symbols are extracted from the **abstracted finding text**, after the distiller
has done its work — never from the raw transcript. The distiller's whole job is
that raw work stays on the device and only sanitised findings cross the
boundary. A symbol index built from transcripts would carry the redacted content
straight across in tag form, while every other check reports clean:
`default_pool_size=25` is already on record as a leak the 8-word
`verbatim_overlap` metric scored 0.00 on, because a single identifier is shorter
than the n-gram that was looking for it.

Same regex, one stage earlier, and this module becomes a channel around the
redaction. Callers pass finding text. There is no entry point that takes a
Segment, deliberately.
"""

from __future__ import annotations

import re
from collections import defaultdict
from dataclasses import dataclass, field

from synapse_contracts import FindingId

# Order matters only for readability; every pattern is applied to every text.
#
# These are tuned for recall, like everything else in candidate selection: a
# spurious symbol costs one bad candidate that the model discards, a missed one
# costs a merge nobody ever learns about.
_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    # 40 ms · 4096 tokens · 25% · 2.5x — the unit is part of the symbol, so
    # "40 ms" and "40 requests" do not collide on the bare number.
    (
        "measure",
        re.compile(
            r"\b(\d+(?:\.\d+)?)\s*"
            r"(ms|msec|s|sec|secs|m|min|h|hr|kb|mb|gb|tb|tok|toks|tokens|%|x)\b",
            re.IGNORECASE,
        ),
    ),
    # 1.9.4 · 3.12 — version-shaped literals, which are high-signal in this
    # corpus (pinned dependencies show up constantly in findings).
    ("version", re.compile(r"\b(\d+\.\d+(?:\.\d+)+)\b")),
    # gateway.yaml · src/synapse_service/log.py · ~/.claude/projects
    (
        "path",
        re.compile(
            r"\b([\w./\\~-]*[/\\][\w./\\~-]+"
            r"|[\w-]+\.(?:py|toml|json|jsonl|ya?ml|md|txt|cfg|ini|lock|sh|ps1))\b"
        ),
    ),
    # default_pool_size · max_tokens · agent_session_id
    ("identifier", re.compile(r"\b([a-z][a-z0-9]*(?:_[a-z0-9]+)+)\b")),
    # usableContext · promptTokens
    ("identifier", re.compile(r"\b([a-z][a-z0-9]*(?:[A-Z][a-z0-9]+)+)\b")),
    # ECONNRESET · HTTP_503 · QAIRT — screaming case is nearly always a code
    ("code", re.compile(r"\b([A-Z][A-Z0-9]{3,}(?:_[A-Z0-9]+)*)\b")),
)

# Bare integers are excluded on purpose. "3" appears in half the findings in any
# corpus and would make the lane return everything, which is indistinguishable
# from the lane being broken. A number earns a symbol by carrying a unit.

_UNIT_ALIASES = {
    "msec": "ms",
    "sec": "s",
    "secs": "s",
    "min": "m",
    "hr": "h",
    "toks": "tok",
    "tokens": "tok",
}

# The `code` pattern matches any 4+ character screaming-case word, which
# swallows units when someone writes "40 MSEC" — yielding both `40ms` and a
# bogus `msec` symbol. A unit is never an error code, so it is excluded there.
_UNIT_WORDS = frozenset(_UNIT_ALIASES) | frozenset(
    "ms s m h kb mb gb tb tok x".split()
)


def extract(text: str) -> frozenset[str]:
    """Pull normalised symbols out of one finding's abstracted text.

    Normalisation exists so that `40 ms`, `40ms` and `40 MS` are one symbol
    rather than three; without it the lane's recall depends on how each
    contributor happened to type it.
    """
    found: set[str] = set()

    for name, pattern in _PATTERNS:
        for match in pattern.finditer(text):
            if name == "measure":
                value, unit = match.group(1), match.group(2).lower()
                unit = _UNIT_ALIASES.get(unit, unit)
                found.add(f"{value}{unit}")
                continue

            symbol = _normalise(match.group(1))
            if name == "code" and symbol in _UNIT_WORDS:
                continue
            found.add(symbol)

    return frozenset(s for s in found if s)


def _normalise(symbol: str) -> str:
    return symbol.strip().lower().replace("\\", "/")


@dataclass
class SymbolIndex:
    """`symbol -> finding ids`. Derived state; rebuilt by replaying the log."""

    postings: dict[str, set[FindingId]] = field(
        default_factory=lambda: defaultdict(set)
    )
    symbols_of: dict[FindingId, frozenset[str]] = field(default_factory=dict)

    def add(self, finding_id: FindingId, text: str) -> frozenset[str]:
        symbols = extract(text)
        self.symbols_of[finding_id] = symbols
        for symbol in symbols:
            self.postings[symbol].add(finding_id)
        return symbols

    def add_merged(
        self, finding_id: FindingId, text: str, sources: tuple[FindingId, ...]
    ) -> frozenset[str]:
        """A merged finding takes the union of its sources' symbols plus its own.

        Union rather than re-extraction alone: the merged text is a summary and
        may drop a symbol one source carried. Losing it would lose a retrieval
        path into a finding that now represents two people's work — the exact
        opposite of what merging is for.
        """
        symbols = set(extract(text))
        for source in sources:
            symbols |= self.symbols_of.get(source, frozenset())

        frozen = frozenset(symbols)
        self.symbols_of[finding_id] = frozen
        for symbol in frozen:
            self.postings[symbol].add(finding_id)
        return frozen

    def search(
        self, text: str, *, exclude: frozenset[FindingId] = frozenset()
    ) -> list[tuple[FindingId, float]]:
        """Findings sharing a symbol with `text`, best first.

        Scored by how many symbols are shared and how rare they are — a shared
        `default_pool_size` says far more than a shared `10ms` in a corpus about
        latency. This is a dict lookup per symbol; the log is never scanned.
        """
        query = extract(text)
        if not query:
            return []

        scores: dict[FindingId, float] = defaultdict(float)
        for symbol in query:
            holders = self.postings.get(symbol)
            if not holders:
                continue
            # Rarity weight. A symbol in one other finding is a strong signal;
            # one in fifty is background.
            weight = 1.0 / len(holders)
            for finding_id in holders:
                if finding_id in exclude:
                    continue
                scores[finding_id] += weight

        return sorted(scores.items(), key=lambda kv: (-kv[1], kv[0]))
