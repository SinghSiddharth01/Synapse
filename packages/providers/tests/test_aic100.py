"""The two probed Cirrascale gotchas, pinned:
   1. schema calls must route via /completions (chat eats JSON into empty tool_calls)
   2. response_format must never be sent (silently ignored -> false confidence)"""
import json

import httpx
import pytest

from synapse_providers.aic100 import (AIC100Provider, _satisfies_schema,
                                      extract_first_json_object)

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


async def test_schema_valid_requires_declared_keys_not_just_valid_json():
    """A 200 with a parseable-but-unrelated JSON object must not be reported
    schema_valid=True -- that's the exact false-confidence trap the module's
    own docstring says a 200 proves nothing was enforced about, one level
    up: 'it parsed' likewise proves nothing was satisfied."""
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={
            "choices": [{"text": '{"totally": "unrelated"}'}],
            "usage": {"prompt_tokens": 4, "completion_tokens": 4}})
    result = await _provider(handler).complete(
        [{"role": "user", "content": "u"}], response_schema=SCHEMA)
    assert result.schema_valid is False
    assert result.data is None


async def test_json_extraction_survives_an_unbalanced_brace_inside_a_string():
    """extract_first_json_object used to brace-count with no string
    awareness, so valid JSON whose string values contain an unbalanced
    brace (routine for working_memory, free prose an 8B writes about code)
    failed to parse and cost a full retry for nothing."""
    schema = {"type": "object",
              "properties": {"working_memory": {"type": "string"}, "merges": {"type": "array"}},
              "required": ["working_memory", "merges"]}
    text = ('{"working_memory": "the guard closes with } before the return", '
            '"merges": []}')

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={
            "choices": [{"text": text}],
            "usage": {"prompt_tokens": 4, "completion_tokens": 4}})
    result = await _provider(handler).complete(
        [{"role": "user", "content": "u"}], response_schema=schema)
    assert result.schema_valid is True
    assert result.data == {"working_memory": "the guard closes with } before the return",
                           "merges": []}


@pytest.mark.parametrize("text,expected", [
    ('{"ok": true}', {"ok": True}),
    ('Sure! {"ok": true} hope that helps', {"ok": True}),
    ('Here is the fix: if (x) { doThing(); }\n{"ok": true}', {"ok": True}),
    ('{"ok": true}\nExample: func() { return; }', {"ok": True}),
    ('{"ok": true}\n\nNote: {"unused": 1}', {"ok": True}),
    ('{"a":1} {"b":2}', {"a": 1}),
    ('{"working_memory": "closes with } before return", "merges": []}',
     {"working_memory": "closes with } before return", "merges": []}),
])
def test_json_extraction_finds_the_first_object_around_narration_and_code(text, expected):
    """A Llama-3.1-8B answering a /completions prompt that ends in 'JSON:'
    routinely narrates, shows a code snippet with its own braces, or
    appends a trailing example -- all of which put a second, unrelated
    brace pair in the response. The extractor must still find the FIRST
    balanced JSON object rather than reaching for the outermost brace span
    (which any second brace pair poisons), while still tolerating an
    unbalanced brace INSIDE a string value (the case the aliasing to
    openai_compat's _parse_json_tolerantly was originally meant to fix)."""
    assert extract_first_json_object(text) == expected


MULTI_KEY_SCHEMA = {
    "type": "object",
    "properties": {"working_memory": {"type": "string"}, "merges": {"type": "array"},
                   "trivial_ids": {"type": "array"}, "conflicts": {"type": "array"}},
    "required": ["working_memory", "merges", "trivial_ids", "conflicts"],
}


def test_satisfies_schema_tolerates_a_verdict_missing_one_of_several_required_keys():
    """synthesis.py reads every one of SYNTH_SCHEMA's keys with
    verdicts.get(key, default) precisely so an 8B dropping one key doesn't
    lose the rest of an otherwise-usable verdict. But the TOP-level gate in
    complete() (schema_valid) is all-or-nothing: if _satisfies_schema
    required EVERY declared key, a response with working_memory + merges
    but no 'conflicts' key would be reported schema_valid=False and
    synthesis.py would discard the whole round -- the exact partial verdict
    its own .get()-based tolerance exists to use. Require that at least one
    declared key is present (proving the object is schema-SHAPED) rather
    than all of them; a response matching none of them is the real
    false-confidence case ('parsed' but unrelated)."""
    partial = {"working_memory": "wm", "merges": []}          # trivial_ids, conflicts missing
    assert _satisfies_schema(partial, MULTI_KEY_SCHEMA) is True


def test_satisfies_schema_still_rejects_a_response_matching_none_of_the_keys():
    unrelated = {"totally": "unrelated"}
    assert _satisfies_schema(unrelated, MULTI_KEY_SCHEMA) is False


async def test_the_retry_is_not_a_byte_identical_resend():
    """Against a deterministic decode, an unconditional identical retry at
    temperature 0.0 is a guaranteed-identical second failure that doubles
    consumption of the shared credit pool for nothing. The retry must
    change something."""
    bodies = []

    def handler(request: httpx.Request) -> httpx.Response:
        bodies.append(json.loads(request.content))
        return httpx.Response(200, json={"choices": [{"text": "no json here at all"}],
                                         "usage": {"prompt_tokens": 4, "completion_tokens": 4}})
    result = await _provider(handler).complete(
        [{"role": "user", "content": "u"}], response_schema=SCHEMA)
    assert len(bodies) == 2
    assert bodies[0] != bodies[1]
    assert result.schema_valid is False and result.data is None
