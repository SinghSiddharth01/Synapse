"""Eval-metric tests — Plan B Task B.7, deterministic axes only."""

from __future__ import annotations

from synapse_distiller.evaluation import verbatim_overlap
from synapse_distiller.fixtures import load_segment


def test_a_direct_quote_scores_high() -> None:
    segment = load_segment("seg-001")
    quoted = segment.events[6].content  # the thinking block, quoted wholesale

    assert verbatim_overlap(quoted, segment) == 1.0


def test_an_abstracted_finding_scores_zero() -> None:
    segment = load_segment("seg-001")
    abstracted = (
        "A pooler that rotates backend connections per transaction cannot support "
        "drivers which implicitly prepare statements."
    )

    assert verbatim_overlap(abstracted, segment) == 0.0


def test_short_text_cannot_be_judged_and_scores_zero() -> None:
    """Below the n-gram width there is nothing to compare; do not report a
    false positive for a three-word finding."""
    segment = load_segment("seg-001")

    assert verbatim_overlap("session pooling", segment) == 0.0


def test_partial_quote_scores_between_zero_and_one() -> None:
    segment = load_segment("seg-001")
    partial = (
        "pgbouncer with pool_mode=transaction does not support prepared statements "
        "which is a constraint worth remembering when choosing a pooling strategy "
        "for any service that uses an asynchronous driver in production"
    )

    score = verbatim_overlap(partial, segment)
    assert 0.0 < score < 1.0
