"""End-to-end loop tests: transcript on disk -> findings at the sink.

Offline — FakeProvider, no NPU, no network.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

import httpx
import pytest
from synapse_contracts import (
    Attribution,
    Finding,
    FindingType,
    LocalBinding,
    SessionBinding,
    write_binding,
)
from synapse_distiller import Distiller, load_pack_by_name
from synapse_providers import FakeProvider, ModelProvider

from synapse_worker.discovery import binding_path_for_agent
from synapse_worker.loop import MAX_DISTIL_ATTEMPTS, WorkerLoop
from synapse_worker.producer import FileSink, Producer

PACK = load_pack_by_name("v4-condense")
BINDING = LocalBinding(
    agent_session_id="sess-1", shared_id="shared-1", contributor="aditya", agent="claude-code"
)
TS = "2026-08-04T09:12:00.000Z"


def line(**kwargs) -> str:
    base = {"sessionId": "sess-1", "timestamp": TS, "cwd": "/repo", "gitBranch": "main"}
    return json.dumps({**base, **kwargs}) + "\n"


def user(text: str) -> str:
    return line(type="user", message={"content": [{"type": "text", "text": text}]})


def assistant(text: str) -> str:
    return line(type="assistant", message={"content": [{"type": "text", "text": text}]})


# Aliases matching this suite's own naming for user()/assistant() lines — kept
# so the triage tests below read the same way the plan wrote them.
user_text = user
assistant_text = assistant


def assistant_tool_use(tool_name: str, command: str, tool_id: str = "tool-1") -> str:
    return line(
        type="assistant",
        message={"content": [
            {"type": "tool_use", "id": tool_id, "name": tool_name, "input": {"command": command}}
        ]},
    )


def tool_result(tool_name: str, content: str, tool_id: str = "tool-1") -> str:
    # tool_name is accepted (matching assistant_tool_use's call site) purely for
    # readability at the call site; ClaudeCodeSource actually resolves the real
    # tool name from tool_id via the preceding tool_use line.
    return line(
        type="user",
        message={"content": [{"type": "tool_result", "tool_use_id": tool_id, "content": content}]},
    )


def write_transcript_lines(path: Path, lines: list[str]) -> None:
    Path(path).write_text("".join(lines), encoding="utf-8")


def condensed(*texts: str) -> dict:
    return {"findings": [{"type": "learning", "text": t} for t in texts]}


def build(
    tmp_path, scripts: list, budget: int = 5000, *, triage_enabled: bool = True
) -> tuple[WorkerLoop, Producer]:
    transcript = tmp_path / "t.jsonl"
    transcript.touch()
    producer = Producer(tmp_path / "wal", FileSink(tmp_path / "upstream.jsonl"))
    loop = WorkerLoop(
        transcript=transcript,
        distiller=Distiller(FakeProvider(scripts=scripts), BINDING, PACK, ["text"], "labelled"),
        producer=producer,
        binding=BINDING,
        state_dir=tmp_path / "state",
        budget_tokens=budget,
        triage_enabled=triage_enabled,
    )
    return loop, producer


@pytest.fixture
def worker_loop_factory():
    """Build a WorkerLoop the way this file's own tests do (see `build()`
    above), exposed as a factory so the triage tests can toggle
    `triage_enabled` without duplicating the arrange block."""

    def _factory(tmp_path, *, triage_enabled: bool = True) -> WorkerLoop:
        loop, _ = build(tmp_path, [condensed("kept")], triage_enabled=triage_enabled)
        return loop

    return _factory


async def test_full_path_transcript_to_upstream(tmp_path) -> None:
    loop, _ = build(tmp_path, [condensed("pooling mode matters")])
    transcript = tmp_path / "t.jsonl"

    # A first, complete turn plus the start of a second, which closes the first.
    transcript.write_text(
        user("add pooling") + assistant("done, session mode") + user("now the cache"),
        encoding="utf-8",
    )
    result = await loop.tick()

    assert result.new_events == 3
    assert result.segments == 1
    assert result.findings == 1
    assert result.sent == 1

    upstream = (tmp_path / "upstream.jsonl").read_text(encoding="utf-8").strip()
    assert json.loads(upstream)["text"] == "pooling mode matters"


async def test_open_turn_is_not_distilled_until_it_closes(tmp_path) -> None:
    """The whole reason periodic capture works: no half turns."""
    loop, _ = build(tmp_path, [condensed("later")])
    transcript = tmp_path / "t.jsonl"

    transcript.write_text(user("start") + assistant("thinking"), encoding="utf-8")
    first = await loop.tick()

    assert (first.segments, first.findings) == (0, 0)
    assert first.pending_events == 2

    with transcript.open("a", encoding="utf-8") as fh:
        fh.write(user("next request"))
    second = await loop.tick()

    assert second.segments == 1


async def test_second_tick_with_no_change_does_nothing(tmp_path) -> None:
    loop, _ = build(tmp_path, [condensed("one")])
    transcript = tmp_path / "t.jsonl"
    transcript.write_text(user("a") + assistant("b") + user("c"), encoding="utf-8")

    await loop.tick()
    second = await loop.tick()

    assert second.skipped_no_change is True
    assert second.new_events == 0


async def test_content_is_never_processed_twice(tmp_path) -> None:
    """A re-read would pay for the same NPU work twice and duplicate findings."""
    loop, producer = build(tmp_path, [condensed("one"), condensed("two")])
    transcript = tmp_path / "t.jsonl"

    transcript.write_text(user("a") + assistant("b") + user("c"), encoding="utf-8")
    await loop.tick()
    with transcript.open("a", encoding="utf-8") as fh:
        fh.write(assistant("d") + user("e"))
    await loop.tick()

    texts = [
        json.loads(row)["text"]
        for row in (tmp_path / "upstream.jsonl").read_text(encoding="utf-8").splitlines()
    ]
    assert texts == ["one", "two"]


async def test_pending_turn_survives_a_restart(tmp_path) -> None:
    """The offset advances past events that are still buffered, so the buffer
    has to be persisted with it or that turn is silently lost."""
    loop, _ = build(tmp_path, [condensed("recovered")])
    transcript = tmp_path / "t.jsonl"
    transcript.write_text(user("start") + assistant("partial"), encoding="utf-8")
    await loop.tick()

    revived, _ = build(tmp_path, [condensed("recovered")])
    assert revived.segmenter.pending_events == 2

    with transcript.open("a", encoding="utf-8") as fh:
        fh.write(user("next"))
    result = await revived.tick()

    assert result.segments == 1
    assert result.findings == 1


def test_agent_param_selects_the_source_via_the_registry(tmp_path) -> None:
    """WorkerLoop must actually read AGENT_REGISTRY[agent].source_class --
    without this, a Codex-bound session would still be parsed by
    ClaudeCodeSource, which yields zero events on Codex's rollout lines."""
    from synapse_worker.sources.claude_code import ClaudeCodeSource
    from synapse_worker.sources.codex import CodexSource

    (tmp_path / "a").mkdir()
    default_loop, _ = build(tmp_path / "a", [])
    assert isinstance(default_loop.source, ClaudeCodeSource)

    transcript = tmp_path / "b" / "t.jsonl"
    transcript.parent.mkdir()
    transcript.touch()
    producer = Producer(tmp_path / "b" / "wal", FileSink(tmp_path / "b" / "upstream.jsonl"))
    codex_loop = WorkerLoop(
        transcript=transcript,
        distiller=Distiller(FakeProvider(scripts=[]), BINDING, PACK, ["text"], "labelled"),
        producer=producer,
        binding=BINDING,
        state_dir=tmp_path / "b" / "state",
        budget_tokens=5000,
        agent="codex",
    )

    assert isinstance(codex_loop.source, CodexSource)


