"""synapse-service CLI — argument parsing and the /debug off switch.

`uvicorn.run()` opens a real socket and serves forever, so every test here
replaces it with a stub that just records how it was called (same pattern as
`packages/orchestrator/tests/test_cli.py`). `build_app` is stubbed too where
a test only cares about what `main()` decided to pass it, not the real app.
"""

from __future__ import annotations

import argparse

import synapse_service.cli as cli


def _stub_build_app(monkeypatch):
    seen: dict = {}

    def fake_build_app(provider, *, debug=True):
        seen["provider"] = provider
        seen["debug"] = debug
        return object()

    monkeypatch.setattr(cli, "build_app", fake_build_app)
    monkeypatch.setattr(cli.uvicorn, "run", lambda app, **kw: seen.setdefault("run", kw))
    return seen


# ---------------------------------------------------------------------------
# _resolve_debug — flag wins over env wins over the enabled-by-default floor
# ---------------------------------------------------------------------------

def test_resolve_debug_defaults_to_enabled(monkeypatch) -> None:
    monkeypatch.delenv("SYNAPSE_SERVICE_DEBUG", raising=False)
    assert cli._resolve_debug(argparse.Namespace(debug=None)) is True


def test_resolve_debug_env_var_disables_when_flag_unset(monkeypatch) -> None:
    monkeypatch.setenv("SYNAPSE_SERVICE_DEBUG", "0")
    assert cli._resolve_debug(argparse.Namespace(debug=None)) is False


def test_resolve_debug_env_var_accepts_false_and_empty(monkeypatch) -> None:
    for value in ("false", "False", ""):
        monkeypatch.setenv("SYNAPSE_SERVICE_DEBUG", value)
        assert cli._resolve_debug(argparse.Namespace(debug=None)) is False


def test_resolve_debug_flag_wins_over_env(monkeypatch) -> None:
    monkeypatch.setenv("SYNAPSE_SERVICE_DEBUG", "0")
    assert cli._resolve_debug(argparse.Namespace(debug=True)) is True


# ---------------------------------------------------------------------------
# main() -- wiring debug into build_app, and the non-localhost warning
# ---------------------------------------------------------------------------

def test_main_enables_debug_by_default(monkeypatch) -> None:
    monkeypatch.delenv("SYNAPSE_SERVICE_DEBUG", raising=False)
    seen = _stub_build_app(monkeypatch)

    exit_code = cli.main([])

    assert exit_code == 0
    assert seen["debug"] is True


def test_main_no_debug_flag_disables_the_dashboard(monkeypatch) -> None:
    seen = _stub_build_app(monkeypatch)

    exit_code = cli.main(["--no-debug"])

    assert exit_code == 0
    assert seen["debug"] is False


def test_main_synapse_service_debug_env_disables_the_dashboard(monkeypatch) -> None:
    monkeypatch.setenv("SYNAPSE_SERVICE_DEBUG", "0")
    seen = _stub_build_app(monkeypatch)

    exit_code = cli.main([])

    assert exit_code == 0
    assert seen["debug"] is False


def test_main_warns_when_debug_is_enabled_on_a_non_local_host(monkeypatch, capsys) -> None:
    _stub_build_app(monkeypatch)

    exit_code = cli.main(["--host", "0.0.0.0"])

    assert exit_code == 0
    assert "WARNING" in capsys.readouterr().err


# ⟨2026-08-06, W1⟩ These two asserted `err == ""`, which said "no /debug
# exposure warning" by saying "nothing on stderr at all". stderr is a shared
# channel and it now also carries the SYNAPSE_SYNTHESIZER banner (the boot-side
# half of decision 008's honesty fix), which fires here because no synthesizer
# is set in a test environment. Narrowed to the property each test is actually
# about — the absence of the EXPOSURE warning — rather than relaxed: an
# emptiness assertion on a shared stream fails for whatever lands there next,
# which is a test that goes red without anything it covers having changed.
def test_main_no_warning_when_debug_is_disabled_on_a_non_local_host(monkeypatch, capsys) -> None:
    _stub_build_app(monkeypatch)

    exit_code = cli.main(["--host", "0.0.0.0", "--no-debug"])

    assert exit_code == 0
    assert "WARNING" not in capsys.readouterr().err


def test_main_no_warning_on_the_default_localhost_host(monkeypatch, capsys) -> None:
    _stub_build_app(monkeypatch)

    exit_code = cli.main([])

    assert exit_code == 0
    assert "WARNING" not in capsys.readouterr().err


def test_main_ctrl_c_during_boot_exits_quietly(monkeypatch) -> None:
    """Ctrl-C before uvicorn installs its own SIGINT handler (i.e. during
    _provider()'s boot preflights) used to escape as a raw traceback.
    Exit 130 (128+SIGINT) instead."""
    seen = _stub_build_app(monkeypatch)

    def _interrupt(app, **kw):
        raise KeyboardInterrupt

    monkeypatch.setattr(cli.uvicorn, "run", _interrupt)
    assert cli.main([]) == 130
