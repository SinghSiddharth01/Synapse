"""StatsBuffer — unit coverage plus one integration test wired through a real
WorkerLoop.tick(), arranged the same way test_loop.py's own tests are.
"""

from __future__ import annotations

import json

from synapse_contracts import LocalBinding
from synapse_distiller import Distiller, load_pack_by_name
from synapse_providers import CallLog, FakeProvider, RecordingProvider

from synapse_worker.loop import WorkerLoop
from synapse_worker.producer import FileSink, Producer
from synapse_worker.stats import StatsBuffer

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