async def test_attach_at_end_primes_the_source_from_the_header(tmp_path) -> None:
    """CodexSource's session_meta is written once, near the top of the file
    -- unlike Claude Code, which repeats cwd/gitBranch on every record.
    attach_at_end's whole job is to jump straight to EOF; without priming
    first, session_meta would never be seen and every later event would
    silently carry agent_session_id="", cwd=None, git_branch=None."""
    from synapse_worker.sources.codex import CodexSource

    loop, _ = build(tmp_path, [])
    loop.source = CodexSource()
    transcript = tmp_path / "t.jsonl"

    def rollout(line_type: str, payload: dict, ts: str = TS) -> str:
        return json.dumps({"timestamp": ts, "type": line_type, "payload": payload}) + "\n"

    transcript.write_text(
        rollout(
            "session_meta",
            {"session_id": "codex-sess-1", "cwd": "/work", "git": {"branch": "perf"}},
        )
        + rollout(
            "response_item",
            {"type": "message", "role": "user", "content": [{"type": "input_text", "text": "already here"}]},
        ),
        encoding="utf-8",
    )

    loop.attach_at_end()

    (event,) = loop.source.parse_line(
        rollout(
            "response_item",
            {"type": "message", "role": "assistant", "content": [{"type": "output_text", "text": "new turn"}]},
        )
    )

    assert event.agent_session_id == "codex-sess-1"
    assert event.cwd == "/work"
    assert event.git_branch == "perf"


