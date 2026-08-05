import pytest
from synapse_providers import FakeProvider
from synapse_providers.recording import CallLog, RecordingProvider


async def test_records_a_call_and_returns_the_inner_result_unchanged():
    log = CallLog()
    inner = FakeProvider(scripts=["hello world"])
    provider = RecordingProvider(inner, "distiller", log)
    result = await provider.complete([{"role": "user", "content": "hi there"}])
    assert result.data == "hello world"
    [call] = log.snapshot()
    assert call["component"] == "distiller"
    assert call["provider_id"] == "fake"
    assert call["ok"] is True
    assert call["input_tokens"] >= 1 and call["output_tokens"] >= 1
    assert "hi there" in call["prompt_preview"]
    assert "hello world" in call["output_preview"]


async def test_prompt_preview_shows_the_segment_not_the_system_prompt():
    """Every real caller (distiller/synthesis/retrieval) sends system prompt +
    few-shots + one final user message holding the actual segment/query. A
    200-char preview built by joining from message[0] never gets past the
    system prompt, so every dashboard entry for a component would render the
    same boilerplate string regardless of what was actually asked. The
    preview must come from the payload, not the pack.
    """
    log = CallLog()
    inner = FakeProvider(scripts=["hello world"])
    provider = RecordingProvider(inner, "distiller", log)
    system_prompt = "You are a distiller. " * 20  # > 200 chars, like a real pack
    messages = [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": "few-shot example input"},
        {"role": "assistant", "content": "few-shot example output"},
        {"role": "user", "content": "THE ACTUAL SEGMENT: pooling mode was flaky"},
    ]
    await provider.complete(messages)
    [call] = log.snapshot()
    assert "THE ACTUAL SEGMENT: pooling mode was flaky" in call["prompt_preview"]
    assert "distiller" not in call["prompt_preview"]


async def test_exceptions_propagate_and_are_recorded_as_failed():
    log = CallLog()
    provider = RecordingProvider(FakeProvider(scripts=[]), "synthesis", log)  # exhausted -> raises
    with pytest.raises(RuntimeError):
        await provider.complete([{"role": "user", "content": "x"}])
    [call] = log.snapshot()
    assert call["ok"] is False and call["component"] == "synthesis"


async def test_capabilities_and_provider_id_pass_through():
    provider = RecordingProvider(FakeProvider(scripts=[]), "retrieval", CallLog())
    assert provider.capabilities.native_structured_output is True
    assert provider.provider_id == "fake"


def test_ring_buffer_bounds():
    log = CallLog(maxlen=3)
    for i in range(5):
        log.append_raw({"n": i})
    assert [c["n"] for c in log.snapshot()] == [2, 3, 4]
