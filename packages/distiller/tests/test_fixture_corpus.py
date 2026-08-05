"""Shape tests for every committed fixture. Prose quality is co-review's job."""
import pytest

from synapse_contracts import Finding, Segment
from synapse_distiller.fixtures import available_fixtures, load_goldens, load_segment

EXPECTED_IDS = ["seg-001", "seg-002", "seg-003", "seg-004",
                "seg-005a", "seg-005b", "seg-006", "seg-007"]


def test_corpus_is_complete():
    assert available_fixtures() == EXPECTED_IDS


@pytest.mark.parametrize("fixture_id", EXPECTED_IDS)
def test_fixture_parses(fixture_id):
    segment = load_segment(fixture_id)
    assert isinstance(segment, Segment)
    assert segment.events, f"{fixture_id} has no events"
    goldens = load_goldens(fixture_id)
    for f in goldens:
        assert isinstance(f, Finding)
        assert f.attributions, f"{fixture_id} golden {f.id} has no attributions"
        for a in f.attributions:
            assert a.agent_session.startswith("as-fixture-")


def test_seg002_is_conversational():
    segment = load_segment("seg-002")
    assert all(e.kind == "text" for e in segment.events), \
        "seg-002 must contain no tool events — that is its entire point"
    types = {f.type.value for f in load_goldens("seg-002")}
    assert "decision" in types
