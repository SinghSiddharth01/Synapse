"""FakeProvider tests — Plan 0 Task 0.4.

Note: the scripted structured payload here uses the CURRENT Finding shape
(`attributions`), not the `contributor` / `source_session` shape shown in
docs/brainstorming/2026-07-25-plan-0-foundation.md Task 4. That doc predates
the Attribution revision; copying its fixture verbatim would reintroduce the
pre-Attribution contract the plans explicitly retired.
"""

from __future__ import annotations

import pytest

from synapse_providers import FakeProvider


async def test_returns_scripted_text_when_no_schema() -> None:
    fake = FakeProvider(scripts=["hello world"])
    result = await fake.complete(messages=[{"role": "user", "content": "hi"}])

    assert result.data == "hello world"
    assert result.provider_id == "fake"
    assert result.schema_valid is True
    assert result.usage.input_tokens > 0
    assert result.usage.output_tokens > 0


async def test_returns_scripted_structured_output() -> None:
    scripted = {
        "findings": [
            {
                "type": "learning",
                "text": "transaction-mode pooling is incompatible with prepared statements",
            }
        ]
    }
    fake = FakeProvider(scripts=[scripted])
    schema = {"type": "object", "properties": {"findings": {"type": "array"}}}

    result = await fake.complete(messages=[], response_schema=schema)

    assert result.data == scripted
    assert result.schema_valid is True


async def test_is_deterministic_across_calls() -> None:
    fake = FakeProvider(scripts=["one", "two", "three"])

    r1 = await fake.complete(messages=[])
    r2 = await fake.complete(messages=[])
    r3 = await fake.complete(messages=[])

    assert (r1.data, r2.data, r3.data) == ("one", "two", "three")


async def test_exhausts_and_raises_rather_than_inventing() -> None:
    fake = FakeProvider(scripts=["only one"])
    await fake.complete(messages=[])

    with pytest.raises(RuntimeError, match="exhausted"):
        await fake.complete(messages=[])


async def test_reports_capabilities() -> None:
    fake = FakeProvider(scripts=["x"])
    assert fake.capabilities.native_structured_output is True


async def test_input_tokens_override_reproduces_prompt_drop() -> None:
    """The override exists so the guard against the vlm prompt-drop bug is testable."""
    fake = FakeProvider(scripts=["invented text"], input_tokens=1)
    result = await fake.complete(messages=[{"role": "user", "content": "a real prompt"}])

    assert result.usage.input_tokens == 1
