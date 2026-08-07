"""Binding the session we started FROM, not the one written to most recently.

docs/superpowers/specs/2026-08-06-session-lifecycle-design.md, "Requirement:
bind the session we started from" — Testing items 6 and 7.

The defect these pin: `agent_session_id` came from `find_live_transcript`, which
answers "the most recently modified transcript under project_slug(cwd), within
30 minutes". Two things break it, both observed rather than theorised:

  * The resolver's cwd is not the conversation's cwd. Verified 2026-08-06 — a
    Claude Code session started in /Users/siddharthsingh writes to
    ~/.claude/projects/-Users-siddharthsingh/<session-id>.jsonl, while an
    orchestrator serving ~/Dev/synapse looks under
    -Users-siddharthsingh-Dev-synapse and finds NOTHING. Not the wrong file —
    no file.
  * Two windows of the same agent product are both live, and "most recently
    modified" flips between the look and the bind.

Fixture trees only; no real ~/.claude or ~/.codex is touched, and nothing here
opens a socket.
"""

from __future__ import annotations

import os
import time

from synapse_worker.discovery import (
    AMBIGUITY_WINDOW_SECONDS,
    binding_path_for_agent,
    find_live_transcript,
    find_live_transcript_candidates,
    find_transcript_by_session_id,
    join_session,
    project_slug,
)

CODEX_UUID = "1c9b6d8e-27ac-4f1e-9f2c-8a2b1e6d4c11"
CODEX_UUID_B = "2d0c7e9f-38bd-5a2f-a03d-9b3c2f7e5d22"


def _make_claude_transcript(root, cwd, name: str, content: str = "{}\n"):
    slug_dir = root / project_slug(cwd)
    slug_dir.mkdir(parents=True, exist_ok=True)
    path = slug_dir / f"{name}.jsonl"
    path.write_text(content, encoding="utf-8")
    return path


def _make_codex_transcript(root, day: str, ts: str, uuid: str, content: str = ""):
    day_dir = root / day
    day_dir.mkdir(parents=True, exist_ok=True)
    path = day_dir / f"rollout-{ts}-{uuid}.jsonl"
    path.write_text(content, encoding="utf-8")
    return path


def _set_age(path, seconds_ago: float) -> None:
    """mtime control, since every assertion here is about relative recency and
    a fixture written by the test is otherwise all 'now' to the same second."""
    when = time.time() - seconds_ago
    os.utime(path, (when, when))


# ---------------------------------------------------------------------------
# find_transcript_by_session_id — spec: "exact filename match across project
# slugs". "Across" is the whole feature.
# ---------------------------------------------------------------------------

def test_finds_a_transcript_in_a_project_slug_that_is_not_cwds_slug(tmp_path) -> None:
    """The verified-2026-08-06 case. The conversation lives under the slug for
    the home directory; the resolver's cwd is a repo inside it. Slug-scoped
    detection returns None here — this must not."""
    root = tmp_path / "projects"
    home_like = tmp_path / "home"
    home_like.mkdir()
    repo = home_like / "Dev" / "synapse"
    repo.mkdir(parents=True)

    wanted = _make_claude_transcript(root, home_like, "360475a1-ba53-4734-ba8e-c3703d1bffcd")
    _make_claude_transcript(root, repo, "a-different-conversation")

    assert project_slug(home_like) != project_slug(repo)  # the premise, made explicit

    found = find_transcript_by_session_id(
        "360475a1-ba53-4734-ba8e-c3703d1bffcd", "claude-code", projects_root=root
    )

    assert found is not None
    assert found.path == wanted
    assert found.agent == "claude-code"


def test_an_unknown_session_id_is_not_found_rather_than_approximated(tmp_path) -> None:
    """EXACT match. A near-miss id must not resolve to the only transcript
    lying around — that is precisely the silent mis-bind being removed."""
    root = tmp_path / "projects"
    cwd = tmp_path / "repo"
    cwd.mkdir()
    _make_claude_transcript(root, cwd, "sess-1")

    assert find_transcript_by_session_id("sess-2", "claude-code", projects_root=root) is None


def test_an_idle_transcript_is_still_found_by_its_session_id(tmp_path) -> None:
    """An explicit id says which conversation this IS. A conversation that has
    been quiet for an hour is still that conversation, so the 30-minute live
    window that gates detection must not gate this lookup."""
    root = tmp_path / "projects"
    cwd = tmp_path / "repo"
    cwd.mkdir()
    path = _make_claude_transcript(root, cwd, "sess-idle")
    _set_age(path, 3600)  # well past LIVE_WINDOW_SECONDS

    found = find_transcript_by_session_id("sess-idle", "claude-code", projects_root=root)

    assert found is not None
    assert found.path == path


