import json
from datetime import datetime, timezone
from pathlib import Path

import httpx
from synapse_contracts import Attribution, Finding

from synapse_orchestrator.relay import Relay

TS = datetime(2026, 8, 4, tzinfo=timezone.utc)


def _finding(fid: str) -> Finding:
    return Finding(id=fid, type="learning", text=f"insight {fid}",
                   attributions=[Attribution(contributor="aditya",
                                             agent_session="as-1", agent="claude-code")],
                   ts=TS)


def _relay(tmp_path: Path, handler) -> Relay:
    return Relay(tmp_path, "http://svc", "sh-1",
                 transport=httpx.MockTransport(handler))


async def test_write_ahead_then_flush(tmp_path):
    received = []
    def handler(request: httpx.Request) -> httpx.Response:
        received.append(json.loads(request.content))
        return httpx.Response(200, json={"accepted": 1, "memory_version": 1})
    relay = _relay(tmp_path, handler)
    relay.record([_finding("f-1")])
    assert (tmp_path / "findings.jsonl").exists()          # durable BEFORE any send
    assert relay.pending_count() == 1
    sent, pending = await relay.flush()
    assert (sent, pending) == (1, 0)
    assert received[0]["findings"][0]["id"] == "f-1"


async def test_service_down_keeps_findings_queued_and_survives_restart(tmp_path):
    def down(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("service down")
    relay = _relay(tmp_path, down)
    relay.record([_finding("f-1")])
    sent, pending = await relay.flush()
    assert (sent, pending) == (0, 1)

    def up(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"accepted": 1, "memory_version": 1})
    reborn = _relay(tmp_path, up)                           # fresh instance = restart
    sent, pending = await reborn.flush()
    assert (sent, pending) == (1, 0)


async def test_resync_repushes_everything_even_after_ack(tmp_path):
    """The service is in-memory; its restart is answered by our retained log."""
    calls = []
    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(json.loads(request.content))
        return httpx.Response(200, json={"accepted": 0, "memory_version": 1})
    relay = _relay(tmp_path, handler)
    relay.record([_finding("f-1"), _finding("f-2")])
    await relay.flush()
    assert relay.pending_count() == 0
    pushed, total = await relay.resync()
    assert (pushed, total) == (2, 2)                        # retained, not deleted on ack
    assert {f["id"] for f in calls[-1]["findings"]} == {"f-1", "f-2"}


async def test_resync_reports_failure_distinctly_from_nothing_to_push(tmp_path):
    """(0, 0) — nothing was ever recorded — must not read the same as a failed
    push of something that WAS recorded. Collapsing both into a bare int is
    exactly the ambiguity `resync`'s (pushed, total) shape exists to remove."""
    def down(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("service down")
    relay = _relay(tmp_path, down)
    relay.record([_finding("f-1")])
    assert await relay.resync() == (0, 1)                   # tried and failed: NOT success

    empty = _relay(tmp_path / "empty", down)
    assert await empty.resync() == (0, 0)                   # nothing to do: distinguishable


async def test_flush_with_nothing_pending_is_free(tmp_path):
    def handler(request: httpx.Request) -> httpx.Response:  # any call would record
        raise AssertionError("no HTTP call expected")
    relay = _relay(tmp_path, handler)
    assert await relay.flush() == (0, 0)


async def test_unbound_relay_never_attempts_the_network(tmp_path):
    """shared_id=None (no Shared Session bound) must behave like a durable,
    fail-open queue — never invent a session id and post to it."""
    def handler(request: httpx.Request) -> httpx.Response:
        raise AssertionError("no HTTP call expected when unbound")
    relay = Relay(tmp_path, "http://svc", None, transport=httpx.MockTransport(handler))
    relay.record([_finding("f-1")])
    assert await relay.flush() == (0, 1)
    assert await relay.resync() == (0, 1)

    relay.rebind("sh-late-join")
    def up(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"accepted": 1, "memory_version": 1})
    relay._transport = httpx.MockTransport(up)               # simulate a later join
    assert await relay.flush() == (1, 0)                     # recovered, not lost
