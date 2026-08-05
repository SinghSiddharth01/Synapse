"""synapse-orchestrator CLI — argument parsing and wiring, without a real server.

`uvicorn.run()` opens a real socket and blocks forever serving requests, so
every serve-path test replaces it with a stub that just records how it was
called. That is the one thing worth mocking here: everything else (argument
parsing, Relay/binding wiring, exit code) is real.
"""

from __future__ import annotations

import synapse_orchestrator.cli as cli


def test_defaults_to_localhost_8787(monkeypatch, tmp_path) -> None:
    calls = []
    monkeypatch.setattr(cli.uvicorn, "run", lambda app, **kw: calls.append(kw))

    exit_code = cli.main(["--state-dir", str(tmp_path)])

    assert exit_code == 0
    assert calls == [{"host": "127.0.0.1", "port": 8787}]


def test_host_and_port_are_applied_to_the_server(monkeypatch, tmp_path) -> None:
    calls = []
    monkeypatch.setattr(cli.uvicorn, "run", lambda app, **kw: calls.append(kw))

    cli.main(["--host", "0.0.0.0", "--port", "9999", "--state-dir", str(tmp_path)])

    assert calls == [{"host": "0.0.0.0", "port": 9999}]


def test_transport_is_http_via_uvicorn_never_stdio(monkeypatch, tmp_path) -> None:
    """ADR 0001: stdio spawns one server process per client, which would give
    one Orchestrator per Agent and dissolve the single-egress property. This
    CLI has no stdio code path at all — the app is always served over HTTP by
    uvicorn."""
    calls = []
    monkeypatch.setattr(cli.uvicorn, "run", lambda app, **kw: calls.append(kw))

    cli.main(["--state-dir", str(tmp_path)])

    assert len(calls) == 1
    assert "host" in calls[0] and "port" in calls[0]  # an HTTP bind, not a transport kwarg


def test_verbose_flag_is_accepted(monkeypatch, tmp_path) -> None:
    monkeypatch.setattr(cli.uvicorn, "run", lambda app, **kw: None)

    exit_code = cli.main(["-v", "--state-dir", str(tmp_path)])

    assert exit_code == 0


def test_unknown_flag_exits_nonzero(monkeypatch, tmp_path) -> None:
    monkeypatch.setattr(cli.uvicorn, "run", lambda app, **kw: None)

    try:
        cli.main(["--not-a-real-flag"])
        raised = False
    except SystemExit as exc:
        raised = True
        assert exc.code != 0
    assert raised, "argparse should reject an unknown flag"


def test_serve_falls_back_to_unbound_session_with_no_binding(monkeypatch, tmp_path, capsys) -> None:
    monkeypatch.setattr(cli.uvicorn, "run", lambda app, **kw: None)

    cli.main(["--state-dir", str(tmp_path)])

    assert "session: unbound" in capsys.readouterr().out


def test_serve_uses_the_joined_session_when_a_binding_exists(monkeypatch, tmp_path, capsys) -> None:
    from datetime import datetime, timezone

    from synapse_contracts.binding import SessionBinding, write_binding

    write_binding(
        tmp_path / "bindings" / "claude-code.json",
        SessionBinding(
            agent_session_id="as-1",
            shared_id="sh-joined",
            contributor="aditya",
            agent="claude-code",
            transcript_path="/tmp/t.jsonl",
            pinned_at=datetime(2026, 8, 4, tzinfo=timezone.utc),
        ),
    )
    monkeypatch.setattr(cli.uvicorn, "run", lambda app, **kw: None)

    cli.main(["--state-dir", str(tmp_path)])

    assert "session: sh-joined" in capsys.readouterr().out


def test_resync_with_nothing_pending_never_touches_the_network(tmp_path, capsys) -> None:
    """No findings were ever recorded, so `_all_findings()` is empty and Relay's
    own short-circuit means `resync` makes zero HTTP calls — the CLI test stays
    fully offline without needing to mock a transport."""
    exit_code = cli.main(["--state-dir", str(tmp_path), "resync"])

    assert exit_code == 0
    out = capsys.readouterr().out
    assert "re-pushed 0 finding(s)" in out
    assert "unbound" in out


def test_resync_reports_the_joined_session(tmp_path, capsys) -> None:
    from datetime import datetime, timezone

    from synapse_contracts.binding import SessionBinding, write_binding

    write_binding(
        tmp_path / "bindings" / "claude-code.json",
        SessionBinding(
            agent_session_id="as-1",
            shared_id="sh-joined",
            contributor="aditya",
            agent="claude-code",
            transcript_path="/tmp/t.jsonl",
            pinned_at=datetime(2026, 8, 4, tzinfo=timezone.utc),
        ),
    )

    exit_code = cli.main(["--state-dir", str(tmp_path), "resync"])

    assert exit_code == 0
    assert "sh-joined" in capsys.readouterr().out