async def test_attach_at_end_with_a_missing_transcript_does_not_raise(tmp_path) -> None:
    loop, _ = build(tmp_path, [])
    (tmp_path / "t.jsonl").unlink()

    loop.attach_at_end()  # must not raise


async def test_shutdown_flushes_the_open_turn(tmp_path) -> None:
    loop, _ = build(tmp_path, [condensed("final")])
    transcript = tmp_path / "t.jsonl"
    transcript.write_text(user("only turn") + assistant("reply"), encoding="utf-8")
    await loop.tick()

    result = await loop.shutdown()

    assert result.segments == 1
    assert result.findings == 1


async def test_dropped_prompt_does_not_poison_the_log(tmp_path) -> None:
    """The guard fires inside the loop; the tick survives and records nothing."""
    transcript = tmp_path / "t.jsonl"
    transcript.touch()
    producer = Producer(tmp_path / "wal", FileSink(tmp_path / "upstream.jsonl"))
    loop = WorkerLoop(
        transcript=transcript,
        distiller=Distiller(
            FakeProvider(scripts=[condensed("invented")], input_tokens=1),
            BINDING, PACK, ["text"], "labelled",
        ),
        producer=producer,
        binding=BINDING,
        state_dir=tmp_path / "state",
        budget_tokens=5000,
    )
    transcript.write_text(user("a") + assistant("b") + user("c"), encoding="utf-8")

    result = await loop.tick()

    assert result.findings == 0
    assert any("prompt-drop" in e for e in result.errors)
    assert producer.unsent() == []


async def test_worker_loop_syncs_the_producer_to_its_own_binding_on_construction(
    tmp_path,
) -> None:
    """The re-join envelope (producer.py's module docstring, STATE.md trap
    #8) is inert unless `flush()`'s notion of "current" actually matches the
    binding this loop runs under. `WorkerLoop.__init__` must sync a
    freshly-built Producer to ITS OWN binding
    (`producer.rebind(binding.shared_id)`) regardless of whatever
    `shared_id` the Producer happened to be constructed with -- this pins
    that construction alone does it, and that it survives a real tick: a
    finding recorded under a DIFFERENT session before this loop existed
    stays held, never silently retargeted to this loop's session."""
    transcript = tmp_path / "t.jsonl"
    transcript.touch()
    producer = Producer(
        tmp_path / "wal", FileSink(tmp_path / "upstream.jsonl"), "session-other"
    )
    stale = Finding(
        id="f-stale", type=FindingType.LEARNING, text="from a previous session",
        attributions=[Attribution(contributor="aditya", agent_session="sess-1",
                                  agent="claude-code")],
        ts=datetime(2026, 8, 4, tzinfo=timezone.utc),
    )
    producer.record([stale])  # recorded while bound to "session-other"

    loop = WorkerLoop(
        transcript=transcript,
        distiller=Distiller(FakeProvider(scripts=[condensed("fresh")]),
                            BINDING, PACK, ["text"], "labelled"),
        producer=producer,
        binding=BINDING,  # shared_id="shared-1" -- a DIFFERENT session than above
        state_dir=tmp_path / "state",
        budget_tokens=5000,
    )

    # Construction alone must already have synced the binding.
    assert producer.pending_count() == (0, 1)

    transcript.write_text(user("a") + assistant("b") + user("c"), encoding="utf-8")
    result = await loop.tick()

    # The fresh finding (produced under THIS loop's real binding) ships; the
    # stale one stays held -- never retargeted to "shared-1", never dropped.
    assert result.sent == 1
    assert result.held == 1
    upstream = [
        json.loads(row)["text"]
        for row in (tmp_path / "upstream.jsonl").read_text(encoding="utf-8").splitlines()
    ]
    assert upstream == ["fresh"]
    assert producer.pending_count() == (0, 1)


