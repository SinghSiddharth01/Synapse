from datetime import datetime, timezone

import httpx
import pytest
from synapse_contracts import Attribution, Finding, LocalBinding
from synapse_providers import FakeProvider

from synapse_orchestrator.briefing import build_briefing
from synapse_orchestrator.relay import Relay
from synapse_orchestrator.server import SENTINEL, create_mcp, register_tools

BINDING = LocalBinding(agent_session_id="as-1", shared_id="sh-1",
                       contributor="aditya", agent="claude-code")
TS = datetime(2026, 8, 4, tzinfo=timezone.utc)


async def test_briefing_reflects_the_watermark_and_fails_open():
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/v1/sessions/sh-1/watermark"
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
    assert SENTINEL in text                     # fail-open default


async def test_query_tool_calls_the_service_and_formats_findings(tmp_path):
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/query"):
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


async def test_contribute_round_trips_through_the_distiller_and_relay(tmp_path):
    from synapse_contracts import Provenance
    from synapse_distiller import Distiller

    sent_to_service = []
    def handler(request: httpx.Request) -> httpx.Response:
        import json as _json
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
