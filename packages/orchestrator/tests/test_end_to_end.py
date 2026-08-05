"""worker sink -> orchestrator -> relay -> REAL service -> teammate's query.

Every hop is real product code with only the HTTP transports swapped for
in-process ASGI — with one named exception: build_orchestrator_app is wired
without resolve_binding_for_agent here, so the producer endpoint takes its
legacy no-resolver branch; the per-agent routing branch cli.main uses in
production is covered separately in test_producer_endpoint.py/test_cli.py.
This is the walking skeleton, grown up (Plan 0 Task 0.6)."""
from datetime import datetime, timezone

import httpx
from synapse_contracts import Attribution, Finding
from synapse_providers import FakeProvider

from synapse_orchestrator.app import build_app as build_orchestrator_app
from synapse_orchestrator.relay import Relay
from synapse_service.api import build_app as build_service_app
from synapse_worker.producer import HttpSink, Producer

TS = datetime(2026, 8, 4, tzinfo=timezone.utc)
MERGE_NOOP = {"working_memory": "wm", "merges": [], "trivial_ids": [], "conflicts": []}


async def test_a_finding_crosses_all_three_packages(tmp_path):
    # real service, FakeProvider scripted for one merge + one query
    service_app = build_service_app(FakeProvider(scripts=[MERGE_NOOP, {"ranked": [0]}]))
    service_transport = httpx.ASGITransport(app=service_app)

    async with httpx.AsyncClient(transport=service_transport,
                                 base_url="http://svc") as bootstrap:
        sid = (await bootstrap.post("/v1/sessions", json={
            "purpose": "e2e", "created_by": "siddsing"})).json()["shared_id"]

    relay = Relay(tmp_path / "relay", "http://svc", sid, transport=service_transport)
    orchestrator_app = build_orchestrator_app(relay)
    orch_transport = httpx.ASGITransport(app=orchestrator_app)

    # the worker's own producer, pointed at the orchestrator
    producer = Producer(tmp_path / "wal",
                        HttpSink("http://orch/producer/findings", transport=orch_transport))
    finding = Finding(id="f-e2e-1", type="learning", text="the 40ms window matters",
                      attributions=[Attribution(contributor="aditya",
                                                agent_session="as-1", agent="claude-code")],
                      ts=TS)
    producer.record([finding])
    sent, pending = await producer.flush()
    assert (sent, pending) == (1, 0)

    # a teammate's agent queries the service and gets it back
    async with httpx.AsyncClient(transport=service_transport,
                                 base_url="http://svc") as teammate:
        resp = await teammate.post(f"/v1/sessions/{sid}/query", json={
            "query": "what do we know about timing", "agent_session": "as-OTHER"})
    assert [f["id"] for f in resp.json()["findings"]] == ["f-e2e-1"]


async def test_suppression_holds_across_the_full_chain(tmp_path):
    """The producing agent itself asks — and is told nothing (it already knows)."""
    service_app = build_service_app(FakeProvider(scripts=[MERGE_NOOP]))
    service_transport = httpx.ASGITransport(app=service_app)
    async with httpx.AsyncClient(transport=service_transport,
                                 base_url="http://svc") as bootstrap:
        sid = (await bootstrap.post("/v1/sessions", json={
            "purpose": "e2e", "created_by": "s"})).json()["shared_id"]
    relay = Relay(tmp_path / "relay", "http://svc", sid, transport=service_transport)
    producer = Producer(tmp_path / "wal",
                        HttpSink("http://orch/producer/findings",
                                 transport=httpx.ASGITransport(app=build_orchestrator_app(relay))))
    producer.record([Finding(id="f-mine", type="learning", text="x",
                             attributions=[Attribution(contributor="a",
                                                       agent_session="as-me",
                                                       agent="claude-code")], ts=TS)])
    await producer.flush()
    async with httpx.AsyncClient(transport=service_transport,
                                 base_url="http://svc") as me:
        resp = await me.post(f"/v1/sessions/{sid}/query",
                             json={"query": "anything", "agent_session": "as-me"})
    assert resp.json()["findings"] == []     # suppressed pre-model: no rank script needed
