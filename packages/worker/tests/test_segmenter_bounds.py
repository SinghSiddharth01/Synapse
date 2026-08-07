"""Bounds over the oversized-event pre-split (7c42e96).

`test_segmenter.py` covers the newline-preferring cut and lossless rejoin at
one budget. This file covers what that left open, and it is deliberately
written the way ADR 0005 says a bound test has to be written: the assertions
say the bound HOLDS across a swept range of inputs, not that it held once on
a fixture chosen to make it hold.

The sweep runs at and past the EFFECTIVE shipped limits, not code defaults
(`docs/overnight/FLOW.md` §2): `segment_budget = 2787` tokens, which is
`int(2787 * 3.5) = 9754` chars in one event before the pre-split fires, with
the newline preferred anywhere in `[5852, 9754)`.

One arithmetic fact the sweep exists to keep honest. The post-split token
bound is `estimate_tokens(chunk) <= budget` at 2787 and only
`<= budget + 1` in general, because the estimator is
`int(chars / 3.5) + 1` and the cut is at `int(budget * 3.5)` chars:

    budget 2787 (odd)  -> int(2787*3.5)=9754 -> int(9754/3.5)+1 = 2787  == budget
    budget 100  (even) -> int(100*3.5) = 350 -> int(350/3.5)+1  =  101  == budget+1

Every even budget overshoots by exactly one token. The shipped configuration
is safe because 2787 is odd — that is luck, not design, so both bounds are
asserted separately and the 2787 case is called out by name.
"""

from __future__ import annotations

import random
from datetime import datetime, timedelta, timezone

from synapse_contracts import AgentEvent

from synapse_worker.segmenter import Segmenter, _split_oversized, estimate_tokens

T0 = datetime(2026, 8, 6, 3, 0, tzinfo=timezone.utc)

# FLOW.md §2: the shipped [distiller] segment_budget, and the char ceiling it
# puts on a single event before _split_oversized cuts it.
SHIPPED_BUDGET = 2787
SHIPPED_MAX_CHARS = 9754


def ev(role: str, kind: str, content: str, minute: int = 0) -> AgentEvent:
    return AgentEvent(
        role=role,  # type: ignore[arg-type]
        kind=kind,  # type: ignore[arg-type]
        content=content,
        ts=T0 + timedelta(minutes=minute),
        agent_session_id="sess-1",
    )


# --- the hard cut: the path with no newline to prefer ----------------------------


def test_with_no_newline_in_the_window_the_cut_lands_exactly_on_the_ceiling() -> None:
    """The `else` half of the cut — and the half nothing asserted before.

    `test_oversized_turn_splits_and_no_chunk_exceeds_budget` feeds "x"*1000,
    which has no newline anywhere, but asserts only `len(segments) > 1` and
    `len(events) >= 1` — both already true from inter-event chunking alone, so
    it passes with the pre-split reverted. This pins the actual number: with
    nothing to prefer, the piece is exactly `int(budget * 3.5)` chars.
    """
    budget = 100
    max_chars = int(budget * 3.5)  # 350
    # A newline at index 50 only — well before the last 40% of the window
    # ([210, 350)), so it must be ignored rather than producing a 51-char shard.
    content = "a" * 50 + "\n" + "b" * 999

    parts = _split_oversized(ev("assistant", "text", content), budget)

    assert len(parts) > 1
    assert len(parts[0].content) == max_chars, "no newline to prefer -> a hard cut"
    assert not parts[0].content.endswith("\n")
    assert "".join(p.content for p in parts) == content


def test_a_newline_before_the_window_is_not_used_as_a_cut_point() -> None:
    """The 60% floor is the reason the cut is not simply `rfind("\\n")`.

    Cutting at the nearest preceding newline WHEREVER it is would turn one
    9754-char event into a 51-char shard plus the rest, and every shard is a
    separate model call on the slowest step in the system.
    """
    budget = 100
    max_chars = int(budget * 3.5)
    content = "a" * 10 + "\n" + "b" * 1000  # only newline sits at index 10

    parts = _split_oversized(ev("assistant", "text", content), budget)

    assert len(parts[0].content) == max_chars


def test_a_newline_inside_the_window_wins_over_the_ceiling() -> None:
    """The whole point of the cut rule: keep the damage at a line boundary."""
    budget = 100
    max_chars = int(budget * 3.5)  # 350; window is [210, 350)
    content = "a" * 300 + "\n" + "b" * 1000

    parts = _split_oversized(ev("assistant", "text", content), budget)

    assert len(parts[0].content) == 301, "cut immediately after the newline"
    assert parts[0].content.endswith("\n")
    assert parts[0].content != content[:max_chars], "not the hard cut"


