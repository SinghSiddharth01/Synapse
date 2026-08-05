"""Plan A.3's parity test, finally executable now CodexSource exists:

    "both adapters produce the same AgentEvent shape from equivalent input"

Everything downstream of a Source adapter is agent-blind (CONTEXT.md: "Agent
... Detected, never configured"); this is what makes that claim more than an
assertion. Equivalent conversational content — a user turn, an assistant
turn, a tool call, its result, a thinking/reasoning aside — fed through each
adapter in its own dialect must come out as the same sequence of
(role, kind, tool_name-presence) AgentEvents, even though the two on-disk
formats (Claude Code's block-list-per-record vs. Codex's
one-item-per-adjacently-tagged-line) do not look alike at all.
"""

from __future__ import annotations

import json

from synapse_worker.sources.claude_code import ClaudeCodeSource
from synapse_worker.sources.codex import CodexSource

CC_TS = "2026-08-05T09:12:00.000Z"
CODEX_TS = "2026-08-05T09:12:00Z"


def _claude_code_events():
    source = ClaudeCodeSource()

    def cc_line(**kwargs) -> str:
        base = {"sessionId": "cc-sess-1", "timestamp": CC_TS, "cwd": "/repo", "gitBranch": "main"}
        return json.dumps({**base, **kwargs})

    events = []
    events += source.parse_line(
        cc_line(type="user", message={"content": [{"type": "text", "text": "Add retry logic to the fetch client"}]})
    )
    events += source.parse_line(
        cc_line(
            type="assistant",
            message={
                "content": [
                    {"type": "thinking", "thinking": "exponential backoff is the standard move here"},
                    {"type": "text", "text": "I'll wrap the fetch call in a retry helper."},
                    {"type": "tool_use", "id": "t1", "name": "shell", "input": {"command": "pytest"}},
                ]
            },
        )
    )
    events += source.parse_line(
        cc_line(type="user", message={"content": [{"type": "tool_result", "tool_use_id": "t1", "content": "3 passed in 1.20s"}]})
    )
    return events


def _codex_events():
    source = CodexSource()

    def codex_line(item_type: str, payload: dict, ordinal: int) -> str:
        return json.dumps({"timestamp": CODEX_TS, "ordinal": ordinal, "type": item_type, "payload": payload})

    events = []
    events += source.parse_line(
        codex_line(
            "session_meta",
            {
                "id": "codex-sess-1",
                "session_id": "codex-sess-1",
                "timestamp": CODEX_TS,
                "cwd": "/repo",
                "originator": "codex_cli_rs",
                "cli_version": "0.146.1",
                "source": "cli",
                "git": {"branch": "main"},
            },
            ordinal=0,
        )
    )
    events += source.parse_line(
        codex_line(
            "response_item",
            {"type": "message", "role": "user", "content": [{"type": "input_text", "text": "Add retry logic to the fetch client"}]},
            ordinal=1,
        )
    )
    events += source.parse_line(
        codex_line(
            "response_item",
            {
                "type": "reasoning",
                "summary": [{"type": "summary_text", "text": "exponential backoff is the standard move here"}],
            },
            ordinal=2,
        )
    )
    events += source.parse_line(
        codex_line(
            "response_item",
            {"type": "message", "role": "assistant", "content": [{"type": "output_text", "text": "I'll wrap the fetch call in a retry helper."}]},
            ordinal=3,
        )
    )
    events += source.parse_line(
        codex_line(
            "response_item",
            {"type": "function_call", "name": "shell", "arguments": '{"command": "pytest"}', "call_id": "call_1"},
            ordinal=4,
        )
    )
    events += source.parse_line(
        codex_line(
            "response_item",
            {"type": "function_call_output", "call_id": "call_1", "output": "3 passed in 1.20s"},
            ordinal=5,
        )
    )
    return events


def test_both_adapters_produce_the_same_agent_event_shape_from_equivalent_input() -> None:
    claude_events = _claude_code_events()
    codex_events = _codex_events()

    claude_shape = [(e.role, e.kind, e.tool_name is not None) for e in claude_events]
    codex_shape = [(e.role, e.kind, e.tool_name is not None) for e in codex_events]

    assert claude_shape == codex_shape == [
        ("user", "text", False),
        ("assistant", "thinking", False),
        ("assistant", "text", False),
        ("assistant", "tool_use", True),
        ("user", "tool_result", True),
    ]


def test_both_adapters_resolve_the_tool_name_onto_the_result_not_just_the_call() -> None:
    claude_events = _claude_code_events()
    codex_events = _codex_events()

    claude_call, claude_result = claude_events[-2], claude_events[-1]
    codex_call, codex_result = codex_events[-2], codex_events[-1]

    assert claude_call.tool_name == codex_call.tool_name == "shell"
    assert claude_result.tool_name == codex_result.tool_name == "shell"


def test_both_adapters_carry_agent_session_id_cwd_and_git_branch_on_every_event() -> None:
    for events, expected_cwd, expected_branch in (
        (_claude_code_events(), "/repo", "main"),
        (_codex_events(), "/repo", "main"),
    ):
        assert events, "fixture produced no events"
        for event in events:
            assert event.cwd == expected_cwd
            assert event.git_branch == expected_branch
            assert event.agent_session_id


def test_the_two_adapters_tag_events_with_different_agent_session_ids() -> None:
    """Same conversational shape, but the session ids must not collide --
    they come from each agent's own transcript, not a shared namespace."""
    claude_events = _claude_code_events()
    codex_events = _codex_events()

    assert claude_events[0].agent_session_id == "cc-sess-1"
    assert codex_events[0].agent_session_id == "codex-sess-1"
