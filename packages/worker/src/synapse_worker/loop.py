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
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from synapse_contracts import AgentEvent, LocalBinding, Segment
from synapse_distiller import Distiller
from synapse_distiller.guards import PromptDropError

from synapse_worker.compaction import compact
from synapse_worker.discovery import has_per_session_bindings, resolve_agent_binding
from synapse_worker.discovery import AGENT_REGISTRY
from synapse_worker.follower import TranscriptFollower
from synapse_worker.producer import Producer
from synapse_worker.segmenter import Segmenter
from synapse_worker.stats import StatsBuffer
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
    held: int = 0
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
        if self.held:
            bits.append(f"{self.held} held (other session)")
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
        agent: str = "claude-code",
        idle_flush_seconds: float = 120.0,
        triage_enabled: bool = True,
        stats: StatsBuffer | None = None,
    ) -> None:
        self.transcript = Path(transcript)
        self.distiller = distiller
        self.producer = producer
        self.binding = binding
        # Sync the Producer's write-ahead log to THIS loop's binding, whatever
        # `shared_id` it happened to be constructed with — the re-join
        # envelope (producer.py's module docstring, STATE.md trap #8) needs
        # `flush()`'s notion of "current" to always match the binding this
        # loop actually runs under, not a stale or default value from
        # construction time.
        #
        # AMENDMENT (fixer pass, blocker): this alone only covers the
        # cross-process half of trap #8 — a NEW `run` started after `join
        # <new_id>` picks the new binding up correctly, because `_build`
        # re-resolves it from disk before constructing this loop at all. The
        # same-process half (`synapse-worker join <new_id>` typed in another
        # terminal while THIS `run` is still live) has no channel to this
        # object at all: nothing here ever re-reads
        # `.synapse/bindings/<agent>.json` again after this one call, so a
        # Finding recorded under the old binding before the re-join would
        # keep reading as deliverable under the old `shared_id` forever, and
        # `flush()` would ship it — which the orchestrator, resolving the
        # destination fresh from disk on every POST
        # (`_resolve_binding_for_agent`, its own docstring: "read fresh from
        # disk"), would then deliver under the NEW session. `_sync_binding_
        # from_disk` below closes that gap by doing on the worker side,
        # once per `tick()`/`shutdown()`, exactly what the orchestrator
        # already does per POST.
        self.producer.rebind(binding.shared_id)
        self.state_dir = Path(state_dir)
        self.state_dir.mkdir(parents=True, exist_ok=True)
        self.stats = stats
        self.agent = agent

        self.follower = TranscriptFollower(self.state_dir / "follow-state.json")
        # Dispatched through AGENT_REGISTRY rather than hard-coded, so a
        # transcript resolved (or explicitly requested) for a registered
        # agent other than Claude Code is actually parsed by ITS adapter —
        # without this, a bindings/codex.json produced by `join` would still
        # be fed to ClaudeCodeSource, which yields zero events on Codex's
        # rollout lines (its own `type` values are never "user"/"assistant").
        self.source = AGENT_REGISTRY[agent].source_class()
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

        `_prime_source_from_header` runs first for a reason that has nothing to
        do with NPU cost: some Source adapters carry state across lines that is
        only ever written once, near the top of the file — `CodexSource`'s
        `session_meta` is the concrete case, unlike Claude Code, which repeats
        `cwd`/`gitBranch` on every record. Jumping straight to EOF, as this
        method's own name says to do, would mean that state is never seen, and
        every event for the rest of the run would silently carry
        `agent_session_id=""`, `cwd=None`, `git_branch=None`. Priming first
        means the Source ends the jump in the same state it would have reached
        by reading from line 1 — without turning any of that history into
        AgentEvents, since only parse_line's side effects are wanted here.
        """
        self._prime_source_from_header()
        self.follower.start_at_end(self.transcript)
        self.follower.save()

    def _sync_binding_from_disk(self) -> None:
        """Re-resolve this Agent product's join binding from disk and
        `rebind()` the Producer when it moved — the same-process half of
        the re-join envelope (STATE.md trap #8) that `__init__`'s one-time
        `rebind()` call cannot cover; see the comment there.

        Called at the top of every `tick()` (including the "no change"
        retry-flush path, so a re-join is noticed even with no new
        transcript content) and at the top of `shutdown()` — anywhere this
        loop is about to consult or act on "the current binding".

        Since W2 this asks for THIS loop's own Agent SESSION first
        (`bindings/<agent>/<agent_session_id>.json`) and only falls back to
        the product-level "most recently pinned" answer when this session has
        no binding of its own — the pre-W2 state, and the state of a `run`
        that was never joined at all. Without the session-first lookup, two
        windows on one machine would re-join each other: window B's join would
        move window A's Producer, because both were reading the same single
        file.

        Deliberately narrow: only a REAL on-disk join binding for THIS
        loop's own Agent product (`binding_path_for_agent(state_dir,
        self.binding.agent)`) can move the Producer's target, and only when
        its `shared_id` actually differs from what's currently bound — an
        absent file (never joined, or `--transcript`/`--shared-id` used
        directly) leaves the constructed binding untouched, exactly as
        before this method existed. A single small JSON file stat+read per
        call is the same cost the orchestrator already pays reading this
        same shape of file fresh on every POST
        (`_resolve_binding_for_agent`).
        """
        agent = self.binding.agent
        joined = resolve_agent_binding(self.state_dir, agent, self.binding.agent_session_id)
        if joined is None and not has_per_session_bindings(self.state_dir, agent):
            # Nothing has joined under the per-session layout for this product,
            # so the legacy file is the only binding on this machine and it is
            # this conversation's by default — a tree written before W2, or one
            # whose join has not happened yet. The moment ANY per-session
            # binding exists, "no binding for my session" means exactly that:
            # somebody else's join is not a re-join of mine.
            joined = resolve_agent_binding(self.state_dir, agent)
        if joined is not None and joined.shared_id != self.producer.shared_id:
            logger.info(
                "Live re-join detected (%r -> %r); syncing the write-ahead "
                "log's notion of \"current\" so nothing already queued is "
                "silently retargeted",
                self.producer.shared_id, joined.shared_id,
            )
            self.producer.rebind(joined.shared_id)

    def _compact(self, segment: Segment) -> Segment:
        """Compaction (Plan A.5) — AFTER triage's keep decision, in both
        `tick()` and `shutdown()`. See compaction.py's module docstring for
        why the order matters: triage's judgment about whether a Segment is
        worth sending to the model at all must be made against the full,
        unmodified Segment triage was actually handed — A.5b's "triage reads
        the full Segment" note, taken literally — never against a
        truncated/ranked view compaction has already reshaped. Compaction's
        job starts only once a Segment has already earned a `keep`: from
        that point on it shapes only what the model sees, never whether the
        Segment survives at all. Only called for segments `triage` (or the
        `triage_enabled=False` bypass) has already decided to keep."""
        original_events = len(segment.events)
        original_chars = sum(len(e.content) for e in segment.events)
        compacted = compact(segment)
        if self.stats:
            kept = len(compacted.events)
            saved = original_chars - sum(len(e.content) for e in compacted.events)
            # `saved` can legitimately come out negative (compaction's own
            # omission/trivial markers are text too, and an error-dense
            # tool_result can survive nearly intact) -- label that honestly
            # rather than showing "-N chars saved", which reads as a bug in
            # the dashboard rather than what actually happened. See
            # compaction.py's MAX_SURVIVOR_LINES amendment: this should now
            # be rare, not impossible, so it still needs to be reported
            # straight when it happens ("dashboards show ... honestly").
            saved_desc = (
                f"{saved} chars saved" if saved >= 0
                else f"{-saved} chars added (markers on an already-small segment)"
            )
            self.stats.event(
                "compaction",
                f"{segment.id}: {kept}/{original_events} events kept · {saved_desc}",
                segment=segment.id,
                events_kept=kept,
                events_total=original_events,
                chars_saved=saved,
            )
        return compacted
    def _prime_source_from_header(self) -> None:
        """Feed every line already in the transcript through `self.source`,
        discarding the events. Cheap: this is JSON parsing, not distillation —
        the expensive step this whole module exists to avoid re-paying for.
        """
        try:
            with self.transcript.open("r", encoding="utf-8", errors="replace") as handle:
                for line in handle:
                    self.source.parse_line(line)
        except OSError:
            # No transcript yet, or it disappeared between resolution and
            # attach — attach_at_end's own start_at_end call handles that the
            # same way; nothing to prime from.
            return

    async def tick(self) -> TickResult:
        result = TickResult()
        # Before anything else: notice a `synapse-worker join <new_id>` that
        # happened in another terminal since the last tick — see
        # `_sync_binding_from_disk`'s docstring and STATE.md trap #8. Must
        # run even on the "no change" retry-flush path immediately below,
        # since that path still calls `flush()`/`pending_count()` against
        # `self.producer`'s notion of "current".
        self._sync_binding_from_disk()

        if not self.follower.has_new_data(self.transcript):
            idle = (datetime.now(timezone.utc) - self._last_change).total_seconds()
            if self.segmenter.pending_events and idle >= self.idle_flush_seconds:
                result.flushed_incomplete = True
            else:
                result.skipped_no_change = True
                result.pending_events = self.segmenter.pending_events
                # Still retry anything the sink rejected earlier.
                result.sent, result.pending_send = await self.producer.flush()
                result.held = self.producer.pending_count()[1]
                if self.stats:
                    # Only when a push actually had something to say. An idle
                    # worker retries its (empty) queue every tick, and emitting
                    # "0 sent, 0 queued" each time buried the handful of rows
                    # that matter under hundreds that never will.
                    if result.sent or result.pending_send:
                        self.stats.event(
                            "push",
                            f"{result.sent} sent, {result.pending_send} queued",
                            sent=result.sent,
                            queued=result.pending_send,
                        )
                    self.stats.tick(asdict(result))
                    self.stats.event("tick", result.summary())
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
            # Triage FIRST, on the Segment exactly as segmented -- before
            # compaction has a chance to reshape it. See compaction.py's
            # module docstring and `_compact`'s docstring above: a
            # keep/skip judgment made against a truncated/ranked view is a
            # judgment made against evidence compaction chose to show, not
            # the evidence that was actually there.
            if self.triage_enabled:
                decision = triage(segment)
                if not decision.keep:
                    self.triage_log.record_skip(segment, decision.reason)
                    result.skipped_triage += 1
                    logger.info("Triage skipped %s (%s)", segment.id, decision.reason)
                    if self.stats:
                        self.stats.event(
                            "triage",
                            f"skip {segment.id} ({decision.reason})",
                            segment=segment.id,
                            reason=decision.reason,
                        )
                    continue
                if self.stats:
                    self.stats.event(
                        "triage",
                        f"keep {segment.id} ({decision.reason})",
                        segment=segment.id,
                        reason=decision.reason,
                    )
            # Only now, for a Segment triage has already decided is worth
            # sending to the model: shape what the model actually sees.
            segment = self._compact(segment)
            if self.stats:
                self.stats.distil_started(segment.id, len(segment.events))
            try:
                findings, distil_stats = await self.distiller.distil(segment)
            except PromptDropError as exc:
                # The model stopped conditioning on its prompt. Every finding it
                # would produce is invented, so drop the segment rather than
                # poison shared memory. Loud, because it means the model or its
                # type is wrong.
                logger.error("Prompt-drop guard tripped on %s: %s", segment.id, exc)
                result.errors.append(f"prompt-drop on {segment.id}")
                if self.stats:
                    self.stats.event(
                        "error", f"prompt-drop on {segment.id}", segment=segment.id
                    )
                continue
            except Exception as exc:  # noqa: BLE001 - a tick must never die
                logger.exception("Distillation failed for %s", segment.id)
                result.errors.append(f"{segment.id}: {exc}")
                if self.stats:
                    self.stats.event(
                        "error", f"{segment.id}: {exc}", segment=segment.id
                    )
                continue
            finally:
                if self.stats:
                    self.stats.distil_finished()

            if self.stats:
                retained = sum(
                    1 for e in segment.events if e.kind in set(self.distiller.kinds)
                )
                self.stats.event(
                    "render",
                    f"{segment.id}: {retained}/{len(segment.events)} events reached the model",
                    events_in=len(segment.events),
                    retained=retained,
                    kinds=list(self.distiller.kinds),
                    input_tokens=distil_stats.input_tokens,
                )

            if distil_stats.skipped_empty:
                continue
            # Write-ahead: on disk before any send is attempted.
            self.producer.record(findings)
            result.findings += len(findings)

        result.sent, result.pending_send = await self.producer.flush()
        result.held = self.producer.pending_count()[1]
        result.pending_events = self.segmenter.pending_events
        if self.stats and (result.sent or result.pending_send):
            self.stats.event(
                "push",
                f"{result.sent} sent, {result.pending_send} queued",
                sent=result.sent,
                queued=result.pending_send,
            )

        # Last, so a crash costs duplicated work rather than lost conversation.
        self._persist_state()
        if self.stats:
            self.stats.tick(asdict(result))
            self.stats.event("tick", result.summary())
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
        # Same reasoning as tick()'s call — a re-join could have happened
        # after the last tick and before shutdown flushes.
        self._sync_binding_from_disk()
        result = TickResult(flushed_incomplete=True)
        segments = self.segmenter.drain(flush_incomplete=True)
        result.segments = len(segments)
        for segment in segments:
            # Same ordering as tick(): triage the Segment as segmented,
            # before compaction can reshape what triage would see.
            if self.triage_enabled:
                decision = triage(segment)
                if not decision.keep:
                    self.triage_log.record_skip(segment, decision.reason)
                    result.skipped_triage += 1
                    logger.info("Triage skipped %s (%s)", segment.id, decision.reason)
                    continue
            segment = self._compact(segment)
            try:
                findings, stats = await self.distiller.distil(segment)
                if not stats.skipped_empty:
                    self.producer.record(findings)
                    result.findings += len(findings)
            except Exception:  # noqa: BLE001
                logger.exception("Distillation failed for %s during shutdown", segment.id)
        result.sent, result.pending_send = await self.producer.flush()
        result.held = self.producer.pending_count()[1]
        self._persist_state()
        return result
