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


async def test_two_windows_of_one_human_are_two_participants_end_to_end(tmp_path):
    """W2's headline claim, through every real hop there is: the MCP tools
    (`register_tools`), the file-backed binding resolver (`cli._resolve_binding`
    over real per-session binding files), the Relay, and the REAL service.

    One machine, one human, one Shared Session, two Claude Code windows. What
    has to be true, and was not before this branch:

      - window B's `query` returns what window A contributed, credited to the
        human who found it — they are teammates, not one participant;
      - window A's `query` does NOT return window A's own contribution, which
        is already in its context window;
      - and the two are told apart by ONE thing only: the `agent_session_id`
        each passes on the call, since MCP carries no per-call identity.

    Before the split at the service, B got nothing (every Attribution named
    the same contributor). Before the threading at the orchestrator, both
    windows spoke as whichever joined last, so A's contribution was stamped
    with B's conversation and B suppressed it as its own."""
    from synapse_contracts.binding import SessionBinding, write_binding
    from synapse_distiller import Distiller

    import synapse_orchestrator.cli as cli
    from synapse_orchestrator.server import create_mcp, register_tools

    service_app = build_service_app(
        FakeProvider(scripts=[MERGE_NOOP, {"ranked": [0]}, MERGE_NOOP, {"ranked": [0]}]))
    service_transport = httpx.ASGITransport(app=service_app)
    async with httpx.AsyncClient(transport=service_transport,
                                 base_url="http://svc") as bootstrap:
        sid = (await bootstrap.post("/v1/sessions", json={
            "purpose": "two windows", "created_by": "aditya"})).json()["shared_id"]

    state_dir = tmp_path / "state"
    for session, transcript in (("conv-1", "/tmp/cc-1.jsonl"), ("conv-2", "/tmp/cc-2.jsonl")):
        write_binding(state_dir / "bindings" / "claude-code" / f"{session}.json",
                      SessionBinding(agent_session_id=session, shared_id=sid,
                                     contributor="aditya", agent="claude-code",
                                     transcript_path=transcript, pinned_at=TS))

    relay = Relay(tmp_path / "relay", "http://svc", sid, transport=service_transport)
    server = create_mcp()
    distiller_provider = FakeProvider(scripts=[{"findings": [
        {"type": "learning", "text": "the retry backoff is what fixes the 401"}]}])
    register_tools(server, resolve_binding=lambda: cli._resolve_binding(state_dir),
                   service_url="http://svc", relay=relay,
                   distiller_factory=lambda b: Distiller(distiller_provider, b),
                   transport=service_transport, state_dir=state_dir,
                   cwd=tmp_path, contributor="aditya")

    await server.call_tool("contribute", {"text": "the retry backoff is what fixes it",
                                          "agent_session_id": "conv-1"})

    from_b = str(await server.call_tool("query", {"question": "the 401?",
                                                  "agent_session_id": "conv-2"}))
    from_a = str(await server.call_tool("query", {"question": "the 401?",
                                                  "agent_session_id": "conv-1"}))

    assert "retry backoff" in from_b and "aditya" in from_b
    assert "retry backoff" not in from_a
    assert "nothing relevant" in from_a
