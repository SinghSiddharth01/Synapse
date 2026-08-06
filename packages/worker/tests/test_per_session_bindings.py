"""Two windows of one agent on one machine are two participants — W2, pass 1.

The defect these pin, in one sentence: `bindings/<agent>.json` is one file per
Agent PRODUCT, so the second Claude Code window to join OVERWROTE the first's
binding and silently became the same participant. One agent invocation is one
Agent Session; the binding layout has to say so.

    <state_dir>/bindings/<agent>/<agent_session_id>.json   # source of truth
    <state_dir>/bindings/<agent>.json                      # legacy mirror

Every bind writes both. The mirror is the migration: an un-upgraded reader (the
installed `freshness_pointer.py`, the orchestrator's single-binding resolvers)
keeps seeing exactly what it saw before — the most recently joined session for
that product — while `resolve_agent_binding` reads the union and can answer for
one named conversation. Nothing here needs a migration step to run, in either
direction.

Fixture trees only; no real ~/.claude or ~/.codex is touched, no socket opened.
"""

from __future__ import annotations

import argparse
import json
import os
import time
from datetime import datetime, timedelta, timezone

from synapse_contracts import (
    Attribution,
    Finding,
    FindingType,
    LocalBinding,
    SessionBinding,
    read_binding,
    write_binding,
)
from synapse_distiller import Distiller, load_pack_by_name
from synapse_providers import FakeProvider

from synapse_worker import cli
from synapse_worker.discovery import (
    binding_dir_for_agent,
    binding_path_for_agent,
    binding_path_for_session,
    join_session,
    project_slug,
    prune_dead_bindings,
    read_bindings_for_agent,
    resolve_agent_binding,
    resolve_transcript,
)
from synapse_worker.loop import WorkerLoop
from synapse_worker.producer import FileSink, Producer

SESSION_A = "3f2b1c04-aaaa-4a1e-8f10-000000000001"
SESSION_B = "8d7e6f15-bbbb-4b2f-9021-000000000002"
CODEX_UUID = "1c9b6d8e-27ac-4f1e-9f2c-8a2b1e6d4c11"


def _make_claude_transcript(root, cwd, name: str, content: str = "{}\n"):
    slug_dir = root / project_slug(cwd)
    slug_dir.mkdir(parents=True, exist_ok=True)
    path = slug_dir / f"{name}.jsonl"
    path.write_text(content, encoding="utf-8")
    return path


def _set_age(path, seconds_ago: float) -> None:
    when = time.time() - seconds_ago
    os.utime(path, (when, when))


def _binding(session_id: str, shared_id: str, transcript, *, pinned_at, agent="claude-code",
             contributor="akhil", scope="session") -> SessionBinding:
    return SessionBinding(
        agent_session_id=session_id,
        shared_id=shared_id,
        contributor=contributor,
        agent=agent,
        transcript_path=str(transcript),
        pinned_at=pinned_at,
        scope=scope,
    )


# ---------------------------------------------------------------------------
# The layout: two joins, two files, nothing clobbered
# ---------------------------------------------------------------------------

def test_two_joins_with_distinct_session_ids_produce_two_binding_files(tmp_path) -> None:
    """THE W2 defect, pinned. Two live conversations in one project slug, each
    joining under its own id: before this change the second join overwrote the
    first's binding file and the two windows became one participant."""
    root = tmp_path / "projects"
    cwd = tmp_path / "repo"
    cwd.mkdir()
    state_dir = tmp_path / "state"

    transcript_a = _make_claude_transcript(root, cwd, SESSION_A)
    transcript_b = _make_claude_transcript(root, cwd, SESSION_B)

    join_session("sh-1", "akhil", cwd, state_dir,
                 projects_root=root, agent_session_id=SESSION_A)
    join_session("sh-1", "akhil", cwd, state_dir,
                 projects_root=root, agent_session_id=SESSION_B)

    path_a = binding_path_for_session(state_dir, "claude-code", SESSION_A)
    path_b = binding_path_for_session(state_dir, "claude-code", SESSION_B)
    assert path_a.is_file()
    assert path_b.is_file()
    assert read_binding(path_a).transcript_path == str(transcript_a)
    assert read_binding(path_b).transcript_path == str(transcript_b)

    # The legacy mirror still exists and still means "most recently joined",
    # which is what every un-upgraded reader has always taken it to mean.
    legacy = read_binding(binding_path_for_agent(state_dir, "claude-code"))
    assert legacy.agent_session_id == SESSION_B
    assert legacy.transcript_path == str(transcript_b)


