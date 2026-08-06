import json
import logging
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
    # Findings FIRST, then Contributor registration (Relay._register_members):
    # registration is metadata about a push that already succeeded, so it must
    # never precede the payload nor fire for a batch that stayed queued.
    assert urls_hit == ["http://svc/v1/sessions/sh-1/findings",
                        "http://svc/v1/sessions/sh-1/members"]


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
        if request.url.path.endswith("/members"):   # Contributor registration
            return httpx.Response(200, json={"members": ["aditya"]})
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
        if request.url.path.endswith("/members"):
            assert request.url.path == "/v1/sessions/sh-late-join/members"
            return httpx.Response(200, json={"members": ["aditya"]})
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
        if request.url.path.endswith("/members"):
            # Registration follows the findings, so it must be aimed at the
            # SAME session the partition chose -- pinned below, not skipped.
            urls_hit.append(str(request.url))
            return httpx.Response(200, json={"members": ["aditya"]})
        urls_hit.append(str(request.url))
        bodies.append(json.loads(request.content))
        return httpx.Response(200, json={"accepted": 1, "memory_version": 1})
    relay._transport = httpx.MockTransport(up)               # service is back up

    assert await relay.flush() == (1, 0)
    assert urls_hit == ["http://svc/v1/sessions/sh-PRIVATE/findings",
                        "http://svc/v1/sessions/sh-PRIVATE/members"]     # NOT sh-OTHERTEAM
    assert bodies[0]["findings"][0]["id"] == "f-private"


async def test_a_404_stays_queued_because_the_session_can_be_recreated(tmp_path, caplog):
    """The likeliest 4xx at a demo is 'service restarted, session unknown'.
    Create-or-return (Task 11 Step 2) makes that recoverable, and a resync
    recreates the session -- so a 404 must stay in the retry queue and flush
    ITSELF the moment the session exists again. Dropping it converts a
    self-healing case into one that needs a human mid-demo.

    The LOGGING is what changes: a named 404 with its URL, not
    'Service unavailable'."""
    calls = []
    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(str(request.url))
        return httpx.Response(404, json={"error": "unknown session sh-gone"})

    relay = Relay(tmp_path, "http://svc", "sh-gone",
                  transport=httpx.MockTransport(handler))
    relay.record([_finding("f-1")])

    with caplog.at_level(logging.WARNING):
        first = await relay.flush()
    second = await relay.flush()

    assert first == (0, 1) and second == (0, 1)          # still pending, both times
    assert len(calls) == 2                               # re-attempted, deliberately
    # ⟨DEVIATION vs. the plan, recorded⟩ `r.message % r.args` assumes
    # `.message` is the unformatted template; pytest's caplog handler has
    # already called `getMessage()` by the time records land here, so
    # `.message` is the FINAL string and re-applying `% r.args` raises
    # "not all arguments converted". `r.getMessage()` is the correct,
    # always-safe accessor and is used instead.
    assert any("404" in r.getMessage() for r in caplog.records)
    assert not (tmp_path / "dropped.jsonl").exists()


async def test_a_422_is_terminal_and_never_re_attempted(tmp_path, caplog):
    """`except (httpx.HTTPError, OSError)` catches HTTPStatusError too, so a
    permanently malformed payload was indistinguishable from a transient
    outage and looped forever logging 'Service unavailable'. A 422 is a
    request that CANNOT succeed no matter how many times it is sent -- unlike
    a 404, which stops being true the moment the session is recreated."""
    calls = []
    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(str(request.url))
        return httpx.Response(422, json={"error": "not a Finding"})

    relay = Relay(tmp_path, "http://svc", "sh-1",
                  transport=httpx.MockTransport(handler))
    relay.record([_finding("f-1")])

    with caplog.at_level(logging.WARNING):
        first = await relay.flush()
    second = await relay.flush()

    assert len(calls) == 1                               # never re-attempted
    assert first == (0, 0) and second == (0, 0)
    assert any("422" in r.getMessage() for r in caplog.records)


