"""The WORKER -> PROVIDER seam's three bounds, asserted so they FAIL when broken.

The ADR-0005 trap this file is written against: the test that covered the
truncation bug asserted on a hand-written `"finish_reason": "length"` the host
never sends, and passed happily throughout a twelve-round outage. So nothing
here fabricates the thing under test. The concurrency assertions drive real
coroutines through the real `SeamLimiter.call`, and the loop assertions drive a
real transcript through a real `WorkerLoop` and count the model calls a real
`Distiller` actually made. Delete the semaphore, delete the `admit()` slice, or
delete the backpressure check, and the corresponding test fails on a number.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest
from synapse_contracts import LocalBinding
from synapse_distiller import Distiller, load_pack_by_name
from synapse_providers import FakeProvider

from synapse_worker.limiter import SeamLimiter
from synapse_worker.loop import WorkerLoop
from synapse_worker.producer import FileSink, Producer

PACK = load_pack_by_name("v4-condense")
BINDING = LocalBinding(
    agent_session_id="sess-1", shared_id="shared-1",
    contributor="aditya", agent="claude-code",
)
TS = "2026-08-04T09:12:00.000Z"


def _line(**kwargs) -> str:
    base = {"sessionId": "sess-1", "timestamp": TS, "cwd": "/repo", "gitBranch": "main"}
    return json.dumps({**base, **kwargs}) + "\n"


def user(text: str) -> str:
    return _line(type="user", message={"content": [{"type": "text", "text": text}]})


def assistant(text: str) -> str:
    return _line(type="assistant", message={"content": [{"type": "text", "text": text}]})


def condensed(text: str) -> dict:
    return {"findings": [{"type": "learning", "text": text}]}


def turns(n: int) -> str:
    """`n` COMPLETE turns plus the opener of one more.

    The segmenter holds the newest turn back until it can see a boundary
    (segmenter.py's module docstring), so the trailing opener is what makes the
    first `n` complete. Each turn's assistant text is long enough to survive
    triage's substance gate.
    """
    out = []
    for i in range(n):
        out.append(user(f"question {i}"))
        out.append(assistant(
            f"answer {i}: the connection pooler runs in transaction mode, so "
            f"prepared statements are not reused across checkouts and the "
            f"migration for change {i} has to declare them per session."))
    out.append(user("one more thing"))
    return "".join(out)


class CountingProvider(FakeProvider):
    """FakeProvider (whose `.calls` already counts completions) plus a
    simultaneity high-water mark. Counts the REAL calls the limiter let
    through — nothing about the number is supplied by the test."""

    def __init__(self, scripts, *, delay: float = 0.0):
        super().__init__(scripts=scripts)
        self.in_flight = 0
        self.peak_in_flight = 0
        self._delay = delay

    async def complete(self, messages, response_schema=None):
        self.in_flight += 1
        self.peak_in_flight = max(self.peak_in_flight, self.in_flight)
        try:
            if self._delay:
                await asyncio.sleep(self._delay)
            return await super().complete(messages, response_schema)
        finally:
            self.in_flight -= 1


class DyingProvider(FakeProvider):
    """Answers `die_after` calls, then raises on every one after that — a model
    server stopped mid-run, which is the teardown order a demo actually has."""

    def __init__(self, scripts, *, die_after: int):
        super().__init__(scripts=scripts)
        self._die_after = die_after
        self._n = 0

    async def complete(self, messages, response_schema=None):
        self._n += 1
        if self._n > self._die_after:
            raise RuntimeError("provider is down")
        return await super().complete(messages, response_schema)


def build(tmp_path, *, limiter: SeamLimiter, scripts, provider=None):
    transcript = tmp_path / "t.jsonl"
    transcript.touch()
    provider = provider or CountingProvider(scripts)
    producer = Producer(tmp_path / "wal", FileSink(tmp_path / "upstream.jsonl"))
    loop = WorkerLoop(
        transcript=transcript,
        distiller=Distiller(provider, BINDING, PACK, ["text"], "labelled"),
        producer=producer,
        binding=BINDING,
        state_dir=tmp_path / "state",
        budget_tokens=5000,
        limiter=limiter,
    )
    return loop, transcript, provider


# --------------------------------------------------------------------------
# The CONCURRENCY bound
# --------------------------------------------------------------------------

@pytest.mark.parametrize("ceiling", [1, 2, 5])
async def test_concurrent_provider_calls_never_exceed_the_ceiling(ceiling) -> None:
    """20 callers, `ceiling` permits. Peak in-flight must equal the ceiling
    exactly: below it the limiter is throttling more than it claims, above it
    the bound is not a bound. Remove the semaphore from `call()` and the peak
    goes to 20."""
    limiter = SeamLimiter(max_concurrent_calls=ceiling)
    observed = {"in_flight": 0, "peak": 0}
    release = asyncio.Event()

    async def work() -> None:
        observed["in_flight"] += 1
        observed["peak"] = max(observed["peak"], observed["in_flight"])
        # Every admitted caller parks here, so the peak is the true simultaneous
        # count and not an artefact of how fast each one finished.
        await release.wait()
        observed["in_flight"] -= 1

    tasks = [asyncio.create_task(limiter.call(work)) for _ in range(20)]
    # Let everything that CAN start, start.
    for _ in range(50):
        await asyncio.sleep(0)
    assert observed["peak"] == ceiling
    assert limiter.peak_in_flight == ceiling

    release.set()
    await asyncio.gather(*tasks)
    assert observed["in_flight"] == 0
    assert limiter.calls_made == 20      # bounded, not shed


async def test_the_bound_is_a_ceiling_on_simultaneity_not_a_cap_on_total() -> None:
    """A limiter that met the concurrency assertion by dropping callers would
    be worse than no limiter. All 20 must complete."""
    limiter = SeamLimiter(max_concurrent_calls=3)
    done: list[int] = []

    async def work(i: int) -> None:
        await asyncio.sleep(0)
        done.append(i)

    await asyncio.gather(*(limiter.call(lambda i=i: work(i)) for i in range(20)))
    assert sorted(done) == list(range(20))
    assert limiter.peak_in_flight <= 3


async def test_a_raising_call_releases_its_permit() -> None:
    """A leaked permit is a limiter that ratchets down to zero throughput and
    then hangs — the failure mode that is hardest to tell from a dead provider.
    """
    limiter = SeamLimiter(max_concurrent_calls=1)

    async def boom() -> None:
        raise RuntimeError("provider said no")

    for _ in range(3):
        with pytest.raises(RuntimeError):
            await limiter.call(boom)
    assert limiter.in_flight == 0

    async def fine() -> str:
        return "ok"

    assert await limiter.call(fine) == "ok"


@pytest.mark.parametrize("kwargs", [
    {"max_calls_per_tick": 0},
    {"max_concurrent_calls": 0},
    {"max_deferred_segments": 0},
    {"max_calls_per_tick": -1},
])
def test_a_bound_below_one_is_refused_at_construction(kwargs) -> None:
    """Zero is not "unlimited", it is "never do any work" — a worker that
    silently distils nothing while reporting healthy ticks."""
    with pytest.raises(ValueError):
        SeamLimiter(**kwargs)


# --------------------------------------------------------------------------
# The RATE bound
# --------------------------------------------------------------------------

async def test_a_tick_makes_at_most_max_calls_per_tick_provider_calls(tmp_path) -> None:
    """Six complete turns arrive at once; the bound is two. The provider must
    see two calls, not six. Counted off the provider, so removing `admit()`
    from the loop fails this on 6 != 2."""
    limiter = SeamLimiter(max_calls_per_tick=2, max_deferred_segments=64)
    loop, transcript, provider = build(
        tmp_path, limiter=limiter, scripts=[condensed(f"f{i}") for i in range(6)])
    transcript.write_text(turns(6), encoding="utf-8")

    result = await loop.tick()

    assert provider.calls == 2
    assert result.segments == 2
    assert result.deferred == 4


async def test_deferred_segments_are_distilled_by_later_ticks_not_dropped(tmp_path) -> None:
    """The whole decision (decisions/002): over the bound is LATER, never
    NEVER. Three more ticks with no new transcript bytes must drain the
    backlog and land every finding."""
    limiter = SeamLimiter(max_calls_per_tick=2, max_deferred_segments=64)
    loop, transcript, provider = build(
        tmp_path, limiter=limiter, scripts=[condensed(f"f{i}") for i in range(6)])
    transcript.write_text(turns(6), encoding="utf-8")

    results = [await loop.tick() for _ in range(3)]

    assert [r.segments for r in results] == [2, 2, 2]
    assert [r.deferred for r in results] == [4, 2, 0]
    assert provider.calls == 6
    landed = [json.loads(x)["text"]
              for x in (tmp_path / "upstream.jsonl").read_text().splitlines() if x]
    assert sorted(landed) == [f"f{i}" for i in range(6)]


async def test_the_deferred_queue_survives_a_restart(tmp_path) -> None:
    """A deferred segment's transcript bytes are already behind the follower's
    offset, so holding it only in memory would be exactly the silent loss
    loop.py's crash-safety ordering exists to forbid."""
    limiter = SeamLimiter(max_calls_per_tick=1, max_deferred_segments=64)
    loop, transcript, _ = build(
        tmp_path, limiter=limiter, scripts=[condensed(f"f{i}") for i in range(3)])
    transcript.write_text(turns(3), encoding="utf-8")

    first = await loop.tick()
    assert first.deferred == 2
    # Per-conversation since 2026-08-07 — asserted through the loop rather than
    # by rebuilding the path here, so this keeps testing "it is on disk" and not
    # "it is at the layout I happened to type".
    assert (loop.session_state_dir / "deferred-segments.json").is_file()

    # A whole new loop over the same state directory: the process died.
    revived, _, provider = build(
        tmp_path, limiter=SeamLimiter(max_calls_per_tick=1),
        scripts=[condensed("f1"), condensed("f2")])
    assert len(revived._deferred) == 2
    await revived.tick()
    await revived.tick()
    assert provider.calls == 2


async def test_deferred_work_drains_without_new_transcript_bytes(tmp_path) -> None:
    """A backlog that only moves when the transcript grows never drains at the
    end of a conversation — which is precisely when the last findings matter.
    So the no-change fast path must not swallow a non-empty queue."""
    limiter = SeamLimiter(max_calls_per_tick=1)
    loop, transcript, provider = build(
        tmp_path, limiter=limiter, scripts=[condensed("a"), condensed("b")])
    transcript.write_text(turns(2), encoding="utf-8")

    first = await loop.tick()
    assert (first.segments, first.deferred) == (1, 1)

    # Nothing appended. The transcript is quiet.
    second = await loop.tick()
    assert second.skipped_no_change is False
    assert (second.segments, second.deferred) == (1, 0)
    assert provider.calls == 2


async def test_shutdown_drains_the_deferred_backlog_in_full(tmp_path) -> None:
    """`max_calls_per_tick` paces a loop that gets another tick. Shutdown does
    not, and its contract is "nothing stranded"."""
    limiter = SeamLimiter(max_calls_per_tick=1)
    # FIVE scripts for five segments — the 4 complete turns plus the trailing
    # open one. ⟨W3b review⟩ This fixture used to supply 4, so the fifth
    # distillation raised on an exhausted script list, and the test passed only
    # because `shutdown()` DROPPED the segment that raised instead of
    # re-queueing it. The bug and the test that should have caught it had the
    # same root: nothing asserted where a failed segment went.
    loop, transcript, provider = build(
        tmp_path, limiter=limiter, scripts=[condensed(f"f{i}") for i in range(5)])
    transcript.write_text(turns(4), encoding="utf-8")

    assert (await loop.tick()).deferred == 3
    result = await loop.shutdown()

    # The 3 deferred, plus the trailing open turn shutdown exists to flush.
    assert result.segments == 4
    assert provider.calls == 5       # tick's 1 + the 3 waiting + the open turn
    assert loop._deferred == []


async def test_shutdown_requeues_what_the_provider_could_not_distil(tmp_path) -> None:
    """DEFER, NEVER SHED — including on the way out.

    `shutdown()` empties `self._deferred` up front, so a segment whose
    distillation raises used to be logged and dropped, and `_persist_state()`
    then wrote `[]` over deferred-segments.json — losing the whole backlog,
    whose transcript bytes are already behind the follower's offset. One
    provider death during teardown (model server stopped first) silently
    discarded every queued segment. Asserted on DISK, because surviving to the
    next run is the entire claim.
    """
    provider = DyingProvider([condensed(f"f{i}") for i in range(6)], die_after=1)
    loop, transcript, _ = build(
        tmp_path, limiter=SeamLimiter(max_calls_per_tick=1), scripts=[],
        provider=provider)
    transcript.write_text(turns(4), encoding="utf-8")

    assert (await loop.tick()).deferred == 3       # 1 distilled, 3 waiting
    await loop.shutdown()                          # provider is dead from here

    # 3 deferred + the trailing open turn = 4 segments shutdown could not distil.
    assert len(loop._deferred) == 4, "a failed segment was dropped, not re-queued"
    on_disk = json.loads(
        (loop.session_state_dir / "deferred-segments.json").read_text(encoding="utf-8"))
    assert len(on_disk) == 4, "the backlog did not survive to the next run"

    # And a fresh loop over the same state dir picks them back up.
    revived, _, _ = build(
        tmp_path, limiter=SeamLimiter(max_calls_per_tick=1),
        scripts=[condensed("later")])
    assert len(revived._deferred) == 4


# --------------------------------------------------------------------------
# The BACKLOG bound — backpressure, and its visibility
# --------------------------------------------------------------------------

async def test_at_the_backlog_bound_the_loop_stops_reading_new_input(tmp_path) -> None:
    """Backpressure, not shedding: the follower's offset must not advance past
    bytes nothing is going to distil. Asserted on the offset, because that is
    the thing that decides whether the bytes are recoverable."""
    limiter = SeamLimiter(max_calls_per_tick=1, max_deferred_segments=2)
    loop, transcript, _ = build(
        tmp_path, limiter=limiter, scripts=[condensed(f"f{i}") for i in range(8)])
    transcript.write_text(turns(4), encoding="utf-8")

    first = await loop.tick()
    # AT the bound, never over it ⟨W3b review⟩. This asserted (1, 3) against a
    # `max_deferred_segments` of 2 — the old drain converted every complete turn
    # the read returned, so the "bound" only ever governed the NEXT read and the
    # queue could overshoot without limit (measured: 196 against a bound of 64).
    assert (first.segments, first.deferred) == (1, 1)
    assert first.deferred <= limiter.max_deferred_segments

    transcript.write_text(turns(4) + turns(4), encoding="utf-8")
    offsets_before = dict(loop.follower.state.offsets)
    second = await loop.tick()

    assert second.backpressured is True
    assert second.new_lines == 0
    assert loop.follower.state.offsets == offsets_before   # bytes still on disk


async def test_backpressure_lifts_and_the_unread_bytes_are_still_there(tmp_path) -> None:
    """The other half of "defer, never shed": once the queue drains below the
    bound the loop reads the bytes it declined to read, and they distil."""
    limiter = SeamLimiter(max_calls_per_tick=1, max_deferred_segments=2)
    loop, transcript, provider = build(
        tmp_path, limiter=limiter, scripts=[condensed(f"f{i}") for i in range(9)])
    transcript.write_text(turns(3), encoding="utf-8")

    await loop.tick()                       # 1 admitted, 2 deferred -> at bound
    grown = turns(3) + user("late question") + assistant(
        "late answer: the pooler's transaction mode is why the prepared "
        "statement cache has to be declared per session, every time.") + user("end")
    transcript.write_text(grown, encoding="utf-8")

    backpressured = await loop.tick()
    assert backpressured.backpressured is True

    # Queue drains; the declined bytes are read on the tick that has room.
    later = [await loop.tick() for _ in range(4)]
    assert any(r.new_lines > 0 for r in later), "the declined bytes were never read"
    # 3 turns from the first write, plus the two the append completed. The
    # count is the point: every byte written while backpressured reached the
    # provider eventually, and none of them twice.
    assert provider.calls == 5


async def test_deferral_is_visible_in_the_log_not_only_in_the_behaviour(
        tmp_path, caplog) -> None:
    """A limiter whose only evidence is that things got slower is
    indistinguishable from a broken provider. The bound, the count and the
    word "deferred" have to be readable by an operator."""
    limiter = SeamLimiter(max_calls_per_tick=1, max_deferred_segments=2)
    loop, transcript, _ = build(
        tmp_path, limiter=limiter, scripts=[condensed(f"f{i}") for i in range(6)])
    transcript.write_text(turns(4), encoding="utf-8")

    with caplog.at_level("INFO", logger="synapse_worker.loop"):
        await loop.tick()
    rate = [r for r in caplog.records if "Provider rate limit" in r.getMessage()]
    assert rate, "a deferred segment produced no log line at all"
    # 1, not 3: the drain is capped at the backlog bound (2) and `admit` takes 1.
    assert "1 deferred" in rate[0].getMessage()
    assert "NOT dropped" in rate[0].getMessage()

    caplog.clear()
    transcript.write_text(turns(8), encoding="utf-8")
    with caplog.at_level("WARNING", logger="synapse_worker.loop"):
        await loop.tick()
    pressure = [r for r in caplog.records if "BACKPRESSURE" in r.getMessage()]
    assert pressure, "backpressure engaged with nothing said about it"
    assert pressure[0].levelname == "WARNING"


async def test_deferral_is_visible_in_the_debug_feed(tmp_path) -> None:
    """The dashboard's half of the same requirement — a `limiter` event with
    the real numbers, not just a slower tick rate."""
    from synapse_providers import CallLog
    from synapse_worker.stats import StatsBuffer

    limiter = SeamLimiter(max_calls_per_tick=1, max_deferred_segments=64)
    transcript = tmp_path / "t.jsonl"
    transcript.touch()
    stats = StatsBuffer(CallLog())
    loop = WorkerLoop(
        transcript=transcript,
        distiller=Distiller(CountingProvider([condensed("a"), condensed("b")]),
                            BINDING, PACK, ["text"], "labelled"),
        producer=Producer(tmp_path / "wal", FileSink(tmp_path / "upstream.jsonl")),
        binding=BINDING,
        state_dir=tmp_path / "state",
        budget_tokens=5000,
        stats=stats,
        limiter=limiter,
    )
    transcript.write_text(turns(3), encoding="utf-8")

    result = await loop.tick()

    events = [e for e in stats.snapshot()["events"] if e["tag"] == "limiter"]
    assert events, "no limiter event reached the dashboard feed"
    assert events[0]["detail"] == {
        "admitted": 1, "deferred": 2, "bound": 1, "backpressured": False,
    }
    # And the operator-facing one-liner says so too.
    assert "2 deferred (provider rate)" in result.summary()


async def test_the_summary_never_reports_no_change_while_work_is_queued(
        tmp_path) -> None:
    """"no change" next to an undistilled backlog is the exact misreading the
    visible bound exists to prevent."""
    # A bound of 4 with 1 call/tick leaves a standing queue after the first
    # tick. ⟨W3b review⟩ This used a bound of 1, which now empties every tick —
    # the drain is capped at the bound and `admit` takes the whole of it — so
    # the test was asserting on a queue that no longer stands still.
    limiter = SeamLimiter(max_calls_per_tick=1, max_deferred_segments=4)
    loop, transcript, _ = build(
        tmp_path, limiter=limiter, scripts=[condensed(f"f{i}") for i in range(6)])
    transcript.write_text(turns(4), encoding="utf-8")
    await loop.tick()

    quiet = await loop.tick()
    assert quiet.deferred > 0
    assert quiet.summary() != "no change"
    assert "deferred" in quiet.summary()


# --------------------------------------------------------------------------
# The bounds are config-driven
# --------------------------------------------------------------------------

def test_the_bounds_come_from_config_not_from_a_constant(tmp_path, monkeypatch) -> None:
    """`cli._build` must hand the loop the operator's numbers. A limiter whose
    values can only be changed in Python is a limiter nobody will change."""
    from synapse_distiller.config import load_config

    monkeypatch.setenv("SYNAPSE_MAX_CALLS_PER_TICK", "7")
    monkeypatch.setenv("SYNAPSE_MAX_CONCURRENT_CALLS", "3")
    monkeypatch.setenv("SYNAPSE_MAX_DEFERRED_SEGMENTS", "11")

    worker = load_config().worker
    assert (worker.max_calls_per_tick, worker.max_concurrent_calls,
            worker.max_deferred_segments) == (7, 3, 11)

    limiter = SeamLimiter(
        max_calls_per_tick=worker.max_calls_per_tick,
        max_concurrent_calls=worker.max_concurrent_calls,
        max_deferred_segments=worker.max_deferred_segments,
    )
    assert limiter.describe() == "7/tick, 3 concurrent, 11 deferred max"


def test_cli_build_wires_the_configured_limiter_into_the_loop() -> None:
    """Pinned by reading the source rather than by running `run`: the point is
    that the ONE production construction site passes a configured limiter, and
    a future edit that drops the argument silently restores the unbounded
    seam."""
    import ast
    import inspect

    from synapse_worker import cli

    tree = ast.parse(inspect.getsource(cli._build))
    calls = [n for n in ast.walk(tree)
             if isinstance(n, ast.Call) and getattr(n.func, "id", "") == "WorkerLoop"]
    assert len(calls) == 1
    limiter_kw = [k for k in calls[0].keywords if k.arg == "limiter"]
    assert limiter_kw, "cli._build no longer bounds the worker->provider seam"
    source = ast.unparse(limiter_kw[0].value)
    assert "SeamLimiter" in source
    for field in ("max_calls_per_tick", "max_concurrent_calls",
                  "max_deferred_segments"):
        assert f"worker_cfg.{field}" in source