# ---------------------------------------------------------------------------
# The Codex rollout-filename case. Codex embeds its uuid inside
# rollout-<timestamp>-<uuid>.jsonl, so path.stem is NOT the session id and a
# stem comparison would return None for every Codex caller, forever, silently.
# ---------------------------------------------------------------------------

def test_codex_resolves_by_the_uuid_embedded_in_its_rollout_filename(tmp_path) -> None:
    root = tmp_path / "sessions"
    path = _make_codex_transcript(root, "2026/08/05", "2026-08-05T10-00-00", CODEX_UUID)
    _make_codex_transcript(root, "2026/08/05", "2026-08-05T11-00-00", CODEX_UUID_B)

    found = find_transcript_by_session_id(CODEX_UUID, "codex", projects_root=root)

    assert found is not None
    assert found.path == path
    assert found.agent == "codex"
    assert found.session_id == CODEX_UUID


def test_codex_lookup_does_not_match_on_the_file_stem(tmp_path) -> None:
    """Regression pin for the wrong implementation: matching `path.stem` would
    make the full `rollout-...` stem resolve and the real session id fail. Both
    halves are asserted so the pin cannot be satisfied by accident."""
    root = tmp_path / "sessions"
    path = _make_codex_transcript(root, "2026/08/05", "2026-08-05T10-00-00", CODEX_UUID)

    assert find_transcript_by_session_id(path.stem, "codex", projects_root=root) is None
    assert find_transcript_by_session_id(CODEX_UUID, "codex", projects_root=root) is not None


def test_codex_lookup_ignores_the_session_meta_cwd_filter(tmp_path) -> None:
    """`find_codex_transcripts` normally excludes any rollout whose recorded
    `session_meta.cwd` is not the directory being queried — correct for
    detection, wrong here for the same reason the Claude Code slug scoping is
    wrong: the caller named the session, so the directory it was started in is
    not ours to second-guess."""
    root = tmp_path / "sessions"
    other_repo = tmp_path / "somewhere-else"
    other_repo.mkdir()
    path = _make_codex_transcript(
        root, "2026/08/05", "2026-08-05T10-00-00", CODEX_UUID,
        content='{"type": "session_meta", "payload": {"cwd": "%s"}}\n' % other_repo,
    )

    found = find_transcript_by_session_id(CODEX_UUID, "codex", projects_root=root)

    assert found is not None
    assert found.path == path


# ---------------------------------------------------------------------------
# Spec Testing item 6: join_session(shared_id, "http://127.0.0.1:8899", agent_session_id=X) binds X
# even when a more recently modified transcript exists.
# ---------------------------------------------------------------------------

def test_join_with_an_explicit_session_id_beats_a_newer_transcript(tmp_path) -> None:
    """The two-windows case. Detection would bind `sess-newer`; the caller
    told us it is `sess-explicit`, and the caller is the one that knows."""
    root = tmp_path / "projects"
    cwd = tmp_path / "repo"
    cwd.mkdir()
    state_dir = tmp_path / "state"

    wanted = _make_claude_transcript(root, cwd, "sess-explicit")
    newer = _make_claude_transcript(root, cwd, "sess-newer")
    _set_age(wanted, 120)
    _set_age(newer, 1)

    # The premise: without the argument, detection binds the other one.
    assert find_live_transcript(cwd, root).path == newer

    bindings = join_session(
        "team-standup", "akhil", cwd, state_dir, "http://127.0.0.1:8899",
        projects_root=root, agent_session_id="sess-explicit",
    )

    assert len(bindings) == 1
    assert bindings[0].agent_session_id == "sess-explicit"
    assert bindings[0].transcript_path == str(wanted)
    assert bindings[0].shared_id == "team-standup"
    assert bindings[0].contributor == "akhil"
    # And it is on disk in the ordinary per-product location, same format as a
    # detected bind — the spec's "one code path" for binding writes.
    assert binding_path_for_agent(state_dir, "claude-code").is_file()