async def test_a_finding_for_an_ended_session_is_dropped_and_never_retargeted(tmp_path,
                                                                              caplog):
    """Spec Testing item 8, and the round 3 partition discipline applied to
    termination (relay.py's termination note).

    A Finding is recorded under sh-CLOSED, whose team has since ended it. The
    push comes back 409 {"error": "session_ended"}. Two things must hold, and
    the second is the one worth a test: it is DROPPED with a log line naming
    the session, and when the same relay is later bound to a live sh-NEXT the
    dropped Finding is not swept into it. "The target session closed, so put
    these somewhere they can land" is the exact cross-session leak the round 2
    partition fix removed, and a closed session is when a human is MOST likely
    to have decided the work is over."""
    remembered: list[str] = []
    urls_hit: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        urls_hit.append(str(request.url))
        if "sh-CLOSED" in str(request.url):
            return httpx.Response(409, json={"error": "session_ended"})
        return httpx.Response(200, json={"accepted": 1, "memory_version": 1})

    relay = Relay(tmp_path, "http://svc", "sh-CLOSED",
                  transport=httpx.MockTransport(handler),
                  on_session_ended=remembered.append)
    relay.record([_finding("f-closed")])

    with caplog.at_level(logging.WARNING):
        first = await relay.flush()

    assert first == (0, 0)                       # neither sent nor still pending
    assert relay.ended_session_ids() == frozenset({"sh-CLOSED"})
    # Persisted outward exactly once, so `resync` cannot resurrect the session
    # after a service restart (the spec's durability stopgap).
    assert remembered == ["sh-CLOSED"]
    assert any("ended" in r.getMessage() and "sh-CLOSED" in r.getMessage()
               for r in caplog.records)

    relay.rebind("sh-NEXT")                      # the team moves to a new session
    relay.record([_finding("f-next")])
    assert await relay.flush() == (1, 0)

    # f-closed is on NOBODY's wire after the 409: not re-attempted against
    # sh-CLOSED, and above all not smuggled into sh-NEXT.
    assert urls_hit == ["http://svc/v1/sessions/sh-CLOSED/findings",
                        "http://svc/v1/sessions/sh-NEXT/findings",
                        "http://svc/v1/sessions/sh-NEXT/members"]
    assert (tmp_path / "dropped.jsonl").read_text().split() == ["f-closed"]


async def test_a_session_already_known_ended_is_never_even_attempted(tmp_path, caplog):
    """The seeded half of the same rule. A session can end while this process
    is down — nobody here ever sees the 409 — and the retained `ended.json`
    (cli.main passes it in) is what stops the backlog being re-POSTed at a
    corpse on every tick forever."""
    urls_hit: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        urls_hit.append(str(request.url))
        return httpx.Response(200, json={"accepted": 1, "memory_version": 1})

    relay = Relay(tmp_path, "http://svc", "sh-CLOSED",
                  transport=httpx.MockTransport(handler),
                  ended_sessions={"sh-CLOSED"})
    relay.record([_finding("f-closed")])

    with caplog.at_level(logging.WARNING):
        assert await relay.flush() == (0, 0)

    assert urls_hit == []                        # no request at all, not even one
    assert (tmp_path / "dropped.jsonl").read_text().split() == ["f-closed"]
    # And an operator resync must not re-create it either: `retained_count`
    # excludes it, so cmd_resync's "pushed N of M" cannot read as a failure.
    assert relay.retained_count() == 0
    assert await relay.resync_sessions() == {}
    assert urls_hit == []


async def test_an_unrelated_409_is_terminal_but_does_not_mark_the_session_ended(tmp_path):
    """`is_session_ended` reads the BODY as well as the status, and this is
    why: 409 is a generic conflict, the local reaction to the session_ended
    one is destructive (drop the backlog, refuse every future push, block
    resync), and anything between this process and the service — a proxy, a
    route added later — can emit a 409 of its own. It stays an ordinary
    terminal 4xx: dropped, but the session is still alive."""
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(409, json={"error": "someone else is writing"})

    relay = Relay(tmp_path, "http://svc", "sh-1", transport=httpx.MockTransport(handler))
    relay.record([_finding("f-1")])

    assert await relay.flush() == (0, 0)
    assert relay.ended_session_ids() == frozenset()
    assert relay.retained_count() == 1           # still deliverable in principle


async def test_a_409_whose_body_is_not_json_at_all_does_not_escape_as_an_exception(tmp_path):
    """The other half of `is_session_ended`'s guard, and the half nothing
    covered: every unrelated-409 test sends a JSON body.

    VERIFIED BY MUTATION 2026-08-06 — removing the `try/except ValueError`
    around `response.json()` in ended.py left all 103 orchestrator tests
    passing, despite that module's docstring calling this case out by name ("A
    409 whose body is not JSON at all is something other than the service's
    liveness gate"). An intermediary answering `409` with an HTML error page is
    the realistic shape, and `response.json()` then raises ValueError from
    INSIDE `Relay._post`'s `except httpx.HTTPStatusError` handler, which
    propagates out of `flush()` — and `app.py`'s `await relay.flush()` on the
    producer endpoint is unwrapped, so the worker's egress gets a 500 instead
    of the documented fail-open queue.

    Same outcome as the JSON-bodied unrelated 409 above: ordinary terminal 4xx,
    dropped, session still alive, nothing raised."""
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(409, text="<html><body>409 Conflict</body></html>")

    relay = Relay(tmp_path, "http://svc", "sh-1", transport=httpx.MockTransport(handler))
    relay.record([_finding("f-1")])

    assert await relay.flush() == (0, 0)
    assert relay.ended_session_ids() == frozenset()
    assert relay.retained_count() == 1


