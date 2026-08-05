"""Exercises the on-disk fixtures in fixtures/raw_lines/codex/ — the
hand-authored, format-confirmed shapes fixtures/raw_lines/codex/README.md
documents. Plan A.3's "Fixtures before code" step for the Codex adapter."""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path

from synapse_worker.sources.codex import CodexSource


@lru_cache(maxsize=1)
def _raw_lines_codex_dir() -> Path:
    for parent in Path(__file__).resolve().parents:
        candidate = parent / "fixtures" / "raw_lines" / "codex"
        if candidate.is_dir():
            return candidate
    raise FileNotFoundError("Could not locate fixtures/raw_lines/codex/ above this test file.")


def _parse_fixture(name: str):
    source = CodexSource()
    events = []
    for line in (_raw_lines_codex_dir() / name).read_text(encoding="utf-8").splitlines():
        events.extend(source.parse_line(line))
    return source, events


def test_basic_turn_fixture_yields_a_user_and_an_assistant_text_event() -> None:
    source, events = _parse_fixture("basic_turn.jsonl")

    assert [(e.role, e.kind) for e in events] == [("user", "text"), ("assistant", "text")]
    assert events[0].content == "Add retry logic to the fetch client"
    assert "retry helper" in events[1].content
    for event in events:
        assert event.agent_session_id == "codex-sess-1"
        assert event.cwd == "/repo"
        assert event.git_branch == "main"
    assert source.malformed_lines == 0
    assert source.skipped_unknown_response_item == 0


def test_tool_call_and_result_fixture_resolves_the_tool_name() -> None:
    source, events = _parse_fixture("tool_call_and_result.jsonl")

    assert [(e.role, e.kind) for e in events] == [("assistant", "tool_use"), ("user", "tool_result")]
    assert events[0].tool_name == "shell"
    assert events[1].tool_name == "shell"
    assert "pytest" in events[0].content
    assert events[1].content == "3 passed in 1.20s"


def test_malformed_fixture_keeps_the_surviving_lines() -> None:
    """One torn line among three must not take the rest of the file down —
    the same guarantee ClaudeCodeSource gives against a mid-write transcript."""
    source, events = _parse_fixture("malformed.jsonl")

    assert source.malformed_lines == 1
    assert [(e.role, e.kind, e.content) for e in events] == [
        ("user", "text", "still here after the bad line")
    ]
