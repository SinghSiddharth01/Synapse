"""StatsBuffer — unit coverage plus one integration test wired through a real
WorkerLoop.tick(), arranged the same way test_loop.py's own tests are.
"""

from __future__ import annotations

import json

from datetime import datetime, timezone

from synapse_contracts import AgentEvent, LocalBinding, Segment
from synapse_distiller import Distiller, load_pack_by_name
from synapse_providers import CallLog, FakeProvider, RecordingProvider

from synapse_worker.compaction import compact
from synapse_worker.loop import WorkerLoop
from synapse_worker.producer import FileSink, Producer
from synapse_worker.stats import StatsBuffer
from synapse_worker.triage import triage

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


def assistant_tool_use(tool_name: str, command: str, tool_id: str = "tool-1") -> str:
    return line(
        type="assistant",
        message={"content": [
            {"type": "tool_use", "id": tool_id, "name": tool_name, "input": {"command": command}}
        ]},
    )


def tool_result(tool_name: str, content: str, tool_id: str = "tool-1") -> str:
    # tool_name accepted purely for readability at the call site -- see
    # test_loop.py's identical helper for why the real name is resolved via
    # tool_id instead.
    return line(
        type="user",
        message={"content": [{"type": "tool_result", "tool_use_id": tool_id, "content": content}]},
    )


def condensed(*texts: str) -> dict:
    return {"findings": [{"type": "learning", "text": t} for t in texts]}


def test_snapshot_is_json_dumps_able() -> None:
    stats = StatsBuffer(CallLog())
    stats.event("tick", "5 lines")
    snapshot = stats.snapshot()
    json.dumps(snapshot)  # must not raise
    assert set(snapshot) == {"now", "current", "ticks", "events", "llm"}


def test_distil_started_and_finished() -> None:
    stats = StatsBuffer(CallLog())
    stats.distil_started("seg-1", 4)
    assert stats.current["segment_id"] == "seg-1"
    assert stats.current["events"] == 4
    assert "started_iso" in stats.current

    stats.distil_finished()
    assert stats.current is None


def test_event_lands_in_events_with_tag_and_ts() -> None:
    stats = StatsBuffer(CallLog())
    stats.event("triage", "skip seg-1 (lint-clean)", segment="seg-1", reason="lint-clean")
    [entry] = stats.snapshot()["events"]
    assert entry["tag"] == "triage"
    assert entry["summary"] == "skip seg-1 (lint-clean)"
    assert entry["detail"] == {"segment": "seg-1", "reason": "lint-clean"}
    assert "ts_iso" in entry


def test_ring_bounds_hold() -> None:
    stats = StatsBuffer(CallLog())
    for i in range(250):
        stats.event("tick", f"n={i}")
    assert len(stats.snapshot()["events"]) == 200

    for i in range(150):
        stats.tick({"n": i})
    assert len(stats.snapshot()["ticks"]) == 100


async def test_worker_tick_populates_the_feed(tmp_path) -> None:
    transcript = tmp_path / "t.jsonl"
    transcript.touch()
    producer = Producer(tmp_path / "wal", FileSink(tmp_path / "upstream.jsonl"))
    call_log = CallLog()
    stats = StatsBuffer(call_log)
    provider = RecordingProvider(
        FakeProvider(scripts=[condensed("pooling mode matters")]), "distiller", call_log
    )
    loop = WorkerLoop(
        transcript=transcript,
        distiller=Distiller(provider, BINDING, PACK, ["text"], "labelled"),
        producer=producer,
        binding=BINDING,
        state_dir=tmp_path / "state",
        budget_tokens=5000,
        stats=stats,
    )
    transcript.write_text(
        user("add pooling") + assistant("done, session mode") + user("now the cache"),
        encoding="utf-8",
    )

    await loop.tick()

    snapshot = stats.snapshot()
    json.dumps(snapshot)

    tick_events = [e for e in snapshot["events"] if e["tag"] == "tick"]
    assert len(tick_events) >= 1

    render_events = [e for e in snapshot["events"] if e["tag"] == "render"]
    assert len(render_events) >= 1
    detail = render_events[0]["detail"]
    assert "events_in" in detail and "retained" in detail and "kinds" in detail

    llm_calls = [c for c in snapshot["llm"] if c["component"] == "distiller"]
    assert len(llm_calls) >= 1


