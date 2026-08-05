"""Fixture loader tests — Plan 0 Task 0.3.

'Done when: every fixture parses into Segment, every golden parses into
Finding[], and a loader test proves it.'
"""

from __future__ import annotations

from synapse_contracts import Finding, FindingType, Segment

from synapse_distiller.evaluation import identifier_leaks
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


def test_no_golden_in_the_corpus_leaks_an_identifier_from_its_segment() -> None:
    """Generalizes test_seg_001_goldens_abstract_rather_than_quote across the
    whole corpus using the Task 5 detector (identifier_leaks), rather than
    seg-001's hand-picked substring list.

    This is load-bearing for a reason beyond ordinary fixture hygiene: the
    goldens ARE the privacy bar a run is measured against (scripts/
    run_npu_eval.py voids the demo the moment any score.leaked_identifiers is
    non-empty). A distiller that reproduces a golden exactly must not itself
    be scored as a privacy failure — if a golden leaks, every fixture using
    it is unusable for the privacy claim regardless of what the model does.
    """
    for fixture_id in available_fixtures():
        segment = load_segment(fixture_id)
        for golden in load_goldens(fixture_id):
            leaks = identifier_leaks(golden.text, segment)
            assert not leaks, (
                f"{fixture_id} golden {golden.id!r} leaks identifiers copied "
                f"verbatim from its own segment: {leaks} — reword the golden "
                f"to abstract rather than quote (see the seg-001 test above), "
                f"or add a reviewed DEFAULT_ALLOWLIST entry if the token is "
                f"genuinely public vocabulary"
            )
