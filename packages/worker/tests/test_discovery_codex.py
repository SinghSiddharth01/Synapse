"""Codex detection — mirrors test_resolve_transcript.py's Claude Code
coverage, plus Task A.1's registry-level tests (docs/plans/2026-08-03-plan-a-
capture.md): 'a fixture tree containing a [agent] transcript yields one
detected Agent Session tagged [agent]; two agents' trees yield two; an
unregistered directory yields none; a stale (non-growing) transcript is not
treated as active.'

`join_session` itself is deliberately NOT extended to auto-detect Codex in
this pass -- see the module docstring note in discovery.py and this chain's
deviations. These tests exercise the registry and per-agent detection/
resolution directly instead, which is the piece Chain A's acceptance
("detection finds a Codex fixture tree") actually asks for.
"""

from __future__ import annotations

import os
import time
from datetime import datetime, timezone

from synapse_contracts import SessionBinding, read_binding, write_binding

from synapse_worker.discovery import (
    AGENT_REGISTRY,
    binding_path_for_agent,
    find_codex_transcripts,
    project_slug,
    resolve_transcript,
)
from synapse_worker.sources.claude_code import ClaudeCodeSource
from synapse_worker.sources.codex import CodexSource


def _make_codex_transcript(root, day: str, ts: str, uuid: str, content: str = ""):
    day_dir = root / day
    day_dir.mkdir(parents=True, exist_ok=True)
    path = day_dir / f"rollout-{ts}-{uuid}.jsonl"
    path.write_text(content, encoding="utf-8")
    return path


def _make_claude_transcript(root, cwd, name: str, content: str = "{}\n"):
    slug_dir = root / project_slug(cwd)
    slug_dir.mkdir(parents=True, exist_ok=True)
    path = slug_dir / f"{name}.jsonl"
    path.write_text(content, encoding="utf-8")
    return path


# ---------------------------------------------------------------------------
# Task A.1: static registry
# ---------------------------------------------------------------------------

def test_registry_has_an_entry_per_agent_with_a_source_class() -> None:
    assert set(AGENT_REGISTRY) == {"claude-code", "codex"}
    assert AGENT_REGISTRY["claude-code"].source_class is ClaudeCodeSource
    assert AGENT_REGISTRY["codex"].source_class is CodexSource


def test_registry_detects_both_agents_independently_two_trees_yield_two(tmp_path) -> None:
    """Task A.1's 'two agents' trees yield two.' Each registered agent's own
    finder only sees its own dialect -- there is no cross-talk between them."""
    claude_root = tmp_path / "claude-projects"
    codex_root = tmp_path / "codex-sessions"
    cwd = tmp_path / "repo"
    cwd.mkdir()

    _make_claude_transcript(claude_root, cwd, "sess-1")
    _make_codex_transcript(codex_root, "2026/08/05", "2026-08-05T10-00-00", "1c9b6d8e-27ac-4f1e-9f2c-8a2b1e6d4c11")

    detected = {
        agent: registration.finder(None, {"claude-code": claude_root, "codex": codex_root}[agent])
        for agent, registration in AGENT_REGISTRY.items()
    }

    assert len(detected["claude-code"]) == 1
    assert len(detected["codex"]) == 1
    assert detected["claude-code"][0].agent == "claude-code"
    assert detected["codex"][0].agent == "codex"


def test_an_unregistered_directory_yields_none_for_either_finder(tmp_path) -> None:
    empty_root = tmp_path / "nothing-here"

    assert find_codex_transcripts(None, empty_root) == []
    assert AGENT_REGISTRY["claude-code"].finder(None, empty_root) == []


# ---------------------------------------------------------------------------
# find_codex_transcripts
# ---------------------------------------------------------------------------

def test_find_codex_transcripts_detects_a_fixture_tree(tmp_path) -> None:
    root = tmp_path / "sessions"
    uuid = "1c9b6d8e-27ac-4f1e-9f2c-8a2b1e6d4c11"
    path = _make_codex_transcript(root, "2026/08/05", "2026-08-05T10-00-00", uuid)

    found = find_codex_transcripts(None, root)

    assert len(found) == 1
    assert found[0].path == path
    assert found[0].agent == "codex"


def test_find_codex_transcripts_yields_none_for_a_missing_root(tmp_path) -> None:
    assert find_codex_transcripts(None, tmp_path / "does-not-exist") == []


def test_session_id_is_parsed_from_the_rollout_filename_not_the_file_stem(tmp_path) -> None:
    """Mirrors openai/codex's own parse_timestamp_uuid_from_filename
    (codex-rs/rollout/src/list.rs) -- the session id is the UUID suffix, not
    the whole filename stem the way Claude Code's transcripts work."""
    root = tmp_path / "sessions"
    uuid = "1c9b6d8e-27ac-4f1e-9f2c-8a2b1e6d4c11"
    _make_codex_transcript(root, "2026/08/05", "2026-08-05T10-00-00", uuid)

    (found,) = find_codex_transcripts(None, root)

    assert found.session_id == uuid


def test_find_codex_transcripts_sorts_most_recently_written_first(tmp_path) -> None:
    root = tmp_path / "sessions"
    older = _make_codex_transcript(root, "2026/08/05", "2026-08-05T09-00-00", "1c9b6d8e-27ac-4f1e-9f2c-8a2b1e6d4c11")
    newer = _make_codex_transcript(root, "2026/08/05", "2026-08-05T11-00-00", "2d0c7e9f-38bd-5a2f-a03d-9b3c2f7e5d22")
    now = time.time()
    os.utime(older, (now - 3600, now - 3600))
    os.utime(newer, (now - 60, now - 60))

    found = find_codex_transcripts(None, root)

    assert [t.path for t in found] == [newer, older]