async def test_render_event_retained_count_excludes_kinds_the_distiller_drops(tmp_path) -> None:
    """`retained` is the one number the render tag exists to report: events by
    kind IN vs. retained under distil_kinds (render_segment's own filter,
    prompt.py). An all-text fixture can't discriminate the correct
    kind-filtered count from the wrong `len(segment.events)` -- they're equal
    either way. This turn mixes a tool_use/tool_result pair in among the text
    events, with kinds=["text"], so retained (2) must come out strictly below
    events_in (4).
    """
    transcript = tmp_path / "t.jsonl"
    transcript.touch()
    producer = Producer(tmp_path / "wal", FileSink(tmp_path / "upstream.jsonl"))
    call_log = CallLog()
    stats = StatsBuffer(call_log)
    provider = RecordingProvider(
        FakeProvider(scripts=[condensed("pooling mode matters")]), "distiller", call_log
    )
    loop = WorkerLoop(
        transcript=transcript,
        distiller=Distiller(provider, BINDING, PACK, ["text"], "labelled"),
        producer=producer,
        binding=BINDING,
        state_dir=tmp_path / "state",
        budget_tokens=5000,
        stats=stats,
    )
    transcript.write_text(
        user("investigate the flaky test")
        + assistant_tool_use("Bash", "pytest -x")
        + tool_result("Bash", "3 passed")
        + assistant("found the pooling issue")
        + user("now look at the cache"),  # closes the turn above
        encoding="utf-8",
    )

    await loop.tick()

    render_events = [e for e in stats.snapshot()["events"] if e["tag"] == "render"]
    assert len(render_events) >= 1
    detail = render_events[0]["detail"]
    assert detail["events_in"] == 4
    assert detail["retained"] == 2
    assert detail["retained"] < detail["events_in"]


def _build_log_content() -> str:
    """40 benign lines with one genuinely lint-clean report buried at index
    20 -- inside `_truncate_tool_result_lines`'s dropped middle window
    (`HEAD_TAIL_LINES = 15`, so line 20 of 40 falls in `lines[15:25]`, not
    the kept head or tail) and NOT matching compaction's own broad
    "might be an error" survivor regex, so compaction's truncation removes
    it. See `test_compaction_runs_before_triage_and_the_render_event_sees_its_output`
    for why that removal is exactly what makes this scenario order-sensitive."""
    lines = []
    for i in range(40):
        if i == 20:
            lines.append("eslint: 15 problems (15 fixed, 0 remaining)")
        else:
            lines.append(f"checking module {i}... ok, nothing notable here")
    return "\n".join(lines)


def _ev(role="assistant", kind="text", content="", tool_name=None) -> AgentEvent:
    return AgentEvent(
        role=role, kind=kind, content=content, tool_name=tool_name,
        ts=datetime(2026, 8, 4, tzinfo=timezone.utc), agent_session_id="sess-1",
    )


