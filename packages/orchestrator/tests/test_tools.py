from datetime import datetime, timezone

import httpx
import pytest
from synapse_contracts import Attribution, Finding, LocalBinding
from synapse_providers import FakeProvider

from synapse_orchestrator.briefing import build_briefing
from synapse_orchestrator.relay import Relay
from synapse_orchestrator.server import (
    SENTINEL,
    _DEFAULT_INSTRUCTIONS,
    create_mcp,
    register_tools,
)

BINDING = LocalBinding(agent_session_id="as-1", shared_id="sh-1",
                       contributor="aditya", agent="claude-code")
TS = datetime(2026, 8, 4, tzinfo=timezone.utc)


async def test_briefing_reflects_the_watermark_and_fails_open():
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/v1/sessions/sh-1/watermark"
        # Invariant 3 (CONTEXT.md): the watermark's `new_since` is per-Agent
        # Session by definition — the service can only enforce that if this
        # field actually arrives. Round 2 review: dropping this query param
        # entirely left the whole suite green (a path-only assertion, same
        # class of gap the sibling `/query` hop already guards against).
        assert request.url.params["agent_session"] == "as-1"
        return httpx.Response(200, json={"version": 3, "new_since": 2,
                                         "by_type": {"learning": 4, "dead_end": 1},
                                         "conflicts": 1})
    text = await build_briefing(BINDING, "http://svc",
                                transport=httpx.MockTransport(handler))
    assert SENTINEL in text and "sh-1" in text
    assert "5 findings" in text and "1 conflict" in text and "v3" in text

    def down(request):  # service dead -> default text, never an exception
        raise httpx.ConnectError("down")
    text = await build_briefing(BINDING, "http://svc",
                                transport=httpx.MockTransport(down))
    # Not just "SENTINEL is present" — SENTINEL lives in BOTH the default text
    # AND the live-watermark template above, so that alone can't tell "fell
    # back to default" apart from "rendered something else entirely". Pin the
    # whole string.
    assert text == _DEFAULT_INSTRUCTIONS


@pytest.mark.parametrize("body", [
    [],                                          # top-level list, not an object
    {"by_type": []},                              # by_type not a dict
    {"by_type": {"learning": "a lot"}},            # by_type values not summable
], ids=["top_level_list", "by_type_is_a_list", "by_type_values_not_numeric"])
async def test_briefing_fails_open_on_a_malformed_watermark_body(body):
    """E3 is not merged; a 200 whose shape doesn't match what this module
    assumes must fail open, not raise and take the orchestrator process down
    with it (build_briefing runs in cli.main before uvicorn.serve starts)."""
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=body)
    text = await build_briefing(BINDING, "http://svc",
                                transport=httpx.MockTransport(handler))
    assert text == _DEFAULT_INSTRUCTIONS


async def test_query_tool_calls_the_service_and_formats_findings(tmp_path):
    captured_body = {}
    urls_hit = []
    def handler(request: httpx.Request) -> httpx.Response:
        urls_hit.append(str(request.url))
        if request.url.path.endswith("/query"):
            import json as _json
            captured_body.update(_json.loads(request.content))
            f = Finding(id="f-9", type="learning", text="the 40ms window",
                        attributions=[Attribution(contributor="akhil",
                                                  agent_session="as-2", agent="codex")],
                        ts=TS)
            return httpx.Response(200, json={"findings": [f.model_dump(mode="json")]})
        raise AssertionError(request.url.path)
    server = create_mcp()
    relay = Relay(tmp_path, "http://svc", "sh-1", transport=httpx.MockTransport(handler))
    register_tools(server, binding=BINDING, service_url="http://svc", relay=relay,
                   distiller_factory=lambda: None,
                   transport=httpx.MockTransport(handler))
    result = await server.call_tool("query", {"question": "timing?"})
    text = str(result)
    assert "40ms window" in text and "akhil" in text
    # Invariant 3 (CONTEXT.md): suppression is scoped to the Agent Session, not
    # the Contributor. The service can only enforce that if this field actually
    # arrives — a body-shape assertion, not just a path assertion.
    assert captured_body == {"query": "timing?", "agent_session": "as-1"}
    # Which Shared Session gets queried is the routing decision that matters
    # most here — pin the exact URL, not just that SOME request happened
    # (round 2 review: a hardcoded WRONG-SESSION url survived every test).
    assert urls_hit == ["http://svc/v1/sessions/sh-1/query"]