async def test_live_worker_notices_a_join_to_a_different_session_mid_run(tmp_path) -> None:
    """The same-process half of trap #8 (fixer-blocker fix). `synapse-worker
    join <new_id>`, typed in another terminal while THIS `run` is still
    live, writes straight to `.synapse/bindings/claude-code.json` -- there
    is no other channel to the running process. The test right above this
    one (`test_worker_loop_syncs_the_producer_to_its_own_binding_on_
    construction`) only pins the ONE-TIME sync `WorkerLoop.__init__` does;
    it cannot detect a re-join that happens after construction, which is
    the normal way a developer switches sessions mid-work. Without
    `_sync_binding_from_disk`, a Finding recorded under the ORIGINAL
    binding keeps reading as deliverable under it forever -- and the
    orchestrator, which resolves the destination fresh from disk on every
    POST (`_resolve_binding_for_agent`), would then deliver it under
    whatever the join moved to: a real cross-session leak, not a cosmetic
    mis-report. This pins the fix end to end through `tick()`; test_producer.py
    already pins the Producer half of the mechanism in isolation."""
    state_dir = tmp_path / "state"
    transcript = tmp_path / "t.jsonl"
    transcript.touch()
    producer = Producer(state_dir / "wal", FileSink(tmp_path / "upstream.jsonl"))
    loop = WorkerLoop(
        transcript=transcript,
        distiller=Distiller(FakeProvider(scripts=[]), BINDING, PACK, ["text"], "labelled"),
        producer=producer,
        binding=BINDING,  # shared_id="shared-1"
        state_dir=state_dir,
        budget_tokens=5000,
    )
    stale = Finding(
        id="f-A", type=FindingType.LEARNING, text="recorded under session A",
        attributions=[Attribution(contributor="aditya", agent_session="sess-1",
                                  agent="claude-code")],
        ts=datetime(2026, 8, 4, tzinfo=timezone.utc),
    )
    producer.record([stale])  # recorded while bound to "shared-1" (construction synced this)
    assert producer.pending_count() == (1, 0)  # deliverable under the original binding

    # `synapse-worker join shared-2` in another terminal -- writes the join
    # binding file directly. Nothing calls `producer.rebind()` from here;
    # this is exactly the gap the fix closes.
    write_binding(
        binding_path_for_agent(state_dir, "claude-code"),
        SessionBinding(service_url="http://127.0.0.1:8899", 
            agent_session_id="sess-1", shared_id="shared-2", contributor="aditya",
            agent="claude-code", transcript_path=str(transcript),
            pinned_at=datetime(2026, 8, 4, tzinfo=timezone.utc),
        ),
    )

    # No new transcript content -- this also exercises the "no change"
    # retry-flush path, which must notice the re-join too (STATE.md trap #8's
    # scenario calls `flush()` with nothing new having happened).
    result = await loop.tick()

    assert result.held == 1
    assert result.sent == 0
    assert producer.pending_count() == (0, 1)  # held, not lost, not retargeted
    assert not (tmp_path / "upstream.jsonl").exists()  # never shipped to session B

    # Re-joining BACK to the session it was actually produced under drains
    # it -- the other half of the guarantee (mirrors test_producer.py's
    # test_rejoin_back_to_the_original_session_drains_the_held_finding).
    write_binding(
        binding_path_for_agent(state_dir, "claude-code"),
        SessionBinding(service_url="http://127.0.0.1:8899", 
            agent_session_id="sess-1", shared_id="shared-1", contributor="aditya",
            agent="claude-code", transcript_path=str(transcript),
            pinned_at=datetime(2026, 8, 4, tzinfo=timezone.utc),
        ),
    )
    drained = await loop.tick()

    assert drained.sent == 1
    assert drained.held == 0
    assert producer.pending_count() == (0, 0)


async def test_bookkeeping_lines_produce_no_segments(tmp_path) -> None:
    loop, _ = build(tmp_path, [])
    transcript = tmp_path / "t.jsonl"
    transcript.write_text(
        line(type="mode", mode="default") + line(type="ai-title", aiTitle="x"),
        encoding="utf-8",
    )

    result = await loop.tick()

    assert result.new_lines == 2
    assert result.new_events == 0
    assert result.segments == 0


async def test_triage_skips_lint_noise_and_logs_it(tmp_path, worker_loop_factory):
    """A lint-clean turn never reaches the distiller; the skip is replayable."""
    loop = worker_loop_factory(tmp_path)
    write_transcript_lines(loop.transcript, [
        user_text("fix the imports"),
        assistant_tool_use("Bash", "ruff check --fix ."),
        tool_result("Bash", "Found 3 errors (3 fixed, 0 remaining)."),
        assistant_text("Done, ruff fixed everything."),
        user_text("next task please"),   # closes the turn
    ])
    result = await loop.tick()
    assert result.skipped_triage == 1
    assert result.findings == 0
    from synapse_worker.triage_log import TriageLog
    [(seg, reason)] = TriageLog(loop.state_dir).load_skipped()
    assert reason == "lint-clean"


