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

    def drain(self, *, flush_incomplete: bool = False) -> list[Segment]:
        """Emit every complete turn as one or more Segments.

        `flush_incomplete` emits the trailing turn too — used when the transcript
        has gone quiet, and when shutting down so nothing is left unprocessed.
        """
        if not self._pending:
            return []

        turns = self._split_into_turns(self._pending)

        if flush_incomplete:
            complete, self._pending = turns, []
        elif len(turns) <= 1:
            # Only one turn so far, and we cannot yet tell whether it has ended.
            return []
        else:
            complete, remainder = turns[:-1], turns[-1]
            self._pending = remainder

        segments: list[Segment] = []
        for turn in complete:
            segments.extend(self._turn_to_segments(turn))
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
