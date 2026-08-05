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
