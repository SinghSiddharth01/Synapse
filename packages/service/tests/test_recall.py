"""Recall harness tests.

These check that the harness measures what it claims to, not that recall is
good. Recall itself is a *number to look at*, not a gate — pinning a threshold
against a synthetic corpus written by the same author as the lanes would turn a
measurement into a self-fulfilling one.

The one exception is the symbol band, which is asserted, because it is
deterministic string matching rather than anything learned: if two findings
that both say `40 ms` stop retrieving each other, something is broken rather
than merely weak.
"""

from __future__ import annotations

from synapse_service.corpus import Band, build
from synapse_service.lanes import Lane
from synapse_service.recall import measure


def test_harness_probes_every_finding_that_has_a_partner() -> None:
    entries = build()
    pairs = sum(1 for entry in entries if entry.partner_id)

    report = measure(entries)

    assert len(report.probes) == pairs


def test_distractors_are_probed_against_but_never_probed_for() -> None:
    report = measure(build(distractors=50))

    assert report.corpus_size > len(report.probes)
    assert all(probe.band is not None for probe in report.probes)


def test_symbol_band_retrieves_even_against_a_large_corpus() -> None:
    """Deterministic string matching. If this drops, something is broken."""
    report = measure(build(distractors=400))

    assert report.by_band()[Band.SYMBOL] == 1.0


def test_the_flagship_pair_is_found_by_the_symbol_lane() -> None:
    report = measure(build(distractors=400))

    flagship = next(probe for probe in report.probes if probe.finding_id == "pair-00-a")
    assert flagship.found
    assert Lane.SYMBOLS in flagship.lanes


def test_recall_is_reported_per_band_and_per_lane() -> None:
    report = measure(build(distractors=100))

    assert set(report.by_band()) == set(Band)
    assert set(report.by_lane()) == set(Lane)


def test_report_formats_without_error() -> None:
    text = measure(build(distractors=20)).format()

    assert "candidate recall" in text
    assert "by band" in text
    assert "by lane" in text


def test_a_larger_corpus_does_not_change_the_prompt_size() -> None:
    """The point of the whole design: what the model sees is flat in N."""
    small = measure(build(distractors=20), top_k=14)
    large = measure(build(distractors=600), top_k=14)

    assert small.top_k == large.top_k
    assert large.corpus_size > small.corpus_size * 5