# ---------------------------------------------------------------------------
# resolve_transcript(..., agent="codex")
# ---------------------------------------------------------------------------

def test_resolve_transcript_heuristic_dispatches_to_codex_via_the_agent_param(tmp_path) -> None:
    """resolve_transcript's `agent` kwarg previously only affected the pinned-
    binding lookup; the heuristic fallback always used Claude Code's finder
    regardless. This is the bug the registry fixes."""
    root = tmp_path / "sessions"
    uuid = "1c9b6d8e-27ac-4f1e-9f2c-8a2b1e6d4c11"
    path = _make_codex_transcript(root, "2026/08/05", "2026-08-05T10-00-00", uuid)
    state_dir = tmp_path / "state"
    cwd = tmp_path / "repo"
    cwd.mkdir()

    result = resolve_transcript(cwd, state_dir, agent="codex", projects_root=root)

    assert result is not None
    assert result.source == "heuristic"
    assert result.path == path
    assert result.agent_session_id == uuid


def test_a_stale_codex_transcript_is_not_treated_as_live(tmp_path) -> None:
    root = tmp_path / "sessions"
    uuid = "1c9b6d8e-27ac-4f1e-9f2c-8a2b1e6d4c11"
    path = _make_codex_transcript(root, "2026/08/05", "2026-08-05T10-00-00", uuid)
    stale = time.time() - 3600  # older than LIVE_WINDOW_SECONDS (30 min)
    os.utime(path, (stale, stale))
    state_dir = tmp_path / "state"
    cwd = tmp_path / "repo"
    cwd.mkdir()

    result = resolve_transcript(cwd, state_dir, agent="codex", projects_root=root)

    assert result is None


def test_a_pinned_codex_binding_wins_over_the_heuristic(tmp_path) -> None:
    root = tmp_path / "sessions"
    uuid = "1c9b6d8e-27ac-4f1e-9f2c-8a2b1e6d4c11"
    pinned_path = _make_codex_transcript(root, "2026/08/05", "2026-08-05T09-00-00", uuid)
    state_dir = tmp_path / "state"
    cwd = tmp_path / "repo"
    cwd.mkdir()

    write_binding(
        binding_path_for_agent(state_dir, "codex"),
        SessionBinding(
            agent_session_id=uuid,
            shared_id="team-standup",
            contributor="aditya",
            agent="codex",
            transcript_path=str(pinned_path),
            pinned_at=datetime.now(timezone.utc),
        ),
    )
    # A newer, unrelated transcript appears after the pin -- must not steal it.
    _make_codex_transcript(root, "2026/08/05", "2026-08-05T11-00-00", "2d0c7e9f-38bd-5a2f-a03d-9b3c2f7e5d22")

    result = resolve_transcript(cwd, state_dir, agent="codex", projects_root=root)

    assert result.source == "pinned"
    assert result.path == pinned_path


# ---------------------------------------------------------------------------
# Binding storage keyed by product -- the plan's "two-product test"
# ---------------------------------------------------------------------------

def test_binding_storage_holds_independent_claude_code_and_codex_pins(tmp_path) -> None:
    """Plan D.2: 'one laptop holds several bindings -- one per Agent Session;
    Claude Code and Codex can sit in different Shared Sessions.' Pinning one
    product must leave the other product's binding (and resolution) alone."""
    state_dir = tmp_path / "state"
    cwd = tmp_path / "repo"
    cwd.mkdir()

    claude_root = tmp_path / "claude-projects"
    codex_root = tmp_path / "codex-sessions"
    claude_path = _make_claude_transcript(claude_root, cwd, "sess-1")
    codex_uuid = "1c9b6d8e-27ac-4f1e-9f2c-8a2b1e6d4c11"
    codex_path = _make_codex_transcript(codex_root, "2026/08/05", "2026-08-05T10-00-00", codex_uuid)

    write_binding(
        binding_path_for_agent(state_dir, "claude-code"),
        SessionBinding(
            agent_session_id="sess-1",
            shared_id="team-standup",
            contributor="akhil",
            agent="claude-code",
            transcript_path=str(claude_path),
            pinned_at=datetime.now(timezone.utc),
        ),
    )
    write_binding(
        binding_path_for_agent(state_dir, "codex"),
        SessionBinding(
            agent_session_id=codex_uuid,
            shared_id="team-standup",
            contributor="akhil",
            agent="codex",
            transcript_path=str(codex_path),
            pinned_at=datetime.now(timezone.utc),
        ),
    )

    claude_result = resolve_transcript(cwd, state_dir, agent="claude-code", projects_root=claude_root)
    codex_result = resolve_transcript(cwd, state_dir, agent="codex", projects_root=codex_root)

    assert claude_result.source == codex_result.source == "pinned"
    assert claude_result.path == claude_path
    assert codex_result.path == codex_path
    assert claude_result.local_binding.agent == "claude-code"
    assert codex_result.local_binding.agent == "codex"

    # Both files exist independently, keyed by product -- no collision.
    assert read_binding(binding_path_for_agent(state_dir, "claude-code")).agent == "claude-code"
    assert read_binding(binding_path_for_agent(state_dir, "codex")).agent == "codex"
