"""Compaction (Plan A.5) — deterministic, before triage. See compaction.py's
module docstring for the four behaviors and why each exists."""

from __future__ import annotations

from datetime import datetime, timezone

from synapse_contracts import AgentEvent, Segment
from synapse_distiller.capability import NPU_QWEN3_4B_INSTRUCT_2507
from synapse_distiller.fixtures import load_segment
from synapse_distiller.promptpack import load_pack_by_name

from synapse_worker.compaction import HEAD_TAIL_LINES, THINKING_LINES, compact

TS = datetime(2026, 8, 4, tzinfo=timezone.utc)


def _seg(events: list[AgentEvent]) -> Segment:
    return Segment(id="t-001", agent_session_id="as-t", events=events,
                   started_at=TS, ended_at=TS)


def _ev(role="assistant", kind="text", content="", tool_name=None) -> AgentEvent:
    return AgentEvent(role=role, kind=kind, content=content, tool_name=tool_name,
                      ts=TS, agent_session_id="as-t")


def test_compact_is_a_pure_function():
    original = _seg([_ev(kind="thinking", content="a\nb\nc\nd")])
    before = original.model_copy(deep=True)

    compact(original)

    assert original == before


def test_trivial_readonly_tool_call_is_dropped():
    segment = _seg([
        _ev(role="user", kind="text", content="what does config.py say"),
        _ev(kind="tool_use", tool_name="Read", content="config.py"),
        _ev(role="user", kind="tool_result", tool_name="Read", content="DEBUG = True"),
        _ev(content="Debug mode is on."),
    ])

    compacted = compact(segment)

    kinds = [e.kind for e in compacted.events]
    assert "tool_use" not in kinds and "tool_result" not in kinds
    assert len(compacted.events) == 2


def test_a_non_readonly_tool_call_survives_even_with_a_tiny_result():
    """Bash isn't in READONLY_TOOLS -- a tiny result doesn't make an
    arbitrary command trivial the way a Read/Grep/Glob/LS/WebSearch/WebFetch
    peek does."""
    segment = _seg([
        _ev(kind="tool_use", tool_name="Bash", content="rm -rf build/"),
        _ev(role="user", kind="tool_result", tool_name="Bash", content="ok"),
    ])

    compacted = compact(segment)

    assert [e.kind for e in compacted.events] == ["tool_use", "tool_result"]


def test_a_readonly_call_with_an_error_result_survives():
    """A tiny result is only "trivial" if it's also clean -- an error-shaped
    result from a read-only tool must not be thrown away."""
    segment = _seg([
        _ev(kind="tool_use", tool_name="Read", content="missing.py"),
        _ev(role="user", kind="tool_result", tool_name="Read",
            content="Error: file not found"),
    ])

    compacted = compact(segment)

    assert [e.kind for e in compacted.events] == ["tool_use", "tool_result"]


def test_a_readonly_call_with_a_large_result_survives():
    """"Tiny" is load-bearing -- a big Read/Grep result is exactly the
    signal-carrying content compaction exists to keep (truncated, not
    dropped)."""
    segment = _seg([
        _ev(kind="tool_use", tool_name="Read", content="big.py"),
        _ev(role="user", kind="tool_result", tool_name="Read", content="x" * 5000),
    ])

    compacted = compact(segment)

    assert [e.kind for e in compacted.events] == ["tool_use", "tool_result"]


def test_thinking_is_trimmed_to_first_n_lines_but_the_kind_survives():
    long_thinking = "\n".join(f"line {i}" for i in range(20))
    segment = _seg([_ev(kind="thinking", content=long_thinking)])

    compacted = compact(segment)

    [event] = compacted.events
    assert event.kind == "thinking"  # triage keys on the kind being PRESENT
    kept_lines = event.content.splitlines()[:THINKING_LINES]
    assert kept_lines == long_thinking.splitlines()[:THINKING_LINES]
    assert len(event.content) < len(long_thinking)


def test_short_thinking_is_left_alone():
    segment = _seg([_ev(kind="thinking", content="line 0\nline 1")])

    compacted = compact(segment)

    assert compacted.events[0].content == "line 0\nline 1"


def test_binary_looking_content_is_stripped():
    blob = "QUJDREVGR0hJSktMTU5PUFFSU1RVVldYWVphYmNkZWZnaGlqa2xtbm9wcXJzdHV2d3h5ejAxMjM0" * 3
    segment = _seg([_ev(role="user", kind="tool_result", tool_name="Bash",
                        content=f"uploading...\n{blob}\ndone")])

    compacted = compact(segment)

    content = compacted.events[0].content
    assert blob not in content
    assert "uploading" in content and "done" in content
    assert "stripped" in content.lower()


def test_a_buried_error_line_survives_head_tail_truncation():
    lines = [f"benign line {i}" for i in range(100)]
    lines[50] = "ConnectionResetError: [Errno 104] Connection reset by peer"
    content = "\n".join(lines)
    segment = _seg([_ev(role="user", kind="tool_result", tool_name="Bash", content=content)])

    compacted = compact(segment)

    result_content = compacted.events[0].content
    assert "ConnectionResetError" in result_content
    assert len(result_content) < len(content)  # truncation actually happened
    assert len(result_content.splitlines()) < len(lines)


def test_short_tool_result_is_left_alone():
    short = "\n".join(f"line {i}" for i in range(2 * HEAD_TAIL_LINES))
    segment = _seg([_ev(role="user", kind="tool_result", tool_name="Bash", content=short)])

    compacted = compact(segment)

    assert compacted.events[0].content == short


def test_compaction_is_idempotent():
    lines = [f"benign line {i}" for i in range(100)]
    lines[50] = "ConnectionResetError: reset by peer"
    segment = _seg([
        _ev(kind="tool_use", tool_name="Read", content="x"),
        _ev(role="user", kind="tool_result", tool_name="Read", content="ok"),
        _ev(kind="thinking", content="\n".join(f"t{i}" for i in range(10))),
        _ev(role="user", kind="tool_result", tool_name="Bash", content="\n".join(lines)),
    ])

    once = compact(segment)
    twice = compact(once)

    assert twice == once


def test_seg003_oversized_tool_result_compacts_within_budget_and_the_buried_error_survives():
    """Plan A.5's first failing test, and the plan-0 fixture's original
    assertion (test_fixture_corpus.py's
    test_seg003_buried_error_reaches_the_model_only_via_prose_under_shipped_
    config documented that this could not be pinned until A.5 landed) —
    finally exercised for real. The tool_result is 118 lines with
    "ConnectionResetError" buried at ~52% -- well outside a naive first/last
    15-line window -- so this also pins that compaction's error-preservation
    is not merely "keep the head and tail"."""
    segment = load_segment("seg-003")

    compacted = compact(segment)

    tool_results = [e for e in compacted.events if e.kind == "tool_result"]
    assert any("ConnectionResetError" in e.content for e in tool_results)

    original_chars = sum(len(e.content) for e in segment.events)
    compacted_chars = sum(len(e.content) for e in compacted.events)
    assert compacted_chars < original_chars

    pack = load_pack_by_name("v4-condense")
    budget = NPU_QWEN3_4B_INSTRUCT_2507.segment_budget(
        pack.overhead_tokens, max_seconds_per_call=30.0
    )
    estimated_tokens = compacted_chars / 3.5
    assert estimated_tokens < budget
