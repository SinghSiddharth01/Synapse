"""`synapse config` / `synapse configure`: the git-style surface, the ping on
service.url set, and non-interactive safety (no prompt may ever hang CI)."""

from __future__ import annotations

import pytest

from synapse_cli import config_cmd
from synapse_cli.main import build_parser, main
from synapse_contracts import userconfig


@pytest.fixture(autouse=True)
def _home(tmp_path, monkeypatch):
    monkeypatch.setenv("SYNAPSE_HOME", str(tmp_path / "home"))
    # Never let a test reach the network for the ping.
    monkeypatch.setattr(config_cmd, "ping_service",
                        lambda url, timeout=5.0: (True, "HTTP 200 in 1ms"))
    yield


def test_set_get_list_unset_exit_codes(capsys):
    assert main(["config", "set", "service.url", "http://h:8899/"]) == 0
    # trailing slash normalised away, like git remotes
    assert userconfig.get("service.url") == "http://h:8899"
    assert main(["config", "get", "service.url"]) == 0
    assert capsys.readouterr().out.splitlines()[-1] == "http://h:8899"
    assert main(["config", "get", "service.missing"]) == 1
    assert main(["config", "list"]) == 0
    assert main(["config", "unset", "service.url"]) == 0
    assert main(["config", "unset", "service.url"]) == 1


def test_set_service_url_reports_ping(monkeypatch, capsys):
    monkeypatch.setattr(config_cmd, "ping_service",
                        lambda url, timeout=5.0: (False, "refused"))
    assert main(["config", "set", "service.url", "http://dead:1"]) == 0
    out = capsys.readouterr().out
    assert "NOT reachable" in out
    # unreachable is WARNED about but still SAVED — configuring before the
    # host has started the server is the normal team order of operations
    assert userconfig.get("service.url") == "http://dead:1"


def test_set_rejects_bad_distiller_arm():
    assert main(["config", "set", "client.distiller", "gpt"]) == 2
    assert userconfig.get("client.distiller") is None


def test_unknown_key_is_stored_with_a_note(capsys):
    assert main(["config", "set", "future.key", "v"]) == 0
    assert "not a key this CLI reads" in capsys.readouterr().out
    assert userconfig.get("future.key") == "v"


def test_configure_non_interactive_never_prompts(monkeypatch, capsys):
    # stdin is not a TTY under pytest; input() raising proves a prompt fired.
    monkeypatch.setattr("builtins.input",
                        lambda *_: pytest.fail("configure prompted non-interactively"))
    assert main(["configure", "--yes",
                 "--service-url", "http://h:8899",
                 "--contributor", "akhil",
                 "--distiller", "listen"]) == 0
    assert userconfig.get("service.url") == "http://h:8899"
    assert userconfig.get("user.contributor") == "akhil"
    assert userconfig.get("client.distiller") == "listen"


def test_configure_without_url_still_configures_the_rest(capsys):
    assert main(["configure", "--yes", "--contributor", "b",
                 "--distiller", "listen"]) == 0
    out = capsys.readouterr().out
    assert "synapse config set service.url" in out
    assert userconfig.get("user.contributor") == "b"


def test_parser_has_no_install_time_coupling():
    """Install-vs-configure decoupling, pinned: the CLI must not grow install
    flags, and the installer must not grow configure flags. --shared-id and
    --purpose belong to `up` (a RUNTIME join), never to configure."""
    parser = build_parser()
    for forbidden in ("--shared-id", "--purpose"):
        with pytest.raises(SystemExit):
            parser.parse_args(["configure", forbidden, "x"])
