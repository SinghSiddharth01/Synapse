"""Shape tests for every committed fixture. Prose quality is co-review's job."""
import json
import re

import pytest

from synapse_contracts import Finding, Segment
from synapse_distiller.capability import NPU_QWEN3_4B_INSTRUCT_2507
from synapse_distiller.fixtures import available_fixtures, fixtures_root, load_goldens, load_segment
from synapse_distiller.prompt import render_segment
from synapse_distiller.promptpack import load_pack_by_name

EXPECTED_IDS = ["seg-001", "seg-002", "seg-003", "seg-004",
                "seg-005a", "seg-005b", "seg-006", "seg-007"]

# The model, prompt pack, and render settings config/synapse.toml declares
# TODAY. Constructed explicitly here rather than via load_config(), which
# deliberately layers SYNAPSE_* environment variables over the committed file
# so a bake-off can sweep models/packs without editing a tracked file — the
# wrong tool for a test whose entire point is pinning behaviour under the
# *committed* config. Calling load_config() even guarded (clearing env vars)
# still routes through that env-merge machinery; constructing the values
# directly sidesteps it rather than merely defending against it. If
# config/synapse.toml's model, prompt_pack, or distil_kinds/render_style
# changes, these constants must be updated deliberately in the same commit —
# that forced review is the point, the same reason KNOWN_CONTAMINATED and
# DEFAULT_ALLOWLIST elsewhere in this corpus are frozen literals rather than
# something read live off a mutable file.
_SHIPPED_DISTIL_KINDS: tuple[str, ...] = ("text",)
_SHIPPED_RENDER_STYLE = "labelled"
_SHIPPED_PROMPT_PACK = "v4-condense"
_SHIPPED_MAX_SECONDS_PER_CALL = 30.0


def _shipped_segment_budget() -> int:
    """The token budget config/synapse.toml's shipped model + pack derive,
    computed the same way SynapseConfig.segment_budget does but without
    building a SynapseConfig (which would need load_config() or a hand-built
    stand-in for every unrelated field load_config() also resolves)."""
    pack = load_pack_by_name(_SHIPPED_PROMPT_PACK)
    return NPU_QWEN3_4B_INSTRUCT_2507.segment_budget(
        pack.overhead_tokens, max_seconds_per_call=_SHIPPED_MAX_SECONDS_PER_CALL
    )


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


