"""Over-budget vs degenerate repetition vs merely malformed.

Before this, all three incremented `dropped_malformed` and logged the same
sentence. They are not the same failure and they do not have the same fix:

  over-budget           the model was fine, the budget was wrong. RAISE the
                        reserve or shrink the segment.
  degenerate-repetition the model looped. Raising the cap makes it WORSE — a
                        longer loop, the same non-answer, more NPU seconds.
  malformed             neither; prose around the JSON, a fence, a refusal.

Reading a degenerate drop as over-budget and raising max_tokens is the specific
wrong move this distinction exists to prevent — and on the NPU arm it is worse
than a wasted round, because `provider.max_tokens` is clamped to the reserve the
segment budget was derived from (7418a63). Raising it past 500 overruns the 4096
context on exactly the segments that used their full 2787.

The bounds here are the EFFECTIVE ones (FLOW.md §2-3): the NPU arm requests 500
output tokens, clamped down from the config's 900.
"""

from __future__ import annotations

import logging

import pytest
from synapse_contracts import LocalBinding
from synapse_providers import FakeProvider

from synapse_distiller import Distiller
from synapse_distiller.distiller import (
    DROP_DEGENERATE,
    DROP_MALFORMED,
    DROP_OVER_BUDGET,
    MAX_ATTEMPTS,
    classify_drop,
    distinct_shingle_ratio,
    is_degenerate,
)
from synapse_distiller.fixtures import load_segment

BINDING = LocalBinding(
    agent_session_id="as-fixture-001",
    shared_id="shared-1",
    contributor="aditya",
    agent="claude-code",
)

# FLOW.md §3: what the NPU arm actually asks for, after the clamp.
EFFECTIVE_MAX_TOKENS = 500

# The three shapes, as they arrive.
TRUNCATED = '{"findings": [{"type": "learning", "text": "the reserve is a prom'
# Deliberately sized BELOW the cap. A loop that runs all the way to max_tokens
# classifies as over-budget by design (the cap is what cut the evidence short),
# so a fixture long enough to reach it would test the wrong branch — which is
# exactly what the first draft of this file did.
LOOPED = ("the worker binds to localhost and " * 2) * 20
PROSE = (
    "I'm sorry, I can't produce structured output for that transcript. "
    "There was nothing durable in it worth recording, and inventing a finding "
    "would be worse than returning none at all."
)


class _CappedProvider(FakeProvider):
    """A FakeProvider that exposes `max_tokens`, like every real provider does.

    `SynthesisBudget.for_provider` already reads this attribute off whichever
    provider is wired in (see the PUBLIC note in anthropic_provider.py), so it is
    the established way to ask a provider for its cap. `classify_drop` reads it
    the same way, and tolerates its absence.
    """

    def __init__(self, scripts, *, max_tokens: int) -> None:
        super().__init__(scripts=scripts)
        self.max_tokens = max_tokens


# --- the measure itself ----------------------------------------------------------


def test_the_repetition_measure_separates_a_loop_from_a_truncated_object() -> None:
    """The two shapes sit at opposite ends, not near a coin-flip. Asserted as a
    gap rather than a threshold: a measure that put them at 0.48 and 0.52 would
    "pass" while classifying real traffic at random."""
    assert distinct_shingle_ratio(LOOPED) < 0.1
    assert distinct_shingle_ratio(TRUNCATED) == 1.0
    assert distinct_shingle_ratio(PROSE) == 1.0

    assert is_degenerate(LOOPED)
    assert not is_degenerate(TRUNCATED)
    assert not is_degenerate(PROSE)


@pytest.mark.parametrize("cycles", [3, 8, 25, 100])
def test_a_loop_is_caught_at_every_length_it_can_reach(cycles: int) -> None:
    """Swept, because a loop's length is a function of how much budget was left
    when it started — anywhere from a few cycles to the whole 500-token cap."""
    text = "raise the timeout and restart the worker " * cycles

    assert is_degenerate(text), f"{cycles} cycles read as non-degenerate"


@pytest.mark.parametrize(
    "text",
    [
        "",
        "short",
        '{"findings": []}',
        TRUNCATED,
        PROSE,
        "a b c d e f g h i j k l m n o p q r s t u v w x y z",
    ],
)
def test_nothing_short_or_varied_is_ever_called_a_loop(text: str) -> None:
    """The false-positive direction, which is the expensive one: a MALFORMED drop
    misreported as a loop sends the reader after the prompt when the parser is
    what needs looking at."""
    assert not is_degenerate(text)


# --- the three-way classification ------------------------------------------------


def test_reaching_the_cap_is_over_budget_whatever_the_text_looks_like() -> None:
    """Over-budget is decided on the token count, before the text is examined.

    A loop that ran until the cap is still, operationally, a response whose
    evidence was truncated — calling it a loop would point at the prompt when
    the cap is what cut the record short.
    """
    for text in (TRUNCATED, LOOPED, PROSE):
        assert classify_drop(text, EFFECTIVE_MAX_TOKENS, EFFECTIVE_MAX_TOKENS) == (
            DROP_OVER_BUDGET
        ), text[:40]


