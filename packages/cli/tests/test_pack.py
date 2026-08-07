"""Installing the awareness pack into ~/.claude.

Every rule pinned here is a promise about files this tool does not own. The
destination is the operator's own `~/.claude/skills/` — the same directory
their hand-written skills live in — so "idempotent" and "never deletes" are
correctness properties, not politeness.
"""

from __future__ import annotations

import argparse

import pytest

from synapse_cli import config_cmd, main, pack


def _configure_args(**overrides) -> argparse.Namespace:
    defaults = dict(service_url="http://svc:8899", contributor="sid",
                    distiller="listen", project=None, no_pack=False, yes=True)
    return argparse.Namespace(**{**defaults, **overrides})


@pytest.fixture
def src(tmp_path):
    """A miniature pack with the shape the real one has: a skill directory,
    a namespaced command directory, and no agents/ at all."""
    root = tmp_path / "packsrc"
    (root / "skills" / "synapse-shared-memory").mkdir(parents=True)
    (root / "skills" / "synapse-shared-memory" / "SKILL.md").write_text("skill v1")
    (root / "commands" / "synapse").mkdir(parents=True)
    (root / "commands" / "synapse" / "health.md").write_text("command v1")
    return root


# ---------------------------------------------------------------------------
# Finding the pack at all.
# ---------------------------------------------------------------------------

def test_source_dir_finds_the_pack_and_the_namespaced_command():
    """The audience for `configure` is people who did not clone this repo, so
    this must resolve from an installed wheel — `force-include` puts the pack
    at `synapse_cli/pack`, and the checkout is only the developer fallback."""
    found = pack.source_dir()
    assert found.is_dir()
    assert (found / "skills" / "synapse-shared-memory" / "SKILL.md").is_file()
    # The directory IS the namespace: this path is what makes it
    # `/synapse:health` rather than `/health`.
    assert (found / "commands" / "synapse" / "health.md").is_file()


# ---------------------------------------------------------------------------
# The install rules, carried over from the installers' P5.
# ---------------------------------------------------------------------------

def test_install_copies_skills_and_commands_preserving_the_namespace(src, tmp_path):
    dest = tmp_path / "dotclaude"
    actions = pack.install(dest, source=src)

    assert (dest / "skills" / "synapse-shared-memory" / "SKILL.md").read_text() == "skill v1"
    assert (dest / "commands" / "synapse" / "health.md").read_text() == "command v1"
    assert {a.outcome for a in actions} == {pack.INSTALLED}
    assert {a.kind for a in actions} == {"skills", "commands"}


def test_a_second_install_changes_nothing_and_says_so(src, tmp_path):
    """The common case: `configure` run twice, or run after an earlier
    install. It must not silently overwrite, and it must not error."""
    dest = tmp_path / "dotclaude"
    pack.install(dest, source=src)
    (dest / "skills" / "synapse-shared-memory" / "SKILL.md").write_text("HAND EDITED")

    actions = pack.install(dest, source=src)

    assert {a.outcome for a in actions} == {pack.SKIPPED}
    assert (dest / "skills" / "synapse-shared-memory" / "SKILL.md").read_text() == "HAND EDITED"
    assert "--update to replace" in actions[0].line()


def test_update_moves_the_previous_aside_rather_than_deleting_it(src, tmp_path):
    """What sits at the destination may be a skill the operator edited. An
    update refreshes it; it does not destroy the only copy of their work."""
    dest = tmp_path / "dotclaude"
    pack.install(dest, source=src)
    edited = dest / "skills" / "synapse-shared-memory" / "SKILL.md"
    edited.write_text("HAND EDITED")

    actions = pack.install(dest, source=src, update=True, stamp="20260806T2100")

    assert edited.read_text() == "skill v1", "update must actually refresh"
    backup = dest / "skills" / "synapse-shared-memory.synapse-bak.20260806T2100"
    assert backup.is_dir(), "the previous version must survive somewhere"
    assert (backup / "SKILL.md").read_text() == "HAND EDITED"
    replaced = [a for a in actions if a.outcome == pack.REPLACED]
    assert replaced and str(backup) in replaced[0].detail


def test_install_skips_a_kind_the_pack_does_not_ship(src, tmp_path):
    """PACK_KINDS carries `agents` so one landing later needs no edit here.
    Until it does, no empty `~/.claude/agents/` may be created — a directory
    this tool made and never fills is litter in someone else's config."""
    dest = tmp_path / "dotclaude"
    pack.install(dest, source=src)
    assert not (dest / "agents").exists()


def test_install_does_not_touch_settings_json_or_the_hook(src, tmp_path):
    """The hook is per-project by construction — settings-snippet.json points
    at $CLAUDE_PROJECT_DIR — and rewriting a settings.json this tool does not
    own is an overwrite risk. Both installers refused to; so does this."""
    dest = tmp_path / "dotclaude"
    settings = dest / "settings.json"
    dest.mkdir()
    settings.write_text('{"mine": true}')

    pack.install(dest, source=src)

    assert settings.read_text() == '{"mine": true}'
    assert not (dest / "hooks").exists()
    assert not (dest / "synapse-pack").exists()