async def test_triage_disabled_passes_everything_through(tmp_path, worker_loop_factory):
    """A lint-clean turn (would be skipped with triage on) must actually reach
    the distiller and produce a finding when triage is off -- the counter
    reading 0 is necessary but not sufficient, since an implementation that
    silently drops every segment also leaves skipped_triage at 0."""
    loop = worker_loop_factory(tmp_path, triage_enabled=False)
    write_transcript_lines(loop.transcript, [
        user_text("fix the imports"),
        assistant_tool_use("Bash", "ruff check --fix ."),
        tool_result("Bash", "Found 3 errors (3 fixed, 0 remaining)."),
        assistant_text("Done."),
        user_text("next"),
    ])
    result = await loop.tick()
    assert result.skipped_triage == 0
    assert result.findings == 1
    from synapse_worker.triage_log import TriageLog
    assert TriageLog(loop.state_dir).load_skipped() == []


async def test_readonly_browsing_turn_is_triaged_out_even_after_compaction(
    tmp_path, worker_loop_factory
):
    """Blocker fix regression, pinned end-to-end through a real
    WorkerLoop.tick(). Before the fix, compaction dropped every trivial
    read-only tool_use/tool_result pair outright; an all-browsing turn (two
    small, clean Read/Grep results, no substantial prose) then had zero
    tool_use events left for triage.readonly-run to key on, fell through to
    default-keep, and reached the distiller -- the exact false positive
    A.5b exists to prevent. See test_compaction.py's unit-level pin of the
    same property."""
    loop = worker_loop_factory(tmp_path)
    write_transcript_lines(loop.transcript, [
        user_text("what does this file do"),
        assistant_tool_use("Read", "a.py", "tid-1"),
        tool_result("Read", "x = 1", "tid-1"),
        assistant_tool_use("Grep", "TODO", "tid-2"),
        tool_result("Grep", "no matches", "tid-2"),
        assistant_text("Nothing notable in there."),
        user_text("ok thanks"),   # closes the turn
    ])
    result = await loop.tick()
    assert result.skipped_triage == 1
    assert result.findings == 0
    from synapse_worker.triage_log import TriageLog
    [(seg, reason)] = TriageLog(loop.state_dir).load_skipped()
    assert reason == "readonly-run"


async def test_shutdown_applies_triage_to_the_flushed_final_turn(tmp_path, worker_loop_factory):
    """The idle-flushed final turn deserves the same filter as tick()'s -- and
    it is the turn most likely to be a lint-clean wrap-up. Regression for the
    untested guard in shutdown()'s segment loop."""
    loop = worker_loop_factory(tmp_path)
    write_transcript_lines(loop.transcript, [
        user_text("fix the imports"),
        assistant_tool_use("Bash", "ruff check --fix ."),
        tool_result("Bash", "Found 3 errors (3 fixed, 0 remaining)."),
        assistant_text("Done, ruff fixed everything."),
        # No closing user line -- this turn is still open when shutdown() runs.
    ])
    await loop.tick()

    result = await loop.shutdown()

    assert result.skipped_triage == 1
    assert result.findings == 0
    from synapse_worker.triage_log import TriageLog
    [(seg, reason)] = TriageLog(loop.state_dir).load_skipped()
    assert reason == "lint-clean"


# -- a provider that dies mid-tick must not cost the conversation -----------
#
# The observed failure (2026-08-07): the seam supervisor declared the NPU dead
# and restarted it while a distillation was in flight. The worker took an
# httpx.ReadError, logged it, dropped the segment, advanced the follower's
# offset past bytes nothing would ever re-read, and reported "0 findings".
# `shutdown()` had been corrected for this in the W3b review; `tick()` — which
# runs every 30 seconds instead of once — had not.


class FlakyProvider(ModelProvider):
    """Raises for its first `failures` calls, then delegates to `inner`.

    Models a provider that is *down*, not one that is wrong: the call never
    produces an answer at all. That distinction is what separates the retry
    path from the prompt-drop path in `tick()`.
    """

    provider_id = "flaky"

    def __init__(self, inner, failures: int) -> None:
        self._inner = inner
        self._failures = failures
        self.attempts = 0

    @property
    def capabilities(self):
        return self._inner.capabilities

    async def complete(self, messages, response_schema=None):
        self.attempts += 1
        if self.attempts <= self._failures:
            raise httpx.ReadError("model seam died mid-request")
        return await self._inner.complete(messages, response_schema)


