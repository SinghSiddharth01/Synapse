"""SessionBinding round-trip and durability."""

from __future__ import annotations

from datetime import datetime, timezone

from synapse_contracts import SessionBinding, clear_binding, read_binding, write_binding
from synapse_contracts.schemas import LocalBinding

TS = datetime(2026, 8, 4, 12, 0, tzinfo=timezone.utc)


def _binding(**overrides) -> SessionBinding:
    base = dict(
        agent_session_id="as-1",
        shared_id="team-standup",
        contributor="aditya",
        agent="claude-code",
        transcript_path="/repo/transcript.jsonl",
        pinned_at=TS,
    )
    return SessionBinding(**{**base, **overrides})


def test_round_trips_through_disk(tmp_path) -> None:
    path = tmp_path / "bindings" / "active.json"
    original = _binding()

    write_binding(path, original)
    restored = read_binding(path)

    assert restored == original


def test_absent_file_reads_as_none(tmp_path) -> None:
    assert read_binding(tmp_path / "nope.json") is None


def test_corrupt_file_reads_as_none_not_raise(tmp_path) -> None:
    path = tmp_path / "active.json"
    path.write_text("{ not json", encoding="utf-8")

    assert read_binding(path) is None


def test_write_is_atomic(tmp_path) -> None:
    path = tmp_path / "active.json"
    write_binding(path, _binding())

    assert path.is_file()
    assert not path.with_suffix(".json.tmp").exists()


def test_clear_removes_the_pin(tmp_path) -> None:
    path = tmp_path / "active.json"
    write_binding(path, _binding())

    clear_binding(path)

    assert read_binding(path) is None


def test_clear_on_absent_binding_does_not_raise(tmp_path) -> None:
    clear_binding(tmp_path / "never-existed.json")  # must not raise


def test_converts_to_local_binding_without_the_disk_only_fields() -> None:
    session = _binding()

    local = session.to_local_binding()

    assert isinstance(local, LocalBinding)
    assert local.agent_session_id == session.agent_session_id
    assert local.shared_id == session.shared_id
    assert local.contributor == session.contributor
    assert local.agent == session.agent
