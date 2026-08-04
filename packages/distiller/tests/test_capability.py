"""Capability-record tests — Plan B Task B.5.

The derivation is what is tested, never a constant. Plan A's segmenter reads
these records, so a hard-coded budget anywhere is the bug this guards against.
"""

from __future__ import annotations

import pytest

from synapse_distiller.capability import (
    NPU_QWEN3_4B_INSTRUCT_2507,
    CapabilityError,
    CapabilityRecord,
)

OVERHEAD = 600  # a typical prompt pack's fixed cost


def _record(**overrides) -> CapabilityRecord:
    base = {
        "model": "test-model",
        "usable_context": 4096,
        "prefill_toks_per_sec": 250.0,
        "response_reserve": 500,
    }
    return CapabilityRecord(**{**base, **overrides})


def test_two_different_records_yield_two_different_budgets() -> None:
    small = _record(usable_context=4096)
    large = _record(usable_context=8192)

    assert small.segment_budget(OVERHEAD) != large.segment_budget(OVERHEAD)
    assert large.segment_budget(OVERHEAD) > small.segment_budget(OVERHEAD)


def test_budget_is_context_minus_overhead_and_reserve() -> None:
    record = _record(usable_context=4096)

    # 4096 - 600 - 500 = 2996; the latency limit (250 * 30 = 7500) does not bind.
    assert record.segment_budget(OVERHEAD, max_seconds_per_call=30.0) == 2996


def test_editing_the_prompt_re_budgets_automatically() -> None:
    """The whole reason overhead is passed in rather than stored: a longer
    prompt must shrink the budget without anyone editing a config value."""
    record = _record()

    assert record.segment_budget(400) > record.segment_budget(900)
    assert record.segment_budget(400) - record.segment_budget(900) == 500


def test_latency_limit_binds_when_prefill_is_slow() -> None:
    record = _record(usable_context=32768, prefill_toks_per_sec=50.0)

    # 50 * 20 = 1000 tokens of prefill fits the latency budget, far below the
    # 31668 the context would otherwise allow.
    assert record.segment_budget(OVERHEAD, max_seconds_per_call=20.0) == 1000


def test_record_implying_an_unusable_budget_fails_loudly() -> None:
    record = _record(usable_context=1024)

    with pytest.raises(CapabilityError, match="below the"):
        record.segment_budget(OVERHEAD)


def test_a_bloated_prompt_pack_fails_loudly() -> None:
    """The failure mode configurable prompts introduce: a pack so long there is
    no room left for a segment."""
    record = _record(usable_context=4096)

    with pytest.raises(CapabilityError, match="shorten the prompt pack"):
        record.segment_budget(3200)


def test_error_reports_which_limit_bound() -> None:
    record = _record(usable_context=32768, prefill_toks_per_sec=1.0)

    with pytest.raises(CapabilityError, match="latency limit gives"):
        record.segment_budget(OVERHEAD, max_seconds_per_call=30.0)


def test_override_smaller_than_derived_is_accepted() -> None:
    record = _record()

    assert record.segment_budget(OVERHEAD, override=1500) == 1500


def test_override_exceeding_the_context_is_rejected() -> None:
    """An override above what fits would truncate every prompt silently — the
    same failure class as a dropped prompt, just partial."""
    record = _record(usable_context=4096)

    with pytest.raises(CapabilityError, match="would be truncated"):
        record.segment_budget(OVERHEAD, override=8000)


def test_override_below_the_minimum_is_rejected() -> None:
    record = _record()

    with pytest.raises(CapabilityError, match="below the"):
        record.segment_budget(OVERHEAD, override=100)


def test_measured_npu_record_reflects_the_4096_ceiling() -> None:
    """Measured 2026-08-04: prompt_tokens=3809 succeeded, ~7000 returned HTTP 400.
    QAIRT bakes context in — there is no --nctx on this path."""
    assert NPU_QWEN3_4B_INSTRUCT_2507.usable_context == 4096
    assert NPU_QWEN3_4B_INSTRUCT_2507.response_reserve == 500