def test_the_last_newline_in_the_window_wins_not_the_first() -> None:
    """`rfind`, not `find` — cutting at the first candidate would leave more
    of the window unused on every piece and multiply the model calls."""
    budget = 100
    content = "a" * 220 + "\n" + "b" * 80 + "\n" + "c" * 1000
    # newlines at 220 and 301, both inside [210, 350)

    parts = _split_oversized(ev("assistant", "text", content), budget)

    assert len(parts[0].content) == 302


def test_a_zero_or_negative_budget_returns_the_event_untouched() -> None:
    """`max_chars <= 0` is the guard that keeps a misconfigured budget from
    turning one event into one part per character (and hanging the worker on
    a `while text:` loop that never shortens `text`)."""
    event = ev("assistant", "text", "some content that is not empty")

    assert _split_oversized(event, 0) == [event]
    assert _split_oversized(event, -5) == [event]


def test_an_event_exactly_on_the_ceiling_is_not_cut() -> None:
    """`<=`, not `<`. An event of exactly `int(budget * 3.5)` chars fits, and
    cutting it would cost coherence for nothing."""
    exact = "z" * SHIPPED_MAX_CHARS

    assert _split_oversized(ev("assistant", "text", exact), SHIPPED_BUDGET) == [
        ev("assistant", "text", exact)
    ]

    one_over = "z" * (SHIPPED_MAX_CHARS + 1)
    assert len(_split_oversized(ev("assistant", "text", one_over), SHIPPED_BUDGET)) == 2


# --- the pre-split is not a text-only path ---------------------------------------


def test_a_giant_tool_result_is_split_too_and_keeps_its_kind() -> None:
    """`_split_oversized` runs over every event in the turn, and the loop order
    in `loop.py` puts segmentation BEFORE compaction — so a huge tool_result is
    cut here first, and compaction's 15-head/15-tail sees the pieces.

    Asserted because the split must preserve `kind` and `role`: a piece that
    arrived as `text` would change what the distiller's kind filter admits
    (`distil_kinds = ("text",)`), silently distilling raw tool output.
    """
    budget = 100
    body = "\n".join(f"stack frame {i} of a very long traceback" for i in range(200))

    parts = _split_oversized(ev("user", "tool_result", body), budget)

    assert len(parts) > 1
    assert {(p.role, p.kind) for p in parts} == {("user", "tool_result")}
    assert "".join(p.content for p in parts) == body


def test_the_split_runs_on_the_held_back_path_too() -> None:
    """Both existing tests drain with `flush_incomplete=True`. The ordinary
    tick path — a completed turn emitted because the NEXT turn started, with
    the newest turn held back — must split identically."""
    budget = 100
    huge = "\n".join(f"line {i} of an assistant message" for i in range(120))
    segmenter = Segmenter(budget_tokens=budget, agent_session_id="sess-1")
    segmenter.add([ev("user", "text", "go", 0), ev("assistant", "text", huge, 1)])
    # ⟨2026-08-07⟩ This used to assert `drain() == []` — "one turn so far;
    # nothing is complete". An open turn already past the budget now emits its
    # FULL chunks and holds only the trailing one, because waiting for a turn
    # that may run for minutes of tool use was the latency this fixes. The
    # property under test is unchanged and now spans both drains: the oversized
    # event is cut, and every part fits.
    early = segmenter.drain()
    assert early, "an open turn past the budget must not be held whole"

    segmenter.add([ev("user", "text", "next", 2)])
    segments = early + segmenter.drain(flush_incomplete=False)

    # NOT `len(segments) > 1`: inter-event chunking alone splits `go` from the
    # assistant message, so that assertion is true with the pre-split reverted.
    # What must be true only WITH it is that the one assistant event came out
    # as several, none over the ceiling.
    assistant_parts = [e for s in segments for e in s.events if e.role == "assistant"]
    assert len(assistant_parts) > 1, "the oversized event itself must be cut"
    for part in assistant_parts:
        assert len(part.content) <= int(budget * 3.5)
    assert "".join(p.content for p in assistant_parts) == huge
    assert segmenter.pending_events == 1, "the new turn is still held"


# --- the swept bound -------------------------------------------------------------


def _text_with_newlines(rng: random.Random, length: int) -> str:
    """Prose-shaped content: words, with a newline every 20-90 chars. Seeded, so
    a failure is reproducible from the parameters alone."""
    out: list[str] = []
    size = 0
    while size < length:
        run = rng.randint(20, 90)
        out.append("".join(rng.choice("abcdefghij ") for _ in range(run)))
        out.append("\n")
        size += run + 1
    return "".join(out)[:length]


