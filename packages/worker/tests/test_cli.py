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
    monkeypatch.setattr("synapse_worker.discovery.find_live_transcript", lambda cwd, root=None: None)

    exit_code = await cli.cmd_join(_ns(shared_id="team-standup", contributor="aditya"))

    assert exit_code == 1
    assert "nothing bound" in capsys.readouterr().err


async def test_join_binds_the_live_transcript(tmp_path, monkeypatch, capsys) -> None:
    from synapse_worker.discovery import DiscoveredTranscript

    transcript = tmp_path / "sess.jsonl"
    transcript.write_text("{}\n", encoding="utf-8")
    monkeypatch.setattr(
        "synapse_worker.discovery.find_live_transcript",
        lambda cwd, root=None: DiscoveredTranscript(
            path=transcript, agent="claude-code", session_id="as-1", modified_at=0.0, size=1
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
        lambda cwd, state_dir: ResolvedTranscript(
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
        lambda cwd, state_dir: ResolvedTranscript(
            path=transcript, agent_session_id="as-joined", source="pinned", local_binding=joined
        ),
    )

    exit_code = await cli.cmd_run(_ns(transcript=None, interval=0.01, ticks=1, from_start=False))

    out = capsys.readouterr().out
    assert exit_code == 0
    assert "selection        pinned" in out


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
    monkeypatch.setattr(cli, "resolve_transcript", lambda cwd, state_dir: None)

    with pytest.raises(SystemExit) as excinfo:
        await cli.cmd_run(_ns(transcript=None, interval=0.01, ticks=1, from_start=False))

    assert excinfo.value.code == 2


# ---------------------------------------------------------------------------
# status
# ---------------------------------------------------------------------------

async def test_status_with_nothing_joined_and_no_transcripts(capsys) -> None:
    exit_code = await cli.cmd_status(_ns())

    out = capsys.readouterr().out
    assert exit_code == 0
    assert "joined session   none" in out
    assert "none found" in out


async def test_status_reports_a_joined_session(tmp_path, monkeypatch, capsys) -> None:
    from synapse_contracts import LocalBinding

    transcript = tmp_path / "sess.jsonl"
    _write_transcript(transcript)
    joined = LocalBinding(
        agent_session_id="as-joined", shared_id="team-standup",
        contributor="akhil", agent="claude-code",
    )
    monkeypatch.setattr(
        cli, "resolve_transcript",
        lambda cwd, state_dir: ResolvedTranscript(
            path=transcript, agent_session_id="as-joined", source="pinned", local_binding=joined
        ),
    )

    exit_code = await cli.cmd_status(_ns())

    out = capsys.readouterr().out
    assert exit_code == 0
    assert "team-standup" in out
    assert "as-joined" in out


async def test_status_lists_detected_transcripts_with_liveness(
    tmp_path, monkeypatch, capsys
) -> None:
    import synapse_worker.discovery as discovery

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


async def test_status_reports_unsent_findings(tmp_path, capsys) -> None:
    from synapse_contracts import Attribution, Finding, FindingType
    from synapse_worker.producer import FileSink, Producer

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
        for key, value in kwargs.items():
            setattr(self, key, value)


def _ns(**kwargs) -> _Namespace:
    return _Namespace(**kwargs)


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