def test_a_detected_join_writes_both_layouts_too(tmp_path) -> None:
    """Dual-write is a property of the single `_bind` writer, not of the
    explicit-id path — a detection join must land in the per-session layout as
    well, or a machine that never passes an id keeps only the old file."""
    root = tmp_path / "projects"
    cwd = tmp_path / "repo"
    cwd.mkdir()
    state_dir = tmp_path / "state"
    transcript = _make_claude_transcript(root, cwd, SESSION_A)

    bindings = join_session("sh-1", "akhil", cwd, state_dir, projects_root=root)

    assert [b.agent_session_id for b in bindings] == [SESSION_A]
    assert binding_path_for_session(state_dir, "claude-code", SESSION_A).is_file()
    assert binding_path_for_agent(state_dir, "claude-code").is_file()
    assert read_binding(
        binding_path_for_session(state_dir, "claude-code", SESSION_A)
    ).transcript_path == str(transcript)


def test_a_session_id_that_is_not_filename_safe_is_still_matched_by_contents(tmp_path) -> None:
    """The filename is a convenience; `agent_session_id` read back out of the
    file is the identity. An id carrying a path separator must not escape the
    bindings directory, and must still resolve exactly."""
    state_dir = tmp_path / "state"
    hostile = "../../etc/pass wd:1"
    path = binding_path_for_session(state_dir, "claude-code", hostile)

    assert path.parent == binding_dir_for_agent(state_dir, "claude-code")
    assert path.name == ".._.._etc_pass_wd_1.json"

    write_binding(path, _binding(hostile, "sh-1", tmp_path / "t.jsonl",
                                 pinned_at=datetime(2026, 8, 6, tzinfo=timezone.utc)))

    assert resolve_agent_binding(state_dir, "claude-code", hostile).shared_id == "sh-1"


# ---------------------------------------------------------------------------
# Resolution: per-session first, legacy always still readable
# ---------------------------------------------------------------------------

def test_resolution_prefers_per_session_file_and_falls_back_to_legacy(tmp_path) -> None:
    state_dir = tmp_path / "state"
    transcript = tmp_path / "t.jsonl"
    transcript.touch()
    older = datetime(2026, 8, 6, 10, 0, tzinfo=timezone.utc)
    newer = older + timedelta(minutes=5)

    # 1. Legacy-only state — a tree written by any version before this one.
    write_binding(binding_path_for_agent(state_dir, "claude-code"),
                  _binding(SESSION_A, "sh-legacy", transcript, pinned_at=older))

    assert resolve_agent_binding(state_dir, "claude-code").shared_id == "sh-legacy"
    assert resolve_agent_binding(state_dir, "claude-code", SESSION_A).shared_id == "sh-legacy"

    # 2. A per-session file for the SAME id, pinned later, wins — dedup keeps
    #    the newer of the two copies rather than showing the session twice.
    write_binding(binding_path_for_session(state_dir, "claude-code", SESSION_A),
                  _binding(SESSION_A, "sh-fresh", transcript, pinned_at=newer))

    assert [b.agent_session_id for b in read_bindings_for_agent(state_dir, "claude-code")] \
        == [SESSION_A]
    assert resolve_agent_binding(state_dir, "claude-code", SESSION_A).shared_id == "sh-fresh"

    # 3. An id nobody has bound resolves to NOTHING. No pinned_at fallback:
    #    handing back the other window's binding is the whole defect.
    assert resolve_agent_binding(state_dir, "claude-code", SESSION_B) is None


def test_without_an_id_resolution_is_the_most_recently_pinned_binding(tmp_path) -> None:
    """The no-argument answer is byte-for-byte today's semantics, which is what
    lets every un-upgraded caller keep calling."""
    state_dir = tmp_path / "state"
    transcript = tmp_path / "t.jsonl"
    transcript.touch()
    base = datetime(2026, 8, 6, 10, 0, tzinfo=timezone.utc)

    write_binding(binding_path_for_session(state_dir, "claude-code", SESSION_A),
                  _binding(SESSION_A, "sh-a", transcript, pinned_at=base))
    write_binding(binding_path_for_session(state_dir, "claude-code", SESSION_B),
                  _binding(SESSION_B, "sh-b", transcript, pinned_at=base + timedelta(minutes=1)))

    assert resolve_agent_binding(state_dir, "claude-code").agent_session_id == SESSION_B
    assert [b.agent_session_id for b in read_bindings_for_agent(state_dir, "claude-code")] \
        == [SESSION_B, SESSION_A]


