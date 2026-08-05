"""synapse-worker CLI — every command, offline.

The only real network boundary in this module is check_canary's call to the
NPU. Every test either mocks that directly or avoids reaching it (a canary
failure returns before any distillation is attempted). Everything else —
config loading, transcript resolution, the write-ahead log, argument parsing —
runs for real against an isolated tmp_path cwd.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone

import pytest
from synapse_distiller.guards import CanaryResult

import synapse_worker.cli as cli
from synapse_worker.discovery import ResolvedTranscript
from synapse_worker.loop import WorkerLoop

PASSING_CANARY = CanaryResult(passed=True, answer="api.internal", input_tokens=49, detail="ok")
FAILING_CANARY = CanaryResult(passed=False, answer="", input_tokens=1, detail="prompt dropped")


@pytest.fixture(autouse=True)
def _isolated_cwd(tmp_path, monkeypatch):
    """Every default path in config.worker (state_dir, sink_file) is relative,
    resolved against the real OS cwd at file-I/O time. Isolating cwd isolates
    all of them at once, the same way a real checkout does."""
    monkeypatch.chdir(tmp_path)
    return tmp_path


def _write_transcript(path, text: str = "") -> None:
    path.write_text(text, encoding="utf-8")


# ---------------------------------------------------------------------------
# join
# ---------------------------------------------------------------------------

async def test_join_with_no_live_transcript_fails(monkeypatch, capsys) -> None:
    # join_session loops over every registered agent, so the substitute must
    # accept (and, for this test, ignore) the `agent` keyword regardless of
    # which agent is being probed.
    monkeypatch.setattr(
        "synapse_worker.discovery.find_live_transcript", lambda cwd, root=None, *, agent=None: None
    )

    exit_code = await cli.cmd_join(_ns(shared_id="team-standup", contributor="aditya"))

    assert exit_code == 1
    assert "nothing bound" in capsys.readouterr().err


async def test_join_binds_the_live_transcript(tmp_path, monkeypatch, capsys) -> None:
    from synapse_worker.discovery import DiscoveredTranscript

    transcript = tmp_path / "sess.jsonl"
    transcript.write_text("{}\n", encoding="utf-8")
    monkeypatch.setattr(
        "synapse_worker.discovery.find_live_transcript",
        lambda cwd, root=None, *, agent=None: (
            DiscoveredTranscript(
                path=transcript, agent="claude-code", session_id="as-1", modified_at=0.0, size=1
            )
            if agent == "claude-code"
            else None
        ),
    )

    exit_code = await cli.cmd_join(_ns(shared_id="team-standup", contributor="akhil"))

    out = capsys.readouterr().out
    assert exit_code == 0
    assert "claude-code" in out
    assert "team-standup" in out

    binding_file = tmp_path / ".synapse" / "bindings" / "claude-code.json"
    assert json.loads(binding_file.read_text(encoding="utf-8"))["contributor"] == "akhil"


# ---------------------------------------------------------------------------
# run
# ---------------------------------------------------------------------------

async def test_run_stops_when_the_canary_fails(tmp_path, monkeypatch, capsys) -> None:
    transcript = tmp_path / "sess.jsonl"
    _write_transcript(transcript)
    monkeypatch.setattr(cli, "check_canary", _async(FAILING_CANARY))
    ran = _spy(monkeypatch, WorkerLoop, "run")

    exit_code = await cli.cmd_run(
        _ns(transcript=str(transcript), interval=None, ticks=1, from_start=False)
    )

    assert exit_code == 1
    assert "CANARY FAILED" in capsys.readouterr().err
    assert ran.called is False


async def test_run_stops_the_debug_server_when_the_canary_fails(
    tmp_path, monkeypatch, capsys
) -> None:
    """A canary failure must not leak the dashboard's listening socket."""
    transcript = tmp_path / "sess.jsonl"
    _write_transcript(transcript)
    monkeypatch.setattr(cli, "check_canary", _async(FAILING_CANARY))
    _FakeDebugServer.instances.clear()
    monkeypatch.setattr(cli, "DebugServer", _FakeDebugServer)

    exit_code = await cli.cmd_run(
        _ns(
            transcript=str(transcript), interval=None, ticks=1, from_start=False,
            debug_port=8790,
        )
    )

    assert exit_code == 1
    [server] = _FakeDebugServer.instances
    assert server.started is True
    assert server.stopped is True


async def test_run_survives_a_debug_port_bind_failure(tmp_path, monkeypatch, capsys) -> None:
    """A second concurrent `synapse-worker run` on the same machine -- or a
    first one started while a stale worker still holds the port -- must not
    abort the core command just because the OPTIONAL dashboard couldn't
    bind. Verified directly: binding an already-held port raises
    `OSError: [Errno 48] Address already in use`; --debug-port defaults ON
    (DEFAULT_DEBUG_PORT), so this failure mode is reachable with zero flags.
    """
    transcript = tmp_path / "sess.jsonl"
    _write_transcript(transcript)
    monkeypatch.setattr(cli, "check_canary", _async(PASSING_CANARY))
    _FakeDebugServer.instances.clear()
    _FakeDebugServer.raise_on_start = OSError(48, "Address already in use")
    monkeypatch.setattr(cli, "DebugServer", _FakeDebugServer)

    try:
        exit_code = await cli.cmd_run(
            _ns(
                transcript=str(transcript), interval=0.01, ticks=1, from_start=False,
                debug_port=8790,
            )
        )
    finally:
        _FakeDebugServer.raise_on_start = None

    assert exit_code == 0                       # the canary + tick still ran
    [server] = _FakeDebugServer.instances
    assert server.started is True
    assert server.stopped is False               # never bound -- nothing to stop
    captured = capsys.readouterr()
    assert "Address already in use" in captured.out + captured.err
    assert "shutdown" in captured.out             # the run completed normally


async def test_run_with_from_start_does_not_attach_at_end(tmp_path, monkeypatch, capsys) -> None:
    transcript = tmp_path / "sess.jsonl"
    _write_transcript(transcript)
    monkeypatch.setattr(cli, "check_canary", _async(PASSING_CANARY))
    attached = _spy(monkeypatch, WorkerLoop, "attach_at_end")

    exit_code = await cli.cmd_run(
        _ns(transcript=str(transcript), interval=0.01, ticks=1, from_start=True)
    )

    assert exit_code == 0
    assert attached.called is False
    assert "beginning of the transcript" in capsys.readouterr().out


