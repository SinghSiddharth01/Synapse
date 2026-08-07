"""`synapse up` resolution: flags > config, loud actionable errors, and the
worker/distiller matrix."""

from __future__ import annotations

import argparse

import pytest

from synapse_cli import up
from synapse_contracts import userconfig


@pytest.fixture(autouse=True)
def _home(tmp_path, monkeypatch):
    monkeypatch.setenv("SYNAPSE_HOME", str(tmp_path / "home"))
    yield


def _args(**overrides) -> argparse.Namespace:
    base = dict(service_url=None, contributor=None, distiller=None,
                claude_model=None, no_worker=False)
    base.update(overrides)
    return argparse.Namespace(**base)


def test_up_rejects_session_flags():
    """`up` starts PROCESSES; sessions belong to the MCP tools. The mirror of
    the installer's refused-flags test, one layer up: --shared-id/--purpose
    were how session identity leaked into the runtime layer, and argparse
    rejecting them outright is the decoupling made permanent."""
    from synapse_cli.main import build_parser
    parser = build_parser()
    for flag in ("--shared-id", "--purpose"):
        with pytest.raises(SystemExit):
            parser.parse_args(["up", flag, "x"])


def test_up_has_no_session_bootstrap():
    """The whole create/adopt/bind block is gone, not just unreachable."""
    assert not hasattr(up, "_bootstrap_session")


def test_no_service_url_is_actionable():
    with pytest.raises(SystemExit, match="synapse config set service.url"):
        up._resolve(_args())


def test_config_supplies_everything():
    userconfig.set_value("service.url", "http://h:8899")
    userconfig.set_value("user.contributor", "akhil")
    userconfig.set_value("client.distiller", "claude-cli")
    resolved = up._resolve(_args())
    assert resolved == {"service_url": "http://h:8899",
                        "contributor": "akhil",
                        "distiller": "claude-cli", "worker": "on"}


def test_flags_beat_config():
    userconfig.set_value("service.url", "http://config:8899")
    userconfig.set_value("client.distiller", "npu")
    resolved = up._resolve(_args(service_url="http://flag:8899/",
                                 distiller="listen", no_worker=True))
    assert resolved["service_url"] == "http://flag:8899"
    assert resolved["distiller"] == "listen"
    assert resolved["worker"] == "off"


def test_unset_distiller_defaults_to_listen():
    userconfig.set_value("service.url", "http://h:8899")
    assert up._resolve(_args())["distiller"] == "listen"


def test_bad_configured_distiller_is_loud():
    userconfig.set_value("service.url", "http://h:8899")
    # written by hand into the file, bypassing the CLI's validation
    userconfig.set_value("client.distiller", "gpt")
    with pytest.raises(SystemExit, match="client.distiller"):
        up._resolve(_args())
