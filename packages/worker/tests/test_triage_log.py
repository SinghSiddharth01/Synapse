from datetime import datetime, timezone
from pathlib import Path

from synapse_contracts import AgentEvent, Segment
from synapse_worker.triage_log import SKIPS_FILENAME, TriageLog

TS = datetime(2026, 8, 4, tzinfo=timezone.utc)


def _seg(seg_id: str) -> Segment:
    ev = AgentEvent(role="assistant", kind="text", content="ruff pass, nothing left",
                    ts=TS, agent_session_id="as-t")
    return Segment(id=seg_id, agent_session_id="as-t", events=[ev],
                   started_at=TS, ended_at=TS)


def test_skip_roundtrips_the_full_segment(tmp_path: Path):
    log = TriageLog(tmp_path)
    log.record_skip(_seg("s-1"), "lint-clean")
    [(segment, reason)] = log.load_skipped()
    assert segment.id == "s-1" and reason == "lint-clean"
    assert segment.events[0].content == "ruff pass, nothing left"  # exact, not approximate


def test_append_only_across_instances(tmp_path: Path):
    TriageLog(tmp_path).record_skip(_seg("s-1"), "lint-clean")
    TriageLog(tmp_path).record_skip(_seg("s-2"), "readonly-run")
    assert [s.id for s, _ in TriageLog(tmp_path).load_skipped()] == ["s-1", "s-2"]


def test_archive_renames_and_resets(tmp_path: Path):
    log = TriageLog(tmp_path)
    log.record_skip(_seg("s-1"), "lint-clean")
    archived = log.archive()
    assert archived is not None and archived.exists()
    assert not (tmp_path / SKIPS_FILENAME).exists()
    assert log.load_skipped() == []
    assert TriageLog(tmp_path).archive() is None  # nothing to archive twice


def test_corrupt_line_is_skipped_not_fatal(tmp_path: Path):
    log = TriageLog(tmp_path)
    log.record_skip(_seg("s-1"), "lint-clean")
    with (tmp_path / SKIPS_FILENAME).open("a", encoding="utf-8") as fh:
        fh.write("{not json\n")
    log.record_skip(_seg("s-2"), "readonly-run")
    assert [s.id for s, _ in log.load_skipped()] == ["s-1", "s-2"]
