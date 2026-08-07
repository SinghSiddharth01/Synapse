"""One worker process, N conversations — the bug and the bound.

The defect, in one sentence: `synapse up` starts ONE worker, a `WorkerLoop`
follows exactly ONE transcript resolved at boot, so the second Claude Code
window to bind on a machine was never distilled — silently, because the process
stays healthy and keeps ticking on the first window's file.

Nothing here fabricates the thing under test. Two real `WorkerLoop`s over two
real transcripts drive a real `Distiller`, and the assertions count findings
that actually reached each conversation's sink.
"""

from __future__ import annotations

import json
from pathlib import Path

from synapse_contracts import LocalBinding
from synapse_distiller import Distiller, load_pack_by_name
from synapse_providers import FakeProvider

from synapse_worker.limiter import SeamLimiter
from synapse_worker.loop import WorkerLoop
from synapse_worker.producer import FileSink, Producer
from synapse_worker.supervisor import WorkerSupervisor

PACK = load_pack_by_name("v4-condense")
TS = "2026-08-04T09:12:00.000Z"


def _line(session: str, **kwargs) -> str:
    base = {"sessionId": session, "timestamp": TS, "cwd": "/repo", "gitBranch": "main"}
    return json.dumps({**base, **kwargs}) + "\n"


def turns(session: str, n: int, topic: str) -> str:
    """`n` complete turns plus the opener of one more — the segmenter holds the
    newest turn back until it can see a boundary. Assistant text is long enough
    to clear triage's substance gate."""
    out = []
    for i in range(n):
        out.append(_line(session, type="user",
                         message={"content": [{"type": "text", "text": f"{topic} {i}?"}]}))
        out.append(_line(session, type="assistant", message={"content": [{
            "type": "text",
            "text": (f"{topic} {i}: the connection pooler runs in transaction "
                     "mode, so prepared statements are not reused across "
                     "checkouts and the driver must disable them explicitly.")}]}))
    out.append(_line(session, type="user",
                     message={"content": [{"type": "text", "text": "and then?"}]}))
    return "".join(out)


class CountingProvider(FakeProvider):
    """Real call count plus a simultaneity high-water mark, so "one at a time"
    is measured rather than assumed."""

    def __init__(self, scripts):
        super().__init__(scripts=scripts)
        self.in_flight = 0
        self.peak_in_flight = 0

    async def complete(self, messages, response_schema=None):
        self.in_flight += 1
        self.peak_in_flight = max(self.peak_in_flight, self.in_flight)
        try:
            return await super().complete(messages, response_schema)
        finally:
            self.in_flight -= 1


def _loop(tmp_path: Path, session: str, shared_id: str, *, limiter, provider=None,
          n_turns: int = 2):
    """One conversation, in the layout two windows on one machine produce: a
    shared state_dir, its own transcript, its own sink."""
    transcript = tmp_path / f"{session}.jsonl"
    transcript.write_text(turns(session, n_turns, session), encoding="utf-8")
    binding = LocalBinding(agent_session_id=session, shared_id=shared_id,
                           contributor="akhil", agent="claude-code")
    provider = provider or CountingProvider(
        [{"findings": [{"type": "learning", "text": f"learned in {session}"}]}] * 8)
    return WorkerLoop(
        transcript=transcript,
        distiller=Distiller(provider, binding, PACK, ["text"], "labelled"),
        producer=Producer(tmp_path / "wal" / session,
                          FileSink(tmp_path / f"up-{session}.jsonl")),
        binding=binding,
        state_dir=tmp_path / "state",
        budget_tokens=5000,
        limiter=limiter,
    ), provider


def _sank(tmp_path: Path, session: str) -> list[dict]:
    path = tmp_path / f"up-{session}.jsonl"
    if not path.is_file():
        return []
    return [json.loads(x) for x in path.read_text(encoding="utf-8").splitlines() if x]