def test_a_naive_pinned_at_in_a_hand_written_binding_does_not_explode(tmp_path) -> None:
    """Hand-authored fixtures (and there are several in this suite) can carry a
    naive datetime; comparing one against a timezone-aware stamp raises
    TypeError in Python. Ordering must degrade to "read it as UTC", never to a
    crash inside a resolver every command calls."""
    state_dir = tmp_path / "state"
    transcript = tmp_path / "t.jsonl"
    transcript.touch()

    write_binding(binding_path_for_session(state_dir, "claude-code", SESSION_A),
                  _binding(SESSION_A, "sh-naive", transcript,
                           pinned_at=datetime(2026, 8, 6, 10, 0)))
    write_binding(binding_path_for_session(state_dir, "claude-code", SESSION_B),
                  _binding(SESSION_B, "sh-aware", transcript,
                           pinned_at=datetime(2026, 8, 6, 11, 0, tzinfo=timezone.utc)))

    assert resolve_agent_binding(state_dir, "claude-code").shared_id == "sh-aware"


def test_bindings_are_per_agent_product_as_well_as_per_session(tmp_path) -> None:
    """Generalisation is by construction: the directory is `bindings/<agent>/`,
    so a Codex conversation and a Claude Code conversation bound to different
    Shared Sessions stay apart — the same property the single-file layout had,
    kept while adding the session dimension."""
    state_dir = tmp_path / "state"
    transcript = tmp_path / "t.jsonl"
    transcript.touch()
    base = datetime(2026, 8, 6, 10, 0, tzinfo=timezone.utc)

    write_binding(binding_path_for_session(state_dir, "claude-code", SESSION_A),
                  _binding(SESSION_A, "sh-claude", transcript, pinned_at=base))
    write_binding(binding_path_for_session(state_dir, "codex", CODEX_UUID),
                  _binding(CODEX_UUID, "sh-codex", transcript, agent="codex",
                           pinned_at=base + timedelta(minutes=1)))

    assert resolve_agent_binding(state_dir, "codex", CODEX_UUID).shared_id == "sh-codex"
    assert resolve_agent_binding(state_dir, "claude-code", CODEX_UUID) is None
    assert resolve_agent_binding(state_dir, "claude-code").shared_id == "sh-claude"


# ---------------------------------------------------------------------------
# scope — the honest fix for serve_local's `as-<contributor>` sentinel (the
# consumers land in later passes; the contract field is here)
# ---------------------------------------------------------------------------

def test_scope_defaults_to_session_and_round_trips_when_machine(tmp_path) -> None:
    """Default `session` is the stricter reading, so a binding written before
    this field existed never silently widens into "any conversation here"."""
    state_dir = tmp_path / "state"
    transcript = tmp_path / "t.jsonl"
    transcript.touch()
    base = datetime(2026, 8, 6, 10, 0, tzinfo=timezone.utc)

    write_binding(binding_path_for_agent(state_dir, "claude-code"),
                  _binding(SESSION_A, "sh-1", transcript, pinned_at=base))
    assert read_binding(binding_path_for_agent(state_dir, "claude-code")).scope == "session"

    machine = binding_path_for_session(state_dir, "claude-code", "as-akhil")
    write_binding(machine, _binding("as-akhil", "sh-1", transcript,
                                    pinned_at=base, scope="machine"))
    assert read_binding(machine).scope == "machine"


def test_a_binding_file_written_before_scope_existed_still_loads(tmp_path) -> None:
    """Additive, in the literal sense: the field is absent from every file on
    every machine that has ever run this, and those files must keep loading."""
    path = binding_path_for_agent(tmp_path / "state", "claude-code")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        '{"agent_session_id": "%s", "shared_id": "sh-1", "contributor": "akhil",'
        ' "agent": "claude-code", "transcript_path": "/tmp/t.jsonl",'
        ' "pinned_at": "2026-08-06T10:00:00Z"}' % SESSION_A,
        encoding="utf-8",
    )

    loaded = read_binding(path)

    assert loaded is not None
    assert loaded.scope == "session"


