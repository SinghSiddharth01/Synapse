"""The periodic cycle: follow -> parse -> segment -> condense -> record -> push.

Crash-safety ordering
---------------------
Two pieces of state can disagree after a crash, and which way they disagree
decides whether the failure costs duplicated NPU work or permanently lost
conversation. Losing is unacceptable — the follower never re-reads a position —
so everything here is ordered to fail toward duplication:

  1. read new lines and advance the in-memory offset
  2. parse and buffer them in the segmenter
  3. drain complete turns, condense each, and RECORD the findings to disk
  4. only then persist the offset AND the still-pending buffer together

If the process dies anywhere before step 4, the next start re-reads from the old
offset and re-does the work. If it dies during a send, the write-ahead log
already holds the findings and `Finding.id` makes the replay idempotent.

The pending buffer is persisted with the offset because a turn normally spans
several ticks. Saving the offset without it would advance past events that were
never turned into findings — silent loss, which is the one outcome worth
engineering against.
"""

from __future__ import annotations

import asyncio
import json
import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from synapse_contracts import AgentEvent, LocalBinding
from synapse_distiller import Distiller
from synapse_distiller.guards import PromptDropError

from synapse_worker.follower import TranscriptFollower
from synapse_worker.producer import Producer
from synapse_worker.segmenter import Segmenter
from synapse_worker.sources.claude_code import ClaudeCodeSource
from synapse_worker.triage import triage
from synapse_worker.triage_log import TriageLog

logger = logging.getLogger(__name__)


@dataclass
class TickResult:
    new_lines: int = 0
    new_events: int = 0
    segments: int = 0
    findings: int = 0
    sent: int = 0
    pending_send: int = 0
    pending_events: int = 0
    skipped_no_change: bool = False
    flushed_incomplete: bool = False
    skipped_triage: int = 0
    errors: list[str] = field(default_factory=list)

    def summary(self) -> str:
        if self.skipped_no_change:
            return "no change"
        bits = [
            f"{self.new_lines} lines",
            f"{self.new_events} events",
            f"{self.segments} segments",
            f"{self.findings} findings",
        ]
        if self.skipped_triage:
            bits.append(f"{self.skipped_triage} triaged out")
        if self.sent:
            bits.append(f"{self.sent} sent")
        if self.pending_send:
            bits.append(f"{self.pending_send} queued")
        if self.pending_events:
            bits.append(f"{self.pending_events} events held (turn open)")
        if self.flushed_incomplete:
            bits.append("idle-flushed")
        return " · ".join(bits)


