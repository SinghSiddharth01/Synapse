import json
from datetime import datetime, timezone

import httpx
from starlette.testclient import TestClient
from synapse_contracts import LocalBinding

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


def test_malformed_bodies_are_422_not_500(tmp_path):
    """The module docstring promises 422 for 'anything else — segments, events,
    raw text'. Raw text and a top-level JSON array both used to reach an
    unguarded `request.json()`/`.get()` and 500 instead."""
    def handler(request):
        raise AssertionError("nothing should egress")
    with TestClient(_app(tmp_path, handler)) as client:
        raw_text = client.post("/producer/findings", content=b"not json at all",
                               headers={"content-type": "application/json"})
        assert raw_text.status_code == 422

        top_level_list = client.post("/producer/findings", json=[FINDING])
        assert top_level_list.status_code == 422


def test_unbound_producer_accepts_and_queues_but_never_invents_a_session(tmp_path):
    """No binding resolved -> durable locally, but no egress to a fabricated
    'unbound' Shared Session (that was the previous, blocker-severity bug)."""
    def handler(request):
        raise AssertionError("nothing should egress while unbound")
    relay = Relay(tmp_path, "http://svc", "sh-1", transport=httpx.MockTransport(handler))
    app = build_app(relay, resolve_binding=lambda: None)
    with TestClient(app) as client:
        resp = client.post("/producer/findings", json={"findings": [FINDING]})
    assert resp.status_code == 200
    assert resp.json() == {"accepted": 1, "sent": False}
    assert relay.shared_id is None                            # rebound away from "sh-1"
    assert (tmp_path / "findings.jsonl").exists()              # still durable


def test_producer_endpoint_stamps_the_bound_attribution_over_a_forged_one(tmp_path):
    """LocalBinding is 'owned by the orchestrator, which stamps it onto every
    Finding arriving from any local producer' (schemas.py). A producer that
    claims to be a different teammate must not be believed."""
    forwarded = []
    def handler(request: httpx.Request) -> httpx.Response:
        forwarded.append(json.loads(request.content))
        return httpx.Response(200, json={"accepted": 1, "memory_version": 1})
    binding = LocalBinding(agent_session_id="as-real", shared_id="sh-real",
                           contributor="aditya", agent="claude-code")
    relay = Relay(tmp_path, "http://svc", None, transport=httpx.MockTransport(handler))
    app = build_app(relay, resolve_binding=lambda: binding)

    forged = dict(FINDING, attributions=[{"contributor": "someone-else",
                                          "agent_session": "as-999", "agent": "codex"}])
    with TestClient(app) as client:
        resp = client.post("/producer/findings", json={"findings": [forged]})

    assert resp.status_code == 200
    assert resp.json() == {"accepted": 1, "sent": True}
    [sent_attribution] = forwarded[0]["findings"][0]["attributions"]
    assert sent_attribution == {"contributor": "aditya", "agent_session": "as-real",
                                "agent": "claude-code"}