async def test_every_bound_conversation_is_distilled_not_just_the_first(tmp_path) -> None:
    """THE bug. One process, two conversations, one tick each — before the
    supervisor, window B produced nothing at all and there was no error to see:
    the worker was healthy and tailing window A.

    Both sinks are asserted, and the findings are matched to their OWN
    conversation, because "two files have content" would also pass if one
    conversation's segments were distilled twice under the wrong binding."""
    limiter = SeamLimiter()
    loop_a, _ = _loop(tmp_path, "conv-a", "sh-a", limiter=limiter)
    loop_b, _ = _loop(tmp_path, "conv-b", "sh-b", limiter=limiter)

    results = await WorkerSupervisor([loop_a, loop_b]).tick()

    assert len(results) == 2
    assert all(r.findings > 0 for r in results), (
        f"a conversation produced nothing: {[r.summary() for r in results]}")

    for session in ("conv-a", "conv-b"):
        landed = _sank(tmp_path, session)
        assert landed, f"{session} was followed but nothing reached its sink"
        sessions = {a["agent_session"]
                    for f in landed for a in f["attributions"]}
        assert sessions == {session}, (
            f"{session}'s sink carried another conversation's work: {sessions}")


async def test_one_conversation_failing_does_not_stop_the_others(tmp_path) -> None:
    """With N conversations in one process, "a tick must never die" has to hold
    ACROSS conversations too. Otherwise one window's bad transcript line stops
    distilling every other window on the machine — a strictly worse failure
    than the one-process-per-window topology this replaces."""
    limiter = SeamLimiter()
    healthy, _ = _loop(tmp_path, "conv-ok", "sh-ok", limiter=limiter)
    broken, _ = _loop(tmp_path, "conv-bad", "sh-bad", limiter=limiter)

    async def explode():
        raise RuntimeError("transcript parse blew up")

    broken.tick = explode

    results = await WorkerSupervisor([broken, healthy]).tick()

    assert len(results) == 1, "the surviving conversation did not tick"
    assert results[0].findings > 0
    assert _sank(tmp_path, "conv-ok"), "the healthy conversation lost its work"


async def test_the_provider_seam_stays_one_call_at_a_time_across_conversations(
    tmp_path,
) -> None:
    """The whole reason one process beats N.

    `SeamLimiter`'s concurrency semaphore is the real ceiling on how hard the
    NPU is driven, and it is per-PROCESS: two `synapse-worker run` processes
    each hold their own, so two windows put 2x the intended load on one device.
    One supervisor sharing one limiter makes it a ceiling again.

    Asserted on a measured high-water mark from the provider itself, not on the
    limiter's own bookkeeping — a shared limiter that failed to serialise would
    still report the right configuration."""
    limiter = SeamLimiter(max_concurrent_calls=1)
    shared_provider = CountingProvider(
        [{"findings": [{"type": "learning", "text": "shared seam"}]}] * 16)
    loop_a, _ = _loop(tmp_path, "conv-a", "sh-a", limiter=limiter,
                      provider=shared_provider)
    loop_b, _ = _loop(tmp_path, "conv-b", "sh-b", limiter=limiter,
                      provider=shared_provider)

    await WorkerSupervisor([loop_a, loop_b]).tick()

    assert shared_provider.calls > 1, "no model calls were made; the bound is untested"
    assert shared_provider.peak_in_flight == 1, (
        f"two conversations drove {shared_provider.peak_in_flight} concurrent "
        "provider calls through a ceiling of 1")


async def test_shutdown_flushes_every_conversation(tmp_path) -> None:
    """Shutdown is the last chance a deferred segment gets — its transcript
    bytes are already behind the follower's offset, so a segment stranded here
    is conversation nothing will ever re-read. Draining only the first
    conversation would strand every other window's backlog permanently.

    `max_calls_per_tick=1` is what makes this a real test rather than a
    tautology: the tick admits one segment per conversation and DEFERS the
    rest, so both loops reach shutdown holding work that only shutdown's
    full-drain contract can clear."""
    limiter = SeamLimiter(max_calls_per_tick=1)
    loop_a, _ = _loop(tmp_path, "conv-a", "sh-a", limiter=limiter, n_turns=4)
    loop_b, _ = _loop(tmp_path, "conv-b", "sh-b", limiter=limiter, n_turns=4)
    supervisor = WorkerSupervisor([loop_a, loop_b])

    ticked = await supervisor.tick()
    assert all(r.deferred > 0 for r in ticked), (
        f"nothing was deferred, so shutdown has nothing to prove: "
        f"{[r.summary() for r in ticked]}")
    before = {s: len(_sank(tmp_path, s)) for s in ("conv-a", "conv-b")}

    results = await supervisor.shutdown()

    assert len(results) == 2
    assert all(loop._deferred == [] for loop in (loop_a, loop_b)), (
        "a conversation's backlog survived shutdown — it is now unreachable")
    for session in ("conv-a", "conv-b"):
        assert len(_sank(tmp_path, session)) > before[session], (
            f"{session}'s deferred work was never drained")
