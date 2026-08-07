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
    assert set(snapshot) == {"now", "phase", "current", "ticks", "events", "llm"}


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
    "might be an error" survivor regex, so a (pre-fix) compaction-before-
    triage pipeline would truncate it away. See
    `test_triage_runs_before_compaction_so_a_lint_clean_decoy_cannot_flip_the_verdict`
    for why that removal is exactly what would make this scenario
    order-sensitive if compaction ran first."""
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


async def test_triage_runs_before_compaction_so_a_lint_clean_decoy_cannot_flip_the_verdict(
    tmp_path,
) -> None:
    """Triage runs BEFORE compaction in WorkerLoop.tick (compaction.py's
    module docstring, "WHY THIS RUNS AFTER TRIAGE") — this pins the ordering
    by observation, not just by reading the source.

    Fixer-blocker finding (adjudicated ruling, overriding this test's own
    prior assertions): this scenario used to demonstrate the OPPOSITE of
    what it should — a segment whose only skip-worthy content (a lint-clean
    report) sits inside compaction's truncated middle window used to have
    its verdict flipped from skip to keep by compaction running first,
    reaching the model as `default-keep` even though nothing in the Segment,
    read in full, was worth sending. That is the exact class of bug the
    reorder exists to close structurally: triage's keep/skip judgment must
    be made against the Segment exactly as segmented, never against a view
    compaction has already reshaped.

    The scenario: a Bash (non-read-only) tool_result, long enough to
    actually truncate, with ONE genuinely lint-clean report buried in what
    would be compaction's dropped middle. Evaluated on the RAW segment,
    triage reads that report and skips (`lint-clean`) — asserted directly
    below. The counterfactual right after it documents WHY order matters:
    had compaction run first (the pre-fix order), that report would have
    been truncated away and triage would have flipped to `default-keep` —
    demonstrated directly against `compact()`, never through `tick()`.
    `WorkerLoop.tick()` itself must stay on the `skip` branch: no
    `compaction` event (compaction never runs on a Segment triage has
    already skipped) and no `render` event (nothing reached the distiller).
    A reordering regression (compaction running before triage again) would
    flip this test's tick() back to the kept/rendered branch and fail it."""
    content = _build_log_content()

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
    # What the real pipeline now does: triage the Segment exactly as
    # segmented, before compaction ever touches it.
    raw_decision = triage(raw_segment)
    assert (raw_decision.keep, raw_decision.reason) == (False, "lint-clean")
    # Counterfactual, kept only to document the bug the reorder closes: had
    # compaction run FIRST (the pre-fix order), the buried lint-clean report
    # would have been truncated away and triage would have flipped to keep.
    # `compact()` is never composed this way by the real pipeline anymore.
    compacted_then_triaged = triage(compact(raw_segment))
    assert (compacted_then_triaged.keep, compacted_then_triaged.reason) == (True, "default-keep")

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

    # The real pipeline stayed on skip -- the ordering-sensitive proof above,
    # now observed through a real tick(). Under the pre-fix order this would
    # be 0 (compaction's truncation would have already erased the decoy by
    # the time triage ran).
    assert result.skipped_triage == 1

    snapshot = stats.snapshot()

    triage_events = [e for e in snapshot["events"] if e["tag"] == "triage"]
    assert len(triage_events) == 1
    assert triage_events[0]["detail"]["reason"] == "lint-clean"
    assert "skip" in triage_events[0]["summary"]

    # Compaction never ran -- it only shapes what a KEPT Segment shows the
    # model, and this one was never kept.
    compaction_events = [e for e in snapshot["events"] if e["tag"] == "compaction"]
    assert compaction_events == []

    # Nothing reached the distiller, so there is no render event either.
    render_events = [e for e in snapshot["events"] if e["tag"] == "render"]
    assert render_events == []


async def test_shutdown_also_triages_before_compaction(tmp_path) -> None:
    """The shutdown() half of the reorder, pinned by observation — the final
    verifier proved reverting shutdown() to compact-then-triage left the whole
    suite green while tick()'s equivalent mutant died. Same lint-clean decoy
    as the tick() pin above, but the closing user turn never arrives, so the
    open turn reaches the pipeline through shutdown()'s flush_incomplete
    drain instead of tick()'s."""
    content = _build_log_content()

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
        + assistant("Looked through it, nothing stood out."),
        # no closing user turn: the segment stays pending until shutdown()
        encoding="utf-8",
    )

    tick_result = await loop.tick()
    assert tick_result.segments == 0          # turn still open, nothing drained
    assert tick_result.pending_events > 0

    result = await loop.shutdown()

    # Triage judged the RAW segment and skipped it. Under the pre-fix order
    # (compact before triage in shutdown()) the buried lint-clean report is
    # truncated away, triage flips to default-keep, and this is 0.
    assert result.skipped_triage == 1
    assert result.findings == 0

    snapshot = stats.snapshot()
    compaction_events = [e for e in snapshot["events"] if e["tag"] == "compaction"]
    assert compaction_events == []            # never compacted a skipped Segment
    render_events = [e for e in snapshot["events"] if e["tag"] == "render"]
    assert render_events == []                # nothing reached the distiller


