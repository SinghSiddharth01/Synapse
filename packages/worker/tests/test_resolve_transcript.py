"""resolve_transcript and join_session — Plan D.2/D.3's actual join semantics.

Binding is per Agent PRODUCT (claude-code.json, codex.json — Plan D.2: "one
laptop holds several bindings — one per Agent Session; Claude Code and Codex
can sit in different Shared Sessions"), not a single active.json. These cover
join_session's DETECTION path — what it does when nobody names a session, which
is still the default and still binds whatever is live right now.

The explicit path (`agent_session_id=...`), added 2026-08-06 by
docs/superpowers/specs/2026-08-06-session-lifecycle-design.md, deliberately does
let a caller pick a specific transcript; it lives in test_session_id_binding.py.
This file's cases must keep passing unchanged, because that argument is opt-in
and detection is what every existing caller still gets.
"""

from __future__ import annotations

from synapse_worker.discovery import (
    binding_path_for_agent,
    join_session,
    project_slug,
    resolve_transcript,
)


def _make_transcript(root, cwd, name: str, content: str = "{}\n"):
    slug_dir = root / project_slug(cwd)
    slug_dir.mkdir(parents=True, exist_ok=True)
    path = slug_dir / f"{name}.jsonl"
    path.write_text(content, encoding="utf-8")
    return path


def test_no_join_and_no_transcript_returns_none(tmp_path) -> None:
    cwd = tmp_path / "repo"
    cwd.mkdir()
    root = tmp_path / "projects"
    state_dir = tmp_path / "state"

    assert resolve_transcript(cwd, state_dir, projects_root=root) is None


def test_no_join_falls_back_to_the_heuristic(tmp_path) -> None:
    cwd = tmp_path / "repo"
    cwd.mkdir()
    root = tmp_path / "projects"
    transcript = _make_transcript(root, cwd, "sess-1")
    state_dir = tmp_path / "state"

    result = resolve_transcript(cwd, state_dir, projects_root=root)

    assert result.source == "heuristic"
    assert result.path == transcript
    assert result.local_binding is None


def test_join_binds_the_currently_detected_transcript(tmp_path) -> None:
    cwd = tmp_path / "repo"
    cwd.mkdir()
    root = tmp_path / "projects"
    transcript = _make_transcript(root, cwd, "sess-1")
    state_dir = tmp_path / "state"

    bindings = join_session("team-standup", "akhil", cwd, state_dir, "http://127.0.0.1:8899", projects_root=root)

    assert len(bindings) == 1
    assert bindings[0].shared_id == "team-standup"
    assert bindings[0].contributor == "akhil"
    assert bindings[0].agent == "claude-code"
    assert bindings[0].transcript_path == str(transcript)


def test_join_with_nothing_live_binds_nothing(tmp_path) -> None:
    cwd = tmp_path / "repo"
    cwd.mkdir()
    root = tmp_path / "projects"
    state_dir = tmp_path / "state"

    bindings = join_session("team-standup", "akhil", cwd, state_dir, "http://127.0.0.1:8899", projects_root=root)

    assert bindings == []


def test_joined_binding_wins_over_a_more_recently_written_transcript(tmp_path) -> None:
    """The exact ambiguity /synapse start would have solved differently: join
    binds whatever was live AT JOIN TIME, and a later, unrelated transcript
    appearing afterward must not steal the worker's attention."""
    cwd = tmp_path / "repo"
    cwd.mkdir()
    root = tmp_path / "projects"
    state_dir = tmp_path / "state"
    joined_transcript = _make_transcript(root, cwd, "sess-at-join-time")

    join_session("team-standup", "aditya", cwd, state_dir, "http://127.0.0.1:8899", projects_root=root)

    _make_transcript(root, cwd, "sess-appeared-later")  # a second, newer transcript

    result = resolve_transcript(cwd, state_dir, projects_root=root)

    assert result.source == "pinned"
    assert result.path == joined_transcript


def test_binding_is_keyed_by_agent_product_not_a_single_file(tmp_path) -> None:
    """Plan D.2: one binding per Agent Session, Claude Code and Codex can sit
    in different Shared Sessions. The storage location must not collide."""
    state_dir = tmp_path / "state"

    assert binding_path_for_agent(state_dir, "claude-code") != binding_path_for_agent(
        state_dir, "codex"
    )
    assert binding_path_for_agent(state_dir, "claude-code").name == "claude-code.json"


def test_join_pointing_at_a_since_deleted_transcript_falls_back_to_heuristic(tmp_path) -> None:
    cwd = tmp_path / "repo"
    cwd.mkdir()
    root = tmp_path / "projects"
    state_dir = tmp_path / "state"
    joined_transcript = _make_transcript(root, cwd, "sess-1")
    join_session("team-standup", "aditya", cwd, state_dir, "http://127.0.0.1:8899", projects_root=root)

    joined_transcript.unlink()
    live = _make_transcript(root, cwd, "sess-still-here")

    result = resolve_transcript(cwd, state_dir, projects_root=root)

    assert result.source == "heuristic"
    assert result.path == live
