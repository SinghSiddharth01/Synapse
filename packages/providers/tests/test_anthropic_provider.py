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

import pytest

from synapse_providers.anthropic_provider import (
    DEFAULT_MAX_TOKENS,
    HAIKU_MAX_TOKENS,
    MAX_TOKENS_ENV,
    AnthropicProvider,
    _tighten_schema,
    default_max_tokens_for,
    supports_effort,
)

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


# --- correction #6: `effort` only where the endpoint accepts it -------------------
#
# `effort` is not a tuning knob that degrades gracefully -- it is a 400 on Haiku
# 4.5 and Sonnet 4.5. claude-haiku-4-5 is the arm the 4096 pin below exists for,
# so sent unconditionally it meant that arm failed on its FIRST request and
# every max_tokens assertion in this file was pinning a cap on an arm that had
# never run. The tests here are on the outbound request body for exactly that
# reason: `_FakeClient.messages.create(**kwargs)` accepts any body at all, so
# nothing else in this file can tell the difference.


@pytest.mark.parametrize(
    "model",
    [
        "claude-haiku-4-5",
        "claude-haiku-4-5-20251001",
        "CLAUDE-HAIKU-4-5",
        "claude-sonnet-4-5",
    ],
)
async def test_no_effort_is_sent_to_a_model_that_rejects_it(model: str) -> None:
    """The demo's distiller arm. Swept over both Haiku spellings this repo
    uses, the upper-case form, and Sonnet 4.5 -- the other model on the same
    row of the same table."""
    provider, client = _provider(_text_response("ok"), model=model)

    await provider.complete(messages=[{"role": "user", "content": "hi"}])

    assert "effort" not in client.messages.last_kwargs.get("output_config", {})


@pytest.mark.parametrize(
    "model",
    ["claude-opus-5", "claude-opus-4-5", "claude-sonnet-5", "claude-fable-5"],
)
async def test_effort_is_still_sent_to_every_model_that_accepts_it(model: str) -> None:
    """The other direction, and the guard on the gate. A fix that just deleted
    the field would pass the test above and quietly stop keeping thinking
    shallow on the default arm -- paying Opus-depth reasoning on every segment
    of every session, silently, with no failing test anywhere.
    """
    provider, client = _provider(_text_response("ok"), model=model)

    await provider.complete(messages=[{"role": "user", "content": "hi"}])

    assert client.messages.last_kwargs["output_config"]["effort"] == "low"


async def test_a_haiku_request_carries_no_output_config_at_all_without_a_schema() -> None:
    """`{}` is not what "no output configuration" looks like on the wire, and
    an empty object is a shape the endpoint has no reason to accept."""
    provider, client = _provider(_text_response("ok"), model="claude-haiku-4-5")

    await provider.complete(messages=[{"role": "user", "content": "hi"}])

    assert "output_config" not in client.messages.last_kwargs


async def test_haiku_still_gets_schema_enforcement_it_only_loses_effort() -> None:
    """The gate is on the KEY, not the container -- structured output IS
    supported on Haiku 4.5. Dropping `output_config` wholesale for Haiku would
    have fixed the 400 by removing schema enforcement from the one arm this
    provider pins a cap for, handing the Distiller un-enforced JSON and moving
    the failure somewhere quieter.
    """
    provider, client = _provider(
        _text_response('{"findings": []}'), model="claude-haiku-4-5"
    )

    await provider.complete(
        messages=[{"role": "user", "content": "hi"}], response_schema=SCHEMA
    )

    sent = client.messages.last_kwargs["output_config"]
    assert sent["format"]["type"] == "json_schema"
    assert sent["format"]["schema"]["additionalProperties"] is False
    assert "effort" not in sent


def test_an_unknown_model_omits_effort_rather_than_risking_the_request() -> None:
    """The fallback direction, stated on its own because it is the opposite of
    the max-tokens table's and the two sit four lines apart. That one falls
    back to the generous default so an unlisted model still works; this one
    falls back to omitting, because a model nobody listed might be the next
    Haiku and losing a tuning knob is cheaper than losing the arm.
    """
    assert supports_effort("some-model-released-next-month") is False
    assert supports_effort("claude-opus-4-1") is False, (
        "predates the parameter -- and the reason the fragments are "
        "version-specific rather than a bare 'opus'"
    )
    assert supports_effort("claude-opus-5") is True


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


