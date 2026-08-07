"""One worker process, N conversations — a supervisor over `WorkerLoop`s.

Until this existed, one `synapse-worker run` followed exactly one transcript,
resolved once at boot and never re-resolved (`cli._build`). Two Claude Code
windows on one machine therefore meant two `run` processes, which is what
`cli.py` documents as the answer and what `synapse up` does NOT do: it starts
one worker, so the SECOND window to bind was silently never distilled. The
symptom is absence — a healthy worker, ticking on schedule, tailing a
transcript nobody is writing to any more — and nothing in `synapse health`
reports which file a follower has open.

WHY A SUPERVISOR RATHER THAN AN N-TRANSCRIPT `WorkerLoop`
---------------------------------------------------------
`WorkerLoop.tick()` is ~250 lines whose entire subject is not losing
conversation: the ordering of read/segment/drain/admit/persist, the two halves
of the backlog bound, and the shutdown re-queue all exist because a specific
silent-loss bug was found there. Teaching that method to interleave N
conversations means rewriting all of it, and a mistake is invisible — the
finding still looks valid, it is just attributed to the wrong conversation.

So the loop is left exactly as it is, and its per-conversation durable state is
already namespaced per Agent Session (loop.py, 2026-08-07). This owns N of
them and shares the things that MUST be shared.

WHAT IS SHARED, AND WHY EACH
----------------------------
- `SeamLimiter` — the point of one process. Its concurrency semaphore is the
  real bound on how hard the NPU is driven, and N separate processes each hold
  their own, so two windows could put 2x the intended concurrent load on one
  device. One instance, passed to every loop, makes it a bound again.
- `StatsBuffer` — one dashboard for the machine. Safe ONLY because ticking is
  sequential; see below.
- `TriageLog` — append-only, one readable audit trail.

NOT shared: the `Producer`. Each loop calls `producer.rebind()` on its own
binding in `__init__` and again every tick via `_sync_binding_from_disk`, so a
shared one would have its `shared_id` flipped by whichever loop ticked last and
`record()` would tag findings with another conversation's Shared Session.
Per-loop Producers over per-loop WAL directories cost nothing and remove the
question.

SEQUENTIAL, ON PURPOSE
----------------------
`tick()` awaits each loop in turn rather than gathering them. Three reasons,
and the first is a correctness bug the parallel version would introduce:

1. `StatsBuffer.distil_started`/`distil_finished` is a SINGLE in-flight slot
   (stats.py). Two concurrent distillations overwrite each other's `current`
   and the first `distil_finished` blanks it for both, so the dashboard reports
   an idle NPU while two calls are in flight.
2. The limiter's `max_calls_per_tick` is per `admit()` call, so N loops
   admitting concurrently would each take a full tick's budget at the same
   instant. Sequential ticking does not fix that arithmetic — see the note in
   `tick()` — but it does keep the resulting model calls one-at-a-time.
3. A tick is I/O-light until it reaches the provider, and the provider is
   already serialised by the shared limiter's semaphore. Parallelism here would
   buy contention, not throughput.
"""

from __future__ import annotations

import asyncio
import logging

from synapse_worker.loop import TickResult, WorkerLoop

logger = logging.getLogger(__name__)


class WorkerSupervisor:
    """N `WorkerLoop`s in one process, ticked in turn."""

    def __init__(self, loops: list[WorkerLoop]) -> None:
        if not loops:
            raise ValueError(
                "a supervisor with no loops would tick forever doing nothing, "
                "which is indistinguishable from the bug this class exists to "
                "fix — build it with at least one conversation")
        self.loops = loops

    @property
    def transcripts(self) -> list:
        return [loop.transcript for loop in self.loops]

    async def tick(self) -> list[TickResult]:
        """One tick of every conversation, in turn.

        A loop that raises does NOT stop the others: `WorkerLoop.run` already
        treats a failed tick as survivable ("the loop outlives any one tick"),
        and with N conversations in one process that property has to hold
        ACROSS conversations too — otherwise one window's bad transcript line
        silently stops distilling every other window on the machine, which is
        the exact failure mode this class was written to remove.
        """
        results: list[TickResult] = []
        for loop in self.loops:
            try:
                results.append(await loop.tick())
            except Exception:  # noqa: BLE001 — one conversation must not sink the rest
                logger.exception(
                    "Tick failed for %s; the other %d conversation(s) continue",
                    loop.transcript, len(self.loops) - 1)
        return results

    async def run(self, interval_seconds: float, max_ticks: int | None = None) -> None:
        tick_number = 0
        while max_ticks is None or tick_number < max_ticks:
            tick_number += 1
            results = await self.tick()
            for loop, result in zip(self.loops, results):
                logger.info("tick %d [%s] — %s", tick_number,
                            loop.binding.agent_session_id, result.summary())
            if max_ticks is None or tick_number < max_ticks:
                await asyncio.sleep(interval_seconds)

    async def shutdown(self) -> list[TickResult]:
        """Flush every conversation's open turn.

        Every loop is attempted even if an earlier one raises, for the same
        reason `tick()` isolates failures — and more sharply here, because
        shutdown is the last chance a deferred segment gets: its transcript
        bytes are already behind the follower's offset.
        """
        results: list[TickResult] = []
        for loop in self.loops:
            try:
                results.append(await loop.shutdown())
            except Exception:  # noqa: BLE001
                logger.exception(
                    "Shutdown failed for %s; still shutting down the rest",
                    loop.transcript)
        return results
