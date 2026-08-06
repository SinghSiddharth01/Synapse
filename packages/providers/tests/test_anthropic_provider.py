"""AnthropicProvider tests.

No network calls, no API key, no `anthropic` SDK client construction — the
SDK client is replaced with a tiny hand-rolled fake matching this package's
existing style (see test_aic100.py / test_npu.py: nothing in this suite uses
unittest.mock). The fake mimics only the small slice of the real SDK's shape
this provider actually touches: `client.messages.create(**kwargs) ->
response` where `response` carries `.content` (list of blocks with `.type` /
`.text`), `.usage` (an object with the four token-count attributes), and
`.stop_reason`.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from synapse_providers.anthropic_provider import AnthropicProvider, _tighten_schema

SCHEMA = {
    "type": "object",
    "properties": {
        "findings": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "type": {"type": "string", "enum": ["learning", "decision"]},
                    "text": {"type": "string"},
                },
                "required": ["type", "text"],
            },
        }
    },
    "required": ["findings"],
}


@dataclass
class _FakeUsage:
    input_tokens: int = 0
    output_tokens: int = 0
    cache_creation_input_tokens: int = 0
    cache_read_input_tokens: int = 0


@dataclass
class _FakeBlock:
    text: str
    type: str = "text"


@dataclass
class _FakeResponse:
    content: list = field(default_factory=list)
    usage: _FakeUsage = field(default_factory=_FakeUsage)
    stop_reason: str = "end_turn"


class _FakeMessages:
    def __init__(self, response: _FakeResponse) -> None:
        self._response = response
        self.last_kwargs: dict[str, Any] | None = None

    async def create(self, **kwargs: Any) -> _FakeResponse:
        self.last_kwargs = kwargs
        return self._response


class _FakeClient:
    def __init__(self, response: _FakeResponse) -> None:
        self.messages = _FakeMessages(response)


def _text_response(text: str, **usage_kwargs: int) -> _FakeResponse:
    return _FakeResponse(content=[_FakeBlock(text=text)], usage=_FakeUsage(**usage_kwargs))


def _provider(response: _FakeResponse, **kwargs: Any) -> tuple[AnthropicProvider, _FakeClient]:
    client = _FakeClient(response)
    provider = AnthropicProvider(client=client, **kwargs)
    return provider, client


# --- construction / capabilities -------------------------------------------------


async def test_default_model_is_claude_opus_5() -> None:
    provider, client = _provider(_text_response("hi"))

    await provider.complete(messages=[{"role": "user", "content": "hi"}])

    assert client.messages.last_kwargs["model"] == "claude-opus-5"


def test_capabilities_are_native_structured_output_true_streaming_false() -> None:
    provider, _ = _provider(_text_response("ok"))

    caps = provider.capabilities

    assert caps.native_structured_output is True
    assert caps.streaming is False


# --- structural break #3: system prompt hoisted -----------------------------------


async def test_system_prompt_is_hoisted_out_of_messages() -> None:
    """build_messages() emits {"role": "system"} as messages[0] (the
    OpenAI-shaped convention every other provider in this package speaks);
    the Anthropic Messages API takes it as a top-level `system` string
    instead, and messages[] must contain no system-role entries."""
    provider, client = _provider(_text_response("ok"))

    await provider.complete(
        messages=[
            {"role": "system", "content": "You are a distiller."},
            {"role": "user", "content": "segment text"},
        ]
    )

    sent = client.messages.last_kwargs
    assert sent["system"] == "You are a distiller."
    assert sent["messages"] == [{"role": "user", "content": "segment text"}]
    assert all(m["role"] != "system" for m in sent["messages"])


async def test_no_system_key_sent_when_no_system_message_present() -> None:
    provider, client = _provider(_text_response("ok"))

    await provider.complete(messages=[{"role": "user", "content": "hi"}])

    assert "system" not in client.messages.last_kwargs


# --- correction #3: no temperature, generous max_tokens, low effort --------------


async def test_no_temperature_is_ever_sent() -> None:
    """temperature is a hard 400 on Opus-tier models -- must never appear in
    the request, not even as 0.0 (the value every other provider in this
    package sends)."""
    provider, client = _provider(_text_response("ok"))

    await provider.complete(messages=[{"role": "user", "content": "hi"}])

    assert "temperature" not in client.messages.last_kwargs


async def test_max_tokens_is_generous_not_the_npu_tuned_default() -> None:
    """On Opus 5, thinking is on by default and max_tokens caps thinking PLUS
    the final text together -- this repo's NPU-tuned provider.max_tokens=900
    would truncate the JSON mid-object."""
    provider, client = _provider(_text_response("ok"))

    await provider.complete(messages=[{"role": "user", "content": "hi"}])

    assert client.messages.last_kwargs["max_tokens"] == 16000


async def test_uses_low_reasoning_effort_by_default() -> None:
    """A distiller doing faithful compression does not need deep reasoning."""
    provider, client = _provider(_text_response("ok"))

    await provider.complete(messages=[{"role": "user", "content": "hi"}])

    assert client.messages.last_kwargs["output_config"]["effort"] == "low"


async def test_effort_is_overridable() -> None:
    provider, client = _provider(_text_response("ok"), effort="medium")

    await provider.complete(messages=[{"role": "user", "content": "hi"}])

    assert client.messages.last_kwargs["output_config"]["effort"] == "medium"


# --- correction #1: schema tightened with additionalProperties: false ------------


async def test_schema_request_is_tightened_with_additional_properties_false() -> None:
    provider, client = _provider(_text_response('{"findings": []}'))

    await provider.complete(
        messages=[{"role": "user", "content": "hi"}], response_schema=SCHEMA
    )

    sent_schema = client.messages.last_kwargs["output_config"]["format"]["schema"]
    assert sent_schema["additionalProperties"] is False
    assert sent_schema["properties"]["findings"]["items"]["additionalProperties"] is False
    # The original module-level SCHEMA constant must be untouched.
    assert "additionalProperties" not in SCHEMA


async def test_no_format_key_sent_when_no_schema_requested() -> None:
    provider, client = _provider(_text_response("plain text"))

    await provider.complete(messages=[{"role": "user", "content": "hi"}])

    assert "format" not in client.messages.last_kwargs["output_config"]


def test_tighten_schema_is_recursive_and_non_mutating() -> None:
    nested = {
        "type": "object",
        "properties": {
            "items": {"type": "array", "items": {"type": "object", "properties": {}}},
        },
    }

    tightened = _tighten_schema(nested)

    assert tightened["additionalProperties"] is False
    assert tightened["properties"]["items"]["items"]["additionalProperties"] is False
    assert "additionalProperties" not in nested  # original untouched


def test_tighten_schema_respects_an_explicit_additional_properties() -> None:
    explicit = {"type": "object", "additionalProperties": True}

    assert _tighten_schema(explicit)["additionalProperties"] is True


def test_tighten_schema_leaves_non_object_nodes_alone() -> None:
    assert _tighten_schema({"type": "string"}) == {"type": "string"}
    assert _tighten_schema("scalar") == "scalar"
    assert _tighten_schema([{"type": "string"}]) == [{"type": "string"}]


# --- parsing the response back into a ModelResult ---------------------------------


async def test_schema_request_returns_parsed_data_and_schema_valid() -> None:
    provider, _ = _provider(_text_response('{"findings": [{"type": "learning", "text": "x"}]}'))

    result = await provider.complete(
        messages=[{"role": "user", "content": "hi"}], response_schema=SCHEMA
    )

    assert result.data == {"findings": [{"type": "learning", "text": "x"}]}
    assert result.schema_valid is True


async def test_unparseable_text_falls_back_to_raw_text_with_schema_invalid() -> None:
    provider, _ = _provider(_text_response("not json at all"))

    result = await provider.complete(
        messages=[{"role": "user", "content": "hi"}], response_schema=SCHEMA
    )

    assert result.data == "not json at all"
    assert result.schema_valid is False


async def test_no_schema_requested_returns_raw_text_with_schema_valid_true() -> None:
    provider, _ = _provider(_text_response("plain text answer"))

    result = await provider.complete(messages=[{"role": "user", "content": "hi"}])

    assert result.data == "plain text answer"
    assert result.schema_valid is True


# --- correction #4: refusal handling -----------------------------------------------


async def test_refusal_yields_schema_invalid_without_raising() -> None:
    """A safety-classifier decline is HTTP 200 with stop_reason == 'refusal',
    not an exception -- developer transcripts contain security tooling and
    credentials, so this WILL happen. Must never propagate as an exception."""
    response = _FakeResponse(content=[], usage=_FakeUsage(), stop_reason="refusal")
    provider, _ = _provider(response)

    result = await provider.complete(
        messages=[{"role": "user", "content": "hi"}], response_schema=SCHEMA
    )

    assert result.schema_valid is False
    assert result.provider_id == "anthropic"


async def test_refusal_with_partial_content_reports_the_partial_text() -> None:
    """A mid-stream decline leaves partial content rather than none."""
    response = _FakeResponse(
        content=[_FakeBlock(text="partial answer before refusal")],
        usage=_FakeUsage(),
        stop_reason="refusal",
    )
    provider, _ = _provider(response)

    result = await provider.complete(messages=[{"role": "user", "content": "hi"}])

    assert result.data == "partial answer before refusal"
    assert result.schema_valid is False


# --- correction #5: usage sums all three token fields ------------------------------


async def test_usage_sums_uncached_cache_write_and_cache_read_tokens() -> None:
    """Anthropic's usage.input_tokens is the UNCACHED remainder only -- the
    TRUE prompt size assert_prompt_conditioned/check_canary depend on is the
    sum of all three fields."""
    response = _text_response(
        "ok",
        input_tokens=100,
        cache_creation_input_tokens=50,
        cache_read_input_tokens=25,
        output_tokens=10,
    )
    provider, _ = _provider(response)

    result = await provider.complete(messages=[{"role": "user", "content": "hi"}])

    assert result.usage.input_tokens == 175
    assert result.usage.output_tokens == 10


async def test_usage_with_no_caching_matches_bare_input_tokens() -> None:
    """When prompt caching is never used (cache fields are 0), the sum
    degrades to plain input_tokens -- the common case today."""
    response = _text_response("ok", input_tokens=809, output_tokens=42)
    provider, _ = _provider(response)

    result = await provider.complete(messages=[{"role": "user", "content": "hi"}])

    assert result.usage.input_tokens == 809
    assert result.usage.output_tokens == 42


async def test_latency_and_provider_id_are_populated() -> None:
    provider, _ = _provider(_text_response("ok"))

    result = await provider.complete(messages=[{"role": "user", "content": "hi"}])

    assert result.latency_ms >= 0
    assert result.provider_id == "anthropic"