def test_install_creates_the_destination_when_claude_has_never_run(src, tmp_path):
    dest = tmp_path / "never-existed" / ".claude"
    pack.install(dest, source=src)
    assert (dest / "commands" / "synapse" / "health.md").is_file()


# ---------------------------------------------------------------------------
# Where it is called from. `configure`, never install — this CLI's contract is
# "Install never configures; configure never starts."
# ---------------------------------------------------------------------------

def test_configure_installs_the_pack(monkeypatch, tmp_path):
    called: list[bool] = []
    monkeypatch.setattr(pack, "install_and_report",
                        lambda **kw: called.append(True) or 0)
    monkeypatch.setattr(config_cmd.shutil, "which", lambda name: "/usr/bin/claude")
    monkeypatch.setattr(config_cmd.userconfig, "load", lambda: {})
    monkeypatch.setattr(config_cmd.userconfig, "set_value", lambda *a: None)
    monkeypatch.setattr(config_cmd.userconfig, "config_path", lambda: tmp_path / "c.toml")
    monkeypatch.setattr(config_cmd, "_ping_and_report", lambda url: None)

    config_cmd.cmd_configure(_configure_args())
    assert called == [True]


def test_configure_can_be_told_not_to(monkeypatch, tmp_path):
    monkeypatch.setattr(pack, "install_and_report",
                        lambda **kw: pytest.fail("--no-pack must not install"))
    monkeypatch.setattr(config_cmd.shutil, "which", lambda name: "/usr/bin/claude")
    monkeypatch.setattr(config_cmd.userconfig, "load", lambda: {})
    monkeypatch.setattr(config_cmd.userconfig, "set_value", lambda *a: None)
    monkeypatch.setattr(config_cmd.userconfig, "config_path", lambda: tmp_path / "c.toml")
    monkeypatch.setattr(config_cmd, "_ping_and_report", lambda url: None)

    config_cmd.cmd_configure(_configure_args(no_pack=True))


def test_configure_does_not_create_a_claude_home_that_does_not_exist(
        monkeypatch, tmp_path):
    """A machine with no Claude Code gets no ~/.claude invented for it. The
    pack is a Claude Code convenience, not part of being configured."""
    monkeypatch.setattr(pack, "install_and_report",
                        lambda **kw: pytest.fail("nothing to install into"))
    monkeypatch.setattr(config_cmd.shutil, "which", lambda name: None)
    monkeypatch.setattr(pack, "claude_home", lambda: tmp_path / "absent")
    monkeypatch.setattr(config_cmd.userconfig, "load", lambda: {})
    monkeypatch.setattr(config_cmd.userconfig, "set_value", lambda *a: None)
    monkeypatch.setattr(config_cmd.userconfig, "config_path", lambda: tmp_path / "c.toml")
    monkeypatch.setattr(config_cmd, "_ping_and_report", lambda url: None)

    config_cmd.cmd_configure(_configure_args())


def test_a_missing_pack_does_not_fail_the_whole_configure(monkeypatch, capsys):
    """`configure`'s job is the config file. Failing guided setup over an
    optional Claude Code convenience would be a bad trade."""
    def _absent():
        raise pack.PackNotFound("nothing bundled, no checkout above")
    monkeypatch.setattr(pack, "source_dir", _absent)

    assert pack.install_and_report() == 1
    assert "not installed" in capsys.readouterr().out


def test_claude_home_honours_claude_config_dir(monkeypatch, tmp_path):
    """Claude Code itself honours CLAUDE_CONFIG_DIR, so a machine that moved
    its config dir must not get a pack installed into a ~/.claude nothing
    reads. This is also the seam conftest.py uses to keep the suite off a real
    home directory — which it needs, because it did not have it and the suite
    wrote into the developer's own ~/.claude/commands/."""
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path / "elsewhere"))
    assert pack.claude_home() == tmp_path / "elsewhere"

    monkeypatch.delenv("CLAUDE_CONFIG_DIR")
    assert pack.claude_home().name == ".claude"


def test_configure_never_writes_outside_the_redirected_home(monkeypatch, tmp_path):
    """The regression that produced conftest.py, pinned end to end: a real
    `cmd_configure` (nothing about the pack stubbed) must land entirely inside
    CLAUDE_CONFIG_DIR."""
    home = tmp_path / "dot-claude"
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(home))
    monkeypatch.setattr(config_cmd.shutil, "which", lambda name: "/usr/bin/claude")
    monkeypatch.setattr(config_cmd.userconfig, "load", lambda: {})
    monkeypatch.setattr(config_cmd.userconfig, "set_value", lambda *a: None)
    monkeypatch.setattr(config_cmd.userconfig, "config_path", lambda: tmp_path / "c.toml")
    monkeypatch.setattr(config_cmd, "_ping_and_report", lambda url: None)

    config_cmd.cmd_configure(_configure_args())

    assert (home / "commands" / "synapse" / "health.md").is_file()
    assert (home / "skills" / "synapse-shared-memory" / "SKILL.md").is_file()


def test_synapse_pack_is_a_real_subcommand():
    """Needed on its own, not just inside `configure`: upgrading Synapse ships
    new skill and command files, and nothing else would ever refresh them."""
    args = main.build_parser().parse_args(["pack", "--update"])
    assert args.func is pack.cmd_pack
    assert args.update is True
