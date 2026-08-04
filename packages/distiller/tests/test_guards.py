"""Guard tests — Plan B Task B.3.

These encode the highest-severity failure mode in the project. If any of these
go red, the distiller can produce findings invented from a dropped prompt.
"""

from __future__ import annotations

import pytest
from synapse_contracts import ModelResult, ModelUsage
from synapse_providers import FakeProvider

from synapse_distiller.guards import (
    CANARY_EXPECTED,
    PromptDropError,
    assert_prompt_conditioned,
    check_canary,
)


def _result(input_tokens: int, output_tokens: int = 434) -> ModelResult:
    return ModelResult(
        data="fluent confident invented text",
        usage=ModelUsage(input_tokens=input_tokens, output_tokens=output_tokens),
        latency_ms=10,
        provider_id="npu",
        schema_valid=True,
    )


def test_dropped_prompt_raises() -> None:
    """The exact signature observed on gemma-4-E4B: prompt_tokens=1, 434 tokens out."""
    with pytest.raises(PromptDropError, match="input_tokens=1"):
        assert_prompt_conditioned(_result(input_tokens=1))


def test_zero_prompt_tokens_raises() -> None:
    with pytest.raises(PromptDropError):
        assert_prompt_conditioned(_result(input_tokens=0))


def test_conditioned_prompt_passes() -> None:
    assert_prompt_conditioned(_result(input_tokens=44)) is None


def test_error_names_the_fix() -> None:
    """The message must carry the remedy — this fires rarely and under pressure."""
    with pytest.raises(PromptDropError, match="set-type"):
        assert_prompt_conditioned(_result(input_tokens=1))


async def test_canary_passes_when_model_extracts_the_fact() -> None:
    provider = FakeProvider(scripts=[f"{CANARY_EXPECTED}"])

    result = await check_canary(provider)

    assert result.passed is True
    assert bool(result) is True


async def test_canary_fails_when_prompt_is_dropped() -> None:
    provider = FakeProvider(
        scripts=["Once upon a time, in a kingdom far away..."], input_tokens=1
    )

    result = await check_canary(provider)

    assert result.passed is False
    assert "prompt dropped" in result.detail
    assert result.input_tokens == 1


async def test_canary_fails_when_answer_misses_the_fact() -> None:
    """Reads the prompt, but cannot extract from it. Still not scoreable."""
    provider = FakeProvider(scripts=["A certificate expired somewhere."])

    result = await check_canary(provider)

    assert result.passed is False
    assert "did not contain" in result.detail


async def test_canary_result_is_falsy_on_failure() -> None:
    """So callers can write `if not await check_canary(p): skip_corpus()`."""
    provider = FakeProvider(scripts=["unrelated"], input_tokens=1)

    assert not await check_canary(provider)
