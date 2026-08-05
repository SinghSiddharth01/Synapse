import json
from datetime import datetime, timezone

import httpx
from starlette.testclient import TestClient

from synapse_orchestrator.app import build_app
from synapse_orchestrator.relay import Relay

TS = "2026-08-04T12:00:00Z"
FINDING = {"id": "f-1", "type": "learning", "text": "insight",
           "attributions": [{"contributor": "aditya", "agent_session": "as-1",
                             "agent": "claude-code"}],
           "ts": TS, "refs": [], "provenance": "distilled", "status": "kept",
           "merged_from": [], "merged_into": None}


def _app(tmp_path, handler):
    relay = Relay(tmp_path, "http://svc", "sh-1", transport=httpx.MockTransport(handler))
    return build_app(relay)


def test_findings_are_accepted_recorded_and_forwarded(tmp_path):
    forwarded = []
    def handler(request: httpx.Request) -> httpx.Response:
        forwarded.append(json.loads(request.content))
        return httpx.Response(200, json={"accepted": 1, "memory_version": 1})
    with TestClient(_app(tmp_path, handler)) as client:
        resp = client.post("/producer/findings", json={"findings": [FINDING]})
    assert resp.status_code == 200
    assert resp.json() == {"accepted": 1, "sent": True}
    assert forwarded[0]["findings"][0]["id"] == "f-1"
    assert (tmp_path / "findings.jsonl").exists()           # write-ahead happened


def test_egress_rule_rejects_non_finding_payloads(tmp_path):
    def handler(request):                                    # must never be reached
        raise AssertionError("nothing should egress")
    with TestClient(_app(tmp_path, handler)) as client:
        assert client.post("/producer/findings",
                           json={"findings": [{"raw": "transcript text"}]}).status_code == 422
        assert client.post("/producer/findings", json={"segments": []}).status_code == 422


def test_service_down_still_accepts_and_queues(tmp_path):
    def down(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("down")
    with TestClient(_app(tmp_path, down)) as client:
        resp = client.post("/producer/findings", json={"findings": [FINDING]})
    assert resp.status_code == 200
    assert resp.json() == {"accepted": 1, "sent": False}     # fail open, queued


def test_mcp_surface_is_mounted_on_the_same_app(tmp_path):
    app = _app(tmp_path, lambda r: httpx.Response(200))
    paths = {getattr(r, "path", "") for r in app.router.routes}
    assert any(p.startswith("/mcp") for p in paths)          # one process, one port