async def test_run_without_from_start_attaches_at_end(tmp_path, monkeypatch, capsys) -> None:
    transcript = tmp_path / "sess.jsonl"
    _write_transcript(transcript)
    monkeypatch.setattr(cli, "check_canary", _async(PASSING_CANARY))
    attached = _spy(monkeypatch, WorkerLoop, "attach_at_end")

    exit_code = await cli.cmd_run(
        _ns(transcript=str(transcript), interval=0.01, ticks=1, from_start=False)
    )

    assert exit_code == 0
    assert attached.called is True


async def test_run_reports_heuristic_selection(tmp_path, monkeypatch, capsys) -> None:
    transcript = tmp_path / "sess.jsonl"
    _write_transcript(transcript)
    monkeypatch.setattr(cli, "check_canary", _async(PASSING_CANARY))
    monkeypatch.setattr(
        cli, "resolve_transcript",
        # _build tries every registered agent (claude-code, then codex) in
        # search of a pinned binding before settling for a heuristic -- the
        # substitute must accept `agent` regardless of which is being probed.
        lambda cwd, state_dir, *, agent=None: ResolvedTranscript(
            path=transcript, agent_session_id="as-1", source="heuristic"
        ),
    )

    exit_code = await cli.cmd_run(_ns(transcript=None, interval=0.01, ticks=1, from_start=False))

    assert exit_code == 0
    assert "HEURISTIC" in capsys.readouterr().out


async def test_run_reports_pinned_selection_and_uses_its_binding(tmp_path, monkeypatch, capsys) -> None:
    from synapse_contracts import LocalBinding

    transcript = tmp_path / "sess.jsonl"
    _write_transcript(transcript)
    monkeypatch.setattr(cli, "check_canary", _async(PASSING_CANARY))
    joined = LocalBinding(
        agent_session_id="as-joined", shared_id="team-standup",
        contributor="akhil", agent="claude-code",
    )
    monkeypatch.setattr(
        cli, "resolve_transcript",
        lambda cwd, state_dir, *, agent=None: ResolvedTranscript(
            path=transcript, agent_session_id="as-joined", source="pinned", local_binding=joined
        ),
    )

    exit_code = await cli.cmd_run(_ns(transcript=None, interval=0.01, ticks=1, from_start=False))

    out = capsys.readouterr().out
    assert exit_code == 0
    assert "selection        pinned" in out


# ---------------------------------------------------------------------------
# run — multi-agent dispatch (the demo-path goal: `run` must actually reach
# CodexSource, not just resolve a Codex transcript path and then silently
# hand it to ClaudeCodeSource)
# ---------------------------------------------------------------------------

def test_build_selects_codex_source_for_a_pinned_codex_binding(tmp_path) -> None:
    """A pinned bindings/codex.json must make `_build` construct a WorkerLoop
    that actually parses with CodexSource -- the concrete regression this
    covers: WorkerLoop used to hard-code ClaudeCodeSource and `_build` used
    to always resolve with agent="claude-code", so even a hand-authored
    codex binding was silently fed to the wrong adapter."""
    from synapse_contracts import SessionBinding, write_binding
    from synapse_worker.discovery import binding_path_for_agent
    from synapse_worker.sources.codex import CodexSource

    codex_transcript = tmp_path / "codex-sess.jsonl"
    codex_transcript.write_text("", encoding="utf-8")
    write_binding(
        binding_path_for_agent(tmp_path / ".synapse", "codex"),
        SessionBinding(
            agent_session_id="codex-sess-1",
            shared_id="team-standup",
            contributor="akhil",
            agent="codex",
            transcript_path=str(codex_transcript),
            pinned_at=datetime.now(timezone.utc),
        ),
    )

    config, loop, transcript, _, source, _ = cli._build(_ns(transcript=None))

    assert source == "pinned"
    assert isinstance(loop.source, CodexSource)
    assert loop.binding.agent == "codex"
    assert transcript == codex_transcript


def test_build_with_explicit_agent_flag_skips_straight_to_that_agent(tmp_path, monkeypatch) -> None:
    """--agent codex must be honored even with nothing pinned anywhere, via
    the heuristic path -- and must not be shadowed by a claude-code pin that
    happens to exist too. CODEX_SESSIONS is monkeypatched to an isolated
    fixture root, the same way test_status_lists_detected_transcripts_with_
    liveness isolates CLAUDE_PROJECTS -- no real machine state is touched."""
    import synapse_worker.discovery as discovery
    from synapse_contracts import LocalBinding, SessionBinding, write_binding
    from synapse_worker.discovery import binding_path_for_agent
    from synapse_worker.sources.codex import CodexSource

    write_binding(
        binding_path_for_agent(tmp_path / ".synapse", "claude-code"),
        SessionBinding(
            agent_session_id="cc-sess",
            shared_id="other-session",
            contributor="akhil",
            agent="claude-code",
            transcript_path=str(tmp_path / "cc.jsonl"),
            pinned_at=datetime.now(timezone.utc),
        ),
    )
    (tmp_path / "cc.jsonl").write_text("", encoding="utf-8")

    codex_root = tmp_path / "codex-sessions"
    monkeypatch.setattr(discovery, "CODEX_SESSIONS", codex_root)
    day_dir = codex_root / "2026" / "08" / "05"
    day_dir.mkdir(parents=True, exist_ok=True)
    # find_codex_transcripts now reads each candidate's own session_meta.cwd
    # to decide whether it belongs to Path.cwd() (== tmp_path here, via the
    # _isolated_cwd autouse fixture) -- an empty file would no longer match.
    session_meta_line = json.dumps(
        {
            "timestamp": "2026-08-05T10-00-00Z", "ordinal": 0, "type": "session_meta",
            "payload": {"id": "codex-sess", "session_id": "codex-sess", "cwd": str(tmp_path)},
        }
    )
    (day_dir / "rollout-2026-08-05T10-00-00-1c9b6d8e-27ac-4f1e-9f2c-8a2b1e6d4c11.jsonl").write_text(
        session_meta_line + "\n", encoding="utf-8"
    )

    config, loop, transcript, _, source, _ = cli._build(_ns(transcript=None, agent="codex"))

    assert source == "heuristic"
    assert isinstance(loop.source, CodexSource)
    assert loop.binding.agent == "codex"
    assert isinstance(loop.binding, LocalBinding)


