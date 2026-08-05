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
    urls_hit = []
    def handler(request: httpx.Request) -> httpx.Response:
        urls_hit.append(str(request.url))
        received.append(json.loads(request.content))
        return httpx.Response(200, json={"accepted": 1, "memory_version": 1})
    relay = _relay(tmp_path, handler)
    relay.record([_finding("f-1")])
    assert (tmp_path / "findings.jsonl").exists()          # durable BEFORE any send
    assert relay.pending_count() == 1
    sent, pending = await relay.flush()
    assert (sent, pending) == (1, 0)
    assert received[0]["findings"][0]["id"] == "f-1"
    assert urls_hit == ["http://svc/v1/sessions/sh-1/findings"]


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
    """The service is in-memory; its restart is answered by our retained log.

    `resync()`'s return type is the plan's documented `-> int` (Task 2
    Interfaces): the total count of Findings re-pushed. A prior pass had
    rewritten this to `tuple[int, int]`, and this test along with it —
    post-review amendment (2026-08-04) restored the plan's signature."""
    calls = []
    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(json.loads(request.content))
        return httpx.Response(200, json={"accepted": 0, "memory_version": 1})
    relay = _relay(tmp_path, handler)
    relay.record([_finding("f-1"), _finding("f-2")])
    await relay.flush()
    assert relay.pending_count() == 0
    pushed = await relay.resync()
    assert pushed == 2                                      # retained, not deleted on ack
    assert {f["id"] for f in calls[-1]["findings"]} == {"f-1", "f-2"}


async def test_resync_result_versus_retained_count_tells_failure_from_nothing_to_push(tmp_path):
    """(0 pushed, 0 retained) — nothing was ever recorded — must remain
    distinguishable from (0 pushed, 1 retained) — something WAS recorded and
    the push failed. `resync()` itself is a bare int per the plan; the
    distinction lives in comparing it against `retained_count()` (see
    `cli.cmd_resync`, which is what actually reports success/failure)."""
    def down(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("service down")
    relay = _relay(tmp_path, down)
    relay.record([_finding("f-1")])
    assert relay.retained_count() == 1
    assert await relay.resync() == 0                        # tried and failed: NOT success

    empty = _relay(tmp_path / "empty", down)
    assert empty.retained_count() == 0
    assert await empty.resync() == 0                        # nothing to do: still 0, but
                                                              # retained_count() tells them apart


async def test_flush_with_nothing_pending_is_free(tmp_path):
    def handler(request: httpx.Request) -> httpx.Response:  # any call would record
        raise AssertionError("no HTTP call expected")
    relay = _relay(tmp_path, handler)
    assert await relay.flush() == (0, 0)


async def test_unbound_relay_never_attempts_the_network(tmp_path):
    """A Finding recorded while `shared_id` is `None` (no Shared Session
    bound at all) carries that `None` forever — see `record()`. It must
    behave like a durable, fail-open queue that NEVER invents a session id
    to post to, even after a later `rebind()`: partitioning means each
    entry is tagged once, at record time, not retargeted at send time."""
    def handler(request: httpx.Request) -> httpx.Response:
        raise AssertionError("no HTTP call expected when unbound")
    relay = Relay(tmp_path, "http://svc", None, transport=httpx.MockTransport(handler))
    relay.record([_finding("f-1")])
    assert await relay.flush() == (0, 1)
    assert relay.retained_count() == 0                      # None-tagged: not resync-eligible
    assert await relay.resync() == 0

    relay.rebind("sh-late-join")
    def up(request: httpx.Request) -> httpx.Response:
        raise AssertionError("f-1 was recorded unbound; it must never be sent, ever")
    relay._transport = httpx.MockTransport(up)
    # f-1 stays stuck — it was tagged None at record time and rebind() only
    # changes what NEW records get tagged with (see record()/rebind() docs).
    assert await relay.flush() == (0, 1)
    assert await relay.resync() == 0

    # A NEW Finding, recorded AFTER the rebind, is tagged "sh-late-join" and
    # DOES go out — this is what "a later join recovers" actually means now.
    def sent_ok(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/v1/sessions/sh-late-join/findings"
        body = json.loads(request.content)
        assert [f["id"] for f in body["findings"]] == ["f-2"]
        return httpx.Response(200, json={"accepted": 1, "memory_version": 1})
    relay._transport = httpx.MockTransport(sent_ok)
    relay.record([_finding("f-2")])
    assert await relay.flush() == (1, 1)                    # f-2 sent; f-1 still stuck forever


async def test_rejoin_does_not_retarget_a_still_queued_finding_to_the_new_session(tmp_path):
    """The exact blocker reproduced (post-review finding, partition fix):
    record a Finding while bound to sh-PRIVATE; the service is down so it
    stays queued; the operator re-joins a DIFFERENT Shared Session,
    sh-OTHERTEAM; the service comes back. `flush()` must deliver the queued
    Finding to sh-PRIVATE — the session it was actually produced under —
    and must never send it to sh-OTHERTEAM."""
    def down(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("service down")
    relay = Relay(tmp_path, "http://svc", "sh-PRIVATE", transport=httpx.MockTransport(down))
    relay.record([_finding("f-private")])
    assert await relay.flush() == (0, 1)                    # queued while sh-PRIVATE, service down

    relay.rebind("sh-OTHERTEAM")                             # user re-joins a DIFFERENT session

    urls_hit = []
    bodies = []
    def up(request: httpx.Request) -> httpx.Response:
        urls_hit.append(str(request.url))
        bodies.append(json.loads(request.content))
        return httpx.Response(200, json={"accepted": 1, "memory_version": 1})
    relay._transport = httpx.MockTransport(up)               # service is back up

    assert await relay.flush() == (1, 0)
    assert urls_hit == ["http://svc/v1/sessions/sh-PRIVATE/findings"]   # NOT sh-OTHERTEAM
    assert bodies[0]["findings"][0]["id"] == "f-private"