def test_seg003_buried_error_reaches_the_model_only_via_prose_under_shipped_config():
    """The test above pins that seg-003's tool_result *file* is oversized with
    a buried error, but asserted nothing about system behaviour on it. Under
    the shipped config (config/synapse.toml: distil_kinds = ("text",)),
    tool_result events never reach the model at all under the SHIPPED
    distil_kinds — so the buried ConnectionResetError line, here, is simply
    absent from what render_segment produces, and the dead_end golden is only
    reachable because the assistant's own prose (event 5) restates the
    failure in words. That means seg-003 exercises the same render_segment
    code path as seg-002 today, unaffected by A.5 landing (E7 Chain B2,
    packages/worker/src/synapse_worker/compaction.py) — compaction runs on
    the Segment downstream of triage's keep decision, upstream of
    distillation (adjudicated fixer ruling: triage judges durability against
    the full uncompacted Segment; compaction only shapes what a KEPT Segment
    then shows the model), but distil_kinds = ("text",) still filters
    tool_result out at RENDER time regardless of whether its content was
    compacted first. distil_kinds stays ("text",) deliberately
    (widening it is a measured decision for the NPU eval, not a side effect
    of compaction landing) — see compaction.py's own module docstring and
    test_compaction.py's test_seg003_oversized_tool_result_compacts_within_
    budget_and_the_buried_error_survives for what IS now pinned: that
    compact() itself keeps the buried error line and respects budget. This
    test pins the current, honest state of render_segment under shipped
    config so that claim cannot silently go stale in either direction.

    Uses the module-level _SHIPPED_* constants rather than load_config():
    load_config() deliberately layers SYNAPSE_* environment variables over
    the committed TOML (config.py's whole point — a bake-off sweeps
    models/packs without editing a tracked file), so calling it here — even
    guarded by clearing env vars — would still be routing a "pin the
    committed config" test through env-merge machinery it has no need of.
    Constructing the values explicitly sidesteps that rather than merely
    defending against it, and previously this test failed spuriously for
    anyone with, say, SYNAPSE_DISTIL_KINDS exported to sweep a pack — exactly
    the workflow config/synapse.toml's own header and
    scripts/run_npu_eval.py's docstring prescribe."""
    segment = load_segment("seg-003")
    rendered = render_segment(segment, _SHIPPED_DISTIL_KINDS, _SHIPPED_RENDER_STYLE)
    assert "ConnectionResetError" not in rendered, (
        "if this now fails, distil_kinds/render config changed such that the "
        "buried tool_result reaches the model — update this test and stop "
        "saying the dead_end survives only via prose restatement"
    )
    assert "connection reset during the response flush" in rendered, (
        "the dead_end must still be recoverable via the assistant's own "
        "prose even though the raw tool_result is excluded by default"
    )
    # Even widening to every kind and rendering it UNCOMPACTED, this fixture
    # does not reach the derived budget -- compaction (A.5) exists now (see
    # above), but this assertion is deliberately about render_segment alone,
    # with no compact() call in front of it, so it stays a true statement
    # about the raw fixture regardless of what compaction later does to it.
    full = render_segment(segment, kinds=None, style=_SHIPPED_RENDER_STYLE)
    estimated_tokens = len(full) / 3.5
    assert estimated_tokens < _shipped_segment_budget(), (
        "seg-003 now exceeds the derived budget even including every event "
        "kind, uncompacted; if that's intentional, widening distil_kinds now "
        "needs compact() in front of render_segment to stay under budget — "
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
    not `keep`, and keeps its empty golden). seg-007's golden restates only
    the assistant's own visible prose. seg-006's golden is scoped the same
    way — see test_seg006_typo_reaches_the_model_only_via_prose_under_
    shipped_config below: its tool_result carries a real NameError, but that
    event is excluded under the shipped distil_kinds, so the golden must not
    assert facts (like a specific failure count) that only the tool_result
    states."""
    for fixture_id in ("seg-006", "seg-007"):
        goldens = load_goldens(fixture_id)
        assert goldens, (
            f"{fixture_id} is marked 'keep' in fixtures/triage.json, so under "
            f"ADR 0003 the distiller must faithfully compress its content — "
            f"an empty golden would encode a durability judgment the "
            f"distiller was explicitly relieved of"
        )


def test_seg006_typo_reaches_the_model_only_via_prose_under_shipped_config():
    """f-006-01 originally asserted 'an undefined-variable typo' and 'caused
    one test failure' — both derived only from the tool_result ("NameError:
    name 'confg' is not defined" / "1 failed, 41 passed"), which
    distil_kinds = ("text",) excludes from what the model actually sees. A
    perfectly faithful distiller could not have produced that golden; it
    would read as a recall miss for a correct model. Mirrors
    test_seg003_buried_error_reaches_the_model_only_via_prose_under_shipped_
    config: same shipped-config-under-test discipline, same explicit-
    construction reason (see the _SHIPPED_* constants above load_config()
    would still route this through env-merge machinery a "pin the committed
    config" test has no need of)."""
    segment = load_segment("seg-006")
    rendered = render_segment(segment, _SHIPPED_DISTIL_KINDS, _SHIPPED_RENDER_STYLE)
    assert "NameError" not in rendered, (
        "if this now fails, distil_kinds widened to include tool_result — "
        "f-006-01 can then be enriched with the specific failure count, but "
        "update this test and the golden together rather than leaving one stale"
    )
    assert "config" in rendered and "42 tests pass" in rendered, (
        "the typo-and-recovery story must still be recoverable via the "
        "assistant's own prose even though the raw tool_result is excluded"
    )
    for golden in load_goldens("seg-006"):
        assert "NameError" not in golden.text and "undefined-variable" not in golden.text, (
            f"{golden.id} asserts a fact ('NameError'/'undefined-variable') that "
            f"lives only in the excluded tool_result — trim it to what the "
            f"visible prose actually supports"
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
