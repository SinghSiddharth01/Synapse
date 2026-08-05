"""Deterministic eval axes — the part of Plan B Task B.7 that needs no judge.

Two metrics, both computable without an LLM, both first-class:

  verbatim-copy rate  n-gram overlap between a finding's text and the Segment it
                      came from. This is the PRIVACY metric. The architecture's
                      whole claim is that raw work stays on the device and only
                      abstracted findings cross the boundary; a model that
                      quotes defeats that while appearing to work.

  empty-segment discipline  the all-noise fixture must yield an empty array.

Quality-vs-goldens is the LLM-judged axis and is deliberately not here. A
two-fixture corpus is a directional signal, not a statistical claim — say so
in any report built from this.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from synapse_contracts import Finding, Segment

# Eight words is long enough that an overlap is copying rather than shared
# vocabulary ("the connection pool was" is not a quote; eight consecutive
# matching words is).
DEFAULT_NGRAM = 8


def _words(text: str) -> list[str]:
    return re.findall(r"\w+", text.lower())


def _ngrams(words: list[str], n: int) -> set[tuple[str, ...]]:
    if len(words) < n:
        return set()
    return {tuple(words[i : i + n]) for i in range(len(words) - n + 1)}


def verbatim_overlap(finding_text: str, segment: Segment, n: int = DEFAULT_NGRAM) -> float:
    """Fraction of the finding's n-grams that appear verbatim in the Segment.

    0.0 means fully abstracted. 1.0 means the finding is a quote.
    """
    finding_grams = _ngrams(_words(finding_text), n)
    if not finding_grams:
        return 0.0
    source_grams = _ngrams(_words(" ".join(e.content for e in segment.events)), n)
    return len(finding_grams & source_grams) / len(finding_grams)


@dataclass
class FixtureScore:
    fixture_id: str
    findings: list[Finding]
    expected_count: int
    schema_valid: bool
    attempts: int
    latency_ms: int
    input_tokens: int
    output_tokens: int
    max_verbatim_overlap: float
    mean_verbatim_overlap: float
    type_coverage: set[str]
    leaked_identifiers: list[str] = field(default_factory=list)

    @property
    def empty_discipline_ok(self) -> bool | None:
        """Only meaningful for the all-noise fixture, whose golden is empty."""
        if self.expected_count != 0:
            return None
        return len(self.findings) == 0


def score_fixture(
    fixture_id: str,
    segment: Segment,
    findings: list[Finding],
    goldens: list[Finding],
    *,
    attempts: int,
    latency_ms: int,
    input_tokens: int,
    output_tokens: int,
) -> FixtureScore:
    overlaps = [verbatim_overlap(f.text, segment) for f in findings]
    leaks = sorted({t for f in findings for t in identifier_leaks(f.text, segment)})
    return FixtureScore(
        fixture_id=fixture_id,
        findings=findings,
        expected_count=len(goldens),
        schema_valid=bool(findings) or len(goldens) == 0,
        attempts=attempts,
        latency_ms=latency_ms,
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        max_verbatim_overlap=max(overlaps, default=0.0),
        mean_verbatim_overlap=(sum(overlaps) / len(overlaps)) if overlaps else 0.0,
        type_coverage={f.type.value for f in findings},
        leaked_identifiers=leaks,
    )


# ── identifier leaks ─────────────────────────────────────────────────────────
# The n-gram metric above cannot see single-token leaks: it scored 0.00 on a
# finding containing `default_pool_size=25`. An identifier-shaped token that
# appears in BOTH the finding and the segment is copied source vocabulary.
#
# Not every identifier is private — some are the public names the goldens use
# deliberately. Those live in DEFAULT_ALLOWLIST (lowercased). A token only in
# the finding is NOT a leak: the model invented it, which is a fidelity
# problem, not a privacy one.

DEFAULT_ALLOWLIST: frozenset[str] = frozenset({
    # public tools & libraries the corpus names on purpose
    "pgbouncer", "asyncpg", "ruff", "pytest", "redis", "uv", "jsonl", "json",
    # our own public vocabulary (CONTEXT.md terms that look identifier-shaped)
    "claude-code", "codex", "tool_use", "tool_result", "dead_end", "open_question",
})

# The trailing boundary intentionally excludes "." (unlike the leading
# boundary below, which still excludes it). An identifier is very often the
# last token before a full stop — "We raised default_pool_size." — and a
# lookahead that forbids a following "." made every sentence-final identifier
# invisible, which is exactly where a leak is most likely to sit. Path-shaped
# alternatives below still *consume* a trailing "." greedily (it is a valid
# path character), so `_identifier_tokens` strips it back off after matching
# rather than trying to exclude it via the lookahead.
_IDENTIFIER_RE = re.compile(
    r"""(?x)
    (?<![\w./\\-])(
        \\\\[\w.$-]+(?:\\[\w.$-]+)+           # Windows UNC path (\\server\share\...)
      | (?:/|[A-Za-z]:\\)[\w.\\/-]+           # absolute path (unix or windows)
      | [\w-]+(?:/[\w.-]+)+                   # relative path with a slash
      | \d{1,3}(?:\.\d{1,3}){3}               # IPv4 address
      | \w+=[^\s,;]+                          # key=value
      | [A-Za-z_]\w*(?:\.[A-Za-z_]\w*)+       # dotted.name (incl. file.ext, host)
      | [A-Za-z0-9]+(?:_[A-Za-z0-9]+)+        # snake_case (either letter case)
      | [A-Za-z0-9]+(?:-[A-Za-z0-9]+)+        # kebab-case (either letter case)
      | [a-z]+(?:[A-Z][a-z0-9]*)+             # camelCase
      | [A-Z][a-z0-9]+(?:[A-Z][a-z0-9]*)+     # CamelCase
    )(?![\w/\\-])
    """
)


def _identifier_tokens(text: str) -> set[str]:
    tokens: set[str] = set()
    for m in _IDENTIFIER_RE.finditer(text):
        tok = m.group(1).rstrip(".")
        if not tok:
            continue
        tokens.add(tok)
        if "=" in tok:
            # The key=value alternative fuses the whole assignment into one
            # token, so a paraphrase that keeps only the key (or only the
            # value) never matches it. Emit the bare key too, and re-run
            # extraction over the value in case it is itself identifier-shaped
            # (e.g. a path or dotted name assigned to a key) — the fused form
            # stays in the set as well, so a byte-identical copy is still
            # caught.
            key, _, value = tok.partition("=")
            if key:
                tokens.add(key)
            if value:
                tokens |= _identifier_tokens(value)
    return tokens


def identifier_leaks(
    finding_text: str,
    segment: Segment,
    allowlist: frozenset[str] = DEFAULT_ALLOWLIST,
) -> list[str]:
    """Identifier-shaped tokens copied from the segment into the finding.

    Comparison is case-insensitive: "TokenValidator" in the segment and
    "tokenvalidator" in the finding is still the same copied token wearing a
    trivially different case, and matching case-sensitively would let that
    evade detection for free. The token reported is spelled as it appears in
    `finding_text`.

    Sorted for stable output. Empty list == no detected leak — which is
    evidence, not proof; the allowlist is a judgment call and reviewed with
    the corpus. Known gaps, because the heuristic keys on visible structure
    (separators, case-humps) rather than meaning or entropy: bare secrets/API
    keys and IDs with no internal structure, single-word codenames or proper
    nouns, partial copies of a longer dotted/path token, and any identifier
    whose case-shape is flattened to one unstructured run (camelCase folded
    to all-lowercase has no separator left to key on). A clean run is "no
    leak this heuristic knows how to detect," never a privacy guarantee.
    """
    source = " ".join(e.content for e in segment.events)
    finding_tokens = _identifier_tokens(finding_text)
    source_tokens_lower = {t.lower() for t in _identifier_tokens(source)}
    leaked = {t for t in finding_tokens if t.lower() in source_tokens_lower}
    return sorted(t for t in leaked if t.lower() not in allowlist)
