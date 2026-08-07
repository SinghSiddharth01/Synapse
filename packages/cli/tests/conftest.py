"""Keep every test in this package off the developer's real home directory.

`test_config_cmd.py` already redirects `SYNAPSE_HOME`, which covers everything
the CLI writes *for itself* — the config file and the state dir. It has no
bearing on `Path.home()`, so the moment `synapse configure` learned to install
the awareness pack into `~/.claude`, running the suite started writing into the
developer's actual Claude Code config. That is how this fixture came to exist:
not defensively, but because it happened.

Autouse and package-wide rather than per-test, because the failure is silent —
a test that quietly installs into a real `~/.claude` still passes.
"""

from __future__ import annotations

import pytest


@pytest.fixture(autouse=True)
def _claude_config_dir(tmp_path, monkeypatch):
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path / "dot-claude"))
    yield