async def test_a_5xx_is_still_retried(tmp_path):
    calls = []
    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(str(request.url))
        return httpx.Response(503)

    relay = Relay(tmp_path, "http://svc", "sh-1",
                  transport=httpx.MockTransport(handler))
    relay.record([_finding("f-1")])

    assert await relay.flush() == (0, 1)
    assert await relay.flush() == (0, 1)
    assert len(calls) == 2


async def test_resync_sessions_reports_only_the_sessions_it_actually_pushed_to(tmp_path):
    """`resync()` returns a bare int, which cannot tell a caller WHICH sessions
    converged -- so cmd_resync re-synthesized only whatever happened to be
    bound, and every other session in the backlog got its findings back with no
    Working Memory, no conflicts and no merges. The partitioning fix in
    relay.py's round 2 is only half a recovery path without this."""
    def handler(request: httpx.Request) -> httpx.Response:
        if "sh-bad" in str(request.url):
            return httpx.Response(503)
        return httpx.Response(200, json={"accepted": 1})

    relay = Relay(tmp_path, "http://svc", "sh-good",
                  transport=httpx.MockTransport(handler))
    relay.record([_finding("f-1")])
    relay.shared_id = "sh-bad"
    relay.record([_finding("f-2")])

    pushed = await relay.resync_sessions()

    assert pushed == {"sh-good": 1}
    assert await relay.resync() == 1          # the documented int, unchanged


async def test_contributors_are_registered_with_the_service_after_a_successful_push(tmp_path):
    """Plan D.2: join "registers the Contributor with the service (POST
    /members)". That step existed NOWHERE -- discovery.join_session recorded
    it NOT DONE ("no Synapse Service exists yet"), and the service's own
    /members route had no caller in the tree, so every session reported
    `members: []` however many people had joined. It lives here rather than in
    `synapse-worker join` because the orchestrator is the single egress.

    Driven off Finding.attributions, not the binding, so a Contributor whose
    work arrives through contribute() or someone else's resync is registered
    too -- pinned by f-2's second contributor below.
    """
    posts: list[tuple[str, dict]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        posts.append((request.url.path, json.loads(request.content)))
        if request.url.path.endswith("/members"):
            return httpx.Response(200, json={"members": []})
        return httpx.Response(200, json={"accepted": 1, "memory_version": 1})

    relay = _relay(tmp_path, handler)
    f2 = _finding("f-2")
    f2.attributions[0].contributor = "akhil"
    relay.record([_finding("f-1"), f2])
    assert await relay.flush() == (2, 0)

    members = [body["contributor"] for path, body in posts if path.endswith("/members")]
    assert sorted(members) == ["aditya", "akhil"]     # every attribution, deduped

    # Cached: a second push of the same contributors costs no further requests.
    posts.clear()
    relay.record([_finding("f-3")])
    assert await relay.flush() == (1, 0)
    assert [p for p, _ in posts if p.endswith("/members")] == []


async def test_a_failed_member_registration_never_fails_the_push(tmp_path):
    """A Contributor list is metadata; the findings in the same request are
    not. A service that 500s on /members must not turn a delivered batch into
    a queued one -- otherwise a cosmetic endpoint can stall the whole egress."""
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/members"):
            return httpx.Response(500, json={"error": "members exploded"})
        return httpx.Response(200, json={"accepted": 1, "memory_version": 1})

    relay = _relay(tmp_path, handler)
    relay.record([_finding("f-1")])
    assert await relay.flush() == (1, 0)          # sent, not queued
    assert relay.pending_count() == 0


async def test_the_timeout_covers_a_synchronous_synthesis_call(tmp_path):
    """`POST /findings` runs synthesis INSIDE the request (api.push_findings),
    so the relay's timeout has to cover a model call, not a round trip.

    Regression: the default was 10.0 s while real Llama-3.3-70B synthesis on
    Cloud AI 100 measured 12.6-52.8 s live. The service stored and synthesized
    every batch; the relay timed out waiting, never marked them sent, and
    re-pushed the same findings every tick -- an extra 70B call per retry
    against a ~20 req/hour key, converging never. A FakeProvider answers
    instantly, so only a live run could expose it."""
    relay = Relay(tmp_path, "http://svc", "sh-1")
    assert relay.timeout >= 60.0, (
        "relay timeout must cover a synchronous synthesis call; a value under "
        "the observed 12.6-52.8 s range silently re-pushes and re-bills forever")
