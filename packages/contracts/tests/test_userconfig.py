"""The user-level config store: git-config semantics, atomic writes,
Windows-path escaping, graceful absence."""

from __future__ import annotations

import pytest

from synapse_contracts import userconfig


@pytest.fixture(autouse=True)
def _home(tmp_path, monkeypatch):
    monkeypatch.setenv("SYNAPSE_HOME", str(tmp_path / "home"))
    yield


def test_missing_file_is_empty_config():
    assert userconfig.load() == {}
    assert userconfig.get("service.url") is None


def test_set_get_roundtrip():
    userconfig.set_value("service.url", "http://192.168.4.44:8899")
    assert userconfig.get("service.url") == "http://192.168.4.44:8899"
    userconfig.set_value("user.contributor", "akhil")
    # both survive together
    assert userconfig.load() == {
        "service.url": "http://192.168.4.44:8899",
        "user.contributor": "akhil",
    }


def test_set_overwrites_and_unset_removes():
    userconfig.set_value("service.url", "http://a:1")
    userconfig.set_value("service.url", "http://b:2")
    assert userconfig.get("service.url") == "http://b:2"
    assert userconfig.unset("service.url") is True
    assert userconfig.unset("service.url") is False
    assert userconfig.get("service.url") is None


def test_windows_paths_and_quotes_survive():
    path = 'C:\\Users\\akhil\\keys "pool".txt'
    userconfig.set_value("server.keys_file", path)
    assert userconfig.get("server.keys_file") == path


def test_key_shape_is_enforced():
    for bad in ("nodot", ".lead", "trail."):
        with pytest.raises(ValueError):
            userconfig.set_value(bad, "x")


def test_corrupt_file_reads_as_empty():
    path = userconfig.config_path()
    path.parent.mkdir(parents=True)
    path.write_text("this is [not toml", encoding="utf-8")
    assert userconfig.load() == {}


def test_state_dir_env_override(monkeypatch, tmp_path):
    assert userconfig.state_dir() == userconfig.synapse_home() / "state"
    monkeypatch.setenv("SYNAPSE_STATE_DIR", str(tmp_path / "elsewhere"))
    assert userconfig.state_dir() == tmp_path / "elsewhere"