def test_join_with_an_explicit_session_id_binds_across_project_slugs(tmp_path) -> None:
    """Spec item 6 combined with the real failure: the named session is not
    under cwd's slug at all, so detection has nothing to be beaten by — it
    simply finds nothing, and the join would silently bind zero sessions."""
    root = tmp_path / "projects"
    home_like = tmp_path / "home"
    home_like.mkdir()
    repo = home_like / "Dev" / "synapse"
    repo.mkdir(parents=True)
    state_dir = tmp_path / "state"

    wanted = _make_claude_transcript(root, home_like, "sess-in-the-home-slug")

    assert find_live_transcript(repo, root) is None  # the premise

    bindings = join_session(
        "team-standup", "akhil", repo, state_dir, "http://127.0.0.1:8899",
        projects_root=root, agent_session_id="sess-in-the-home-slug",
    )

    assert [b.transcript_path for b in bindings] == [str(wanted)]


def test_join_with_an_explicit_codex_session_id_binds_codex(tmp_path) -> None:
    """The caller supplies an id, not a product name. Whichever registered
    agent's own extraction claims the id owns the bind — and for Codex that
    extraction is the embedded uuid, not the stem."""
    root = tmp_path / "sessions"
    state_dir = tmp_path / "state"
    cwd = tmp_path / "repo"
    cwd.mkdir()
    wanted = _make_codex_transcript(root, "2026/08/05", "2026-08-05T10-00-00", CODEX_UUID)

    bindings = join_session(
        "team-standup", "akhil", cwd, state_dir, "http://127.0.0.1:8899",
        projects_root=root, agent_session_id=CODEX_UUID,
    )

    assert len(bindings) == 1
    assert bindings[0].agent == "codex"
    assert bindings[0].agent_session_id == CODEX_UUID
    assert bindings[0].transcript_path == str(wanted)
    assert binding_path_for_agent(state_dir, "codex").is_file()


def test_join_with_an_unresolvable_session_id_binds_nothing_and_does_not_guess(tmp_path) -> None:
    """Falling back to detection here would be the worst of both worlds: the
    caller asked for a specific conversation, we could not find it, and we
    would hand back a different one under the name it asked for."""
    root = tmp_path / "projects"
    cwd = tmp_path / "repo"
    cwd.mkdir()
    state_dir = tmp_path / "state"
    _make_claude_transcript(root, cwd, "sess-live-right-now")

    bindings = join_session(
        "team-standup", "akhil", cwd, state_dir, "http://127.0.0.1:8899",
        projects_root=root, agent_session_id="sess-that-does-not-exist",
    )

    assert bindings == []
    assert not binding_path_for_agent(state_dir, "claude-code").exists()


def test_join_without_an_explicit_session_id_is_unchanged(tmp_path) -> None:
    """The argument is opt-in. Every existing caller — `synapse-worker join`
    included — must keep getting detection, unaltered."""
    root = tmp_path / "projects"
    cwd = tmp_path / "repo"
    cwd.mkdir()
    state_dir = tmp_path / "state"
    older = _make_claude_transcript(root, cwd, "sess-older")
    newer = _make_claude_transcript(root, cwd, "sess-newer")
    _set_age(older, 600)
    _set_age(newer, 1)

    bindings = join_session("team-standup", "akhil", cwd, state_dir, "http://127.0.0.1:8899", projects_root=root)

    assert [b.transcript_path for b in bindings] == [str(newer)]


# ---------------------------------------------------------------------------
# Spec Testing item 7: two transcripts within AMBIGUITY_WINDOW_SECONDS are
# reported ambiguous rather than silently resolved.
# ---------------------------------------------------------------------------

def test_two_live_transcripts_written_within_five_seconds_are_ambiguous(tmp_path) -> None:
    root = tmp_path / "projects"
    cwd = tmp_path / "repo"
    cwd.mkdir()
    a = _make_claude_transcript(root, cwd, "window-one")
    b = _make_claude_transcript(root, cwd, "window-two")
    _set_age(a, 4)
    _set_age(b, 2)  # 2s apart, inside AMBIGUITY_WINDOW_SECONDS

    result = find_live_transcript_candidates(cwd, root)

    assert result.ambiguous is True
    # The caller has to be able to LIST the candidates in its refusal, per the
    # spec's error table ("Refuse, list candidates, tell the caller to pass
    # agent_session_id"), so both must come back, not just the count.
    assert {t.path for t in result.candidates} == {a, b}


def test_a_single_live_transcript_is_never_ambiguous(tmp_path) -> None:
    root = tmp_path / "projects"
    cwd = tmp_path / "repo"
    cwd.mkdir()
    only = _make_claude_transcript(root, cwd, "sess-1")

    result = find_live_transcript_candidates(cwd, root)

    assert result.ambiguous is False
    assert result.chosen.path == only