def build_flaky(tmp_path, scripts: list, failures: int) -> tuple[WorkerLoop, FlakyProvider]:
    transcript = tmp_path / "t.jsonl"
    transcript.touch()
    provider = FlakyProvider(FakeProvider(scripts=scripts), failures)
    loop = WorkerLoop(
        transcript=transcript,
        distiller=Distiller(provider, BINDING, PACK, ["text"], "labelled"),
        producer=Producer(tmp_path / "wal", FileSink(tmp_path / "upstream.jsonl")),
        binding=BINDING,
        state_dir=tmp_path / "state",
        budget_tokens=5000,
    )
    return loop, provider


def deferred_on_disk(loop: WorkerLoop) -> list[dict]:
    path = loop.session_state_dir / "deferred-segments.json"
    return json.loads(path.read_text(encoding="utf-8")) if path.is_file() else []


async def test_a_failed_distillation_is_requeued_not_dropped(tmp_path) -> None:
    """The regression. One provider death used to delete the segment."""
    loop, provider = build_flaky(tmp_path, [condensed("pooling mode matters")], failures=1)
    loop.transcript.write_text(
        user("add pooling") + assistant("done") + user("next"), encoding="utf-8"
    )

    first = await loop.tick()

    assert first.findings == 0
    assert first.requeued == 1
    assert first.abandoned == 0
    # The load-bearing assertion: it is still ON DISK. `_persist_state()` runs
    # at the end of every tick and writes the queue over this file, so a
    # segment that only survived in memory would read as [] here.
    assert len(deferred_on_disk(loop)) == 1
    # And it is still counted as waiting, not silently gone.
    assert first.deferred == 1


async def test_the_requeued_segment_is_distilled_on_the_next_tick(tmp_path) -> None:
    """Re-queued is worthless unless something retries it. Nothing new arrives
    on the transcript between the two ticks — the second tick's finding can
    only have come from the backlog."""
    loop, provider = build_flaky(tmp_path, [condensed("pooling mode matters")], failures=1)
    loop.transcript.write_text(
        user("add pooling") + assistant("done") + user("next"), encoding="utf-8"
    )

    await loop.tick()
    second = await loop.tick()

    assert second.new_lines == 0
    assert second.findings == 1
    assert second.deferred == 0
    assert deferred_on_disk(loop) == []
    assert provider.attempts == 2
    upstream = (tmp_path / "upstream.jsonl").read_text(encoding="utf-8").strip()
    assert json.loads(upstream)["text"] == "pooling mode matters"


class PoisonProvider(ModelProvider):
    """Raises on calls whose messages carry `marker`; answers everything else
    by delegating to `inner`.

    Models the case MAX_DISTIL_ATTEMPTS exists for — a SEGMENT the provider
    dies on deterministically while demonstrably serving the rest — as
    distinct from FlakyProvider's provider that is DOWN, which must never
    cost any segment its place in line.
    """

    provider_id = "poison"

    def __init__(self, inner, marker: str) -> None:
        self._inner = inner
        self._marker = marker
        self.poison_calls = 0

    @property
    def capabilities(self):
        return self._inner.capabilities

    async def complete(self, messages, response_schema=None):
        if any(self._marker in str(m) for m in messages):
            self.poison_calls += 1
            raise httpx.ReadError("provider dies on this segment")
        return await self._inner.complete(messages, response_schema)


def build_poison(tmp_path, scripts: list, marker: str = "POISON") -> tuple[WorkerLoop, PoisonProvider]:
    transcript = tmp_path / "t.jsonl"
    transcript.touch()
    provider = PoisonProvider(FakeProvider(scripts=scripts), marker)
    loop = WorkerLoop(
        transcript=transcript,
        distiller=Distiller(provider, BINDING, PACK, ["text"], "labelled"),
        producer=Producer(tmp_path / "wal", FileSink(tmp_path / "upstream.jsonl")),
        binding=BINDING,
        state_dir=tmp_path / "state",
        budget_tokens=5000,
    )
    return loop, provider


def append(loop: WorkerLoop, text: str) -> None:
    with loop.transcript.open("a", encoding="utf-8") as handle:
        handle.write(text)


