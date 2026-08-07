"""The Claude Code awareness pack, and putting it where Claude Code looks.

`packs/claude-code/` is a shipped artifact: a skill (signal ④, the relevance
trigger) and `commands/synapse/` (not a signal — slash commands are things a
human types). This module ships it inside the wheel and installs it into
`~/.claude/`.

Two decisions worth keeping written down:

**Why installing lives here and not in `install.sh`.** Both installers used to
copy the pack in a phase called P5; both were rewritten to install-only, and
P5 went with them. That is deliberate — this CLI's own contract is "Install
never configures; configure never starts" — so the pack moved to where
`register_mcp` already sits, under the comment "a configure-time concern,
never an install-time one".

**Why the pack ships in the wheel.** The audience for `synapse configure` is
people who did not clone this repo. Resolving the pack relative to the source
tree would work on developer machines and nowhere else, which is precisely
backwards.

The install rules are P5's, carried over unchanged, because each one is a
promise about someone else's files: idempotent; anything already there is left
alone unless asked; and on `--update` it is moved aside with a timestamp,
never deleted. What sits at the destination may be a skill the operator edited
by hand.
"""

from __future__ import annotations

import argparse
import os
import shutil
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

# Every kind Claude Code reads out of ~/.claude. Iterated rather than named
# one by one so an `agents/` directory landing in the pack later needs no
# edit here — the same glob-over-what-ships property P5 had.
PACK_KINDS = ("skills", "commands", "agents")

INSTALLED, SKIPPED, REPLACED = "installed", "skipped", "replaced"


@dataclass
class PackAction:
    outcome: str          # installed | skipped | replaced
    kind: str             # skills | commands | agents
    name: str             # the entry's own directory/file name
    detail: str = ""      # where a replaced entry was moved to

    def line(self) -> str:
        if self.outcome == SKIPPED:
            return (f"  pack      {self.kind}/{self.name} already installed — "
                    f"left alone (--update to replace)")
        if self.outcome == REPLACED:
            return (f"  pack      {self.kind}/{self.name} replaced — previous "
                    f"moved to {self.detail}")
        return f"  pack      {self.kind}/{self.name} installed"


class PackNotFound(RuntimeError):
    """No pack to install — neither bundled in the wheel nor in a checkout."""


def source_dir() -> Path:
    """Where the pack's contents actually are.

    The bundled copy first (`force-include` puts it at `synapse_cli/pack`),
    then the repo checkout, so a working tree tests the files it is editing
    rather than a stale copy installed into a venv.
    """
    bundled = Path(__file__).resolve().parent / "pack"
    if (bundled / "skills").is_dir():
        return bundled
    for parent in Path(__file__).resolve().parents:
        candidate = parent / "packs" / "claude-code"
        if (candidate / "skills").is_dir():
            return candidate
    raise PackNotFound(
        "the awareness pack is neither bundled with this install nor in a "
        "checkout above it. Reinstall synapse-cli, or copy "
        "packs/claude-code/ by hand — see that directory's INSTALL.md.")


def claude_home() -> Path:
    """Where Claude Code keeps skills and commands.

    `CLAUDE_CONFIG_DIR` first, because Claude Code itself honours it — a
    machine that moved its config dir must not have a pack installed into a
    `~/.claude` nothing reads. It is also the seam that keeps tests off a real
    home directory: `SYNAPSE_HOME` redirects everything else this CLI writes,
    but it has no bearing on `Path.home()`.
    """
    override = os.environ.get("CLAUDE_CONFIG_DIR")
    return Path(override) if override else Path.home() / ".claude"


def install(dest_root: Path | None = None, *, update: bool = False,
            source: Path | None = None,
            stamp: str | None = None) -> list[PackAction]:
    """Copy the pack into `dest_root` (default `~/.claude`).

    Returns what it did, one entry per skill/command, so the caller can print
    it. Never raises on an entry that is already present — that is the normal
    second run, not an error.
    """
    src_root = source if source is not None else source_dir()
    dest_root = claude_home() if dest_root is None else dest_root
    stamp = stamp or datetime.now().strftime("%Y%m%d%H%M%S")

    actions: list[PackAction] = []
    for kind in PACK_KINDS:
        src = src_root / kind
        if not src.is_dir():
            continue
        dest = dest_root / kind
        dest.mkdir(parents=True, exist_ok=True)
        for entry in sorted(src.iterdir()):
            target = dest / entry.name
            if target.exists() and not update:
                actions.append(PackAction(SKIPPED, kind, entry.name))
                continue
            detail = ""
            if target.exists():
                # Move aside, never delete. `--update` is documented as a
                # refresh, and an unprompted rm -rf under that description is
                # a surprise nobody consented to.
                backup = dest / f"{entry.name}.synapse-bak.{stamp}"
                target.rename(backup)
                detail = str(backup)
            if entry.is_dir():
                shutil.copytree(entry, target)
            else:
                shutil.copy2(entry, target)
            actions.append(PackAction(
                REPLACED if detail else INSTALLED, kind, entry.name, detail))
    return actions


def install_and_report(*, update: bool = False) -> int:
    """`install()` plus the printing, shared by `configure` and `pack install`.

    A pack that cannot be found is reported and shrugged off, never fatal:
    `configure`'s job is the config file, and failing the whole guided setup
    over an optional Claude Code convenience would be a bad trade.
    """
    try:
        actions = install(update=update)
    except PackNotFound as exc:
        print(f"  pack      not installed — {exc}")
        return 1
    for action in actions:
        print(action.line())
    if any(a.outcome in (INSTALLED, REPLACED) for a in actions):
        print("  pack      start a new Claude Code session to pick it up "
              "(`/synapse:health` checks this machine)")
    return 0


def cmd_pack(args: argparse.Namespace) -> int:
    return install_and_report(update=args.update)


def add_parser(sub: argparse._SubParsersAction) -> None:
    parser = sub.add_parser(
        "pack", help="install the Claude Code awareness pack into ~/.claude")
    parser.add_argument(
        "--update", action="store_true",
        help="replace entries that are already installed (the previous copy "
             "is moved aside, never deleted). Without this, anything already "
             "there is left alone — which is what you want on a re-run and "
             "not what you want after upgrading Synapse.")
    parser.set_defaults(func=cmd_pack)
