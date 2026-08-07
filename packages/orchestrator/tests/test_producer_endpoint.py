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
    app = build_app(relay, resolve_binding_for_agent=lambda agent: None)
    with TestClient(app) as client:
        resp = client.post("/producer/findings", json={"findings": [FINDING]})
    assert resp.status_code == 503
    assert "not joined" in resp.json()["error"]
    assert "claude-code" in resp.json()["error"]              # names the unmatched agent
    assert relay.shared_id == "sh-1"                          # never rebound away
    assert not (tmp_path / "findings.jsonl").exists()          # never durably recorded either


def test_producer_endpoint_preserves_producer_supplied_attribution(tmp_path):
    """Post-review amendment (2026-08-04): the worker already stamps
    Attribution correctly from its own LocalBinding (distiller.py) — this
    endpoint must NOT re-stamp it from whichever single binding happens to
    be resolved, since a naive single 'most recently joined' pick relabels a
    DIFFERENT product's Finding with the wrong Contributor/Agent
    Session/Agent whenever more than one product is joined at once (round 2
    review, blocker)."""
    forwarded = []
    def handler(request: httpx.Request) -> httpx.Response:
        forwarded.append(json.loads(request.content))
        return httpx.Response(200, json={"accepted": 1, "memory_version": 1})
    binding = LocalBinding(agent_session_id="as-claude", shared_id="sh-real",
                           contributor="aditya", agent="claude-code")
    relay = Relay(tmp_path, "http://svc", None, transport=httpx.MockTransport(handler))
    app = build_app(relay, resolve_binding_for_agent=lambda agent:
                    binding if agent == "claude-code" else None)

    # A Finding attributed to a DIFFERENT product than the one this test's
    # ONLY joined binding belongs to (claude-code) -- e.g. produced by a
    # codex worker with nothing joined for codex specifically. Must be
    # rejected (503, nothing egresses), not silently routed/stamped onto
    # claude-code's binding -- that IS the round 3 regression this test
    # would have missed if it kept resolving a single "current" binding.
    codex_attributed = dict(FINDING, id="f-codex",
                            attributions=[{"contributor": "akhil",
                                          "agent_session": "as-codex", "agent": "codex"}])
    with TestClient(app) as client:
        resp = client.post("/producer/findings", json={"findings": [codex_attributed]})
        assert resp.status_code == 503
        assert "codex" in resp.json()["error"]

        # The SAME app, SAME session: a Finding correctly matched against a
        # binding that IS joined for its own product -- attribution passes
        # through unchanged.
        resp = client.post("/producer/findings", json={"findings": [FINDING]})
    assert resp.status_code == 200
    assert resp.json() == {"accepted": 1, "sent": True}
    [sent_attribution] = forwarded[0]["findings"][0]["attributions"]
    assert sent_attribution == {"contributor": "aditya", "agent_session": "as-1",
                                "agent": "claude-code"}       # preserved, not overwritten


def test_producer_endpoint_routes_each_agent_to_its_own_shared_session(tmp_path):
    """The residual round 3 blocker, closed: with TWO Agent products joined
    to TWO different Shared Sessions at once, a Finding correctly attributed
    to one must never egress into the other's Shared Session, even though a
    single 'most recently joined overall' binding pick would have sent it
    there. Reproduces the exact scenario from round 3's verify_probe2.py."""
    claude_binding = LocalBinding(agent_session_id="as-claude", shared_id="sh-A",
                                  contributor="aditya", agent="claude-code")
    codex_binding = LocalBinding(agent_session_id="as-codex", shared_id="sh-B",
                                 contributor="akhil", agent="codex")
    bindings = {"claude-code": claude_binding, "codex": codex_binding}

    forwarded = []
    def handler(request: httpx.Request) -> httpx.Response:
        forwarded.append((str(request.url), json.loads(request.content)))
        return httpx.Response(200, json={"accepted": 1, "memory_version": 1})
    relay = Relay(tmp_path, "http://svc", None, transport=httpx.MockTransport(handler))
    app = build_app(relay, resolve_binding_for_agent=bindings.get)

    claude_attributed = dict(FINDING, id="f-claude",
                             attributions=[{"contributor": "aditya", "agent_session": "as-claude",
                                           "agent": "claude-code"}])
    codex_attributed = dict(FINDING, id="f-codex",
                            attributions=[{"contributor": "akhil", "agent_session": "as-codex",
                                          "agent": "codex"}])
    with TestClient(app) as client:
        resp = client.post("/producer/findings", json={"findings": [claude_attributed]})
        assert resp.status_code == 200
        resp = client.post("/producer/findings", json={"findings": [codex_attributed]})
        assert resp.status_code == 200

    by_url = {url: body for url, body in forwarded}
    assert by_url["http://svc/v1/sessions/sh-A/findings"]["findings"][0]["id"] == "f-claude"
    assert by_url["http://svc/v1/sessions/sh-B/findings"]["findings"][0]["id"] == "f-codex"
    assert by_url["http://svc/v1/sessions/sh-A/findings"]["findings"][0]["attributions"][0][
        "agent"] == "claude-code"
    assert by_url["http://svc/v1/sessions/sh-B/findings"]["findings"][0]["attributions"][0][
        "agent"] == "codex"