async def test_query_tool_credits_every_contributor_on_a_synthesized_finding(tmp_path):
    """A Synthesized Finding carries every source's Attribution (CONTEXT.md).
    Rendering only attributions[0] would make a three-way pooled insight read
    as one person's — and in the common case, as the asking agent's own."""
    def handler(request: httpx.Request) -> httpx.Response:
        f = Finding(id="f-pooled", type="learning", text="pooled insight from three people",
                    provenance="synthesized", merged_from=["a", "b", "c"],
                    attributions=[
                        Attribution(contributor="aditya", agent_session="as-1", agent="claude-code"),
                        Attribution(contributor="akhil", agent_session="as-2", agent="codex"),
                        Attribution(contributor="sid", agent_session="as-3", agent="claude-code"),
                    ], ts=TS)
        return httpx.Response(200, json={"findings": [f.model_dump(mode="json")]})
    server = create_mcp()
    relay = Relay(tmp_path, "http://svc", "sh-1", transport=httpx.MockTransport(handler))
    register_tools(server, binding=BINDING, service_url="http://svc", relay=relay,
                   distiller_factory=lambda: None, transport=httpx.MockTransport(handler))
    text = str(await server.call_tool("query", {"question": "anything?"}))
    assert "aditya" in text and "akhil" in text and "sid" in text


@pytest.mark.parametrize("body", [
    {"nope": "wrong envelope key"},               # no "findings" key at all
    {"findings": [{"type": "learning"}]},          # finding missing 'text'/'attributions'
    {"findings": [{"type": "learning", "text": "x", "attributions": []}]},  # no attributions
], ids=["wrong_envelope_key", "finding_missing_fields", "empty_attributions"])
async def test_query_tool_does_not_report_a_false_negative_on_a_shape_mismatch(tmp_path, body):
    """resp.json().get('findings', []) used to default to [] on ANY shape
    mismatch, rendering the confident "(Checked — not skipped.)" negative for
    what is actually a parse failure — the one message query() must never
    produce by accident, since it exists to make the agent stop looking."""
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=body)
    server = create_mcp()
    relay = Relay(tmp_path, "http://svc", "sh-1", transport=httpx.MockTransport(handler))
    register_tools(server, binding=BINDING, service_url="http://svc", relay=relay,
                   distiller_factory=lambda: None, transport=httpx.MockTransport(handler))
    text = str(await server.call_tool("query", {"question": "anything?"}))
    assert "Checked — not skipped" not in text
    assert "couldn't parse" in text


async def test_contribute_round_trips_through_the_distiller_and_relay(tmp_path):
    from synapse_contracts import Provenance
    from synapse_distiller import Distiller

    sent_to_service = []
    urls_hit = []
    def handler(request: httpx.Request) -> httpx.Response:
        import json as _json
        urls_hit.append(str(request.url))
        sent_to_service.append(_json.loads(request.content))
        return httpx.Response(200, json={"accepted": 1, "memory_version": 1})

    fake = FakeProvider(scripts=[{"findings": [
        {"type": "learning", "text": "contributed insight about the retry backoff"}]}])
    server = create_mcp()
    relay = Relay(tmp_path, "http://svc", "sh-1", transport=httpx.MockTransport(handler))
    register_tools(server, binding=BINDING, service_url="http://svc", relay=relay,
                   distiller_factory=lambda: Distiller(fake, BINDING),
                   transport=httpx.MockTransport(handler))
    await server.call_tool("contribute", {"text": "the retry backoff matters because…"})

    [pushed] = sent_to_service
    [finding] = pushed["findings"]
    assert finding["provenance"] == Provenance.CONTRIBUTED.value
    assert finding["attributions"][0]["contributor"] == "aditya"
    # Which Shared Session contribute()'s finding is pushed to is the whole
    # point of it landing in team memory at all — pin the exact URL (round 2
    # review: a hardcoded WRONG-SESSION url survived every test).
    assert urls_hit == ["http://svc/v1/sessions/sh-1/findings"]
