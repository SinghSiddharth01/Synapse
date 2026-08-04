"""Fixture loader tests — Plan 0 Task 0.3.

'Done when: every fixture parses into Segment, every golden parses into
Finding[], and a loader test proves it.'
"""

from __future__ import annotations

from synapse_contracts import Finding, FindingType, Segment

from synapse_distiller.fixtures import available_fixtures, load_goldens, load_segment


def test_every_fixture_parses_as_a_segment_and_golden_findings() -> None:
    fixtures = available_fixtures()
    assert fixtures, "no fixtures found"

    for fixture_id in fixtures:
        segment = load_segment(fixture_id)
        assert isinstance(segment, Segment)
        assert segment.id == fixture_id
        assert all(isinstance(f, Finding) for f in load_goldens(fixture_id))


def test_seg_001_golden_covers_all_four_finding_types() -> None:
    """The ordinary-turn fixture is what proves each type is reachable."""
    goldens = load_goldens("seg-001")

    assert {g.type for g in goldens} == set(FindingType)


def test_seg_004_golden_is_empty() -> None:
    """Load-bearing. This is the only guard against invented findings."""
    assert load_goldens("seg-004") == []


def test_seg_001_goldens_abstract_rather_than_quote() -> None:
    """Goldens set the privacy bar the model is measured against, so they must
    not themselves contain identifiers copied out of the Segment."""
    segment_text = " ".join(e.content for e in load_segment("seg-001").events)
    quotable = ["__asyncpg_stmt_3__", "pgbouncer.ini", "test_reserve_stock", "default_pool_size"]

    for token in quotable:
        assert token in segment_text, f"{token} should appear in the source segment"
        for golden in load_goldens("seg-001"):
            assert token not in golden.text, f"golden quotes {token!r} from the segment"