class _CapturingProvider(FakeProvider):
    """FakeProvider that also remembers the exact `messages` it was called
    with, so a test can inspect the literal text the model would have
    received -- not just the token-count/char-count stats derived from it."""

    def __init__(self, scripts: list) -> None:
        super().__init__(scripts)
        self.last_messages: list[dict] | None = None

    async def complete(self, messages, response_schema=None):  # type: ignore[override]
        self.last_messages = messages
        return await super().complete(messages, response_schema=response_schema)


def _build_buried_error_log_content() -> str:
    """Adjudicated-ruling pin scenario: 60 benign lines with exactly ONE real,
    uncleared error -- `fatal: ...`, matching `triage.ERROR_RE`'s `\\bfatal\\b`
    -- buried at index 30, i.e. inside `_truncate_tool_result_lines`'s
    dropped middle window (`HEAD_TAIL_LINES = 15` on a 60-line body: kept
    head is [0:15], kept tail is [45:60], dropped middle is [15:45]) and well
    outside either the naive first-15 or last-15 lines the ruling names."""
    lines = [f"checking module {i}... ok, nothing notable here" for i in range(60)]
    lines[30] = "fatal: unable to access repo: Could not resolve host"
    return "\n".join(lines)


async def test_a_buried_error_is_kept_by_triage_and_its_render_event_is_the_compacted_form(
    tmp_path,
) -> None:
    """Adjudicated ruling, pinning the reorder end to end: triage runs BEFORE
    compaction, on the full uncompacted Segment (A.5b's "triage reads the
    full Segment", made literal) -- and compaction, once a Segment has
    earned a `keep`, shapes only what the model sees.

    The scenario is the ruling's own: a Segment whose only error sits mid
    `tool_result`, outside compaction's head/tail 15-line window. Triage
    must KEEP it -- on the raw content, error-signal is the only keep-signal
    that can fire here (no thinking, no decision language, no other
    tool_result). The `render` event -- observed through a real
    `WorkerLoop.tick()`, not a direct `compact()`/`triage()` call -- must
    then show the COMPACTED form: the omission marker present, the buried
    `fatal:` line preserved (compaction's own error-preservation, still
    doing its job for the model's sake now rather than triage's -- see
    compaction.py's module docstring), and the full 60-line body gone.

    `distil_kinds=["text", "tool_result"]` (unlike this file's other tests)
    so the tool_result's compacted CONTENT actually reaches the rendered
    prompt where it can be inspected directly, rather than being filtered
    out by the default text-only kind list before compaction's effect on it
    would be observable at all."""
    content = _build_buried_error_log_content()

    # Establishes the premise directly: triage on the RAW segment reads the
    # buried, uncleared `fatal:` line and keeps for that reason alone.
    raw_segment = Segment(
        id="sess-1-00001",
        agent_session_id="sess-1",
        started_at=datetime(2026, 8, 4, tzinfo=timezone.utc),
        ended_at=datetime(2026, 8, 4, tzinfo=timezone.utc),
        events=[
            _ev(role="user", content="can you check the whole build log for anything odd"),
            _ev(kind="tool_use", tool_name="Bash", content="run full lint+build"),
            _ev(role="user", kind="tool_result", tool_name="Bash", content=content),
        ],
    )
    raw_decision = triage(raw_segment)
    assert (raw_decision.keep, raw_decision.reason) == (True, "error-signal")

    transcript = tmp_path / "t.jsonl"
    transcript.touch()
    producer = Producer(tmp_path / "wal", FileSink(tmp_path / "upstream.jsonl"))
    stats = StatsBuffer(CallLog())
    provider = _CapturingProvider([condensed("connection reset while syncing")])
    loop = WorkerLoop(
        transcript=transcript,
        distiller=Distiller(provider, BINDING, PACK, ["text", "tool_result"], "labelled"),
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
        + user("thanks, that's helpful"),  # closes the turn above
        encoding="utf-8",
    )

    result = await loop.tick()

    # Kept, not skipped -- the real pipeline reached the same verdict as the
    # raw-segment counterfactual above.
    assert result.skipped_triage == 0
    assert result.findings == 1

    snapshot = stats.snapshot()

    triage_events = [e for e in snapshot["events"] if e["tag"] == "triage"]
    assert len(triage_events) == 1
    assert triage_events[0]["detail"]["reason"] == "error-signal"

    # Compaction DID run -- only reachable for a Segment triage already kept
    # -- and it shrank the tool_result.
    compaction_events = [e for e in snapshot["events"] if e["tag"] == "compaction"]
    assert len(compaction_events) == 1
    assert compaction_events[0]["detail"]["chars_saved"] > 0

    render_events = [e for e in snapshot["events"] if e["tag"] == "render"]
    assert len(render_events) == 1

    # The strongest evidence: the literal prompt text the model received is
    # the COMPACTED form, not the raw 60-line body -- the render event's
    # "events_in"/"retained" counts alone can't distinguish "compaction ran"
    # from "compaction ran and then got discarded before rendering", so this
    # inspects what `WorkerLoop.tick()` actually handed the distiller.
    assert provider.last_messages is not None
    rendered = str(provider.last_messages[-1].get("content", ""))
    assert "fatal: unable to access repo" in rendered  # the buried error survived
    assert "checking module 0... ok" in rendered  # a kept head line survived
    assert "checking module 59... ok" in rendered  # a kept tail line survived
    assert "checking module 20... ok" not in rendered  # a dropped middle line did not
    assert "⋯ compacted ⋯" in rendered  # compaction's own omission marker
    assert len(rendered) < len(content)  # net shrink -- the compacted form, not the raw one