# --- the Haiku output-cap pin (decisions/006) ------------------------------------
#
# 16000 is sized for Opus 5, where max_tokens caps thinking PLUS text and
# thinking is on by default. Haiku carries no such thinking overhead, so the
# same ceiling is several times more room than a findings object can use — and
# several times the exposure when a small model degenerates into a repetition
# loop, the failure `distiller.classify_drop` exists to name. A cap is the only
# thing that bounds a loop's cost.
#
# The pin lives in the PROVIDER, not in config/synapse.toml, because synthesis
# reads `provider.max_tokens` (`SynthesisBudget.for_provider`) and never reads a
# capability record — a config-only cap would be inert on the arm it was written
# for. Full reasoning and the undo path: docs/overnight/decisions/006.


@pytest.fixture(autouse=True)
def _no_anthropic_env(monkeypatch):
    """Both variables this provider reads, cleared. `SYNAPSE_ANTHROPIC_MODEL`
    matters as much as the max-tokens one: an exported Haiku model in the
    runner's environment would silently turn the Opus-default assertions in
    this file into Haiku assertions, and they would keep passing while
    asserting the wrong thing."""
    monkeypatch.delenv("SYNAPSE_ANTHROPIC_MODEL", raising=False)
    monkeypatch.delenv(MAX_TOKENS_ENV, raising=False)


@pytest.mark.parametrize(
    "model",
    [
        "claude-haiku-4-5",
        "claude-haiku-4-5-20251001",
        "claude-haiku-5",
        "CLAUDE-HAIKU-4-5",
    ],
)
async def test_every_haiku_spelling_gets_the_pinned_cap(model: str) -> None:
    """Swept, and this is the whole reason the table matches a substring.

    The arm is chosen by SYNAPSE_ANTHROPIC_MODEL — free text — and this repo
    already carries two spellings (scripts/serve_local.py's help text says
    `claude-haiku-4-5-20251001`; the API alias is `claude-haiku-4-5`). An
    exact-match table would fall back to 16000 for whichever spelling it did
    not list, which is failing OPEN on the number that costs money. A dated
    successor id is swept for the same reason.
    """
    provider, client = _provider(_text_response("ok"), model=model)

    await provider.complete(messages=[{"role": "user", "content": "hi"}])

    assert provider.max_tokens == HAIKU_MAX_TOKENS == 4096
    assert client.messages.last_kwargs["max_tokens"] == HAIKU_MAX_TOKENS


async def test_the_pin_applies_when_the_model_comes_from_the_environment(
    monkeypatch,
) -> None:
    """THE path that matters. All three call sites — worker, orchestrator,
    service — construct `AnthropicProvider()` with NO arguments and select the
    model purely through SYNAPSE_ANTHROPIC_MODEL, so a pin that only fired for
    an explicit `model=` argument would never fire in production."""
    monkeypatch.setenv("SYNAPSE_ANTHROPIC_MODEL", "claude-haiku-4-5-20251001")

    provider, client = _provider(_text_response("ok"))
    await provider.complete(messages=[{"role": "user", "content": "hi"}])

    assert provider.max_tokens == HAIKU_MAX_TOKENS
    assert client.messages.last_kwargs["max_tokens"] == HAIKU_MAX_TOKENS


@pytest.mark.parametrize(
    "model",
    ["claude-opus-5", "claude-sonnet-5", "claude-opus-4-8", "some-unlisted-model"],
)
async def test_no_other_model_is_lowered_by_the_pin(model: str) -> None:
    """The false-positive direction, and the guard on the substring match.

    A per-model cap that quietly applied to everything would be a blanket
    lowering — it would cut Opus 5 to a quarter of the room its always-on
    thinking needs, and the JSON would come back truncated mid-object. An
    unlisted model must keep the generous default rather than inherit the
    tightest entry in the table.
    """
    provider, client = _provider(_text_response("ok"), model=model)

    await provider.complete(messages=[{"role": "user", "content": "hi"}])

    assert provider.max_tokens == DEFAULT_MAX_TOKENS == 16000
    assert client.messages.last_kwargs["max_tokens"] == DEFAULT_MAX_TOKENS


