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


class _Tty:
    """Stand-in stdin whose isatty() answers True — enough to open
    cmd_configure's interactive gate without a real terminal."""

    def isatty(self) -> bool:
        return True


def test_configure_ctrl_c_at_a_prompt_exits_quietly(monkeypatch):
    """Ctrl-C at any prompt used to escape main() as a raw KeyboardInterrupt
    traceback. Interrupting a prompt is a normal way to leave, not a crash:
    exit 130 (128+SIGINT), no traceback."""
    monkeypatch.setattr("sys.stdin", _Tty())

    def _interrupt(*_):
        raise KeyboardInterrupt

    monkeypatch.setattr("builtins.input", _interrupt)
    assert main(["configure", "--no-pack"]) == 130


def test_configure_eof_at_a_prompt_means_no_answer(monkeypatch):
    """Ctrl-D is "no answer" — the same as pressing Enter on an empty line —
    so configure finishes on defaults instead of an EOFError traceback."""
    monkeypatch.setattr("sys.stdin", _Tty())
    # The MCP default is now user-scope registration, so "no answer" at that
    # prompt takes it — stub the actual `claude mcp` subprocess call away.
    registered = []
    monkeypatch.setattr(config_cmd, "register_mcp",
                        lambda project=None: registered.append(project) or 0)

    def _eof(*_):
        raise EOFError

    monkeypatch.setattr("builtins.input", _eof)
    assert main(["configure", "--no-pack"]) == 0
    assert userconfig.get("user.contributor") is not None
    assert userconfig.get("client.distiller") in config_cmd.DISTILLER_ARMS


def _interactive_mcp_prompt(monkeypatch, answer: str):
    """Drive configure so ONLY the MCP prompt fires interactively, answering
    it with `answer`; returns the register_mcp calls that resulted."""
    monkeypatch.setattr("sys.stdin", _Tty())
    monkeypatch.setattr(config_cmd.shutil, "which", lambda name: f"/usr/bin/{name}")
    monkeypatch.setattr("builtins.input", lambda *_: answer)
    calls: list = []
    monkeypatch.setattr(config_cmd, "register_mcp",
                        lambda project=None: calls.append(project) or 0)
    assert main(["configure", "--no-pack",
                 "--service-url", "http://h:8899",
                 "--contributor", "sid",
                 "--distiller", "listen"]) == 0
    return calls


def test_configure_mcp_defaults_to_user_scope(monkeypatch):
    """Empty answer at the MCP prompt = the default = USER scope (all
    projects) — registering per project was the old behaviour and is now the
    opt-in, not the default (2026-08-06 decision)."""
    assert _interactive_mcp_prompt(monkeypatch, "") == [None]


def test_configure_mcp_path_registers_project_scope(monkeypatch):
    assert _interactive_mcp_prompt(monkeypatch, "/some/proj") == ["/some/proj"]


def test_configure_mcp_n_skips_registration(monkeypatch):
    assert _interactive_mcp_prompt(monkeypatch, "n") == []


def test_register_mcp_user_scope_is_the_no_arg_form(monkeypatch, capsys):
    """register_mcp() with no project must issue `--scope user` from the
    current directory; register_mcp(dir) must issue `--scope project` in that
    directory — the command that lands in someone's config, pinned."""
    seen: list = []

    class _Done:
        returncode = 0
        stdout = ""
        stderr = ""

    def _run(argv, cwd=None, **kwargs):
        seen.append((argv, cwd))
        return _Done()

    monkeypatch.setattr(config_cmd.shutil, "which", lambda name: "/usr/bin/claude")
    monkeypatch.setattr(config_cmd.subprocess, "run", _run)

    assert config_cmd.register_mcp() == 0
    add = seen[-1][0]
    assert ["--scope", "user"] == add[add.index("--scope"):add.index("--scope") + 2]
    assert seen[-1][1] is None

    seen.clear()
    assert config_cmd.register_mcp("/some/proj") == 0
    add = seen[-1][0]
    assert ["--scope", "project"] == add[add.index("--scope"):add.index("--scope") + 2]
    assert seen[-1][1] == "/some/proj"


def test_parser_has_no_install_time_coupling():
    """Install-vs-configure decoupling, pinned: the CLI must not grow install
    flags, and the installer must not grow configure flags. --shared-id and
    --purpose belong to `up` (a RUNTIME join), never to configure."""
    parser = build_parser()
    for forbidden in ("--shared-id", "--purpose"):
        with pytest.raises(SystemExit):
            parser.parse_args(["configure", forbidden, "x"])