async def test_a_poison_segment_is_abandoned_after_charged_attempts_only(tmp_path) -> None:
    """The other half of the bound. A segment the provider deterministically
    dies on must not occupy an admit slot forever — but a failure only burns
    retry budget on ticks where the provider answered something ELSE, which is
    the evidence that the failure was the segment's own. Healthy traffic flows
    around the poison segment the whole time."""
    loop, provider = build_poison(
        tmp_path,
        [condensed("healthy 1"), condensed("healthy 2"), condensed("healthy 3")],
    )
    loop.transcript.write_text(
        user("POISON: plan the migration") + assistant("POISON: acknowledged")
        + user("how do we pool connections") + assistant("pgbouncer, transaction mode")
        + user("open question"),
        encoding="utf-8",
    )

    first = await loop.tick()                      # [poison, healthy-1] admitted
    append(loop, assistant("session pooling breaks prepared statements") + user("next"))
    second = await loop.tick()                     # [poison, healthy-2]
    append(loop, assistant("noted") + user("more"))
    third = await loop.tick()                      # [poison, healthy-3] -> give up
    results = [first, second, third]

    assert [r.requeued for r in results] == [1, 1, 0]
    assert [r.abandoned for r in results] == [0, 0, 1]
    # The healthy segment landed every tick — the poison one starved nothing.
    assert [r.findings for r in results] == [1, 1, 1]
    assert provider.poison_calls == MAX_DISTIL_ATTEMPTS
    # Gone, deliberately — and the queue is empty rather than spinning forever.
    assert deferred_on_disk(loop) == []
    assert "ABANDONED" in results[-1].summary()


async def test_a_provider_outage_longer_than_the_retry_budget_sheds_nothing(tmp_path) -> None:
    """DEFER, NEVER SHED, even when the outage outlives MAX_DISTIL_ATTEMPTS
    ticks. Every tick of a dead provider fails ALL its calls, which is
    indistinguishable from N bad segments — so nobody is charged, and the
    segment is still there when the provider comes back. This is the observed
    incident (an NPU restart spanning several ticks) done honestly: the first
    version of this fix abandoned the segment on the third tick of it."""
    outage_ticks = MAX_DISTIL_ATTEMPTS + 2
    loop, provider = build_flaky(
        tmp_path, [condensed("survived the outage")], failures=outage_ticks)
    loop.transcript.write_text(
        user("add pooling") + assistant("done") + user("next"), encoding="utf-8"
    )

    down = [await loop.tick() for _ in range(outage_ticks)]

    assert [r.abandoned for r in down] == [0] * outage_ticks
    assert [r.requeued for r in down] == [1] * outage_ticks
    assert len(deferred_on_disk(loop)) == 1
    assert loop._attempts == {}, "an all-fail tick must not charge the segment"

    recovered = await loop.tick()
    assert recovered.findings == 1
    assert deferred_on_disk(loop) == []


async def test_mid_batch_failures_requeue_in_order_and_healthy_segments_still_land(tmp_path) -> None:
    """Several failures in one tick keep their relative (admitted) order at
    the head of the queue, and a healthy segment behind them still distils in
    the same tick — its success is also what charges the failures."""
    loop, provider = build_poison(tmp_path, [condensed("healthy finding")])
    loop.transcript.write_text(
        user("POISON one") + assistant("POISON r1")
        + user("POISON two") + assistant("POISON r2")
        + user("clean question") + assistant("clean answer")
        + user("open"),
        encoding="utf-8",
    )

    result = await loop.tick()                     # [poison-1, poison-2, clean]

    assert result.findings == 1
    assert result.requeued == 2
    assert result.abandoned == 0
    assert [d["id"] for d in deferred_on_disk(loop)] == ["sess-1-00001", "sess-1-00002"]
    assert loop._attempts == {"sess-1-00001": 1, "sess-1-00002": 1}
    upstream = (tmp_path / "upstream.jsonl").read_text(encoding="utf-8").strip()
    assert json.loads(upstream)["text"] == "healthy finding"