# ---------------------------------------------------------------------------
# pruning — one file per conversation accumulates; dead ones go
# ---------------------------------------------------------------------------

def test_join_prunes_per_session_bindings_whose_transcript_is_gone(tmp_path) -> None:
    root = tmp_path / "projects"
    cwd = tmp_path / "repo"
    cwd.mkdir()
    state_dir = tmp_path / "state"
    live = _make_claude_transcript(root, cwd, SESSION_A)
    base = datetime(2026, 8, 6, 10, 0, tzinfo=timezone.utc)

    dead_path = binding_path_for_session(state_dir, "claude-code", SESSION_B)
    write_binding(dead_path, _binding(SESSION_B, "sh-old", tmp_path / "deleted.jsonl",
                                      pinned_at=base))
    # The legacy mirror is NEVER pruned — un-upgraded readers depend on it, and
    # it is overwritten on every bind anyway.
    legacy = binding_path_for_agent(state_dir, "claude-code")
    write_binding(legacy, _binding(SESSION_B, "sh-old", tmp_path / "deleted.jsonl",
                                   pinned_at=base))

    join_session("sh-1", "akhil", cwd, state_dir,
                 projects_root=root, agent_session_id=SESSION_A)

    assert not dead_path.exists()
    assert legacy.is_file()
    assert read_binding(legacy).transcript_path == str(live)
    assert [b.agent_session_id for b in read_bindings_for_agent(state_dir, "claude-code")] \
        == [SESSION_A]


def test_pruning_keeps_bindings_whose_transcript_still_exists(tmp_path) -> None:
    state_dir = tmp_path / "state"
    transcript = tmp_path / "t.jsonl"
    transcript.touch()
    keep = binding_path_for_session(state_dir, "claude-code", SESSION_A)
    write_binding(keep, _binding(SESSION_A, "sh-1", transcript,
                                 pinned_at=datetime(2026, 8, 6, tzinfo=timezone.utc)))

    assert prune_dead_bindings(state_dir, "claude-code") == []
    assert keep.is_file()


# ---------------------------------------------------------------------------
# resolve_transcript / `run --agent-session-id` — one process, one conversation
# ---------------------------------------------------------------------------

def test_run_follows_its_own_binding(tmp_path) -> None:
    """Two windows, one machine, two `run` processes. Each must follow the
    transcript it named; the un-named case stays the most recently pinned
    binding, which is what a single-window machine has always got."""
    root = tmp_path / "projects"
    cwd = tmp_path / "repo"
    cwd.mkdir()
    state_dir = tmp_path / "state"
    transcript_a = _make_claude_transcript(root, cwd, SESSION_A)
    transcript_b = _make_claude_transcript(root, cwd, SESSION_B)
    _set_age(transcript_a, 300)
    _set_age(transcript_b, 1)

    join_session("sh-a", "akhil", cwd, state_dir,
                 projects_root=root, agent_session_id=SESSION_A)
    join_session("sh-b", "akhil", cwd, state_dir,
                 projects_root=root, agent_session_id=SESSION_B)

    resolved_a = resolve_transcript(cwd, state_dir, agent_session_id=SESSION_A,
                                    projects_root=root)
    resolved_b = resolve_transcript(cwd, state_dir, agent_session_id=SESSION_B,
                                    projects_root=root)
    resolved_default = resolve_transcript(cwd, state_dir, projects_root=root)

    assert (resolved_a.path, resolved_a.source) == (transcript_a, "pinned")
    assert resolved_a.local_binding.shared_id == "sh-a"
    assert (resolved_b.path, resolved_b.source) == (transcript_b, "pinned")
    assert resolved_b.local_binding.shared_id == "sh-b"
    assert resolved_default.local_binding.shared_id == "sh-b"  # most recently joined


def test_an_unbound_session_id_refuses_rather_than_following_the_other_window(
    tmp_path,
) -> None:
    """The refusal that makes the argument worth passing. A live transcript is
    sitting right there and detection would happily return it — following it
    would mean this process distils window B's conversation into window A's
    Shared Session, silently."""
    root = tmp_path / "projects"
    cwd = tmp_path / "repo"
    cwd.mkdir()
    state_dir = tmp_path / "state"
    _make_claude_transcript(root, cwd, SESSION_B)
    join_session("sh-b", "akhil", cwd, state_dir,
                 projects_root=root, agent_session_id=SESSION_B)

    # The premise: without the argument there IS an answer.
    assert resolve_transcript(cwd, state_dir, projects_root=root) is not None

    assert resolve_transcript(cwd, state_dir, agent_session_id=SESSION_A,
                              projects_root=root) is None


