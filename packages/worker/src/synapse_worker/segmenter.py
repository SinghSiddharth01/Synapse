"""Group AgentEvents into Segments on turn boundary AND token budget.

The subtlety that makes periodic capture work
---------------------------------------------
A timer fires whenever it fires — usually in the middle of a turn, while the
agent is still working. Distilling a half-written turn produces a half finding:
the error is there but not the pivot, the decision is there but not its
consequence. So the segmenter **holds the final turn back** until it can see
that a new one has started, and only then emits it as complete.

That means a turn is emitted one boundary late, which is correct but would
strand the last turn forever once the developer stops typing. Hence the idle
flush: if nothing has arrived for a while, the pending turn is emitted anyway.

What counts as a turn boundary
------------------------------
A *user text* event — an actual human prompt. Not merely `role == "user"`,
because Claude Code records tool results with `role: "user"` too, and treating
those as boundaries would cut a turn at every tool call, which is precisely the
mid-turn fragmentation this exists to avoid.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field

from synapse_contracts import AgentEvent, Segment

logger = logging.getLogger(__name__)

# Matches promptpack's estimator. Over-counting is the safe direction: it makes
# segments smaller than the budget rather than larger, and an oversized segment
# is silently truncated at the model's context boundary.
_CHARS_PER_TOKEN = 3.5


def estimate_tokens(events: list[AgentEvent]) -> int:
    return int(sum(len(e.content) for e in events) / _CHARS_PER_TOKEN) + 1


def is_turn_boundary(event: AgentEvent) -> bool:
    """A human prompt starts a new turn. A tool result does not."""
    return event.role == "user" and event.kind == "text"


def _split_oversized(event: AgentEvent, budget_tokens: int) -> list[AgentEvent]:
    """`event`, or the pieces of it that each fit the budget.

    Cutting inside one event's content is a genuine loss of coherence, so it is
    done only when the alternative is worse: an event over the budget cannot be
    distilled at all, and is dropped after the model fails on it twice. A cut
    that prefers the nearest preceding newline keeps the damage at a line
    boundary rather than mid-sentence where it can.
    """
    max_chars = int(budget_tokens * _CHARS_PER_TOKEN)
    if max_chars <= 0 or len(event.content) <= max_chars:
        return [event]

    parts: list[AgentEvent] = []
    text = event.content
    while text:
        if len(text) <= max_chars:
            cut = len(text)
        else:
            cut = max_chars
            # Only accept a newline in the last 40% of the window: nearer the
            # start it would produce tiny fragments and many more model calls.
            newline = text.rfind("\n", int(max_chars * 0.6), max_chars)
            if newline > 0:
                cut = newline + 1
        parts.append(event.model_copy(update={"content": text[:cut]}))
        text = text[cut:]

    logger.info(
        "A single %s/%s event of %d chars exceeded the %d-token budget; "
        "split into %d parts",
        event.role, event.kind, len(event.content), budget_tokens, len(parts),
    )
    return parts


@dataclass
class Segmenter:
    """Accumulates events across ticks and emits complete Segments.

    Stateful by necessity: a turn usually spans several polling periods, so the
    partial turn has to survive between calls.
    """

    budget_tokens: int
    agent_session_id: str = ""
    _pending: list[AgentEvent] = field(default_factory=list)
    _counter: int = 0

    def add(self, events: list[AgentEvent]) -> None:
        self._pending.extend(events)

    @property
    def pending_events(self) -> int:
        return len(self._pending)

    @property
    def has_undrained_turns(self) -> bool:
        """Whether a COMPLETE turn is sitting here that `drain()` has not
        emitted yet — i.e. whether a capped drain held work back.

        The second half of the worker's backlog bound (W3b review). Once
        `drain(max_segments=...)` can leave complete turns behind, "how deep is
        the deferred queue" stops being the whole answer to "should I read more
        transcript?": the queue can sit below its bound while the segmenter is
        still holding turns the last drain had no room for. Reading more bytes
        in that state grows `_pending` without bound, which is the same
        unboundedness the deferred cap just closed, one buffer upstream.

        Cheap enough to ask every tick: `_pending` is one partial turn in the
        steady state, and the only time it is large is exactly the burst this
        predicate exists to detect.
        """
        return len(self._split_into_turns(self._pending)) > 1

    def drain(self, *, flush_incomplete: bool = False,
              max_segments: int | None = None) -> list[Segment]:
        """Emit every complete turn as one or more Segments.

        `flush_incomplete` emits the trailing turn too — used when the transcript
        has gone quiet, and when shutting down so nothing is left unprocessed.

        `max_segments` stops converting turns once that many Segments have been
        produced; the turns not yet converted STAY in `_pending` and come out of
        the next `drain()`. Added 2026-08-06 (W3b review) because
        `max_deferred_segments` did not bound what it claimed to:
        `WorkerLoop.tick()` checks `accepting_input()` BEFORE the read, and the
        read has no cap, so one tick over a long transcript — `--from-start`, or
        a restart after a gap — could hand the caller an arbitrary number of
        Segments in a single call and overshoot the bound without limit
        (measured: 196 segments against a bound of 64). Back-pressure only ever
        prevented the NEXT read.

        Exact to within one turn: a turn is never split across drains, so the
        return can exceed `max_segments` by at most the segments one turn
        produces beyond the first over the line. Splitting a turn would hand
        synthesis a half-turn as if it were whole, which costs more than the
        slack does. `flush_incomplete` callers (shutdown) pass no cap on
        purpose — that path's contract is "nothing stranded".
        """
        if not self._pending:
            return []
        if max_segments is not None and max_segments < 1:
            return []

        turns = self._split_into_turns(self._pending)

        if flush_incomplete:
            complete, remainder = turns, []
        elif len(turns) <= 1:
            # Only one turn so far, and we cannot yet tell whether it has ended
            # -- but "cannot tell" is not a reason to hold content that is
            # already finished. ⟨2026-08-07⟩
            #
            # The hold exists so a HALF-WRITTEN turn is not distilled into a
            # half finding. It was unconditional, and in a long agentic turn
            # nothing could end it: a turn closes on a user TEXT event, and the
            # idle flush measures time since the transcript last grew, which
            # every tool result resets. Neither trigger could fire while the
            # agent worked. Measured live: 19 -> 46 events held across eight
            # minutes, then 16 findings in one burst once the human stopped.
            #
            # Two things already true make the early emit safe. An assistant
            # message is FINAL the moment it lands in the transcript, so holding
            # it buys no completeness. And anything past a budget boundary is
            # already emitted as an independent sub-segment -- see
            # `_turn_to_segments`: "Sub-segments are not merged back together
            # anywhere: each is distilled independently and deduplication is
            # synthesis's job, service-side." So the trade is latency for a
            # little duplication, and the dedup already exists upstream.
            #
            # Only FULL chunks go. The trailing chunk -- the part still being
            # written -- stays pending, which is the half-turn protection the
            # hold was for, now bounded to one budget's worth instead of a whole
            # turn.
            chunks = self._chunk(turns[0])
            if len(chunks) <= 1:
                return []
            if max_segments is not None:
                chunks = chunks[:max_segments + 1]
            self._pending = chunks[-1]
            return self._segments_from_chunks(chunks[:-1])
        else:
            complete, remainder = turns[:-1], turns[-1]

        segments: list[Segment] = []
        for i, turn in enumerate(complete):
            if max_segments is not None and len(segments) >= max_segments:
                # Out of room. Everything from here on goes back to the front of
                # `_pending`, ahead of the trailing partial turn, so FIFO order
                # across ticks is preserved — the same ordering argument
                # `SeamLimiter.admit` makes: slow is recoverable, scrambled is
                # not. These events are re-split next drain; splitting is pure.
                held_back = [e for t in complete[i:] for e in t]
                self._pending = held_back + remainder
                return segments
            segments.extend(self._turn_to_segments(turn))
        self._pending = remainder
        return segments

    def _split_into_turns(self, events: list[AgentEvent]) -> list[list[AgentEvent]]:
        turns: list[list[AgentEvent]] = []
        current: list[AgentEvent] = []
        for event in events:
            if is_turn_boundary(event) and current:
                turns.append(current)
                current = []
            current.append(event)
        if current:
            turns.append(current)
        return turns

    def _turn_to_segments(self, turn: list[AgentEvent]) -> list[Segment]:
        """One turn -> one Segment, or several if it exceeds the budget.

        Sub-segments are not merged back together anywhere: each is distilled
        independently and deduplication is synthesis's job, service-side.
        """
        return self._segments_from_chunks(self._chunk(turn))

    def _chunk(self, turn: list[AgentEvent]) -> list[list[AgentEvent]]:
        """Split one turn into budget-sized groups of events.

        Separated from `_turn_to_segments` so `drain` can ask where the
        boundaries fall WITHOUT minting Segments — an open turn needs its full
        chunks emitted and its trailing chunk put back, and putting Segments
        back is not a thing `_pending` can hold. Pure, so re-chunking the tail
        on the next drain alongside newly arrived events is safe.
        """
        if not turn:
            return []

        # Chunking below can only ever split BETWEEN events, so a single event
        # larger than the budget used to defeat it entirely: the `current and`
        # guard admits the first event unconditionally, producing a segment that
        # cannot fit the model's context no matter what follows. One long
        # assistant message is enough, which is not rare. Split those first so
        # every event entering the loop is known to fit on its own.
        turn = [part for event in turn
                for part in _split_oversized(event, self.budget_tokens)]

        chunks: list[list[AgentEvent]] = []
        current: list[AgentEvent] = []
        for event in turn:
            candidate = current + [event]
            if current and estimate_tokens(candidate) > self.budget_tokens:
                chunks.append(current)
                current = [event]
            else:
                current = candidate
        if current:
            chunks.append(current)

        if len(chunks) > 1:
            logger.info(
                "Turn exceeded the %d-token budget; split into %d sub-segments",
                self.budget_tokens,
                len(chunks),
            )
        return chunks

    def _segments_from_chunks(self, chunks: list[list[AgentEvent]]) -> list[Segment]:
        segments: list[Segment] = []
        for chunk in chunks:
            session = self.agent_session_id or chunk[0].agent_session_id
            self._counter += 1
            segments.append(
                Segment(
                    id=f"{session}-{self._counter:05d}",
                    agent_session_id=session,
                    events=chunk,
                    started_at=chunk[0].ts,
                    ended_at=chunk[-1].ts,
                )
            )
        return segments