def test_build_prefers_a_pinned_binding_over_an_earlier_agents_heuristic(
    tmp_path, monkeypatch
) -> None:
    """MAJOR regression pin: _resolve_agent_and_transcript's own documented
    precedence -- "a join-pinned binding for ANY agent wins over every
    agent's heuristic" -- must hold even when the heuristic candidate
    (claude-code, tried first in AGENT_REGISTRY order) resolves to something
    real. Without this, a live claude-code heuristic tree would shadow a
    pinned bindings/codex.json simply because claude-code is tried first."""
    import synapse_worker.discovery as discovery
    from synapse_contracts import SessionBinding, write_binding
    from synapse_worker.discovery import binding_path_for_agent, project_slug
    from synapse_worker.sources.codex import CodexSource

    # A live (heuristic) Claude Code transcript for THIS cwd -- resolves to
    # something, just not a pin.
    claude_root = tmp_path / "claude-projects"
    monkeypatch.setattr(discovery, "CLAUDE_PROJECTS", claude_root)
    slug_dir = claude_root / project_slug(tmp_path)
    slug_dir.mkdir(parents=True)
    (slug_dir / "sess-live.jsonl").write_text("{}\n", encoding="utf-8")

    # A pinned Codex binding -- must win despite being tried second.
    codex_transcript = tmp_path / "codex-sess.jsonl"
    codex_transcript.write_text("", encoding="utf-8")
    write_binding(
        binding_path_for_agent(tmp_path / ".synapse", "codex"),
        SessionBinding(
            agent_session_id="codex-sess-1",
            shared_id="team-standup",
            contributor="akhil",
            agent="codex",
            transcript_path=str(codex_transcript),
            pinned_at=datetime.now(timezone.utc),
        ),
    )

    config, loop, transcript, _, source, _ = cli._build(_ns(transcript=None))

    assert source == "pinned"
    assert isinstance(loop.source, CodexSource)
    assert loop.binding.agent == "codex"
    assert transcript == codex_transcript


def test_build_with_explicit_transcript_honors_the_agent_flag(tmp_path) -> None:
    """MAJOR regression pin: `--agent codex --transcript <rollout>` used to
    hard-set DEFAULT_AGENT (claude-code) in the --transcript branch and never
    look at args.agent at all -- ClaudeCodeSource would then silently skip
    every line of a Codex rollout as bookkeeping (zero events, no error).
    Both flags live on the same `run` subparser, so this is a supported
    combination an operator debugging the demo is likely to type."""
    from synapse_worker.sources.codex import CodexSource

    codex_transcript = tmp_path / "some-codex-rollout.jsonl"
    codex_transcript.write_text("", encoding="utf-8")

    config, loop, transcript, _, source, _ = cli._build(
        _ns(transcript=str(codex_transcript), agent="codex")
    )

    assert isinstance(loop.source, CodexSource)
    assert loop.binding.agent == "codex"
    assert transcript == codex_transcript
    assert source == "explicit --transcript"


def test_build_with_explicit_transcript_and_no_agent_flag_defaults_to_claude_code(
    tmp_path,
) -> None:
    """The un-widened case must keep behaving exactly as before: an explicit
    --transcript with no --agent at all still has no dialect to infer from,
    so it stays DEFAULT_AGENT."""
    from synapse_worker.sources.claude_code import ClaudeCodeSource

    transcript_path = tmp_path / "sess.jsonl"
    transcript_path.write_text("", encoding="utf-8")

    config, loop, transcript, _, source, _ = cli._build(_ns(transcript=str(transcript_path)))

    assert isinstance(loop.source, ClaudeCodeSource)
    assert loop.binding.agent == "claude-code"


async def test_run_reports_other_agents_bindings_it_is_not_following(
    tmp_path, monkeypatch, capsys
) -> None:
    """MAJOR regression pin: `join` can bind two agents (claude-code and
    codex) in one call, but `run` follows exactly one Source per process.
    Nothing previously told the operator the second binding sits there
    unfollowed after a `join` that printed both as bound."""
    from synapse_contracts import SessionBinding, write_binding
    from synapse_worker.discovery import binding_path_for_agent

    monkeypatch.setattr(cli, "check_canary", _async(PASSING_CANARY))

    claude_transcript = tmp_path / "cc.jsonl"
    _write_transcript(claude_transcript)
    codex_transcript = tmp_path / "codex.jsonl"
    _write_transcript(codex_transcript)

    write_binding(
        binding_path_for_agent(tmp_path / ".synapse", "claude-code"),
        SessionBinding(
            agent_session_id="cc-sess", shared_id="team-standup", contributor="akhil",
            agent="claude-code", transcript_path=str(claude_transcript),
            pinned_at=datetime.now(timezone.utc),
        ),
    )
    write_binding(
        binding_path_for_agent(tmp_path / ".synapse", "codex"),
        SessionBinding(
            agent_session_id="codex-sess", shared_id="team-standup", contributor="akhil",
            agent="codex", transcript_path=str(codex_transcript),
            pinned_at=datetime.now(timezone.utc),
        ),
    )

    exit_code = await cli.cmd_run(_ns(transcript=None, interval=0.01, ticks=1, from_start=False))

    out = capsys.readouterr().out
    assert exit_code == 0
    assert "selection        pinned" in out
    assert "also bound: codex" in out


async def test_run_says_nothing_when_no_other_agent_is_bound(
    tmp_path, monkeypatch, capsys
) -> None:
    """The common single-agent case must not print a spurious note."""
    transcript = tmp_path / "sess.jsonl"
    _write_transcript(transcript)
    monkeypatch.setattr(cli, "check_canary", _async(PASSING_CANARY))

    exit_code = await cli.cmd_run(
        _ns(transcript=str(transcript), interval=0.01, ticks=1, from_start=False)
    )

    out = capsys.readouterr().out
    assert exit_code == 0
    assert "also bound" not in out


async def test_run_swallows_keyboard_interrupt_and_still_shuts_down(
    tmp_path, monkeypatch, capsys
) -> None:
    """Ctrl+C during the poll loop must not crash past a clean shutdown --
    otherwise a findings flush and offset save on the way out get skipped."""
    transcript = tmp_path / "sess.jsonl"
    _write_transcript(transcript)
    monkeypatch.setattr(cli, "check_canary", _async(PASSING_CANARY))

    async def raise_interrupt(self, *args, **kwargs):
        raise KeyboardInterrupt

    monkeypatch.setattr(WorkerLoop, "run", raise_interrupt)
    shut_down = _spy(monkeypatch, WorkerLoop, "shutdown")

    exit_code = await cli.cmd_run(
        _ns(transcript=str(transcript), interval=0.01, ticks=1, from_start=False)
    )

    assert exit_code == 0
    assert shut_down.called is True
    assert "shutdown" in capsys.readouterr().out


