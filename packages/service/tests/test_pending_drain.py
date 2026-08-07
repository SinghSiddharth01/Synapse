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