async def test_compaction_runs_before_triage_and_the_render_event_sees_its_output(
    tmp_path,
) -> None:
    """Compaction runs BEFORE triage in WorkerLoop.tick (compaction.py's
    module docstring) — this pins the ordering by observation, not just by
    reading the source.

    Fixer amendment: the original scenario here was a trivial read-only
    Read call, which the FIRST version of compaction dropped outright
    (tool_use + tool_result both vanish). That version's bug (a blocker,
    fixed separately -- see compaction.py's AMENDMENT and test_compaction.py)
    is exactly what this test's old assertions relied on: `events_kept == 2`
    assumed dropping, which the corrected compaction never does again (it
    only ever collapses `tool_result` CONTENT, never removes a `tool_use`/
    `tool_result` event -- see compaction.py's module docstring). Worse, the
    corrected compaction+triage pipeline now triages that exact old scenario
    OUT as `readonly-run` (unaffected by the fix -- both `tool_use` events
    survive compaction, so the skip-signature still fires), so no `render`
    event was ever reachable through the old scenario at all.

    The new scenario is chosen to still discriminate order, not just avoid
    crashing: a Bash (non-read-only) tool_result, long enough to actually
    truncate, with ONE genuinely lint-clean report buried in the dropped
    middle. Evaluated on the RAW segment, triage reads that report and skips
    (`lint-clean`) -- asserted directly below as the counterfactual. Evaluated
    on the segment `WorkerLoop.tick()` ACTUALLY triages -- the compacted one
    -- that report is gone (truncated away, `chars_saved > 0`) and the
    segment falls through to `default-keep`, reaching a real `render` event.
    A reordering bug (triage seeing the pre-compaction segment) would flip
    this test's tick() back to the skipped branch and fail it."""
    content = _build_log_content()

    # The counterfactual: triage(), called directly on the RAW segment (as
    # it would be if compaction ran AFTER triage, or not at all), skips.
    raw_segment = Segment(
        id="sess-1-00001",
        agent_session_id="sess-1",
        started_at=datetime(2026, 8, 4, tzinfo=timezone.utc),
        ended_at=datetime(2026, 8, 4, tzinfo=timezone.utc),
        events=[
            _ev(role="user", content="can you check the whole build log for anything odd"),
            _ev(kind="tool_use", tool_name="Bash", content="run full lint+build"),
            _ev(role="user", kind="tool_result", tool_name="Bash", content=content),
            _ev(content="Looked through it, nothing stood out."),
        ],
    )
    raw_decision = triage(raw_segment)
    assert (raw_decision.keep, raw_decision.reason) == (False, "lint-clean")
    # And the compacted segment -- what the real pipeline below triages --
    # reverses that verdict, because compaction truncated the buried report
    # away before triage ever ran.
    compacted_decision = triage(compact(raw_segment))
    assert (compacted_decision.keep, compacted_decision.reason) == (True, "default-keep")

    transcript = tmp_path / "t.jsonl"
    transcript.touch()
    producer = Producer(tmp_path / "wal", FileSink(tmp_path / "upstream.jsonl"))
    call_log = CallLog()
    stats = StatsBuffer(call_log)
    provider = RecordingProvider(
        FakeProvider(scripts=[condensed("nothing notable in the build log")]),
        "distiller", call_log,
    )
    loop = WorkerLoop(
        transcript=transcript,
        distiller=Distiller(provider, BINDING, PACK, ["text"], "labelled"),
        producer=producer,
        binding=BINDING,
        state_dir=tmp_path / "state",
        budget_tokens=5000,
        stats=stats,
    )
    transcript.write_text(
        user("can you check the whole build log for anything odd")
        + assistant_tool_use("Bash", "run full lint+build")
        + tool_result("Bash", content)
        + assistant("Looked through it, nothing stood out.")
        + user("thanks, that's helpful"),  # closes the turn above
        encoding="utf-8",
    )

    result = await loop.tick()

    # The real pipeline reached keep, not skip -- the ordering-sensitive
    # half of the proof above, now observed through a real tick().
    assert result.skipped_triage == 0

    snapshot = stats.snapshot()

    compaction_events = [e for e in snapshot["events"] if e["tag"] == "compaction"]
    assert len(compaction_events) == 1
    detail = compaction_events[0]["detail"]
    assert detail["events_total"] == 4
    # Compaction never removes events post-fix (see the docstring above) --
    # only the trivial/lint-clean CONTENT inside them shrinks.
    assert detail["events_kept"] == 4
    assert detail["chars_saved"] > 0
    assert "4/4" in compaction_events[0]["summary"]
    assert "kept" in compaction_events[0]["summary"]
    assert "chars saved" in compaction_events[0]["summary"]

    triage_events = [e for e in snapshot["events"] if e["tag"] == "triage"]
    assert len(triage_events) == 1
    assert triage_events[0]["detail"]["reason"] == "default-keep"

    render_events = [e for e in snapshot["events"] if e["tag"] == "render"]
    assert len(render_events) >= 1
    # events_in reflects the segment triage/render actually saw -- the
    # compacted one, same event count as raw (compaction only ever shrinks
    # content, per the fix), but only reachable at all because that
    # compacted content is what triage evaluated.
    assert render_events[0]["detail"]["events_in"] == 4