async def test_run_with_no_transcript_anywhere_exits_cleanly(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(cli, "resolve_transcript", lambda cwd, state_dir, *, agent=None: None)

    with pytest.raises(SystemExit) as excinfo:
        await cli.cmd_run(_ns(transcript=None, interval=0.01, ticks=1, from_start=False))

    assert excinfo.value.code == 2


# ---------------------------------------------------------------------------
# run — --debug-port
# ---------------------------------------------------------------------------

async def test_run_starts_the_debug_server_on_the_default_port(
    tmp_path, monkeypatch, capsys
) -> None:
    transcript = tmp_path / "sess.jsonl"
    _write_transcript(transcript)
    monkeypatch.setattr(cli, "check_canary", _async(PASSING_CANARY))
    monkeypatch.delenv("SYNAPSE_DEBUG_PORT", raising=False)
    _FakeDebugServer.instances.clear()
    monkeypatch.setattr(cli, "DebugServer", _FakeDebugServer)

    exit_code = await cli.cmd_run(
        _ns(
            transcript=str(transcript), interval=0.01, ticks=1, from_start=False,
            debug_port=None,
        )
    )

    assert exit_code == 0
    [server] = _FakeDebugServer.instances
    assert server.port == 8790
    assert server.started is True
    assert server.stopped is True
    assert "debug" in capsys.readouterr().out


async def test_run_with_debug_port_zero_disables_the_dashboard(
    tmp_path, monkeypatch, capsys
) -> None:
    from synapse_providers import RecordingProvider

    transcript = tmp_path / "sess.jsonl"
    _write_transcript(transcript)
    seen = {}

    async def fake_canary(provider):
        seen["provider"] = provider
        return PASSING_CANARY

    monkeypatch.setattr(cli, "check_canary", fake_canary)
    _FakeDebugServer.instances.clear()
    monkeypatch.setattr(cli, "DebugServer", _FakeDebugServer)

    exit_code = await cli.cmd_run(
        _ns(
            transcript=str(transcript), interval=0.01, ticks=1, from_start=False,
            debug_port=0,
        )
    )

    assert exit_code == 0
    assert _FakeDebugServer.instances == []
    assert not isinstance(seen["provider"], RecordingProvider)


async def test_run_uses_synapse_debug_port_env_when_flag_unset(
    tmp_path, monkeypatch, capsys
) -> None:
    transcript = tmp_path / "sess.jsonl"
    _write_transcript(transcript)
    monkeypatch.setattr(cli, "check_canary", _async(PASSING_CANARY))
    monkeypatch.setenv("SYNAPSE_DEBUG_PORT", "9999")
    _FakeDebugServer.instances.clear()
    monkeypatch.setattr(cli, "DebugServer", _FakeDebugServer)

    exit_code = await cli.cmd_run(
        _ns(
            transcript=str(transcript), interval=0.01, ticks=1, from_start=False,
            debug_port=None,
        )
    )

    assert exit_code == 0
    [server] = _FakeDebugServer.instances
    assert server.port == 9999


async def test_run_wraps_the_distiller_provider_when_debug_enabled(
    tmp_path, monkeypatch, capsys
) -> None:
    from synapse_providers import RecordingProvider

    transcript = tmp_path / "sess.jsonl"
    _write_transcript(transcript)
    seen = {}

    async def fake_canary(provider):
        seen["provider"] = provider
        return PASSING_CANARY

    monkeypatch.setattr(cli, "check_canary", fake_canary)
    monkeypatch.setattr(cli, "DebugServer", _FakeDebugServer)
    _FakeDebugServer.instances.clear()

    exit_code = await cli.cmd_run(
        _ns(
            transcript=str(transcript), interval=0.01, ticks=1, from_start=False,
            debug_port=None,
        )
    )

    assert exit_code == 0
    assert isinstance(seen["provider"], RecordingProvider)
    assert seen["provider"].component == "distiller"


# ---------------------------------------------------------------------------
# status
# ---------------------------------------------------------------------------

def _isolate_agent_roots(monkeypatch, tmp_path) -> None:
    """`cmd_status` now walks BOTH registered agents' finders directly (not
    only through a mockable `resolve_transcript`), so every status test needs
    an isolated CLAUDE_PROJECTS/CODEX_SESSIONS the same way the join/discovery
    tests already do -- otherwise it depends on whether the machine running
    the suite happens to have a real `~/.codex/sessions` (join_session's own
    module docstring documents exactly this hazard for the analogous loop)."""
    import synapse_worker.discovery as discovery

    monkeypatch.setattr(discovery, "CLAUDE_PROJECTS", tmp_path / "no-claude-projects")
    monkeypatch.setattr(discovery, "CODEX_SESSIONS", tmp_path / "no-codex-sessions")


async def test_status_with_nothing_joined_and_no_transcripts(tmp_path, monkeypatch, capsys) -> None:
    _isolate_agent_roots(monkeypatch, tmp_path)

    exit_code = await cli.cmd_status(_ns())

    out = capsys.readouterr().out
    assert exit_code == 0
    assert "joined session   none" in out
    assert "none found" in out


async def test_status_reports_a_joined_session(tmp_path, monkeypatch, capsys) -> None:
    from synapse_contracts import LocalBinding

    _isolate_agent_roots(monkeypatch, tmp_path)
    transcript = tmp_path / "sess.jsonl"
    _write_transcript(transcript)
    joined = LocalBinding(
        agent_session_id="as-joined", shared_id="team-standup",
        contributor="akhil", agent="claude-code",
    )
    monkeypatch.setattr(
        cli, "resolve_transcript",
        # cmd_status now tries every registered agent -- only claude-code
        # resolves to the pinned binding here, so codex must fall through to
        # None the same way it would for a real, un-joined product.
        lambda cwd, state_dir, *, agent=None: (
            ResolvedTranscript(
                path=transcript, agent_session_id="as-joined", source="pinned",
                local_binding=joined,
            )
            if agent == "claude-code"
            else None
        ),
    )

    exit_code = await cli.cmd_status(_ns())

    out = capsys.readouterr().out
    assert exit_code == 0
    assert "team-standup" in out
    assert "as-joined" in out


async def test_status_reports_a_codex_only_joined_session(tmp_path, monkeypatch, capsys) -> None:
    """MAJOR regression pin: a Codex-only join (Claude Code never joined or
    live) must not make `status` print 'joined session none' -- that directly
    contradicts what `join` itself just reported."""
    from synapse_contracts import LocalBinding

    _isolate_agent_roots(monkeypatch, tmp_path)
    transcript = tmp_path / "codex-sess.jsonl"
    _write_transcript(transcript)
    joined = LocalBinding(
        agent_session_id="codex-joined", shared_id="team-standup",
        contributor="akhil", agent="codex",
    )
    monkeypatch.setattr(
        cli, "resolve_transcript",
        lambda cwd, state_dir, *, agent=None: (
            ResolvedTranscript(
                path=transcript, agent_session_id="codex-joined", source="pinned",
                local_binding=joined,
            )
            if agent == "codex"
            else None
        ),
    )

    exit_code = await cli.cmd_status(_ns())

    out = capsys.readouterr().out
    assert exit_code == 0
    assert "joined session   none" not in out
    assert "[codex]" in out
    assert "team-standup" in out
    assert "codex-joined" in out


async def test_status_lists_detected_transcripts_with_liveness(
    tmp_path, monkeypatch, capsys
) -> None:
    import synapse_worker.discovery as discovery

    _isolate_agent_roots(monkeypatch, tmp_path)
    fake_projects = tmp_path / "claude-projects"
    slug_dir = fake_projects / discovery.project_slug(tmp_path)
    slug_dir.mkdir(parents=True)
    (slug_dir / "sess-live.jsonl").write_text("{}\n", encoding="utf-8")
    monkeypatch.setattr(discovery, "CLAUDE_PROJECTS", fake_projects)

    exit_code = await cli.cmd_status(_ns())

    out = capsys.readouterr().out
    assert exit_code == 0
    assert "sess-live.jsonl" in out
    assert "[LIVE]" in out
    assert "[claude-code]" in out


async def test_status_reports_unsent_findings(tmp_path, monkeypatch, capsys) -> None:
    from synapse_contracts import Attribution, Finding, FindingType
    from synapse_worker.producer import FileSink, Producer

    _isolate_agent_roots(monkeypatch, tmp_path)
    producer = Producer(tmp_path / ".synapse" / "wal", FileSink(tmp_path / "upstream.jsonl"))
    producer.record([
        Finding(
            id="f-1", type=FindingType.LEARNING, text="x",
            attributions=[Attribution(contributor="a", agent_session="s", agent="claude-code")],
            ts=datetime(2026, 8, 4, tzinfo=timezone.utc),
        )
    ])

    exit_code = await cli.cmd_status(_ns())

    out = capsys.readouterr().out
    assert exit_code == 0
    assert "unsent findings  1" in out


# ---------------------------------------------------------------------------
# replay
# ---------------------------------------------------------------------------

async def test_replay_with_nothing_pending(capsys) -> None:
    exit_code = await cli.cmd_replay(_ns())

    assert exit_code == 0
    assert "sent 0, still pending 0" in capsys.readouterr().out


def _fake_distiller_builder(monkeypatch, *, scripts_factory=None):
    """Monkeypatch `cli.build_distiller` to return a FakeProvider-backed
    Distiller per call, recording every `binding` it was constructed with so
    tests can assert on it directly — the thing the original test never did,
    which is what let the "replay" sentinel and wrong-contributor bugs hide
    behind a green suite.
    """
    from synapse_distiller import Distiller, load_pack_by_name
    from synapse_providers import FakeProvider

    seen_bindings = []
    if scripts_factory is None:
        def scripts_factory():
            return [{"findings": [{"type": "learning", "text": "redistilled"}]}]

    def fake_build_distiller(config, binding):
        seen_bindings.append(binding)
        return Distiller(
            FakeProvider(scripts=scripts_factory()),
            binding,
            load_pack_by_name("v2-hardened"),
            ["text"],
            "labelled",
        )

    monkeypatch.setattr(cli, "build_distiller", fake_build_distiller)
    return seen_bindings


async def test_replay_skipped_redistils_and_archives(tmp_path, monkeypatch, capsys) -> None:
    """A triage-skipped segment can be forced through the pipeline later, with
    contributor/shared_id read from the CURRENTLY joined binding -- never
    fabricated (see test_replay_skipped_refuses_without_a_joined_binding for
    the no-binding case, adjudicated 2026-08-04).

    Deviation from the plan's literal test body: this file has no `invoke_cli`
    helper and no existing FakeProvider-backed CLI test, so the arrange block
    follows this file's actual pattern instead — call `cli.cmd_replay(_ns(...))`
    directly (same as every other test in this section) and monkeypatch
    `cli.build_distiller` (the construction path extracted below) to return a
    FakeProvider-backed Distiller rather than reaching a real NPU.
    """
    from synapse_contracts import AgentEvent, Segment, SessionBinding, write_binding
    from synapse_worker.triage_log import TriageLog

    state_dir = tmp_path / ".synapse"  # config default, relative to the isolated cwd
    ts = datetime(2026, 8, 4, tzinfo=timezone.utc)
    seg = Segment(id="s-skipped", agent_session_id="as-real-42",
                  events=[AgentEvent(role="assistant", kind="text",
                                     content="ruff run, all clean", ts=ts,
                                     agent_session_id="as-real-42")],
                  started_at=ts, ended_at=ts)
    TriageLog(state_dir).record_skip(seg, "lint-clean")
    write_binding(
        cli.binding_path_for_agent(state_dir, cli.DEFAULT_AGENT),
        SessionBinding(
            agent_session_id="as-real-42", shared_id="local-dev",
            contributor="aditya", agent=cli.DEFAULT_AGENT,
            transcript_path=str(tmp_path / "sess.jsonl"), pinned_at=ts,
        ),
    )

    monkeypatch.setattr(cli, "check_canary", _async(PASSING_CANARY))
    seen_bindings = _fake_distiller_builder(monkeypatch)

    exit_code = await cli.cmd_replay(_ns(skipped=True))

    assert exit_code == 0
    # Regression: the binding cmd_replay actually built is now observed. It
    # must carry the segment's OWN agent_session_id (round-tripped through the
    # skip log), not a "replay" sentinel that matches no real Agent Session.
    assert len(seen_bindings) == 1
    assert seen_bindings[0].agent_session_id == "as-real-42"
    assert seen_bindings[0].contributor == "aditya"     # read from the joined binding
    assert seen_bindings[0].shared_id == "local-dev"    # read from the joined binding
    assert TriageLog(state_dir).load_skipped() == []          # archived
    archives = list(state_dir.glob("triage-skips.replayed-*.jsonl"))
    assert len(archives) == 1
    sink_file = state_dir / "upstream.jsonl"                  # findings flowed
    assert sink_file.exists() and sink_file.read_text().strip()
    assert "Replayed 1 skipped segments -> 1 findings" in capsys.readouterr().out


async def test_replay_skipped_refuses_without_a_joined_binding(
    tmp_path, monkeypatch, capsys
) -> None:
    """Adjudicated 2026-08-04: replay --skipped must never fabricate
    Attribution from CLI defaults. The segment's own agent_session_id
    round-trips through the skip log, but contributor/shared_id do not -- the
    only place to get real values for those is the CURRENTLY joined binding.
    Without one, refuse outright rather than guess, and touch nothing."""
    from synapse_contracts import AgentEvent, Segment
    from synapse_worker.triage_log import TriageLog

    state_dir = tmp_path / ".synapse"
    ts = datetime(2026, 8, 4, tzinfo=timezone.utc)
    seg = Segment(id="s-skipped", agent_session_id="as-real-42",
                  events=[AgentEvent(role="assistant", kind="text", content="x",
                                     ts=ts, agent_session_id="as-real-42")],
                  started_at=ts, ended_at=ts)
    TriageLog(state_dir).record_skip(seg, "lint-clean")

    def _must_not_be_called(config, binding):
        raise AssertionError("build_distiller must not run without a joined binding")

    monkeypatch.setattr(cli, "build_distiller", _must_not_be_called)

    exit_code = await cli.cmd_replay(_ns(skipped=True))

    err = capsys.readouterr().err
    assert exit_code == 1
    assert "No joined Shared Session" in err
    assert "synapse-worker join" in err
    remaining = TriageLog(state_dir).load_skipped()
    assert [(s.id, r) for s, r in remaining] == [("s-skipped", "lint-clean")]  # untouched
    assert not list(state_dir.glob("triage-skips.replayed-*.jsonl"))          # not archived


async def test_replay_skipped_prefers_joined_binding_over_cli_defaults(
    tmp_path, monkeypatch, capsys
) -> None:
    """`_build` prefers a joined SessionBinding over --contributor/--shared-id
    for `run`; `replay --skipped` must make the same choice (there is no
    --contributor/--shared-id fallback for --skipped at all -- see
    test_replay_skipped_refuses_without_a_joined_binding -- so this pins that
    the VALUES that make it onto the rebuilt binding are the joined ones, not
    some other stale or default value)."""
    from synapse_contracts import AgentEvent, Segment, SessionBinding, write_binding
    from synapse_worker.triage_log import TriageLog

    state_dir = tmp_path / ".synapse"
    ts = datetime(2026, 8, 4, tzinfo=timezone.utc)
    seg = Segment(id="s-skipped", agent_session_id="as-real-42",
                  events=[AgentEvent(role="assistant", kind="text", content="clean",
                                     ts=ts, agent_session_id="as-real-42")],
                  started_at=ts, ended_at=ts)
    TriageLog(state_dir).record_skip(seg, "lint-clean")

    write_binding(
        cli.binding_path_for_agent(state_dir, cli.DEFAULT_AGENT),
        SessionBinding(
            agent_session_id="as-real-42", shared_id="team-standup",
            contributor="akhil", agent=cli.DEFAULT_AGENT,
            transcript_path=str(tmp_path / "sess.jsonl"), pinned_at=ts,
        ),
    )

    monkeypatch.setattr(cli, "check_canary", _async(PASSING_CANARY))
    seen_bindings = _fake_distiller_builder(monkeypatch)

    exit_code = await cli.cmd_replay(_ns(skipped=True))

    assert exit_code == 0
    assert seen_bindings[0].contributor == "akhil"        # joined
    assert seen_bindings[0].shared_id == "team-standup"    # joined


async def test_replay_skipped_refuses_when_canary_fails(tmp_path, monkeypatch, capsys) -> None:
    """The prompt-drop refusal `cmd_run` applies before any distillation must
    also gate `replay --skipped` — otherwise a model that stopped reading its
    prompt writes invented findings straight into the write-ahead log."""
    from synapse_contracts import AgentEvent, Segment, SessionBinding, write_binding
    from synapse_worker.triage_log import TriageLog

    state_dir = tmp_path / ".synapse"
    ts = datetime(2026, 8, 4, tzinfo=timezone.utc)
    seg = Segment(id="s-skipped", agent_session_id="as-t",
                  events=[AgentEvent(role="assistant", kind="text", content="x",
                                     ts=ts, agent_session_id="as-t")],
                  started_at=ts, ended_at=ts)
    TriageLog(state_dir).record_skip(seg, "lint-clean")
    write_binding(
        cli.binding_path_for_agent(state_dir, cli.DEFAULT_AGENT),
        SessionBinding(
            agent_session_id="as-t", shared_id="local-dev",
            contributor="aditya", agent=cli.DEFAULT_AGENT,
            transcript_path=str(tmp_path / "sess.jsonl"), pinned_at=ts,
        ),
    )

    monkeypatch.setattr(cli, "check_canary", _async(FAILING_CANARY))

    class _MustNotBeCalled:
        provider = object()

        async def distil(self, segment):
            raise AssertionError("distil() must not run when the canary fails")

    monkeypatch.setattr(cli, "build_distiller", lambda config, binding: _MustNotBeCalled())

    exit_code = await cli.cmd_replay(_ns(skipped=True))

    assert exit_code == 1
    assert "CANARY FAILED" in capsys.readouterr().err
    remaining = TriageLog(state_dir).load_skipped()
    assert [(s.id, r) for s, r in remaining] == [("s-skipped", "lint-clean")]  # untouched
    assert not list(state_dir.glob("triage-skips.replayed-*.jsonl"))          # not archived


async def test_replay_skipped_requeues_only_the_failed_segment(
    tmp_path, monkeypatch, capsys
) -> None:
    """One bad segment must not sink the whole batch, and a retry must be
    idempotent: only the segment that actually failed goes back to the skip
    log, so re-running `replay --skipped` doesn't re-distil (and re-count)
    segments that already succeeded."""
    from synapse_contracts import AgentEvent, Attribution, Finding, FindingType, Segment
    from synapse_worker.triage_log import TriageLog

    from synapse_contracts import SessionBinding, write_binding

    state_dir = tmp_path / ".synapse"
    ts = datetime(2026, 8, 4, tzinfo=timezone.utc)
    log = TriageLog(state_dir)
    good = Segment(id="s-good", agent_session_id="as-t",
                    events=[AgentEvent(role="assistant", kind="text", content="ok",
                                       ts=ts, agent_session_id="as-t")],
                    started_at=ts, ended_at=ts)
    bad = Segment(id="s-bad", agent_session_id="as-t",
                  events=[AgentEvent(role="assistant", kind="text", content="boom",
                                     ts=ts, agent_session_id="as-t")],
                  started_at=ts, ended_at=ts)
    log.record_skip(good, "lint-clean")
    log.record_skip(bad, "readonly-run")
    write_binding(
        cli.binding_path_for_agent(state_dir, cli.DEFAULT_AGENT),
        SessionBinding(
            agent_session_id="as-t", shared_id="local-dev",
            contributor="aditya", agent=cli.DEFAULT_AGENT,
            transcript_path=str(tmp_path / "sess.jsonl"), pinned_at=ts,
        ),
    )

    monkeypatch.setattr(cli, "check_canary", _async(PASSING_CANARY))

    class _FlakyDistiller:
        provider = object()

        async def distil(self, segment):
            if segment.id == "s-bad":
                raise RuntimeError("model timed out")
            from types import SimpleNamespace
            finding = Finding(
                id="f-good", type=FindingType.LEARNING, text="ok",
                attributions=[Attribution(contributor="a", agent_session="as-t",
                                          agent="claude-code")],
                ts=ts,
            )
            return [finding], SimpleNamespace(skipped_empty=False)

    monkeypatch.setattr(cli, "build_distiller", lambda config, binding: _FlakyDistiller())

    exit_code = await cli.cmd_replay(_ns(skipped=True))

    assert exit_code == 1
    remaining = TriageLog(state_dir).load_skipped()
    assert [(s.id, r) for s, r in remaining] == [("s-bad", "readonly-run")]
    out = capsys.readouterr().out
    assert "Replayed 1 skipped segments -> 1 findings" in out
    assert "1 segment(s) failed and were re-queued" in out
    sink_file = state_dir / "upstream.jsonl"
    assert sink_file.exists() and sink_file.read_text().strip()  # the good one still shipped


async def test_replay_skipped_records_each_finding_before_moving_on(
    tmp_path, monkeypatch, capsys
) -> None:
    """Invariant 4 (durability before send): a finding must be written to the
    write-ahead log the moment its segment is distilled, before the NEXT
    segment is even touched, before `flush()`, and well before `archive()`
    destroys the recovery journal. Two mutations pin here: buffering findings
    in a list and calling `producer.record()` once right before `flush()`
    (no write-ahead at all), and moving `archive()` before the distillation
    loop (destroying the journal before anything is durable). Both leave the
    end-state files on disk identical on this happy path -- only the ORDER of
    calls exposes them, so this test pins order directly rather than only
    final content."""
    from synapse_contracts import (
        AgentEvent,
        Attribution,
        Finding,
        FindingType,
        Segment,
        SessionBinding,
        write_binding,
    )
    from synapse_worker.producer import Producer
    from synapse_worker.triage_log import TriageLog

    state_dir = tmp_path / ".synapse"
    ts = datetime(2026, 8, 4, tzinfo=timezone.utc)
    log = TriageLog(state_dir)
    seg_a = Segment(id="s-a", agent_session_id="as-t",
                     events=[AgentEvent(role="assistant", kind="text", content="a",
                                        ts=ts, agent_session_id="as-t")],
                     started_at=ts, ended_at=ts)
    seg_b = Segment(id="s-b", agent_session_id="as-t",
                     events=[AgentEvent(role="assistant", kind="text", content="b",
                                        ts=ts, agent_session_id="as-t")],
                     started_at=ts, ended_at=ts)
    log.record_skip(seg_a, "lint-clean")
    log.record_skip(seg_b, "lint-clean")
    write_binding(
        cli.binding_path_for_agent(state_dir, cli.DEFAULT_AGENT),
        SessionBinding(
            agent_session_id="as-t", shared_id="local-dev",
            contributor="aditya", agent=cli.DEFAULT_AGENT,
            transcript_path=str(tmp_path / "sess.jsonl"), pinned_at=ts,
        ),
    )

    monkeypatch.setattr(cli, "check_canary", _async(PASSING_CANARY))

    calls: list[str] = []

    class _OrderedDistiller:
        provider = object()

        async def distil(self, segment):
            calls.append(f"distil:{segment.id}")
            finding = Finding(
                id=f"f-{segment.id}", type=FindingType.LEARNING, text="x",
                attributions=[Attribution(contributor="a", agent_session="as-t",
                                          agent="claude-code")],
                ts=ts,
            )
            from types import SimpleNamespace
            return [finding], SimpleNamespace(skipped_empty=False)

    monkeypatch.setattr(cli, "build_distiller", lambda config, binding: _OrderedDistiller())

    original_record = Producer.record

    def spy_record(self, findings):
        for f in findings:
            calls.append(f"record:{f.id}")
        return original_record(self, findings)

    original_flush = Producer.flush

    async def spy_flush(self):
        calls.append("flush")
        return await original_flush(self)

    original_archive = TriageLog.archive

    def spy_archive(self):
        calls.append("archive")
        return original_archive(self)

    monkeypatch.setattr(Producer, "record", spy_record)
    monkeypatch.setattr(Producer, "flush", spy_flush)
    monkeypatch.setattr(TriageLog, "archive", spy_archive)

    exit_code = await cli.cmd_replay(_ns(skipped=True))

    assert exit_code == 0
    assert calls == [
        "distil:s-a", "record:f-s-a",
        "distil:s-b", "record:f-s-b",
        "flush", "archive",
    ]


async def test_replay_skipped_requeues_a_prompt_drop_segment(
    tmp_path, monkeypatch, capsys, caplog
) -> None:
    """`cmd_replay`'s PromptDropError branch is distinct from its generic
    Exception branch (different log message: "Prompt-drop guard tripped
    replaying ..." vs "Replay distillation failed for ...", mirroring the
    same distinction `WorkerLoop.tick()` makes for the live path in
    test_dropped_prompt_does_not_poison_the_log). One dropped-prompt segment
    among two must not sink the batch -- it requeues like any other failure
    -- but the specific log line pins that the dedicated branch, not the
    fallback `except Exception`, is what actually ran."""
    from synapse_contracts import (
        AgentEvent,
        Attribution,
        Finding,
        FindingType,
        Segment,
        SessionBinding,
        write_binding,
    )
    from synapse_distiller import PromptDropError
    from synapse_worker.triage_log import TriageLog

    state_dir = tmp_path / ".synapse"
    ts = datetime(2026, 8, 4, tzinfo=timezone.utc)
    log = TriageLog(state_dir)
    good = Segment(id="s-good", agent_session_id="as-t",
                    events=[AgentEvent(role="assistant", kind="text", content="ok",
                                       ts=ts, agent_session_id="as-t")],
                    started_at=ts, ended_at=ts)
    dropped = Segment(id="s-dropped", agent_session_id="as-t",
                       events=[AgentEvent(role="assistant", kind="text", content="x",
                                          ts=ts, agent_session_id="as-t")],
                       started_at=ts, ended_at=ts)
    log.record_skip(good, "lint-clean")
    log.record_skip(dropped, "readonly-run")
    write_binding(
        cli.binding_path_for_agent(state_dir, cli.DEFAULT_AGENT),
        SessionBinding(
            agent_session_id="as-t", shared_id="local-dev",
            contributor="aditya", agent=cli.DEFAULT_AGENT,
            transcript_path=str(tmp_path / "sess.jsonl"), pinned_at=ts,
        ),
    )

    monkeypatch.setattr(cli, "check_canary", _async(PASSING_CANARY))

    class _PromptDroppingDistiller:
        provider = object()

        async def distil(self, segment):
            if segment.id == "s-dropped":
                raise PromptDropError("input_tokens=1, prompt was dropped")
            from types import SimpleNamespace
            finding = Finding(
                id="f-good", type=FindingType.LEARNING, text="ok",
                attributions=[Attribution(contributor="a", agent_session="as-t",
                                          agent="claude-code")],
                ts=ts,
            )
            return [finding], SimpleNamespace(skipped_empty=False)

    monkeypatch.setattr(cli, "build_distiller", lambda config, binding: _PromptDroppingDistiller())

    with caplog.at_level("ERROR", logger="synapse_worker.cli"):
        exit_code = await cli.cmd_replay(_ns(skipped=True))

    assert exit_code == 1
    assert any(
        "Prompt-drop guard tripped replaying" in r.message for r in caplog.records
    )
    remaining = TriageLog(state_dir).load_skipped()
    assert [(s.id, r) for s, r in remaining] == [("s-dropped", "readonly-run")]
    out = capsys.readouterr().out
    assert "1 segment(s) failed and were re-queued" in out
    sink_file = state_dir / "upstream.jsonl"
    assert sink_file.exists() and sink_file.read_text().strip()  # the good one still shipped


async def test_replay_skipped_with_nothing_pending(tmp_path, capsys) -> None:
    exit_code = await cli.cmd_replay(_ns(skipped=True))

    assert exit_code == 0
    assert "No triage-skipped segments to replay." in capsys.readouterr().out


async def test_replay_delivers_queued_findings(tmp_path, capsys) -> None:
    """cmd_replay builds its own Producer from config's default paths
    (.synapse/wal, .synapse/upstream.jsonl, both relative to cwd) -- seed the
    SAME paths a real prior `run` would have used, then let replay deliver for
    real via the default file sink, checked by reading the sink file back."""
    from synapse_contracts import Attribution, Finding, FindingType
    from synapse_worker.producer import FileSink, Producer

    sink_file = tmp_path / ".synapse" / "upstream.jsonl"  # config default
    producer = Producer(tmp_path / ".synapse" / "wal", FileSink(sink_file))
    producer.record([
        Finding(
            id="f-1", type=FindingType.LEARNING, text="x",
            attributions=[Attribution(contributor="a", agent_session="s", agent="claude-code")],
            ts=datetime(2026, 8, 4, tzinfo=timezone.utc),
        )
    ])

    exit_code = await cli.cmd_replay(_ns())

    assert exit_code == 0
    assert "sent 1, still pending 0" in capsys.readouterr().out
    assert sink_file.is_file()


# ---------------------------------------------------------------------------
# main() dispatch
# ---------------------------------------------------------------------------

def test_main_requires_a_subcommand() -> None:
    with pytest.raises(SystemExit) as excinfo:
        cli.main([])
    assert excinfo.value.code == 2


def test_main_dispatches_join_with_parsed_arguments(monkeypatch) -> None:
    seen = {}

    async def fake_join(args):
        seen["shared_id"] = args.shared_id
        seen["contributor"] = args.contributor
        return 0

    monkeypatch.setattr(cli, "cmd_join", fake_join)

    exit_code = cli.main(["join", "team-standup", "--contributor", "akhil"])

    assert exit_code == 0
    assert seen == {"shared_id": "team-standup", "contributor": "akhil"}


def test_main_dispatches_run_with_parsed_flags(monkeypatch) -> None:
    seen = {}

    async def fake_run(args):
        seen["ticks"] = args.ticks
        seen["from_start"] = args.from_start
        return 0

    monkeypatch.setattr(cli, "cmd_run", fake_run)

    exit_code = cli.main(["run", "--ticks", "3", "--from-start"])

    assert exit_code == 0
    assert seen == {"ticks": 3, "from_start": True}


def test_main_dispatches_status(monkeypatch) -> None:
    async def fake_status(args):
        return 0

    monkeypatch.setattr(cli, "cmd_status", fake_status)

    assert cli.main(["status"]) == 0


def test_main_dispatches_replay_and_propagates_exit_code(monkeypatch) -> None:
    async def fake_replay(args):
        return 1

    monkeypatch.setattr(cli, "cmd_replay", fake_replay)

    assert cli.main(["replay"]) == 1


def test_main_rejects_an_unknown_subcommand() -> None:
    with pytest.raises(SystemExit) as excinfo:
        cli.main(["not-a-real-command"])
    assert excinfo.value.code == 2


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------

class _Namespace:
    def __init__(self, **kwargs):
        self.verbose = False
        self.shared_id = "local-dev"
        self.contributor = "aditya"
        self.transcript = None
        self.interval = None
        self.ticks = 1
        self.from_start = False
        self.skipped = False
        # 0 (disabled) by default so the many cmd_run tests above that never
        # mention --debug-port don't each try to bind a real socket on 8790.
        # Tests that care about the dashboard pass debug_port explicitly.
        self.debug_port = 0
        for key, value in kwargs.items():
            setattr(self, key, value)


def _ns(**kwargs) -> _Namespace:
    return _Namespace(**kwargs)


class _FakeDebugServer:
    """Stands in for the real DebugServer so cmd_run tests never bind a real
    socket. Records construction args and start()/stop() calls.

    `raise_on_start`, when set, makes `.start()` raise instead of returning a
    port -- a class attribute (not per-instance) since `cmd_run` constructs
    the server itself; tests that use it must reset it to None afterward so
    it doesn't leak into later tests in this file.
    """

    instances: list["_FakeDebugServer"] = []
    raise_on_start: Exception | None = None

    def __init__(self, stats, port, *, transcript=None):
        self.stats = stats
        self.port = port
        self.transcript = transcript
        self.started = False
        self.stopped = False
        _FakeDebugServer.instances.append(self)

    def start(self) -> int:
        self.started = True
        if _FakeDebugServer.raise_on_start is not None:
            raise _FakeDebugServer.raise_on_start
        return self.port

    def stop(self) -> None:
        self.stopped = True


def _async(value):
    async def fn(*args, **kwargs):
        return value
    return fn


class _Spy:
    def __init__(self):
        self.called = False


def _spy(monkeypatch, cls, method_name: str) -> _Spy:
    """Patch a method on a class, recording whether it was called, then
    delegate to the real implementation so behaviour stays real. Handles both
    sync (attach_at_end) and async (run) methods on WorkerLoop."""
    import inspect

    spy = _Spy()
    original = getattr(cls, method_name)

    if inspect.iscoroutinefunction(original):
        async def wrapper(self, *args, **kwargs):
            spy.called = True
            return await original(self, *args, **kwargs)
    else:
        def wrapper(self, *args, **kwargs):
            spy.called = True
            return original(self, *args, **kwargs)

    monkeypatch.setattr(cls, method_name, wrapper)
    return spy
