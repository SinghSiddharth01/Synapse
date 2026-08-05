"""Shape tests for every committed fixture. Prose quality is co-review's job."""
import json
import os
import re

import pytest

from synapse_contracts import Finding, Segment
from synapse_distiller.config import load_config
from synapse_distiller.fixtures import available_fixtures, fixtures_root, load_goldens, load_segment
from synapse_distiller.prompt import render_segment

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


def test_seg003_buried_error_reaches_the_model_only_via_prose_under_shipped_config(monkeypatch):
    """The test above pins that seg-003's tool_result *file* is oversized with
    a buried error, but asserted nothing about system behaviour on it. Under
    the shipped config (config/synapse.toml: distil_kinds = ("text",)),
    tool_result events never reach the model at all — so the buried
    ConnectionResetError line is not truncated or split by a budget, it is
    simply absent, and the dead_end golden is only reachable because the
    assistant's own prose (event 5) restates the failure in words. That means
    seg-003 exercises the same code path as seg-002 today; no compaction or
    budget-splitting behaviour is pinned by this fixture until distil_kinds
    is widened and/or A.5 (compaction) lands. This test pins the current,
    honest state so that claim cannot silently go stale in either direction.

    load_config() layers SYNAPSE_* environment variables over the committed
    TOML (config.py's whole point — a bake-off sweeps models/packs without
    editing a tracked file), so this test must clear them first. Otherwise
    it fails spuriously for anyone with, say, SYNAPSE_DISTIL_KINDS exported
    to sweep a pack — exactly the workflow config/synapse.toml's own header
    and scripts/run_npu_eval.py's docstring prescribe — and the failure talks
    about fixture semantics rather than about the runner's shell."""
    for key in list(os.environ):
        if key.startswith("SYNAPSE_"):
            monkeypatch.delenv(key, raising=False)
    segment = load_segment("seg-003")
    config = load_config()
    rendered = render_segment(segment, config.distil_kinds, config.render_style)
    assert "ConnectionResetError" not in rendered, (
        "if this now fails, distil_kinds/render config changed such that the "
        "buried tool_result reaches the model — update this test and stop "
        "saying the dead_end survives only via prose restatement"
    )
    assert "connection reset during the response flush" in rendered, (
        "the dead_end must still be recoverable via the assistant's own "
        "prose even though the raw tool_result is excluded by default"
    )
    # Even widening to every kind, this fixture does not reach the derived
    # budget, so no truncation/compaction path exists for it to pin yet.
    full = render_segment(segment, kinds=None, style=config.render_style)
    estimated_tokens = len(full) / 3.5
    assert estimated_tokens < config.segment_budget, (
        "seg-003 now exceeds the derived budget even including every event "
        "kind; if that's intentional, wire compaction (A.5) rather than "
        "relying on distil_kinds filtering to avoid truncation, and flip "
        "this assertion to pin the new boundary deliberately"
    )


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


_TRACEBACK_ERROR_RE = re.compile(r"\b[A-Za-z]+Error\b\s*:|Traceback \(most recent call last\)")
_RESOLVED_SUMMARY_LINE_RE = re.compile(r"\bfound \d+ errors?\s*\(\d+ fixed,\s*0 remaining\)", re.I)


def test_seg004_and_seg006_are_distinguishable_by_traceback_vs_summary_line():
    """seg-004 (triage.json: skip) and seg-006 (triage.json: keep) are both
    'an error signal that resolved fully within the segment' — a naive
    resolved-vs-unresolved triage rule would skip both, breaking seg-006's
    keep. The two ARE distinguishable by a generalizable, non-fixture-specific
    feature: seg-004's tool_result is a linter's own aggregate summary line
    with nothing outstanding; seg-006's is an actual exception traceback. A
    triage rule keyed on "traceback present" (unconditional keep, recall-
    tuned) overriding "resolved-count summary line" (skip) satisfies both
    rows of fixtures/triage.json. This test pins that the two fixtures still
    carry that distinguishing shape, so the expectation map stays
    satisfiable for E2's triage rule — see fixtures/triage.json's notes."""
    seg004_tool_text = " ".join(
        e.content for e in load_segment("seg-004").events if e.kind == "tool_result"
    )
    seg006_tool_text = " ".join(
        e.content for e in load_segment("seg-006").events if e.kind == "tool_result"
    )
    assert _RESOLVED_SUMMARY_LINE_RE.search(seg004_tool_text), (
        "seg-004 must contain a resolved lint-summary line — that is its skip signal"
    )
    assert not _TRACEBACK_ERROR_RE.search(seg004_tool_text), (
        "seg-004 must contain no exception traceback, or a traceback-keyed "
        "keep rule would collide with its skip expectation"
    )
    assert _TRACEBACK_ERROR_RE.search(seg006_tool_text), (
        "seg-006 must contain an exception traceback — the signal that "
        "overrides recall-tuned skip and keeps it distinct from seg-004"
    )


def test_triage_expectation_map_covers_the_whole_corpus():
    raw = json.loads((fixtures_root() / "triage.json").read_text(encoding="utf-8"))
    assert set(raw) == set(EXPECTED_IDS)
    assert all(v["expected"] in ("keep", "skip") for v in raw.values())
    # The two load-bearing entries: all-noise is skipped, quiet insight is kept.
    assert raw["seg-004"]["expected"] == "skip"
    assert raw["seg-002"]["expected"] == "keep"