# (budget, content length). Budgets bracket the shipped 2787 on both sides and
# include even ones (where the +1 overshoot is real). Lengths run from well
# under the per-event ceiling to several times past it — 9754 is exactly the
# ceiling at 2787, so 9753/9754/9755 sit either side of the cut decision, and
# 40000 is past four ceilings.
SWEEP: tuple[tuple[int, int], ...] = (
    (SHIPPED_BUDGET, 1),
    (SHIPPED_BUDGET, SHIPPED_MAX_CHARS - 1),
    (SHIPPED_BUDGET, SHIPPED_MAX_CHARS),
    (SHIPPED_BUDGET, SHIPPED_MAX_CHARS + 1),
    (SHIPPED_BUDGET, SHIPPED_MAX_CHARS * 2),
    (SHIPPED_BUDGET, 40000),
    (SHIPPED_BUDGET - 1, 40000),   # even budget: the +1 overshoot case
    (SHIPPED_BUDGET + 1, 40000),   # even budget
    (500, 20000),                  # MIN_USABLE_SEGMENT_TOKENS, the floor
    (501, 20000),
    (100, 5000),
    (7, 400),
    (1, 60),
)


def test_no_event_survives_the_split_over_the_char_ceiling() -> None:
    """`len(content) <= int(budget * 3.5)` for every piece, at every budget."""
    for budget, length in SWEEP:
        rng = random.Random(f"{budget}:{length}")
        content = _text_with_newlines(rng, length)
        max_chars = int(budget * 3.5)

        parts = _split_oversized(ev("assistant", "text", content), budget)

        for part in parts:
            assert len(part.content) <= max_chars, (
                f"budget={budget} length={length}: a piece of "
                f"{len(part.content)} chars overruns the {max_chars}-char ceiling"
            )
        assert "".join(p.content for p in parts) == content, (
            f"budget={budget} length={length}: the split lost or invented content"
        )


def test_no_emitted_segment_exceeds_the_token_budget_by_more_than_one() -> None:
    """The bound the segmenter actually owes the prompt builder.

    `estimate_tokens` is what `promptpack` uses to size the prompt, so this —
    not the char count — is the number that decides whether a segment fits.
    The universal bound is `budget + 1`: the char ceiling `int(budget * 3.5)`
    rounds back up through `int(chars / 3.5) + 1` for every even budget.
    """
    for budget, length in SWEEP:
        rng = random.Random(f"{budget}:{length}")
        content = _text_with_newlines(rng, length)
        segmenter = Segmenter(budget_tokens=budget, agent_session_id="sess-1")
        segmenter.add([ev("user", "text", "go", 0),
                       ev("assistant", "text", content, 1)])

        segments = segmenter.drain(flush_incomplete=True)

        assert segments, f"budget={budget} length={length}: nothing emitted"
        for segment in segments:
            assert estimate_tokens(segment.events) <= budget + 1, (
                f"budget={budget} length={length}: segment {segment.id} "
                f"estimates {estimate_tokens(segment.events)} tokens"
            )
        rejoined = "".join(e.content for s in segments for e in s.events
                           if e.role == "assistant")
        assert rejoined == content, (
            f"budget={budget} length={length}: content lost across segments"
        )


def test_at_the_shipped_budget_the_bound_holds_exactly_with_no_slack() -> None:
    """2787 is the one that has to hold on the nose.

    A full prompt is `2787 + 809 overhead + 500 reserve = 4096`, which IS the
    NPU's usable context (FLOW.md §2, "Three things worth flagging"). There is
    no slack to absorb the +1 that every even budget carries, so this asserts
    `<= budget`, not `<= budget + 1`, and it asserts the tightness too: some
    segment must actually reach the budget, or the sweep is not exercising the
    edge it claims to.
    """
    rng = random.Random("shipped")
    prose = _text_with_newlines(rng, SHIPPED_MAX_CHARS * 5)
    # A newline-free block takes the HARD cut, which is the only way a piece
    # comes out at exactly `int(2787 * 3.5)` chars and therefore the only way
    # the worst case is reached at all. Prose alone cuts a little early (the
    # preferred newline sits inside the window), so a sweep over prose only
    # would never touch the edge this test is named after.
    unbroken = "".join(rng.choice("abcdefghij") for _ in range(SHIPPED_MAX_CHARS * 5))

    worst = 0
    for content in (prose, unbroken):
        segmenter = Segmenter(budget_tokens=SHIPPED_BUDGET, agent_session_id="sess-1")
        segmenter.add([ev("user", "text", "go", 0),
                       ev("assistant", "text", content, 1)])

        segments = segmenter.drain(flush_incomplete=True)

        sizes = [estimate_tokens(s.events) for s in segments]
        assert max(sizes) <= SHIPPED_BUDGET, (
            f"a segment reached {max(sizes)} tokens against the 2787 budget "
            f"that leaves 4096 - 809 - 500 = 2787 and not one token more"
        )
        worst = max(worst, max(sizes))

    assert worst == SHIPPED_BUDGET, (
        "no segment reached the budget — this test would then pass without "
        "exercising the edge it exists for"
    )
