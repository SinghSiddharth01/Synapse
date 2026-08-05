"""Shape tests for every committed fixture. Prose quality is co-review's job."""
import json

import pytest

from synapse_contracts import Finding, Segment
from synapse_distiller.fixtures import available_fixtures, fixtures_root, load_goldens, load_segment

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


def test_seg003_error_is_buried_in_an_oversized_tool_result():
    segment = load_segment("seg-003")
    big = [e for e in segment.events if e.kind == "tool_result" and len(e.content) > 4000]
    assert big, "seg-003 must contain a tool_result over 4000 chars"
    assert "ConnectionResetError" in big[0].content
    pos = big[0].content.index("ConnectionResetError") / len(big[0].content)
    assert 0.3 < pos < 0.7, "the error must be buried mid-log, not at head or tail"
    types = {f.type.value for f in load_goldens("seg-003")}
    assert "dead_end" in types


def test_seg005_pair_is_a_merge_candidate():
    a = load_goldens("seg-005a")
    b = load_goldens("seg-005b")
    assert [f.id for f in a] == ["f-005a-01"]
    assert [f.id for f in b] == ["f-005b-01"]
    assert a[0].attributions[0].contributor == "aditya"
    assert b[0].attributions[0].contributor == "akhil"
    assert a[0].attributions[0].contributor != b[0].attributions[0].contributor
    # Same fact, different halves: both mention the 40ms window, only b has load.
    assert "40" in a[0].text and "40" in b[0].text
    assert "load" in b[0].text.lower() and "load" not in a[0].text.lower()


def test_seg006_seg007_have_faithful_compression_goldens():
    """seg-006 and seg-007 reach the distiller: fixtures/triage.json marks
    both 'keep' (recall-tuned ACCEPTED FALSE POSITIVE entries). ADR 0003 says
    the on-device distiller compresses whatever reaches it and does not judge
    durability — that judgment is triage's job upstream, which has already
    run by the time these segments get here. An empty golden on a kept
    segment encodes exactly the durability judgment ADR 0003 relieves the
    distiller of (this is the same bug the ADR names for seg-004: 'the
    distiller should judge this worthless' — which is why seg-004 is `skip`,
    not `keep`, and keeps its empty golden). Both segments contain real
    content to compress: a genuine NameError and three real call sites."""
    for fixture_id in ("seg-006", "seg-007"):
        goldens = load_goldens(fixture_id)
        assert goldens, (
            f"{fixture_id} is marked 'keep' in fixtures/triage.json, so under "
            f"ADR 0003 the distiller must faithfully compress its content — "
            f"an empty golden would encode a durability judgment the "
            f"distiller was explicitly relieved of"
        )


def test_triage_expectation_map_covers_the_whole_corpus():
    raw = json.loads((fixtures_root() / "triage.json").read_text(encoding="utf-8"))
    assert set(raw) == set(EXPECTED_IDS)
    assert all(v["expected"] in ("keep", "skip") for v in raw.values())
    # The two load-bearing entries: all-noise is skipped, quiet insight is kept.
    assert raw["seg-004"]["expected"] == "skip"
    assert raw["seg-002"]["expected"] == "keep"