def test_producer_endpoint_routes_two_windows_of_one_product_separately(tmp_path):
    """W2 pass 2, and the same leak one axis over. Round 3 closed "two
    PRODUCTS, two Shared Sessions"; W2 makes "one product, two WINDOWS, two
    Shared Sessions" reachable, and on that machine `attributions[0].agent`
    no longer identifies a destination at all.

    Not a stub resolver: this goes through the production
    `cli._resolve_binding_for_agent` against real binding files in the W2
    layout, because the defect was precisely that the resolver read one file
    per product. Reproduced before the fix: window A's finding egressed to
    sh-b, because the legacy mirror named whichever window joined last."""
    import synapse_orchestrator.cli as cli
    from synapse_contracts.binding import SessionBinding, write_binding

    state_dir = tmp_path / "state"
    for session, shared, pinned in (("conv-1", "sh-a", datetime(2026, 8, 6, tzinfo=timezone.utc)),
                                    ("conv-2", "sh-b", datetime(2026, 8, 7, tzinfo=timezone.utc))):
        binding = SessionBinding(service_url="http://127.0.0.1:8899", agent_session_id=session, shared_id=shared,
                                 contributor="sid", agent="claude-code",
                                 transcript_path=f"/tmp/{session}.jsonl", pinned_at=pinned)
        write_binding(state_dir / "bindings" / "claude-code" / f"{session}.json", binding)
    # The legacy mirror, naming the LAST window to join — what the old
    # per-product resolver returned for every finding on this machine.
    write_binding(state_dir / "bindings" / "claude-code.json",
                  SessionBinding(service_url="http://127.0.0.1:8899", agent_session_id="conv-2", shared_id="sh-b",
                                 contributor="sid", agent="claude-code",
                                 transcript_path="/tmp/conv-2.jsonl",
                                 pinned_at=datetime(2026, 8, 7, tzinfo=timezone.utc)))

    forwarded = []
    def handler(request: httpx.Request) -> httpx.Response:
        forwarded.append((str(request.url), json.loads(request.content)))
        return httpx.Response(200, json={"accepted": 1, "memory_version": 1})
    relay = Relay(tmp_path / "relay", "http://svc", None,
                  transport=httpx.MockTransport(handler))
    app = build_app(
        relay,
        resolve_binding_for_session=lambda agent, session:
            cli._resolve_binding_for_agent(state_dir, agent, session))

    from_a = dict(FINDING, id="f-a",
                  attributions=[{"contributor": "sid", "agent_session": "conv-1",
                                 "agent": "claude-code"}])
    from_b = dict(FINDING, id="f-b",
                  attributions=[{"contributor": "sid", "agent_session": "conv-2",
                                 "agent": "claude-code"}])
    with TestClient(app) as client:
        assert client.post("/producer/findings",
                           json={"findings": [from_a]}).status_code == 200
        assert client.post("/producer/findings",
                           json={"findings": [from_b]}).status_code == 200

    by_url = {url: body for url, body in forwarded}
    assert by_url["http://svc/v1/sessions/sh-a/findings"]["findings"][0]["id"] == "f-a"
    assert by_url["http://svc/v1/sessions/sh-b/findings"]["findings"][0]["id"] == "f-b"


def test_producer_endpoint_falls_back_to_the_product_binding_for_an_unbound_window(
    tmp_path,
):
    """A conversation with no per-session binding of its own — a pre-W2 tree,
    or `serve_local.py`'s machine-scope stand-in — still routes by product,
    which is what every producer did before W2. The narrowing must not turn
    "I don't know this conversation" into a 503 for trees that were working."""
    import synapse_orchestrator.cli as cli
    from synapse_contracts.binding import SessionBinding, write_binding

    state_dir = tmp_path / "state"
    write_binding(state_dir / "bindings" / "claude-code.json",
                  SessionBinding(service_url="http://127.0.0.1:8899", agent_session_id="as-sid", shared_id="local-dev",
                                 contributor="sid", agent="claude-code",
                                 transcript_path="/tmp/scratch.jsonl",
                                 pinned_at=datetime(2026, 8, 6, tzinfo=timezone.utc),
                                 scope="machine"))
    forwarded = []
    def handler(request: httpx.Request) -> httpx.Response:
        forwarded.append(str(request.url))
        return httpx.Response(200, json={"accepted": 1, "memory_version": 1})
    relay = Relay(tmp_path / "relay", "http://svc", None,
                  transport=httpx.MockTransport(handler))
    app = build_app(
        relay,
        resolve_binding_for_session=lambda agent, session:
            cli._resolve_binding_for_agent(state_dir, agent, session))

    unknown_window = dict(FINDING, id="f-w",
                          attributions=[{"contributor": "sid",
                                         "agent_session": "a-real-window-id",
                                         "agent": "claude-code"}])
    with TestClient(app) as client:
        assert client.post("/producer/findings",
                           json={"findings": [unknown_window]}).status_code == 200

    assert "http://svc/v1/sessions/local-dev/findings" in forwarded


