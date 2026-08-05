"""The two probed Cirrascale gotchas, pinned:
   1. schema calls must route via /completions (chat eats JSON into empty tool_calls)
   2. response_format must never be sent (silently ignored -> false confidence)"""
import json

import httpx
import pytest

from synapse_providers.aic100 import AIC100Provider

SCHEMA = {"type": "object", "properties": {"ok": {"type": "boolean"}}, "required": ["ok"]}


def _provider(handler) -> AIC100Provider:
    return AIC100Provider(base_url="https://fake.cirrascale.test/apis/v2",
                          api_key="k", transport=httpx.MockTransport(handler))


async def test_plain_call_uses_chat_completions():
    seen = {}
    def handler(request: httpx.Request) -> httpx.Response:
        seen["path"] = request.url.path
        seen["body"] = json.loads(request.content)
        return httpx.Response(200, json={"choices": [{"message": {"content": "Pong"}}],
                                         "usage": {"prompt_tokens": 5, "completion_tokens": 1}})
    result = await _provider(handler).complete([{"role": "user", "content": "ping"}])
    assert seen["path"].endswith("/chat/completions")
    assert "response_format" not in seen["body"]          # gotcha 2
    assert result.data == "Pong" and result.usage.input_tokens == 5


async def test_schema_call_routes_via_completions_and_extracts_json():
    seen = {}
    def handler(request: httpx.Request) -> httpx.Response:
        seen["path"] = request.url.path
        seen["body"] = json.loads(request.content)
        return httpx.Response(200, json={
            "choices": [{"text": "Sure! Here you go: {\"ok\": true} hope that helps"}],
            "usage": {"prompt_tokens": 40, "completion_tokens": 12}})
    result = await _provider(handler).complete(
        [{"role": "system", "content": "sys"}, {"role": "user", "content": "u"}],
        response_schema=SCHEMA)
    assert seen["path"].endswith("/completions") and "/chat/" not in seen["path"]
    assert "sys" in seen["body"]["prompt"] and "u" in seen["body"]["prompt"]  # flattened
    assert "max_tokens" in seen["body"]                    # credit-pool bound, always
    assert result.data == {"ok": True} and result.schema_valid


async def test_unparseable_json_retries_once_then_reports_invalid():
    calls = {"n": 0}
    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        return httpx.Response(200, json={"choices": [{"text": "no json here at all"}],
                                         "usage": {"prompt_tokens": 4, "completion_tokens": 4}})
    result = await _provider(handler).complete(
        [{"role": "user", "content": "u"}], response_schema=SCHEMA)
    assert calls["n"] == 2
    assert result.schema_valid is False and result.data is None


async def test_capabilities_are_honest():
    provider = AIC100Provider(base_url="https://x", api_key="k")
    assert provider.capabilities.native_structured_output is False