async def test_a_restart_cannot_reissue_a_queued_segments_id(tmp_path) -> None:
    """Segment ids number off an in-memory counter that restarts at zero,
    while the deferred queue survives on disk with the OLD numbering. Without
    the counter bump in `_restore_deferred`, run 2's first new segment took
    the same id as the restored one still in the queue, and the two shared a
    single retry budget — reproduced in review: the restored segment was
    abandoned a charged attempt early because a same-named newcomer's failure
    was billed to it."""
    loop1, _ = build_flaky(tmp_path, [], failures=99)
    loop1.transcript.write_text(
        user("first question") + assistant("first answer") + user("second"),
        encoding="utf-8",
    )
    await loop1.tick()
    assert [s.id for s in loop1._deferred] == ["sess-1-00001"]
    # The process dies without shutdown; the queue survives on disk.

    loop2, _ = build_flaky(tmp_path, [], failures=99)
    assert [s.id for s in loop2._deferred] == ["sess-1-00001"]
    append(loop2, assistant("second answer") + user("third"))
    await loop2.tick()

    ids = [s.id for s in loop2._deferred]
    assert ids == ["sess-1-00001", "sess-1-00002"], (
        "a new segment re-used a restored segment's id"
    )
    # And with the provider down, neither carries a charged attempt.
    assert loop2._attempts == {}


async def test_an_abort_outside_the_distil_guard_still_requeues_the_batch(tmp_path) -> None:
    """A raise from anything OTHER than the distillation itself — the recorder
    is the live case (disk full) — aborts the tick after `admit()` has already
    taken segments off the queue. `run()` catches the raise and ticks again,
    and THAT tick persists the (now short) queue, so without the salvage
    clause the aborted batch would quietly vanish from disk. Fail toward
    duplication, never loss."""
    loop, _ = build(tmp_path, [condensed("kept"), condensed("kept")])
    loop.transcript.write_text(
        user("q") + assistant("a") + user("next"), encoding="utf-8"
    )
    real_record = loop.producer.record

    def dying_record(findings):
        raise RuntimeError("disk full")

    loop.producer.record = dying_record
    with pytest.raises(RuntimeError):
        await loop.tick()

    assert [s.id for s in loop._deferred] == ["sess-1-00001"]

    loop.producer.record = real_record
    second = await loop.tick()
    assert second.findings == 1


async def test_shutdown_drops_a_prompt_dropped_segment_instead_of_bequeathing_it(tmp_path) -> None:
    """tick() drops a prompt-drop on the first strike because the model
    ANSWERED, wrongly — re-queueing buys nothing but another invented answer.
    `shutdown()`'s bare `except Exception` used to catch PromptDropError too
    and hand the segment to the next run for exactly that pointless retry."""
    transcript = tmp_path / "t.jsonl"
    transcript.touch()
    loop = WorkerLoop(
        transcript=transcript,
        distiller=Distiller(
            FakeProvider(scripts=[condensed("invented")], input_tokens=1),
            BINDING, PACK, ["text"], "labelled",
        ),
        producer=Producer(tmp_path / "wal", FileSink(tmp_path / "upstream.jsonl")),
        binding=BINDING,
        state_dir=tmp_path / "state",
        budget_tokens=5000,
    )
    transcript.write_text(user("a") + assistant("b"), encoding="utf-8")
    await loop.tick()                              # buffer the open turn

    result = await loop.shutdown()

    assert any("prompt-drop" in e for e in result.errors)
    assert deferred_on_disk(loop) == [], "an invented answer was bequeathed to the next run"
    upstream = tmp_path / "upstream.jsonl"
    assert not upstream.exists() or upstream.read_text(encoding="utf-8").strip() == ""


async def test_a_prompt_drop_is_not_retried(tmp_path) -> None:
    """The two except clauses must stay distinct. A prompt-drop is a segment
    the model answered WRONGLY — retrying only buys another invented answer,
    so it is dropped on the first strike, while a segment the provider merely
    FAILED on is re-queued (uncharged while the provider looks down, three
    charged strikes once it is provably up)."""
    transcript = tmp_path / "t.jsonl"
    transcript.touch()
    loop = WorkerLoop(
        transcript=transcript,
        distiller=Distiller(
            FakeProvider(scripts=[condensed("invented")], input_tokens=1),
            BINDING, PACK, ["text"], "labelled",
        ),
        producer=Producer(tmp_path / "wal", FileSink(tmp_path / "upstream.jsonl")),
        binding=BINDING,
        state_dir=tmp_path / "state",
        budget_tokens=5000,
    )
    transcript.write_text(user("a") + assistant("b") + user("c"), encoding="utf-8")

    result = await loop.tick()

    assert any("prompt-drop" in e for e in result.errors)
    assert result.requeued == 0
    assert deferred_on_disk(loop) == []
