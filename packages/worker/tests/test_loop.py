"""End-to-end loop tests: transcript on disk -> findings at the sink.

Offline — FakeProvider, no NPU, no network.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

import pytest
from synapse_contracts import Attribution, Finding, FindingType, LocalBinding
from synapse_distiller import Distiller, load_pack_by_name
from synapse_providers import FakeProvider

from synapse_worker.loop import WorkerLoop
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