def test_below_the_cap_the_text_shape_decides() -> None:
    assert classify_drop(LOOPED, 120, EFFECTIVE_MAX_TOKENS) == DROP_DEGENERATE
    assert classify_drop(TRUNCATED, 120, EFFECTIVE_MAX_TOKENS) == DROP_MALFORMED
    assert classify_drop(PROSE, 120, EFFECTIVE_MAX_TOKENS) == DROP_MALFORMED


def test_a_provider_with_no_cap_still_gets_the_text_shaped_distinction() -> None:
    """`ClaudeCliProvider` ignores max_tokens entirely and `FakeProvider` has
    none. Over-budget is simply not knowable there — which must degrade to "the
    other two still work", never to "everything is over-budget"."""
    assert classify_drop(LOOPED, 999999, None) == DROP_DEGENERATE
    assert classify_drop(PROSE, 999999, None) == DROP_MALFORMED


def test_a_zero_cap_is_not_read_as_everything_being_over_budget() -> None:
    """`> 0` in the guard. A provider reporting max_tokens=0 is misconfigured,
    and labelling every drop over-budget would send the reader to raise a cap
    that is not the problem."""
    assert classify_drop(PROSE, 0, 0) == DROP_MALFORMED


# --- end to end, through distil() ------------------------------------------------


async def test_a_truncated_response_at_the_cap_is_recorded_as_over_budget(
    caplog,
) -> None:
    """The whole point, at the level an operator reads. FakeProvider reports
    output_tokens as `len(str(data)) // 4`, so the script is sized to land on
    the 500-token cap the NPU arm actually requests."""
    segment = load_segment("seg-001")
    at_the_cap = TRUNCATED + "x" * (EFFECTIVE_MAX_TOKENS * 4)
    provider = _CappedProvider(
        scripts=[at_the_cap, at_the_cap], max_tokens=EFFECTIVE_MAX_TOKENS
    )

    with caplog.at_level(logging.WARNING, logger="synapse_distiller.distiller"):
        findings, stats = await Distiller(provider, BINDING).distil(segment)

    assert findings == []
    assert stats.dropped_malformed == MAX_ATTEMPTS
    assert stats.drop_reasons == [DROP_OVER_BUDGET] * MAX_ATTEMPTS
    drops = [r.getMessage() for r in caplog.records if "unparseable" in r.getMessage()]
    assert all(f"reason={DROP_OVER_BUDGET}" in m for m in drops)


async def test_a_repetition_loop_below_the_cap_is_recorded_as_degenerate(
    caplog,
) -> None:
    """And NOT as over-budget — this is the one that must never suggest raising
    max_tokens, because more room buys a longer loop and nothing else."""
    segment = load_segment("seg-001")
    provider = _CappedProvider(
        scripts=[LOOPED, LOOPED], max_tokens=EFFECTIVE_MAX_TOKENS
    )

    with caplog.at_level(logging.WARNING, logger="synapse_distiller.distiller"):
        _, stats = await Distiller(provider, BINDING).distil(segment)

    assert stats.drop_reasons == [DROP_DEGENERATE] * MAX_ATTEMPTS
    drops = [r.getMessage() for r in caplog.records if "unparseable" in r.getMessage()]
    assert all(f"reason={DROP_DEGENERATE}" in m for m in drops)
    assert all(DROP_OVER_BUDGET not in m for m in drops), (
        "a loop reported as over-budget invites raising the cap, which makes it "
        "worse and overruns the 4096 context the clamp exists to protect"
    )


async def test_ordinary_unparseable_prose_stays_plainly_malformed(caplog) -> None:
    """The third case has to stay distinguishable from the other two, or the
    distinction is just two labels for one bucket."""
    segment = load_segment("seg-001")
    provider = _CappedProvider(
        scripts=[PROSE, PROSE], max_tokens=EFFECTIVE_MAX_TOKENS
    )

    with caplog.at_level(logging.WARNING, logger="synapse_distiller.distiller"):
        _, stats = await Distiller(provider, BINDING).distil(segment)

    assert stats.drop_reasons == [DROP_MALFORMED] * MAX_ATTEMPTS


async def test_the_three_reasons_are_actually_three_distinct_labels() -> None:
    """The trap this file is written against: a "distinction" whose branches all
    return the same string would pass every test above that only checks its own
    branch."""
    assert len({DROP_OVER_BUDGET, DROP_DEGENERATE, DROP_MALFORMED}) == 3


async def test_a_recovered_segment_records_the_reason_for_the_failed_attempt_only(
    caplog,
) -> None:
    """One entry per increment of `dropped_malformed`, so the two stay readable
    against each other. A retry that succeeds must not leave a phantom reason
    behind claiming a drop that did not happen."""
    segment = load_segment("seg-001")
    provider = _CappedProvider(
        scripts=[LOOPED, {"findings": [{"type": "learning", "text": "recovered"}]}],
        max_tokens=EFFECTIVE_MAX_TOKENS,
    )

    findings, stats = await Distiller(provider, BINDING).distil(segment)

    assert [f.text for f in findings] == ["recovered"]
    assert stats.dropped_malformed == 1
    assert stats.drop_reasons == [DROP_DEGENERATE]
