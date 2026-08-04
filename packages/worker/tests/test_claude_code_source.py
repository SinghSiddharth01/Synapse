"""Claude Code adapter tests.

Shapes here are taken from a live 2.9 MB transcript inspected 2026-08-04, not
invented — including the ones that are easy to assume away, like string content
and tool_result arriving under type "user".
"""

from __future__ import annotations

import json

from synapse_worker.sources.claude_code import ClaudeCodeSource

TS = "2026-08-04T09:12:00.000Z"


def line(**kwargs) -> str:
    base = {"sessionId": "sess-1", "timestamp": TS, "cwd": "/repo", "gitBranch": "main"}
    return json.dumps({**base, **kwargs})


def test_parses_assistant_text() -> None:
    source = ClaudeCodeSource()
    events = source.parse_line(
        line(type="assistant", message={"content": [{"type": "text", "text": "hello"}]})
    )

    (event,) = events
    assert (event.role, event.kind, event.content) == ("assistant", "text", "hello")
    assert event.agent_session_id == "sess-1"
    assert event.cwd == "/repo"
    assert event.git_branch == "main"


def test_parses_string_content_not_just_block_lists() -> None:
    """43 of 762 message lines in the real transcript use a bare string. Handling
    only the list shape silently drops user turns."""
    source = ClaudeCodeSource()
    events = source.parse_line(line(type="user", message={"content": "fix the build"}))

    (event,) = events
    assert (event.role, event.kind, event.content) == ("user", "text", "fix the build")


def test_one_line_can_yield_several_events() -> None:
    source = ClaudeCodeSource()
    events = source.parse_line(
        line(
            type="assistant",
            message={
                "content": [
                    {"type": "thinking", "thinking": "considering options"},
                    {"type": "text", "text": "I'll run the tests"},
                    {"type": "tool_use", "id": "t1", "name": "bash", "input": {"command": "pytest"}},
                ]
            },
        )
    )

    assert [e.kind for e in events] == ["thinking", "text", "tool_use"]
    assert events[2].tool_name == "bash"
    assert "pytest" in events[2].content


def test_tool_result_resolves_its_tool_name_from_the_earlier_tool_use() -> None:
    """tool_result carries only tool_use_id, so the name comes from a map."""
    source = ClaudeCodeSource()
    source.parse_line(
        line(
            type="assistant",
            message={"content": [{"type": "tool_use", "id": "t7", "name": "bash", "input": {}}]},
        )
    )
    events = source.parse_line(
        line(
            type="user",
            message={"content": [{"type": "tool_result", "tool_use_id": "t7", "content": "ok"}]},
        )
    )

    (event,) = events
    assert event.kind == "tool_result"
    assert event.role == "user"  # Claude Code records results as the user's turn
    assert event.tool_name == "bash"


def test_bookkeeping_lines_are_skipped() -> None:
    """437 of 1199 lines in the real transcript are not conversation at all."""
    source = ClaudeCodeSource()
    for kind in ("mode", "permission-mode", "file-history-snapshot", "ai-title",
                 "last-prompt", "attachment", "queue-operation", "file-history-delta"):
        assert source.parse_line(line(type=kind)) == []
    assert source.skipped_bookkeeping == 8


def test_sidechain_and_meta_are_skipped() -> None:
    source = ClaudeCodeSource()
    body = {"content": [{"type": "text", "text": "x"}]}

    assert source.parse_line(line(type="assistant", message=body, isSidechain=True)) == []
    assert source.parse_line(line(type="assistant", message=body, isMeta=True)) == []
    assert source.skipped_sidechain == 1
    assert source.skipped_meta == 1


def test_unknown_block_type_is_skipped_not_raised() -> None:
    """The transcript format belongs to someone else's process and will change."""
    source = ClaudeCodeSource()
    events = source.parse_line(
        line(
            type="assistant",
            message={"content": [{"type": "future_block"}, {"type": "text", "text": "kept"}]},
        )
    )

    assert [e.content for e in events] == ["kept"]
    assert source.skipped_unknown_block == 1


def test_malformed_json_is_counted_not_raised() -> None:
    source = ClaudeCodeSource()

    assert source.parse_line("{not json") == []
    assert source.parse_line("") == []
    assert source.malformed_lines == 1


def test_empty_blocks_produce_no_events() -> None:
    source = ClaudeCodeSource()
    events = source.parse_line(
        line(type="assistant", message={"content": [{"type": "text", "text": "   "}]})
    )

    assert events == []