class WorkerLoop:
    """One followed transcript, condensed periodically and pushed upstream."""

    def __init__(
        self,
        transcript: Path,
        distiller: Distiller,
        producer: Producer,
        binding: LocalBinding,
        state_dir: Path,
        budget_tokens: int,
        *,
        idle_flush_seconds: float = 120.0,
        triage_enabled: bool = True,
    ) -> None:
        self.transcript = Path(transcript)
        self.distiller = distiller
        self.producer = producer
        self.binding = binding
        self.state_dir = Path(state_dir)
        self.state_dir.mkdir(parents=True, exist_ok=True)

        self.follower = TranscriptFollower(self.state_dir / "follow-state.json")
        self.source = ClaudeCodeSource()
        self.segmenter = Segmenter(
            budget_tokens=budget_tokens,
            agent_session_id=binding.agent_session_id,
        )
        self.idle_flush_seconds = idle_flush_seconds
        self.triage_enabled = triage_enabled
        self.triage_log = TriageLog(self.state_dir)

        self._pending_path = self.state_dir / "pending-events.json"
        self._restore_pending()
        self._last_change = datetime.now(timezone.utc)

    def _restore_pending(self) -> None:
        if not self._pending_path.is_file():
            return
        try:
            raw = json.loads(self._pending_path.read_text(encoding="utf-8"))
            events = [AgentEvent.model_validate(item) for item in raw]
        except (json.JSONDecodeError, ValueError) as exc:
            logger.warning("Could not restore pending events (%s); starting empty", exc)
            return
        if events:
            self.segmenter.add(events)
            logger.info("Restored %d pending events from the previous run", len(events))

    def _persist_state(self) -> None:
        """Offset and pending buffer together — see the module docstring."""
        payload = [e.model_dump(mode="json") for e in self.segmenter._pending]
        tmp = self._pending_path.with_suffix(".tmp")
        tmp.write_text(json.dumps(payload), encoding="utf-8")
        tmp.replace(self._pending_path)
        self.follower.save()

    def attach_at_end(self) -> None:
        """Ignore everything already in the transcript.

        Without this, attaching to a conversation in progress would re-distil its
        entire history — hours of NPU time on a multi-megabyte transcript.
        """
        self.follower.start_at_end(self.transcript)
        self.follower.save()

    async def tick(self) -> TickResult:
        result = TickResult()

        if not self.follower.has_new_data(self.transcript):
            idle = (datetime.now(timezone.utc) - self._last_change).total_seconds()
            if self.segmenter.pending_events and idle >= self.idle_flush_seconds:
                result.flushed_incomplete = True
            else:
                result.skipped_no_change = True
                result.pending_events = self.segmenter.pending_events
                # Still retry anything the sink rejected earlier.
                result.sent, result.pending_send = await self.producer.flush()
                return result
        else:
            self._last_change = datetime.now(timezone.utc)

        lines = self.follower.read_new_lines(self.transcript)
        result.new_lines = len(lines)

        events: list[AgentEvent] = []
        for line in lines:
            events.extend(self.source.parse_line(line))
        result.new_events = len(events)
        self.segmenter.add(events)

        segments = self.segmenter.drain(flush_incomplete=result.flushed_incomplete)
        result.segments = len(segments)

        for segment in segments:
            if self.triage_enabled:
                decision = triage(segment)
                if not decision.keep:
                    self.triage_log.record_skip(segment, decision.reason)
                    result.skipped_triage += 1
                    logger.info("Triage skipped %s (%s)", segment.id, decision.reason)
                    continue
            try:
                findings, stats = await self.distiller.distil(segment)
            except PromptDropError as exc:
                # The model stopped conditioning on its prompt. Every finding it
                # would produce is invented, so drop the segment rather than
                # poison shared memory. Loud, because it means the model or its
                # type is wrong.
                logger.error("Prompt-drop guard tripped on %s: %s", segment.id, exc)
                result.errors.append(f"prompt-drop on {segment.id}")
                continue
            except Exception as exc:  # noqa: BLE001 - a tick must never die
                logger.exception("Distillation failed for %s", segment.id)
                result.errors.append(f"{segment.id}: {exc}")
                continue

            if stats.skipped_empty:
                continue
            # Write-ahead: on disk before any send is attempted.
            self.producer.record(findings)
            result.findings += len(findings)

        result.sent, result.pending_send = await self.producer.flush()
        result.pending_events = self.segmenter.pending_events

        # Last, so a crash costs duplicated work rather than lost conversation.
        self._persist_state()
        return result

    async def run(self, interval_seconds: float, max_ticks: int | None = None) -> None:
        tick_number = 0
        while max_ticks is None or tick_number < max_ticks:
            tick_number += 1
            try:
                result = await self.tick()
                logger.info("tick %d — %s", tick_number, result.summary())
            except Exception:  # noqa: BLE001 - the loop outlives any one tick
                logger.exception("Tick %d failed; continuing", tick_number)
            if max_ticks is None or tick_number < max_ticks:
                await asyncio.sleep(interval_seconds)

    async def shutdown(self) -> TickResult:
        """Flush the open turn so nothing is stranded mid-conversation."""
        logger.info("Shutting down: flushing the open turn")
        result = TickResult(flushed_incomplete=True)
        segments = self.segmenter.drain(flush_incomplete=True)
        result.segments = len(segments)
        for segment in segments:
            if self.triage_enabled:
                decision = triage(segment)
                if not decision.keep:
                    self.triage_log.record_skip(segment, decision.reason)
                    result.skipped_triage += 1
                    logger.info("Triage skipped %s (%s)", segment.id, decision.reason)
                    continue
            try:
                findings, stats = await self.distiller.distil(segment)
                if not stats.skipped_empty:
                    self.producer.record(findings)
                    result.findings += len(findings)
            except Exception:  # noqa: BLE001
                logger.exception("Distillation failed for %s during shutdown", segment.id)
        result.sent, result.pending_send = await self.producer.flush()
        self._persist_state()
        return result
