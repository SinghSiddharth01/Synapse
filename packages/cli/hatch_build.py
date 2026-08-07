"""Wheel builds must carry the awareness pack no matter HOW they are built.

Same defect and same shape as packages/distiller/hatch_build.py: the sdist
force-include in pyproject.toml only runs on the sdist path, so a wheel built
DIRECTLY from the source tree (`uv tool install --from packages/cli`,
`uv build --wheel`) shipped without `synapse_cli/pack`, and `synapse
configure` on such an install reported "pack not installed" (2026-08-06 —
observed alongside the distiller's missing prompt data, which killed the edge
worker outright). Building from a checkout, force-include the canonical
packs/claude-code; building from an sdist, the copy already sits inside src/
and the hook does nothing.
"""

from __future__ import annotations

from pathlib import Path

from hatchling.builders.hooks.plugin.interface import BuildHookInterface


class BundleAwarenessPack(BuildHookInterface):
    def initialize(self, version: str, build_data: dict) -> None:
        pack = Path(self.root).parent.parent / "packs" / "claude-code"
        if not (pack / "skills").is_dir():
            return          # sdist build: src/synapse_cli/pack already exists
        build_data.setdefault("force_include", {})[str(pack)] = "synapse_cli/pack"
