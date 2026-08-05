"""The orchestrator is currently a transport shell only.

No `start`/`status` prompt-or-tool tests here anymore — that surface was
removed for contradicting Plan D Task D.3 ("there is no attach(shared_id)").
See server.py's module docstring. These tests just pin the honest state: named
correctly, nothing registered, ready for Task D.3's real query/contribute once
the Synapse Service exists.
"""

from __future__ import annotations

import synapse_orchestrator.server as server


def test_server_is_named_synapse() -> None:
    assert server.mcp.name == "synapse"


def test_no_tools_or_prompts_are_registered_yet() -> None:
    """Deliberately empty. A registered tool/prompt here would need to be a
    real, working capability — not a placeholder — per Plan D Task D.3."""
    assert server.mcp._prompt_manager._prompts == {}
    assert server.mcp._tool_manager._tools == {}


from synapse_orchestrator.server import SENTINEL, create_mcp


def test_factory_carries_custom_instructions():
    server = create_mcp("Shared Session: fec decode. 3 findings.")
    assert server.instructions == "Shared Session: fec decode. 3 findings."


def test_default_instructions_contain_the_sentinel():
    assert SENTINEL in create_mcp().instructions
