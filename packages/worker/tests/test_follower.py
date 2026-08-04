"""Follower tests — the delta, and the durability that makes it safe."""

from __future__ import annotations

import pytest

from synapse_worker.follower import TranscriptFollower


def write(path, text: str) -> None:
    with path.open("a", encoding="utf-8") as handle:
        handle.write(text)


def test_reads_only_what_is_new(tmp_path) -> None:
    transcript = tmp_path / "t.jsonl"
    write(transcript, "one\ntwo\n")
    follower = TranscriptFollower(tmp_path / "state.json")

    assert follower.read_new_lines(transcript) == ["one", "two"]
    assert follower.read_new_lines(transcript) == []

    write(transcript, "three\n")
    assert follower.read_new_lines(transcript) == ["three"]


def test_partial_trailing_line_waits_for_its_newline(tmp_path) -> None:
    """The transcript is being appended to by another process, so a read can
    land mid-line. Returning it would hand the parser a torn record."""
    transcript = tmp_path / "t.jsonl"
    write(transcript, "complete\npar")
    follower = TranscriptFollower(tmp_path / "state.json")

    assert follower.read_new_lines(transcript) == ["complete"]

    write(transcript, "tial\n")
    assert follower.read_new_lines(transcript) == ["partial"]


def test_a_single_unterminated_line_does_not_advance(tmp_path) -> None:
    transcript = tmp_path / "t.jsonl"
    write(transcript, "no newline yet")
    follower = TranscriptFollower(tmp_path / "state.json")

    assert follower.read_new_lines(transcript) == []

    write(transcript, "\n")
    assert follower.read_new_lines(transcript) == ["no newline yet"]


def test_offset_survives_a_restart(tmp_path) -> None:
    """The core durability property: a restart must not re-distil old content
    at ~13 tok/s, and must not skip content either."""
    transcript = tmp_path / "t.jsonl"
    state = tmp_path / "state.json"
    write(transcript, "one\ntwo\n")

    first = TranscriptFollower(state)
    assert first.read_new_lines(transcript) == ["one", "two"]
    first.save()

    write(transcript, "three\n")
    second = TranscriptFollower(state)
    assert second.read_new_lines(transcript) == ["three"]


def test_truncation_resets_to_the_start(tmp_path) -> None:
    transcript = tmp_path / "t.jsonl"
    write(transcript, "one\ntwo\nthree\n")
    follower = TranscriptFollower(tmp_path / "state.json")
    follower.read_new_lines(transcript)

    transcript.write_text("fresh\n", encoding="utf-8")

    assert follower.read_new_lines(transcript) == ["fresh"]


def test_stat_gate_reports_no_change(tmp_path) -> None:
    transcript = tmp_path / "t.jsonl"
    write(transcript, "one\n")
    follower = TranscriptFollower(tmp_path / "state.json")

    assert follower.has_new_data(transcript) is True
    follower.read_new_lines(transcript)
    assert follower.has_new_data(transcript) is False

    write(transcript, "two\n")
    assert follower.has_new_data(transcript) is True


def test_missing_file_is_not_an_error(tmp_path) -> None:
    follower = TranscriptFollower(tmp_path / "state.json")
    missing = tmp_path / "nope.jsonl"

    assert follower.has_new_data(missing) is False
    assert follower.read_new_lines(missing) == []


def test_attach_at_end_skips_existing_history(tmp_path) -> None:
    """A live transcript is routinely megabytes; distilling it from scratch
    would be hours of NPU time for context nobody asked for."""
    transcript = tmp_path / "t.jsonl"
    write(transcript, "old\nhistory\n")
    follower = TranscriptFollower(tmp_path / "state.json")

    follower.start_at_end(transcript)
    assert follower.read_new_lines(transcript) == []

    write(transcript, "new\n")
    assert follower.read_new_lines(transcript) == ["new"]


def test_corrupt_state_refuses_to_guess(tmp_path) -> None:
    """Guessing costs either hours of duplicate NPU work or silent data loss."""
    state = tmp_path / "state.json"
    state.write_text("{ broken", encoding="utf-8")

    with pytest.raises(RuntimeError, match="unreadable"):
        TranscriptFollower(state)


def test_state_write_is_atomic(tmp_path) -> None:
    transcript = tmp_path / "t.jsonl"
    write(transcript, "one\n")
    state = tmp_path / "state.json"
    follower = TranscriptFollower(state)
    follower.read_new_lines(transcript)
    follower.save()

    assert state.is_file()
    assert not state.with_suffix(".json.tmp").exists()
