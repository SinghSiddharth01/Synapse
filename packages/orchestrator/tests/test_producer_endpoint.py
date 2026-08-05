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


def test_unbound_producer_returns_503_and_never_invents_a_session(tmp_path):
    """Post-review amendment (2026-08-04): no binding resolved -> 503, not a
    200 accept-and-queue. The worker's own HttpSink treats any non-2xx as
    'stay queued, retry later' (producer.py) -- that IS the fail-open path.
    This endpoint has nowhere real to route a Finding when unbound, so it
    must not take custody of one either: nothing is written to disk here,
    and nothing egresses to a fabricated 'unbound' Shared Session."""
    def handler(request):
        raise AssertionError("nothing should egress while unbound")
    relay = Relay(tmp_path, "http://svc", "sh-1", transport=httpx.MockTransport(handler))
    app = build_app(relay, resolve_binding=lambda: None)
    with TestClient(app) as client:
        resp = client.post("/producer/findings", json={"findings": [FINDING]})
    assert resp.status_code == 503
    assert "not joined" in resp.json()["error"]
    assert relay.shared_id == "sh-1"                          # never rebound away
    assert not (tmp_path / "findings.jsonl").exists()          # never durably recorded either


def test_producer_endpoint_preserves_producer_supplied_attribution(tmp_path):
    """Post-review amendment (2026-08-04): the worker already stamps
    Attribution correctly from its own LocalBinding (distiller.py) — this
    endpoint must NOT re-stamp it from whichever single binding
    `resolve_binding` currently resolves to, since that binding is picked
    across every joined Agent product ("most recently joined wins") and
    re-stamping silently relabelled a DIFFERENT product's Finding with the
    wrong Contributor/Agent Session/Agent whenever more than one product was
    joined at once (round 2 review, blocker)."""
    forwarded = []
    def handler(request: httpx.Request) -> httpx.Response:
        forwarded.append(json.loads(request.content))
        return httpx.Response(200, json={"accepted": 1, "memory_version": 1})
    binding = LocalBinding(agent_session_id="as-claude", shared_id="sh-real",
                           contributor="aditya", agent="claude-code")
    relay = Relay(tmp_path, "http://svc", None, transport=httpx.MockTransport(handler))
    app = build_app(relay, resolve_binding=lambda: binding)

    # A Finding attributed to a DIFFERENT product/session than the currently
    # resolved binding -- e.g. produced by a codex worker while claude-code's
    # binding happens to be what resolve_binding() currently returns.
    codex_attributed = dict(FINDING, attributions=[{"contributor": "akhil",
                                                     "agent_session": "as-codex",
                                                     "agent": "codex"}])
    with TestClient(app) as client:
        resp = client.post("/producer/findings", json={"findings": [codex_attributed]})

    assert resp.status_code == 200
    assert resp.json() == {"accepted": 1, "sent": True}
    [sent_attribution] = forwarded[0]["findings"][0]["attributions"]
    assert sent_attribution == {"contributor": "akhil", "agent_session": "as-codex",
                                "agent": "codex"}             # preserved, not overwritten


def test_producer_endpoint_rejects_empty_attributions(tmp_path):
    def handler(request):
        raise AssertionError("nothing should egress")
    empty_attribution = dict(FINDING, attributions=[])
    with TestClient(_app(tmp_path, handler)) as client:
        resp = client.post("/producer/findings", json={"findings": [empty_attribution]})
    assert resp.status_code == 422
    assert "attributions" in resp.json()["error"]


def test_producer_endpoint_rejects_service_written_fields_from_a_producer(tmp_path):
    """schemas.py is explicit: status/merged_from/merged_into are 'Written by
    synthesis, service-side. Producers leave these at defaults.' A producer
    that sets them is manufacturing a tombstone or merge lineage nobody
    made -- reject outright rather than forward it (round 2 review, major)."""
    def handler(request):
        raise AssertionError("nothing should egress")
    with TestClient(_app(tmp_path, handler)) as client:
        tombstone = dict(FINDING, status="trivial")
        assert client.post("/producer/findings",
                           json={"findings": [tombstone]}).status_code == 422

        fabricated_merge = dict(FINDING, merged_into="f-victim")
        assert client.post("/producer/findings",
                           json={"findings": [fabricated_merge]}).status_code == 422

        fabricated_lineage = dict(FINDING, merged_from=["f-a", "f-b"])
        assert client.post("/producer/findings",
                           json={"findings": [fabricated_lineage]}).status_code == 422


def test_producer_endpoint_rejects_synthesized_provenance_from_a_producer(tmp_path):
    """provenance is producer-legitimate only as 'distilled' or 'contributed'
    (schemas.py) -- 'synthesized' is written by synthesis, service-side."""
    def handler(request):
        raise AssertionError("nothing should egress")
    forged = dict(FINDING, provenance="synthesized")
    with TestClient(_app(tmp_path, handler)) as client:
        resp = client.post("/producer/findings", json={"findings": [forged]})
    assert resp.status_code == 422
    assert "provenance" in resp.json()["error"]


def test_rejoin_does_not_retarget_a_still_queued_finding_through_the_real_endpoint(tmp_path):
    """End-to-end reproduction of the partition blocker (round 2 review)
    through the actual Starlette endpoint: a POST while bound to sh-PRIVATE
    queues (service down); the operator re-joins sh-OTHERTEAM; the service
    comes back for the NEXT POST. The first (private) Finding must still be
    delivered to sh-PRIVATE, never to sh-OTHERTEAM."""
    private = LocalBinding(agent_session_id="as-1", shared_id="sh-PRIVATE",
                           contributor="aditya", agent="claude-code")
    joined = {"binding": private}

    def down(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("down")
    relay = Relay(tmp_path, "http://svc", None, transport=httpx.MockTransport(down))
    app = build_app(relay, resolve_binding=lambda: joined["binding"])

    private_finding = dict(FINDING, id="f-private", text="PRIVATE: the auth key rotation trick")
    with TestClient(app) as client:
        resp = client.post("/producer/findings", json={"findings": [private_finding]})
        assert resp.json() == {"accepted": 1, "sent": False}   # queued, service down

        joined["binding"] = LocalBinding(agent_session_id="as-1", shared_id="sh-OTHERTEAM",
                                         contributor="aditya", agent="claude-code")
        sent_pairs = []
        def up(request: httpx.Request) -> httpx.Response:
            sent_pairs.append((str(request.url), json.loads(request.content)))
            return httpx.Response(200, json={"accepted": 1, "memory_version": 1})
        relay._transport = httpx.MockTransport(up)             # service back up

        other_finding = dict(FINDING, id="f-other")
        resp2 = client.post("/producer/findings", json={"findings": [other_finding]})
        assert resp2.json() == {"accepted": 1, "sent": True}

    by_url = {url: body for url, body in sent_pairs}
    assert by_url["http://svc/v1/sessions/sh-PRIVATE/findings"]["findings"][0]["id"] == "f-private"
    assert by_url["http://svc/v1/sessions/sh-OTHERTEAM/findings"]["findings"][0]["id"] == "f-other"