def test_the_pin_is_a_real_reduction_not_a_restatement() -> None:
    """The ADR-0005 trap for this change: if HAIKU_MAX_TOKENS were ever edited
    to equal DEFAULT_MAX_TOKENS, every test above would still pass while the
    pin did nothing at all."""
    assert HAIKU_MAX_TOKENS < DEFAULT_MAX_TOKENS
    assert default_max_tokens_for("claude-haiku-4-5") != default_max_tokens_for(
        "claude-opus-5"
    )


def test_the_pinned_cap_is_ours_and_not_the_endpoints() -> None:
    """Recorded as an assertion because a docstring alone gets skimmed.

    `claude-haiku-4-5` serves a 200K context and will return up to 64K output
    tokens — 4096 is a spend and shape decision of ours. A number that looks
    like a platform limit stops being questioned, and this is the one an
    operator is most likely to want to move.
    """
    assert HAIKU_MAX_TOKENS < 64000, (
        "if this ever equals Haiku's real output ceiling the pin has become a "
        "restatement of the endpoint rather than a decision — see decisions/006"
    )


# --- the config override ---------------------------------------------------------


@pytest.mark.parametrize("model", ["claude-haiku-4-5", "claude-opus-5"])
async def test_the_env_override_wins_on_every_arm(monkeypatch, model: str) -> None:
    """`SYNAPSE_ANTHROPIC_MAX_TOKENS`, named to match AIC100Provider's
    INFERENCE_CLOUD_MAX_TOKENS, which exists for exactly this reason and records
    it: the worker, the orchestrator, and the service all construct this
    provider with no arguments, so a constructor default is reachable only from
    Python and cannot be changed on a running deployment.

    Asserted on both arms, because an override that only worked where the pin
    applies would leave the default arm unadjustable — the very situation the
    variable exists to end.
    """
    monkeypatch.setenv(MAX_TOKENS_ENV, "2048")

    provider, client = _provider(_text_response("ok"), model=model)
    await provider.complete(messages=[{"role": "user", "content": "hi"}])

    assert provider.max_tokens == 2048
    assert client.messages.last_kwargs["max_tokens"] == 2048


async def test_the_override_can_raise_the_cap_as_well_as_lower_it(monkeypatch) -> None:
    """A pin nobody can loosen is a ceiling, not a default. If a Haiku run
    genuinely needs more room, the override has to be able to grant it —
    otherwise the only way past 4096 is a code change and a redeploy."""
    monkeypatch.setenv(MAX_TOKENS_ENV, "32000")

    provider, _ = _provider(_text_response("ok"), model="claude-haiku-4-5")

    assert provider.max_tokens == 32000
    assert provider.max_tokens > HAIKU_MAX_TOKENS


async def test_an_explicit_argument_still_beats_the_per_model_pin() -> None:
    """The pin is a DEFAULT, not a clamp. The NPU arm and ClaudeCliProvider are
    both handed an explicit number by their caller; this provider must behave
    the same way when asked."""
    provider, client = _provider(
        _text_response("ok"), model="claude-haiku-4-5", max_tokens=9001
    )

    await provider.complete(messages=[{"role": "user", "content": "hi"}])

    assert provider.max_tokens == 9001
    assert client.messages.last_kwargs["max_tokens"] == 9001


async def test_an_empty_override_is_not_read_as_a_cap_of_zero(monkeypatch) -> None:
    """An exported-but-empty variable is an ordinary shell accident, and
    `int("")` raises. Falling through to the resolved default is the only safe
    reading — a cap of 0 would make every response empty."""
    monkeypatch.setenv(MAX_TOKENS_ENV, "")

    provider, _ = _provider(_text_response("ok"), model="claude-haiku-4-5")

    assert provider.max_tokens == HAIKU_MAX_TOKENS