def test_live_transcripts_far_apart_are_not_ambiguous(tmp_path) -> None:
    """Two windows both open but only one being typed into is the ordinary
    case, and refusing there would make the feature unusable. Ten minutes
    apart is a clear winner, not a coin toss."""
    root = tmp_path / "projects"
    cwd = tmp_path / "repo"
    cwd.mkdir()
    stale_ish = _make_claude_transcript(root, cwd, "idle-window")
    active = _make_claude_transcript(root, cwd, "active-window")
    _set_age(stale_ish, 600)
    _set_age(active, 1)

    result = find_live_transcript_candidates(cwd, root)

    assert result.ambiguous is False
    assert result.chosen.path == active


def test_a_transcript_outside_the_live_window_cannot_create_ambiguity(tmp_path) -> None:
    """Ambiguity is only ever between transcripts that are BOTH live. A
    finished conversation from last week sitting a second away from another
    finished conversation is not a decision anyone has to make."""
    root = tmp_path / "projects"
    cwd = tmp_path / "repo"
    cwd.mkdir()
    live = _make_claude_transcript(root, cwd, "live")
    dead_a = _make_claude_transcript(root, cwd, "dead-a")
    dead_b = _make_claude_transcript(root, cwd, "dead-b")
    _set_age(live, 5)
    _set_age(dead_a, 7200)
    _set_age(dead_b, 7201)  # 1s apart, but neither is live

    result = find_live_transcript_candidates(cwd, root)

    assert result.ambiguous is False
    assert [t.path for t in result.candidates] == [live]


def test_ambiguity_is_detected_between_any_pair_not_just_the_top_two(tmp_path) -> None:
    """The spec's rule is 'two or more transcripts inside the live window whose
    modified_at values are within AMBIGUITY_WINDOW_SECONDS of each other' — any
    pair. A near-simultaneous pair anywhere in the live set is evidence that
    more than one window is actively producing output, and any of them can be
    newest by the time the binding is written."""
    root = tmp_path / "projects"
    cwd = tmp_path / "repo"
    cwd.mkdir()
    clear_winner = _make_claude_transcript(root, cwd, "clear-winner")
    tied_a = _make_claude_transcript(root, cwd, "tied-a")
    tied_b = _make_claude_transcript(root, cwd, "tied-b")
    _set_age(clear_winner, 1)
    _set_age(tied_a, 300)
    _set_age(tied_b, 301)

    result = find_live_transcript_candidates(cwd, root)

    assert result.ambiguous is True
    assert result.chosen.path == clear_winner  # still ordered, still newest-first


def test_ambiguity_detection_works_for_codex_too(tmp_path) -> None:
    """Dispatch is through AGENT_REGISTRY, so a Codex-only machine gets the
    same protection — with cwd scoping coming from each rollout's own
    session_meta rather than a slug directory."""
    root = tmp_path / "sessions"
    cwd = tmp_path / "repo"
    cwd.mkdir()
    meta = '{"type": "session_meta", "payload": {"cwd": "%s"}}\n' % cwd
    a = _make_codex_transcript(root, "2026/08/05", "2026-08-05T10-00-00", CODEX_UUID, content=meta)
    b = _make_codex_transcript(root, "2026/08/05", "2026-08-05T10-00-03", CODEX_UUID_B, content=meta)
    _set_age(a, 4)
    _set_age(b, 2)

    result = find_live_transcript_candidates(cwd, root, agent="codex")

    assert result.ambiguous is True
    assert {t.session_id for t in result.candidates} == {CODEX_UUID, CODEX_UUID_B}


def test_find_live_transcript_still_returns_one_transcript_when_ambiguous(tmp_path) -> None:
    """Deliberate: the ambiguity signal is additive. `join_session` and
    `resolve_transcript` both call `find_live_transcript` and both want 'the
    one to follow' — refusing inside it would break `synapse-worker run` on a
    machine with two windows open, which is a working configuration today.
    The refusal belongs to the caller that can act on it."""
    root = tmp_path / "projects"
    cwd = tmp_path / "repo"
    cwd.mkdir()
    a = _make_claude_transcript(root, cwd, "window-one")
    b = _make_claude_transcript(root, cwd, "window-two")
    _set_age(a, 4)
    _set_age(b, 2)

    assert find_live_transcript_candidates(cwd, root).ambiguous is True

    transcript = find_live_transcript(cwd, root)

    assert transcript is not None
    assert transcript.path == b


def test_the_ambiguity_window_is_five_seconds(tmp_path) -> None:
    """Pinned because the spec names the number, and a caller's refusal message
    ("two sessions were touched within 5s") quotes it."""
    assert AMBIGUITY_WINDOW_SECONDS == 5