def test_cli_run_resolution_threads_the_agent_session_id(tmp_path, monkeypatch) -> None:
    """`run --agent-session-id` reaches `resolve_transcript`. Which registered
    product owns the id is still not the caller's problem — every candidate is
    probed with it."""
    root = tmp_path / "projects"
    cwd = tmp_path / "repo"
    cwd.mkdir()
    state_dir = tmp_path / "state"
    transcript_a = _make_claude_transcript(root, cwd, SESSION_A)
    _make_claude_transcript(root, cwd, SESSION_B)

    join_session("sh-a", "akhil", cwd, state_dir,
                 projects_root=root, agent_session_id=SESSION_A)
    join_session("sh-b", "akhil", cwd, state_dir,
                 projects_root=root, agent_session_id=SESSION_B)

    monkeypatch.chdir(cwd)
    args = argparse.Namespace(agent=None, agent_session_id=SESSION_A)

    agent, resolved = cli._resolve_agent_and_transcript(args, state_dir)

    assert agent == "claude-code"
    assert resolved.path == transcript_a
    assert resolved.local_binding.shared_id == "sh-a"


def test_cli_parses_agent_session_id_on_join_run_and_replay() -> None:
    parser = cli.build_parser()

    assert parser.parse_args(
        ["join", "sh-1", "--agent-session-id", SESSION_A]
    ).agent_session_id == SESSION_A
    assert parser.parse_args(
        ["run", "--agent-session-id", SESSION_A]
    ).agent_session_id == SESSION_A
    assert parser.parse_args(
        ["replay", "--agent-session-id", SESSION_A]
    ).agent_session_id == SESSION_A
    # Omitted everywhere, so every existing invocation is unchanged.
    assert parser.parse_args(["run"]).agent_session_id is None


# ---------------------------------------------------------------------------
# the running loop: window B's join must not move window A's producer
# ---------------------------------------------------------------------------

async def test_a_second_windows_join_does_not_retarget_this_loop(tmp_path) -> None:
    """Before per-session bindings, `_sync_binding_from_disk` read the single
    `bindings/claude-code.json`, so the OTHER window joining a different Shared
    Session moved THIS live worker's producer onto it — window A's findings
    would have been held (or worse, delivered) under window B's session. This
    loop follows session A, so only session A's binding may move it."""
    state_dir = tmp_path / "state"
    transcript = tmp_path / "t.jsonl"
    transcript.touch()
    binding_a = LocalBinding(agent_session_id=SESSION_A, shared_id="sh-a",
                             contributor="akhil", agent="claude-code")
    producer = Producer(state_dir / "wal", FileSink(tmp_path / "upstream.jsonl"))
    loop = WorkerLoop(
        transcript=transcript,
        distiller=Distiller(FakeProvider(scripts=[]), binding_a,
                            load_pack_by_name("v4-condense"), ["text"], "labelled"),
        producer=producer,
        binding=binding_a,
        state_dir=state_dir,
        budget_tokens=5000,
    )
    producer.record([Finding(
        id="f-A", type=FindingType.LEARNING, text="learned in window A",
        attributions=[Attribution(contributor="akhil", agent_session=SESSION_A,
                                  agent="claude-code")],
        ts=datetime(2026, 8, 6, tzinfo=timezone.utc),
    )])
    assert producer.pending_count() == (1, 0)

    # Window B joins a different Shared Session. Dual-write means this also
    # rewrites the legacy mirror — which is exactly what used to hijack us.
    base = datetime(2026, 8, 6, 12, 0, tzinfo=timezone.utc)
    for path in (binding_path_for_session(state_dir, "claude-code", SESSION_B),
                 binding_path_for_agent(state_dir, "claude-code")):
        write_binding(path, _binding(SESSION_B, "sh-b", transcript, pinned_at=base))

    result = await loop.tick()

    assert producer.shared_id == "sh-a"
    assert result.sent == 1  # still deliverable under ITS OWN session
    assert producer.pending_count() == (0, 0)

    # And this window's own re-join still moves it — the pre-W2 guarantee
    # (STATE.md trap #8) is kept, not traded away.
    write_binding(binding_path_for_session(state_dir, "claude-code", SESSION_A),
                  _binding(SESSION_A, "sh-a2", transcript,
                           pinned_at=base + timedelta(minutes=1)))
    await loop.tick()

    assert producer.shared_id == "sh-a2"


