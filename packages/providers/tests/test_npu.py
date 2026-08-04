"""NPUProvider / OpenAICompatibleProvider tests — Plan B Task B.4.

Runs against pytest-httpserver, never against real hardware, so the suite
stays CI-safe and offline.
"""

from __future__ import annotations

import json

from pytest_httpserver import HTTPServer

from synapse_providers import NPUProvider
from synapse_providers.openai_compat import _parse_json_tolerantly

SCHEMA = {"type": "object", "properties": {"findings": {"type": "array"}}}


def _chat_response(content: str, *, prompt_tokens: int = 42) -> dict:
    return {
        "choices": [{"message": {"content": content}, "finish_reason": "stop"}],
        "usage": {"prompt_tokens": prompt_tokens, "completion_tokens": 7},
    }


def _provider(httpserver: HTTPServer) -> NPUProvider:
    return NPUProvider(base_url=httpserver.url_for("/v1"), model="test-model")


async def test_returns_text_and_populates_usage_and_latency(httpserver: HTTPServer) -> None:
    httpserver.expect_request("/v1/chat/completions").respond_with_json(
        _chat_response("plain text answer", prompt_tokens=44)
    )

    result = await _provider(httpserver).complete(
        messages=[{"role": "user", "content": "hi"}]
    )

    assert result.data == "plain text answer"
    assert result.provider_id == "npu"
    assert result.usage.input_tokens == 44
    assert result.usage.output_tokens == 7
    assert result.latency_ms >= 0


async def test_capability_flag_is_false_by_measurement(httpserver: HTTPServer) -> None:
    assert _provider(httpserver).capabilities.native_structured_output is False


async def test_schema_request_does_not_send_response_format(httpserver: HTTPServer) -> None:
    """The provider must not send a field it has no evidence is honoured.

    GenieX accepts unknown parameters silently, so sending response_format would
    produce a request that looks enforced and is not.
    """
    captured: list[dict] = []

    def handler(request):
        captured.append(json.loads(request.data))
        from werkzeug.wrappers import Response

        return Response(
            json.dumps(_chat_response('{"findings": []}')),
            content_type="application/json",
        )

    httpserver.expect_request("/v1/chat/completions").respond_with_handler(handler)

    await _provider(httpserver).complete(messages=[], response_schema=SCHEMA)

    assert "response_format" not in captured[0]
    assert captured[0]["temperature"] == 0.0


async def test_schema_request_falls_back_to_tolerant_parse(httpserver: HTTPServer) -> None:
    httpserver.expect_request("/v1/chat/completions").respond_with_json(
        _chat_response('Here you go:\n```json\n{"findings": [{"type": "learning"}]}\n```')
    )

    result = await _provider(httpserver).complete(messages=[], response_schema=SCHEMA)

    assert result.schema_valid is True
    assert result.data == {"findings": [{"type": "learning"}]}


async def test_unparseable_output_reports_schema_invalid(httpserver: HTTPServer) -> None:
    httpserver.expect_request("/v1/chat/completions").respond_with_json(
        _chat_response("I could not produce JSON, sorry.")
    )

    result = await _provider(httpserver).complete(messages=[], response_schema=SCHEMA)

    assert result.schema_valid is False


def test_tolerant_parse_recovers_the_shapes_small_models_actually_emit() -> None:
    expected = {"findings": []}

    assert _parse_json_tolerantly('{"findings": []}') == expected
    assert _parse_json_tolerantly('```json\n{"findings": []}\n```') == expected
    assert _parse_json_tolerantly('```\n{"findings": []}\n```') == expected
    assert _parse_json_tolerantly('Sure!\n{"findings": []}\nHope that helps.') == expected
    assert _parse_json_tolerantly("no json at all") is None
    assert _parse_json_tolerantly("") is None
