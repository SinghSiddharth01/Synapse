"""A deferral must be a delay, not a stall.

2026-08-06: findings pushed at 23:03 and 23:10 against a working memory last
written at 22:57. Both pushes deferred, and nothing retried them -- `_pending`
is drained only by the NEXT push, so a session that goes quiet stays stale
forever. `POST /synthesize` already does the work; nothing called it.
"""
from synapse_service.api import drain_pending


class _Synth:
    def __init__(self):
        self.merged: list[str] = []

    async def merge(self, store, sid, findings):
        self.merged.append(sid)


async def test_a_session_past_the_interval_is_drained():
    synth = _Synth()
    pending = {"sh-1": ["f"]}
    merged = await drain_pending(
        store=None, synthesizer=synth, pending=pending,
        last_merge={"sh-1": 0.0}, affordable=lambda: (True, ""),
        interval_s=60.0, now=120.0)
    assert merged == ["sh-1"]
    assert synth.merged == ["sh-1"]
    assert pending == {}                      # drained, not re-queued


async def test_a_session_inside_the_interval_is_left_alone():
    synth = _Synth()
    merged = await drain_pending(
        store=None, synthesizer=synth, pending={"sh-1": ["f"]},
        last_merge={"sh-1": 100.0}, affordable=lambda: (True, ""),
        interval_s=60.0, now=120.0)
    assert merged == []
    assert synth.merged == []


async def test_an_unaffordable_drain_keeps_the_findings_pending():
    """Budget refusal must not consume the backlog -- the whole point of
    `_pending` is that nothing is lost by waiting."""
    pending = {"sh-1": ["f"]}
    merged = await drain_pending(
        store=None, synthesizer=_Synth(), pending=pending,
        last_merge={"sh-1": 0.0}, affordable=lambda: (False, "token budget"),
        interval_s=60.0, now=120.0)
    assert merged == []
    assert pending == {"sh-1": ["f"]}


async def test_a_session_with_nothing_pending_is_skipped():
    merged = await drain_pending(
        store=None, synthesizer=_Synth(), pending={"sh-1": []},
        last_merge={}, affordable=lambda: (True, ""),
        interval_s=60.0, now=120.0)
    assert merged == []


async def test_one_failing_session_does_not_block_the_others():
    class _Boom(_Synth):
        async def merge(self, store, sid, findings):
            if sid == "sh-1":
                raise RuntimeError("provider down")
            self.merged.append(sid)

    synth = _Boom()
    pending = {"sh-1": ["f"], "sh-2": ["g"]}
    merged = await drain_pending(
        store=None, synthesizer=synth, pending=pending,
        last_merge={"sh-1": 0.0, "sh-2": 0.0}, affordable=lambda: (True, ""),
        interval_s=60.0, now=120.0)
    assert merged == ["sh-2"]
    # The sick session keeps its backlog for the next tick rather than losing it.
    assert pending == {"sh-1": ["f"]}


async def test_a_session_never_merged_before_is_drained():
    """No `last_merge` entry means nothing has ever synthesised for it -- the
    interval cannot have elapsed because it never started."""
    synth = _Synth()
    merged = await drain_pending(
        store=None, synthesizer=synth, pending={"sh-1": ["f"]},
        last_merge={}, affordable=lambda: (True, ""),
        interval_s=60.0, now=120.0)
    assert merged == ["sh-1"]


# --------------------------------------------------------------------------
# The loop wiring, end to end
#
# The unit tests above drive `drain_pending` directly. These two drive the
# BACKGROUND TASK: a real app, a real Starlette lifespan, a real deferral, and
# no second push. That is the actual reported failure -- findings landed at
# 23:03 and 23:10 against a memory last written at 22:57 -- and nothing above
# would have caught the task simply never being started.
# --------------------------------------------------------------------------
import time
from datetime import datetime, timezone

from starlette.testclient import TestClient
from synapse_providers import FakeProvider

from synapse_service.api import build_app

TS = datetime(2026, 8, 6, tzinfo=timezone.utc)
VERDICT = {"working_memory": "wm", "merges": [], "trivial_ids": [], "conflicts": []}

# Long enough that the second push is reliably inside it on a loaded machine,
# short enough that waiting out one tick does not dominate the suite.
DRAIN_INTERVAL_S = 0.3


def _finding(fid: str) -> dict:
    return {"id": fid, "type": "learning",
            "text": "the pool trips under allocation pressure",
            "attributions": [{"contributor": "sid", "agent_session": "as-1",
                              "agent": "claude-code"}],
            "ts": TS.isoformat()}


def _app(monkeypatch, interval: float = DRAIN_INTERVAL_S):
    monkeypatch.setattr("synapse_service.api.MERGE_MIN_INTERVAL_S", interval)
    return build_app(FakeProvider(scripts=[VERDICT] * 20),
                     merge_min_interval_s=interval)


def _deferred_session(client) -> tuple[str, int]:
    """A session with one merged round and one DEFERRED push behind it.
    Returns the sid and the memory_version the deferral left behind."""
    sid = client.post("/v1/sessions", json={"purpose": "p", "created_by": "sid"}
                      ).json()["shared_id"]
    first = client.post(f"/v1/sessions/{sid}/findings",
                        json={"findings": [_finding("f-1")]}).json()
    assert first["deferred"] is False, "the first push should merge immediately"
    second = client.post(f"/v1/sessions/{sid}/findings",
                         json={"findings": [_finding("f-2")]}).json()
    assert second["deferred"] is True, "the second push should be debounced"
    assert second["pending"] == 1
    return sid, second["memory_version"]


def _version(app, sid: str) -> int:
    """Read through `app.state.store`, the seam api.py documents as "no route
    reads it". Going via a route would mean picking one that reports the
    version AND mutates a watermark as it does so."""
    return app.state.store.get_context(sid).memory_version


def test_the_background_loop_drains_a_deferral_with_no_further_push(monkeypatch):
    """THE regression test for the reported symptom. One push merges, the next
    defers, and then NOTHING else happens -- no push, no /synthesize call. The
    working memory must still catch up on its own."""
    app = _app(monkeypatch)
    with TestClient(app) as client:
        sid, deferred_at = _deferred_session(client)

        deadline = time.monotonic() + 5.0
        while time.monotonic() < deadline and _version(app, sid) <= deferred_at:
            time.sleep(0.05)

        assert _version(app, sid) > deferred_at, (
            "the deferred merge never ran: the drain loop is not wired to the "
            "app lifespan, so `deferred: true` is a promise nothing keeps")


def test_the_drain_can_be_switched_off(monkeypatch):
    """The opt-out works -- and, by staying stale, proves the test above is
    measuring the LOOP rather than some other path that happens to merge."""
    monkeypatch.setenv("SYNAPSE_DRAIN_DISABLED", "1")
    app = _app(monkeypatch)
    with TestClient(app) as client:
        sid, deferred_at = _deferred_session(client)
        time.sleep(DRAIN_INTERVAL_S * 4)
        assert _version(app, sid) == deferred_at