# ---------------------------------------------------------------------------
# generalisation: nothing above is Claude-Code-specific
# ---------------------------------------------------------------------------

CODEX_UUID_B = "5e4d3c2b-1a09-4f8e-b7d6-c5b4a3928170"


def _make_codex_rollout(root, day: str, ts: str, uuid: str, cwd):
    """A Codex rollout, named the way Codex names them —
    `rollout-<timestamp>-<uuid>.jsonl`, with the id EMBEDDED in the filename
    rather than being the stem. Same shape `test_discovery_codex.py` builds;
    rebuilt here so this file keeps its "fixture trees only" promise without
    importing another test module."""
    day_dir = root / day
    day_dir.mkdir(parents=True, exist_ok=True)
    path = day_dir / f"rollout-{ts}-{uuid}.jsonl"
    path.write_text(json.dumps({
        "timestamp": "2026-08-06T09:00:00Z", "ordinal": 0, "type": "session_meta",
        "payload": {"id": uuid, "session_id": uuid, "cwd": str(cwd)},
    }) + "\n", encoding="utf-8")
    return path


def test_two_codex_conversations_bind_separately_through_the_join_path(
    tmp_path, monkeypatch
) -> None:
    """The W2 layout is per-AGENT, not per-Claude-Code: `bindings/<agent>/`,
    `AGENT_REGISTRY` dispatch, `find_transcript_by_session_id` matching on the
    id each product's own adapter extracts. This drives that end to end for
    Codex through the real `join_session` — two rollouts, two explicit ids,
    two files — and it is the case a stem comparison would silently fail,
    since a Codex rollout's stem is `rollout-<ts>-<uuid>`, never the id
    itself.

    Codex needs no pack, no hook and no new code for this: registering an
    agent is the whole of the work. That is the claim; this is the proof."""
    import synapse_worker.discovery as discovery

    cwd = tmp_path / "repo"
    cwd.mkdir()
    claude_root = tmp_path / "claude-projects"   # left empty: a Codex-only machine
    codex_root = tmp_path / "codex-sessions"
    monkeypatch.setattr(discovery, "CLAUDE_PROJECTS", claude_root)
    monkeypatch.setattr(discovery, "CODEX_SESSIONS", codex_root)
    state_dir = tmp_path / "state"

    path_a = _make_codex_rollout(codex_root, "2026/08/06", "2026-08-06T10-00-00",
                                 CODEX_UUID, cwd)
    path_b = _make_codex_rollout(codex_root, "2026/08/06", "2026-08-06T11-00-00",
                                 CODEX_UUID_B, cwd)

    [first] = join_session("sh-codex-a", "akhil", cwd, state_dir,
                           agent_session_id=CODEX_UUID)
    [second] = join_session("sh-codex-b", "akhil", cwd, state_dir,
                           agent_session_id=CODEX_UUID_B)

    assert first.agent == second.agent == "codex"
    assert first.transcript_path == str(path_a)
    assert second.transcript_path == str(path_b)

    # Two files, neither clobbered -- the same property proved for Claude Code
    # at the top of this file, reached here through a different adapter.
    on_disk = sorted(p.name for p in binding_dir_for_agent(state_dir, "codex").glob("*.json"))
    assert on_disk == sorted([f"{CODEX_UUID}.json", f"{CODEX_UUID_B}.json"])

    assert resolve_agent_binding(state_dir, "codex", CODEX_UUID).shared_id == "sh-codex-a"
    assert resolve_agent_binding(state_dir, "codex", CODEX_UUID_B).shared_id == "sh-codex-b"
    # The mirror names the most recent join, for readers that predate the layout.
    assert read_binding(binding_path_for_agent(state_dir, "codex")).shared_id == "sh-codex-b"
    # And nothing was invented for the product that has no live conversation.
    assert read_binding(binding_path_for_agent(state_dir, "claude-code")) is None
    assert not binding_dir_for_agent(state_dir, "claude-code").exists()
