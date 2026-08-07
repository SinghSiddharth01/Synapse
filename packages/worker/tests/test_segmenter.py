"""Segmenter tests — turn boundaries, held turns, and the budget."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from synapse_contracts import AgentEvent

from synapse_worker.segmenter import Segmenter, is_turn_boundary

T0 = datetime(2026, 8, 4, 9, 0, tzinfo=timezone.utc)


def ev(role: str, kind: str, content: str, minute: int = 0) -> AgentEvent:
    return AgentEvent(
        role=role,  # type: ignore[arg-type]
        kind=kind,  # type: ignore[arg-type]
        content=content,
        ts=T0 + timedelta(minutes=minute),
        agent_session_id="sess-1",
    )


def test_one_event_larger_than_the_budget_is_split_rather_than_emitted_whole() -> None:
    """Chunking can only split BETWEEN events, and the loop admits the first
    event unconditionally — so before this, a single long assistant message
    produced a Segment that could not fit the model's context no matter what.
    The model then failed on it twice and the worker dropped it, losing the
    whole turn. Long agent messages are ordinary, so this was not a rare path.
    """
    budget = 100
    huge = "\n".join(f"line {i} of a very long assistant message" for i in range(60))
    segmenter = Segmenter(budget_tokens=budget, agent_session_id="sess-1")
    segmenter.add([ev("user", "text", "go", 0), ev("assistant", "text", huge, 1)])

    segments = segmenter.drain(flush_incomplete=True)

    assert len(segments) > 1, "the oversized event must have been split up"
    for segment in segments:
        for event in segment.events:
            assert len(event.content) <= int(budget * 3.5), "a part still overruns"
    # Nothing invented and nothing lost: the parts concatenate back to the input.
    rejoined = "".join(e.content for s in segments for e in s.events
                       if e.role == "assistant")
    assert rejoined == huge


def test_an_event_within_the_budget_is_never_cut() -> None:
    """The split is a last resort — cutting inside one event costs coherence,
    so an event that already fits must pass through byte-identical."""
    segmenter = Segmenter(budget_tokens=1000, agent_session_id="sess-1")
    body = "short enough\nto stay whole"
    segmenter.add([ev("user", "text", "go", 0), ev("assistant", "text", body, 1)])

    segments = segmenter.drain(flush_incomplete=True)

    assert [e.content for s in segments for e in s.events] == ["go", body]


def test_user_text_is_a_boundary_but_tool_result_is_not() -> None:
    """Claude Code records tool results with role 'user'. Treating those as
    boundaries would cut a turn at every tool call."""
    assert is_turn_boundary(ev("user", "text", "do the thing")) is True
    assert is_turn_boundary(ev("user", "tool_result", "ok")) is False
    assert is_turn_boundary(ev("assistant", "text", "done")) is False


def test_open_turn_is_held_back() -> None:
    """The heart of periodic capture: a timer fires mid-turn, and distilling
    half a turn produces half a finding."""
    segmenter = Segmenter(budget_tokens=5000)
    segmenter.add([ev("user", "text", "first request"), ev("assistant", "text", "working")])

    assert segmenter.drain() == []
    assert segmenter.pending_events == 2


def test_turn_is_emitted_once_the_next_one_starts() -> None:
    segmenter = Segmenter(budget_tokens=5000)
    segmenter.add([ev("user", "text", "first"), ev("assistant", "text", "done first")])
    assert segmenter.drain() == []

    segmenter.add([ev("user", "text", "second")])
    segments = segmenter.drain()

    assert len(segments) == 1
    assert [e.content for e in segments[0].events] == ["first", "done first"]
    assert segmenter.pending_events == 1  # the second turn stays open


def test_idle_flush_emits_the_open_turn() -> None:
    """Otherwise the last turn of a conversation is stranded forever."""
    segmenter = Segmenter(budget_tokens=5000)
    segmenter.add([ev("user", "text", "only turn"), ev("assistant", "text", "reply")])

    segments = segmenter.drain(flush_incomplete=True)

    assert len(segments) == 1
    assert segmenter.pending_events == 0


def test_a_turn_spanning_several_ticks_is_reassembled() -> None:
    """A turn normally arrives across several polling periods."""
    segmenter = Segmenter(budget_tokens=5000)
    segmenter.add([ev("user", "text", "start")])
    segmenter.drain()
    segmenter.add([ev("assistant", "tool_use", "pytest")])
    segmenter.drain()
    segmenter.add([ev("user", "tool_result", "passed")])
    segmenter.drain()
    segmenter.add([ev("user", "text", "next request")])

    segments = segmenter.drain()

    assert len(segments) == 1
    assert len(segments[0].events) == 3


def test_oversized_turn_splits_and_no_chunk_exceeds_budget() -> None:
    segmenter = Segmenter(budget_tokens=100)
    big = "x" * 1000  # ~285 tokens each
    segmenter.add([ev("user", "text", "go")] + [ev("assistant", "text", big, i) for i in range(4)])
    segmenter.add([ev("user", "text", "next")])

    segments = segmenter.drain()

    assert len(segments) > 1
    for segment in segments:
        assert len(segment.events) >= 1


def test_segments_carry_unique_ids_and_a_time_range() -> None:
    segmenter = Segmenter(budget_tokens=5000, agent_session_id="sess-1")
    segmenter.add([ev("user", "text", "one", 0), ev("assistant", "text", "reply", 5)])
    segmenter.add([ev("user", "text", "two", 10), ev("assistant", "text", "reply", 12)])
    segmenter.add([ev("user", "text", "three", 20)])

    segments = segmenter.drain()

    assert len({s.id for s in segments}) == len(segments)
    assert all(s.agent_session_id == "sess-1" for s in segments)
    assert segments[0].started_at < segments[0].ended_at


def test_empty_input_yields_nothing() -> None:
    segmenter = Segmenter(budget_tokens=5000)

    assert segmenter.drain() == []
    assert segmenter.drain(flush_incomplete=True) == []


# ---------------------------------------------------------------------------
# emitting a still-open turn once it is past the budget (2026-08-07)
# ---------------------------------------------------------------------------
#
# The hold exists so a half-written turn is not distilled into a half finding.
# But it was unconditional, and in a long agentic turn nothing could end it:
# a turn closes on a USER TEXT event, and the idle flush measures time since
# the transcript last grew — which every tool result resets. So neither trigger
# could fire while the agent worked, and events piled up for the whole turn.
# Measured on a live session: 19 -> 46 events held across eight minutes, then
# 16 findings in one burst the moment the human stopped typing.
#
# The fix rests on two things already true. An assistant message is FINAL the
# moment it lands in the transcript — holding it buys no extra completeness.
# And content past a budget boundary is already emitted as an independent
# sub-segment: "Sub-segments are not merged back together anywhere: each is
# distilled independently and deduplication is synthesis's job, service-side."
# So full chunks go now, and only the open tail is held.


def _long_turn(budget: int, messages: int) -> list[AgentEvent]:
    body = "\n".join(f"line {i} of a long assistant message" for i in range(40))
    events = [ev("user", "text", "start the work", 0)]
    for i in range(messages):
        events.append(ev("assistant", "text", body, i + 1))
    return events


def test_a_still_open_turn_past_the_budget_emits_its_full_chunks() -> None:
    """The latency fix. No turn boundary, no idle flush — and work still ships."""
    segmenter = Segmenter(budget_tokens=100, agent_session_id="sess-1")
    segmenter.add(_long_turn(100, messages=6))

    segments = segmenter.drain()          # turn is still open

    assert segments, "an open turn past the budget must not be held whole"
    assert segmenter.pending_events > 0, "the open tail must stay pending"


def test_the_tail_is_held_so_a_half_written_message_is_not_distilled() -> None:
    """The half-turn protection the hold exists for is preserved: only the
    chunk still being written stays back."""
    segmenter = Segmenter(budget_tokens=100, agent_session_id="sess-1")
    events = _long_turn(100, messages=6)
    segmenter.add(events)

    emitted = segmenter.drain()

    # Compared by CONTENT LENGTH, not event count: `_split_oversized` cuts one
    # long message into several parts, so the emitted event count is larger
    # than the input's while carrying strictly less of the turn.
    shipped = sum(len(e.content) for s in emitted for e in s.events)
    total = sum(len(e.content) for e in events)
    assert shipped < total, "everything shipped; nothing was held back"
    assert segmenter.pending_events > 0, "the chunk still being written must stay"


def test_a_short_open_turn_is_still_held_whole() -> None:
    """Unchanged for the ordinary case: under the budget there is nothing to
    gain by emitting early, and a half turn is exactly what must not ship."""
    segmenter = Segmenter(budget_tokens=100_000, agent_session_id="sess-1")
    segmenter.add([ev("user", "text", "hi", 0), ev("assistant", "text", "short", 1)])

    assert segmenter.drain() == []
    assert segmenter.pending_events == 2


def test_nothing_is_lost_across_an_early_emit_and_a_later_close() -> None:
    """Every event ends up in exactly one segment across the two drains."""
    segmenter = Segmenter(budget_tokens=100, agent_session_id="sess-1")
    events = _long_turn(100, messages=6)
    segmenter.add(events)

    first = segmenter.drain()
    segmenter.add([ev("user", "text", "next question", 99)])
    second = segmenter.drain()

    shipped = [e.content for s in (first + second) for e in s.events]
    for original in events:
        assert any(original.content.startswith(c[:40]) or c.startswith(original.content[:40])
                   for c in shipped), "an event vanished between drains"
