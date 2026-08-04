"""synapse-orchestrator CLI — argument parsing and wiring, without a real server.

`mcp.run()` opens a real socket and blocks forever serving requests, so every
test replaces it with a stub that just records how it was called. That is the
one thing worth mocking here: everything else (argument parsing, settings
assignment, exit code) is real.
"""

from __future__ import annotations

import synapse_orchestrator.cli as cli
from synapse_orchestrator.server import mcp


def test_defaults_to_localhost_8787(monkeypatch) -> None:
    calls = []
    monkeypatch.setattr(mcp, "run", lambda **kw: calls.append(kw))

    exit_code = cli.main([])

    assert exit_code == 0
    assert mcp.settings.host == "127.0.0.1"
    assert mcp.settings.port == 8787
    assert calls == [{"transport": "streamable-http"}]


def test_host_and_port_are_applied_to_the_server(monkeypatch) -> None:
    monkeypatch.setattr(mcp, "run", lambda **kw: None)

    cli.main(["--host", "0.0.0.0", "--port", "9999"])

    assert mcp.settings.host == "0.0.0.0"
    assert mcp.settings.port == 9999


def test_transport_is_streamable_http_never_stdio(monkeypatch) -> None:
    """ADR 0001: stdio spawns one server per client, which would give one
    Orchestrator per Agent and dissolve the single-egress property."""
    calls = []
    monkeypatch.setattr(mcp, "run", lambda **kw: calls.append(kw))

    cli.main([])

    assert calls[0]["transport"] == "streamable-http"
    assert calls[0]["transport"] != "stdio"


def test_verbose_flag_is_accepted(monkeypatch) -> None:
    monkeypatch.setattr(mcp, "run", lambda **kw: None)

    exit_code = cli.main(["-v"])

    assert exit_code == 0


def test_unknown_flag_exits_nonzero(monkeypatch) -> None:
    monkeypatch.setattr(mcp, "run", lambda **kw: None)

    try:
        cli.main(["--not-a-real-flag"])
        raised = False
    except SystemExit as exc:
        raised = True
        assert exc.code != 0
    assert raised, "argparse should reject an unknown flag"
