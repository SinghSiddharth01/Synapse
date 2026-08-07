"""Wheel builds must carry config/prompts no matter HOW the wheel is built.

The sdist force-include in pyproject.toml covers the release path: the sdist
copies the repo's canonical config into src/synapse_distiller/data/, and a
wheel built FROM that sdist ships it as ordinary package data. But a wheel
built DIRECTLY from the source tree — `uv tool install --from packages/cli`
resolving the workspace, or `uv build --wheel` — never passes through the
sdist, the force-include never runs, and the result imported fine and died
with PromptPackError at the first distil (2026-08-06: the edge worker crashed
with a raw traceback the moment a session was joined, hours after the install
looked healthy).

This hook closes that path at wheel build time: when the canonical files are
reachable (building from a checkout/workspace), they are force-included into
the wheel via build_data — no copy into the source tree, nothing to
git-ignore. Building from an sdist, ../../config does not exist and the
data/ copy the sdist already placed inside src/ ships as normal package
data, so the hook does nothing.
"""

from __future__ import annotations

from pathlib import Path

from hatchling.builders.hooks.plugin.interface import BuildHookInterface


class BundlePromptData(BuildHookInterface):
    def initialize(self, version: str, build_data: dict) -> None:
        repo_config = Path(self.root).parent.parent / "config"
        if not (repo_config / "prompts").is_dir():
            return          # sdist build: data/ is already inside src/
        force = build_data.setdefault("force_include", {})
        force[str(repo_config / "prompts")] = "synapse_distiller/data/prompts"
        force[str(repo_config / "synapse.toml")] = "synapse_distiller/data/synapse.toml"