def test_producer_endpoint_routes_a_codex_conversation_by_its_own_binding(tmp_path):
    """The generalisation claim, at the seam where it would break first.

    Nothing in the W2 layout is Claude-Code-specific: the directory is
    `bindings/<agent>/`, resolution dispatches through `AGENT_REGISTRY`, and
    the producer routes on the Finding's own `(agent, agent_session)`. A Codex
    rollout bound to its own Shared Session must therefore egress there while
    a claude-code window on the same machine is bound elsewhere — with no pack,
    no hook and no code that names Codex. Registering an agent is the whole of
    the work; this is the proof at the egress."""
    import synapse_orchestrator.cli as cli
    from synapse_contracts.binding import SessionBinding, write_binding

    codex_uuid = "1c9b6d8e-27ac-4f1e-9f2c-8a2b1e6d4c11"
    state_dir = tmp_path / "state"
    write_binding(state_dir / "bindings" / "codex" / f"{codex_uuid}.json",
                  SessionBinding(service_url="http://127.0.0.1:8899", agent_session_id=codex_uuid, shared_id="sh-codex",
                                 contributor="akhil", agent="codex",
                                 transcript_path="/tmp/rollout.jsonl",
                                 pinned_at=datetime(2026, 8, 6, tzinfo=timezone.utc)))
    write_binding(state_dir / "bindings" / "claude-code" / "conv-1.json",
                  SessionBinding(service_url="http://127.0.0.1:8899", agent_session_id="conv-1", shared_id="sh-claude",
                                 contributor="akhil", agent="claude-code",
                                 transcript_path="/tmp/conv-1.jsonl",
                                 pinned_at=datetime(2026, 8, 7, tzinfo=timezone.utc)))

    forwarded = []
    def handler(request: httpx.Request) -> httpx.Response:
        forwarded.append((str(request.url), json.loads(request.content)))
        return httpx.Response(200, json={"accepted": 1, "memory_version": 1})
    relay = Relay(tmp_path / "relay", "http://svc", None,
                  transport=httpx.MockTransport(handler))
    app = build_app(
        relay,
        resolve_binding_for_session=lambda agent, session:
            cli._resolve_binding_for_agent(state_dir, agent, session))

    from_codex = dict(FINDING, id="f-codex",
                      attributions=[{"contributor": "akhil", "agent_session": codex_uuid,
                                     "agent": "codex"}])
    from_claude = dict(FINDING, id="f-claude",
                       attributions=[{"contributor": "akhil", "agent_session": "conv-1",
                                      "agent": "claude-code"}])
    with TestClient(app) as client:
        assert client.post("/producer/findings",
                           json={"findings": [from_codex, from_claude]}).status_code == 200

    by_url = {url: body for url, body in forwarded}
    assert by_url["http://svc/v1/sessions/sh-codex/findings"]["findings"][0]["id"] == "f-codex"
    assert by_url["http://svc/v1/sessions/sh-claude/findings"]["findings"][0]["id"] == "f-claude"


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
    delivered to sh-PRIVATE, never to sh-OTHERTEAM.

    NOTE (round 3 review, scope caveat): this reproduces the leak being
    closed at the ORCHESTRATOR's own boundary — the durable log this
    process owns is partitioned correctly. It does NOT reproduce (and
    cannot, from here) the sibling gap where the Finding is still sitting in
    the WORKER's own write-ahead log at the moment of the re-join — that
    needs a worker-side fix out of scope for this branch; see relay.py's
    module docstring, round 3 note."""
    private = LocalBinding(agent_session_id="as-1", shared_id="sh-PRIVATE",
                           contributor="aditya", agent="claude-code")
    joined = {"binding": private}

    def down(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("down")
    relay = Relay(tmp_path, "http://svc", None, transport=httpx.MockTransport(down))
    app = build_app(relay, resolve_binding_for_agent=lambda agent: joined["binding"])

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
