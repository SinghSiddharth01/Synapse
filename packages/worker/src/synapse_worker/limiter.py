"""The WORKER -> PROVIDER seam: how much work may be in flight, and how fast.

This is NOT the service-side merge governor. ADR 0005 already bounds how often
the *service* calls the synthesis model, on a token/request ledger for a hosted
key. That seam is upstream of the model and downstream of the push. THIS one is
the other side of the pipeline entirely: `WorkerLoop.tick()` -> `Distiller` ->
whichever provider `SYNAPSE_DISTILLER` selected. Nothing bounded it at all
(FLOW.md 1.3 item 8: "no rate limiter on the distiller side"), which is two
distinct unboundednesses wearing one name:

  1. **Backlog.** `drain()` returns every complete turn it has, and `tick()`
     distilled all of them in one pass. Paste a 5,000-line log into a
     conversation and one tick issues however many model calls that segments
     into, back to back, for as long as it takes.
  2. **Concurrency.** Today `tick()` awaits each `distil()` in sequence, so the
     in-flight count happens to be one. That is an accident of how the loop is
     written, not a property anything checks; `asyncio.gather` over the same
     list is a one-line refactor away from N simultaneous calls into a single
     local NPU, which is exactly the "infinite sessions in parallel" failure
     this module was asked for.

Three bounds, all config-driven (`[worker]` in config/synapse.toml):

  `max_calls_per_tick`       the RATE. At most this many segments reach the
                             provider per tick; the rest wait.
  `max_concurrent_calls`     the CONCURRENCY CEILING. Every worker->provider
                             call goes through `call()`, which holds a
                             semaphore, so the ceiling is enforced rather than
                             implied.
  `max_deferred_segments`    the VISIBLE BOUND on the backlog. The deferred
                             queue is never filled past it, and while work is
                             held back the loop stops reading new transcript
                             bytes.

⟨CORRECTED 2026-08-06, W3b review⟩ That third line used to claim only the
second half, and the code only implemented the second half. `accepting_input`
is checked BEFORE the read and `read_new_lines` returns everything from the
offset to EOF, so a single tick could drain an arbitrary number of segments
into the queue and overshoot without limit -- measured at 196 segments against
a bound of 64 on a `--from-start` attach, rewriting 128 KB of segment JSON
every tick for the ~25 minutes it took to drain. Back-pressure only ever
prevented the NEXT read. The bound is now enforced at BOTH ends: `WorkerLoop`
caps `Segmenter.drain(max_segments=...)` at the queue's remaining headroom, and
what does not fit stays in the segmenter rather than in the queue. Exact to
within one turn -- a turn is never split across ticks.

The read gate moved with it, and had to. On queue depth alone it would now be
dead code: `admit()` takes `max_calls_per_tick` off the queue every tick, so a
queue filled exactly to its bound is back under the bound by the time the next
tick asks, and backpressure could never latch. The honest question is whether
any buffer still holds unconverted work, so the loop checks the segmenter too
(`has_undrained_turns`).

DEFER, NEVER SHED (decisions/002)
---------------------------------
Beyond the bound, work waits; it is never discarded. Shedding a segment is
indistinguishable, from the outside, from a segment that distilled to nothing —
and this system's whole failure mode is silent loss that looks like success
("findings landed, memory unchanged"). Deferral is safe here for a reason
specific to this seam: the transcript on disk is the durable queue. Refusing to
read further bytes leaves them exactly where they are, behind the follower's
offset, and the next tick with room picks them up. Nothing needs to be held in
memory to avoid losing it. The deferred segments that ARE held in memory are
persisted next to the pending-events buffer for the same reason `loop.py`
persists that one — see its module docstring's crash-safety ordering.

The bound is visible in four places at once, deliberately: a WARNING log line
when it engages, a `limiter` stats event for `/debug`, `TickResult.deferred` /
`.backpressured` fields, and the tick summary line the CLI prints.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Awaitable, Callable, TypeVar

logger = logging.getLogger(__name__)

T = TypeVar("T")

# Defaults, and why these numbers.
#
# CALLS_PER_TICK = 4. One distillation is ~10s on the NPU and the shipped poll
# interval is 30s (config/synapse.toml:107), so ~3 calls is what a tick's own
# period actually pays for. 4 leaves a little headroom to catch up after a
# quiet stretch without letting a single paste occupy the provider for an hour
# with nothing in the logs saying so.
DEFAULT_MAX_CALLS_PER_TICK = 4

# CONCURRENT_CALLS = 1. The distiller's shipped arm is one local NPU serving
# one request at a time (config/synapse.toml:132), so more in flight is not
# more throughput, it is a queue somewhere less visible. The value is a knob
# rather than a constant because the cloud arms (SYNAPSE_DISTILLER=anthropic /
# claude-cli) genuinely do serve concurrent calls; raising it is a deliberate
# act with a number attached.
DEFAULT_MAX_CONCURRENT_CALLS = 1

# DEFERRED_SEGMENTS = 64. At 4/tick and a 30s interval that is 8 minutes of
# backlog — long enough to absorb a burst, short enough that an operator
# watching the tick summary sees the number climb before it is hours. Past it,
# holding MORE segments in memory buys nothing: the unread transcript is on
# disk and the follower will still be there.
DEFAULT_MAX_DEFERRED_SEGMENTS = 64


class SeamLimiter:
    """Bounds on what one worker may ask of one provider.

    Stateless with respect to WHAT is queued — the caller owns its backlog and
    hands this object counts and lists. That keeps the limiter testable on its
    own and keeps `WorkerLoop` the only thing that knows a `Segment` from a
    hole in the ground.
    """

    def __init__(
        self,
        *,
        max_calls_per_tick: int = DEFAULT_MAX_CALLS_PER_TICK,
        max_concurrent_calls: int = DEFAULT_MAX_CONCURRENT_CALLS,
        max_deferred_segments: int = DEFAULT_MAX_DEFERRED_SEGMENTS,
    ) -> None:
        # A bound of zero is not "no limit", it is "no work ever", and a
        # negative one is a typo. Both are refused at construction rather than
        # producing a worker that silently does nothing — the single most
        # expensive shape of bug in a pipeline whose success signal is
        # "findings landed".
        for name, value in (
            ("max_calls_per_tick", max_calls_per_tick),
            ("max_concurrent_calls", max_concurrent_calls),
            ("max_deferred_segments", max_deferred_segments),
        ):
            if value < 1:
                raise ValueError(f"{name} must be >= 1, got {value!r}")

        self.max_calls_per_tick = int(max_calls_per_tick)
        self.max_concurrent_calls = int(max_concurrent_calls)
        self.max_deferred_segments = int(max_deferred_segments)

        # Constructed outside a running loop (WorkerLoop.__init__ runs from
        # cli._build, synchronously). Safe since 3.10: asyncio primitives no
        # longer bind to a loop at construction time.
        self._semaphore = asyncio.Semaphore(self.max_concurrent_calls)
        self.in_flight = 0
        # High-water mark, never reset. This is what a test asserts against:
        # a bound that is not enforced shows up here as a peak above the
        # ceiling, which no amount of "it looked sequential" can hide.
        self.peak_in_flight = 0
        self.calls_made = 0

    # -- the RATE bound ------------------------------------------------

    def admit(self, queued: list[T]) -> tuple[list[T], list[T]]:
        """Split a backlog into `(run now, still deferred)`, oldest first.

        FIFO on purpose. A segment's value decays — it is context for a
        conversation still in progress — but reordering would hand synthesis
        findings out of causal order, and the working memory is a fold over
        that order. Slow is recoverable; scrambled is not.
        """
        return queued[:self.max_calls_per_tick], queued[self.max_calls_per_tick:]

    # -- the BACKLOG bound ---------------------------------------------

    def accepting_input(self, deferred_depth: int) -> bool:
        """Whether the caller may take on more work this round.

        False means BACKPRESSURE, not shed: the caller must leave its input
        unread, not drop it.
        """
        return deferred_depth < self.max_deferred_segments

    # -- the CONCURRENCY bound -----------------------------------------

    async def call(self, make_call: Callable[[], Awaitable[T]]) -> T:
        """Run one provider call under the concurrency ceiling.

        Takes a factory rather than a coroutine so nothing is even *created*
        until a permit is held — an un-awaited coroutine waiting on a
        semaphore is a warning-emitting object with a live reference to its
        arguments, and at a deep backlog there would be one per queued call.

        Exceptions propagate unchanged: this is a gate, not a supervisor.
        `WorkerLoop` already has the per-segment try/except that decides a
        failed distillation must not kill the tick, and duplicating that
        judgement here would give a future caller two places to look for it.
        """
        async with self._semaphore:
            self.in_flight += 1
            self.peak_in_flight = max(self.peak_in_flight, self.in_flight)
            self.calls_made += 1
            try:
                return await make_call()
            finally:
                self.in_flight -= 1

    def describe(self) -> str:
        return (f"{self.max_calls_per_tick}/tick, "
                f"{self.max_concurrent_calls} concurrent, "
                f"{self.max_deferred_segments} deferred max")
