"""Codex adapter tests.

Shapes here are taken from github.com/openai/codex's own struct definitions
and its tests' literal JSON, not a live transcript — no Codex install was
available to capture one against. See fixtures/raw_lines/codex/README.md for
the full research trail with links.
"""

from __future__ import annotations

import json

from synapse_worker.sources.codex import CodexSource

TS = "2026-08-05T09:12:00Z"


def rollout_line(item_type: str, payload: dict, ts: str = TS, ordinal: int = 1) -> str:
    return json.dumps({"timestamp": ts, "ordinal": ordinal, "type": item_type, "payload": payload})


def session_meta(session_id: str = "codex-sess-1", cwd: str = "/repo", branch: str | None = "main"):
    payload = {
        "id": session_id,
        "session_id": session_id,
        "timestamp": TS,
        "cwd": cwd,
        "originator": "codex_cli_rs",
        "cli_version": "0.146.1",
        "source": "cli",
    }
    if branch is not None:
        payload["git"] = {"branch": branch}
    return rollout_line("session_meta", payload, ordinal=0)


def response_item(payload: dict, ts: str = TS, ordinal: int = 1) -> str:
    return rollout_line("response_item", payload, ts=ts, ordinal=ordinal)


def test_session_meta_carries_cwd_and_git_branch_onto_later_events() -> None:
    """Unlike Claude Code, cwd/git.branch arrive once, in session_meta, not on
    every line -- the adapter must remember them across parse_line calls."""
    source = CodexSource()
    assert source.parse_line(session_meta(session_id="sess-9", cwd="/work", branch="perf")) == []

    (event,) = source.parse_line(
        response_item({"type": "message", "role": "user", "content": [{"type": "input_text", "text": "hi"}]})
    )

    assert event.agent_session_id == "sess-9"
    assert event.cwd == "/work"
    assert event.git_branch == "perf"


def test_session_meta_without_git_leaves_branch_none() -> None:
    source = CodexSource()
    source.parse_line(session_meta(branch=None))

    (event,) = source.parse_line(
        response_item({"type": "message", "role": "assistant", "content": [{"type": "output_text", "text": "ok"}]})
    )

    assert event.git_branch is None


def test_parses_user_input_text() -> None:
    source = CodexSource()
    source.parse_line(session_meta())
    events = source.parse_line(
        response_item({"type": "message", "role": "user", "content": [{"type": "input_text", "text": "fix the build"}]})
    )

    (event,) = events
    assert (event.role, event.kind, event.content) == ("user", "text", "fix the build")


def test_parses_assistant_output_text() -> None:
    source = CodexSource()
    source.parse_line(session_meta())
    events = source.parse_line(
        response_item({"type": "message", "role": "assistant", "content": [{"type": "output_text", "text": "hello"}]})
    )

    (event,) = events
    assert (event.role, event.kind, event.content) == ("assistant", "text", "hello")


def test_reasoning_maps_to_thinking_and_prefers_content_over_summary() -> None:
    source = CodexSource()
    source.parse_line(session_meta())
    events = source.parse_line(
        response_item(
            {
                "type": "reasoning",
                "content": [{"type": "reasoning_text", "text": "considering options"}],
                "summary": [{"type": "summary_text", "text": "short version"}],
                "encrypted_content": None,
            }
        )
    )

    (event,) = events
    assert event.kind == "thinking"
    assert event.content == "considering options"


def test_reasoning_falls_back_to_summary_when_content_is_absent() -> None:
    """Real transcripts often carry only encrypted_content -- content is None
    and summary is the only readable signal."""
    source = CodexSource()
    source.parse_line(session_meta())
    events = source.parse_line(
        response_item(
            {
                "type": "reasoning",
                "summary": [{"type": "summary_text", "text": "short version"}],
                "encrypted_content": "enc_opaque",
            }
        )
    )

    (event,) = events
    assert event.content == "short version"


def test_function_call_and_output_resolve_tool_name_from_call_id() -> None:
    """function_call_output carries only call_id, not the tool's name -- same
    shape as Claude Code's tool_use_id -> name map."""
    source = CodexSource()
    source.parse_line(session_meta())
    (call_event,) = source.parse_line(
        response_item(
            {"type": "function_call", "name": "shell", "arguments": '{"command": ["pytest"]}', "call_id": "call_1"}
        )
    )

    assert call_event.kind == "tool_use"
    assert call_event.role == "assistant"
    assert call_event.tool_name == "shell"
    assert "pytest" in call_event.content

    (result_event,) = source.parse_line(
        response_item({"type": "function_call_output", "call_id": "call_1", "output": "3 passed in 1.20s"})
    )

    assert result_event.kind == "tool_result"
    assert result_event.role == "user"
    assert result_event.tool_name == "shell"
    assert result_event.content == "3 passed in 1.20s"


def test_function_call_output_content_items_are_flattened() -> None:
    """output is EITHER a plain string OR an array of content items -- both
    occur on the wire (FunctionCallOutputPayload's hand-written Serialize)."""
    source = CodexSource()
    source.parse_line(session_meta())
    source.parse_line(
        response_item({"type": "function_call", "name": "read_file", "arguments": "{}", "call_id": "call_2"})
    )

    (event,) = source.parse_line(
        response_item(
            {
                "type": "function_call_output",
                "call_id": "call_2",
                "output": [{"type": "input_text", "text": "line one"}, {"type": "input_text", "text": "line two"}],
            }
        )
    )

    assert "line one" in event.content
    assert "line two" in event.content


def test_custom_tool_call_and_output_resolve_tool_name_from_call_id() -> None:
    """apply_patch is a Freeform tool (ToolSpec::Freeform), so it serializes
    as custom_tool_call/custom_tool_call_output, not function_call -- same
    call_id -> name bookkeeping, but the argument field is "input" (raw
    grammar text) rather than "arguments" (JSON-encoded)."""
    source = CodexSource()
    source.parse_line(session_meta())
    (call_event,) = source.parse_line(
        response_item(
            {
                "type": "custom_tool_call",
                "name": "apply_patch",
                "call_id": "call_9",
                "input": "*** Begin Patch\n*** Update File: a.py\n*** End Patch",
            }
        )
    )

    assert call_event.kind == "tool_use"
    assert call_event.role == "assistant"
    assert call_event.tool_name == "apply_patch"
    assert "Update File: a.py" in call_event.content

    (result_event,) = source.parse_line(
        response_item(
            {"type": "custom_tool_call_output", "call_id": "call_9", "output": "Success. Updated the following files:\na.py"}
        )
    )

    assert result_event.kind == "tool_result"
    assert result_event.role == "user"
    assert result_event.tool_name == "apply_patch"
    assert result_event.content == "Success. Updated the following files:\na.py"


def test_custom_tool_call_output_prefers_its_own_name_field() -> None:
    """CustomToolCallOutput carries an optional `name` directly on the wire,
    unlike function_call_output -- prefer it over the call_id map.

    The call_id map is deliberately seeded with a DIFFERENT name than the
    output's own `name` field, so the two disagree: if the map ever won this
    would assert "renamed_mid_flight" instead. (An earlier version of this
    test registered the SAME name on both sides, so the call_id fallback
    produced an identical answer and the branch under test -- `tool_name =
    str(name) if name else None` -- was never actually distinguished; a
    mutant that deleted the name-preference entirely still passed.)"""
    source = CodexSource()
    source.parse_line(session_meta())
    source.parse_line(
        response_item(
            {"type": "custom_tool_call", "name": "renamed_mid_flight", "call_id": "call_1", "input": "patch"}
        )
    )

    (event,) = source.parse_line(
        response_item(
            {
                "type": "custom_tool_call_output",
                "call_id": "call_1",
                "name": "apply_patch",
                "output": "Success.",
            }
        )
    )

    assert event.tool_name == "apply_patch"


def test_bookkeeping_line_types_are_skipped() -> None:
    """event_msg duplicates response_item's content for Codex's own TUI
    history; parsing it too would double every turn. `compacted` is NOT
    bookkeeping -- see test_compacted_line_yields_an_assistant_text_event --
    so it is deliberately excluded from this list."""
    source = CodexSource()
    for line_type in ("event_msg", "turn_context", "world_state", "inter_agent_communication"):
        assert source.parse_line(rollout_line(line_type, {"type": "user_message", "message": "x"})) == []
    assert source.skipped_bookkeeping == 4


def test_compacted_line_yields_an_assistant_text_event() -> None:
    """RolloutItem::Compacted carries the compaction summary the agent wrote
    -- the only place that text is persisted on disk (CompactedItem's own
    `From<CompactedItem> for ResponseItem` is in-memory history
    reconstruction, never a second persisted line). Plan A.3: 'Compaction
    summaries the agent writes are ingested as events -- they are
    high-density signal, not noise.'"""
    source = CodexSource()
    source.parse_line(session_meta(session_id="sess-9", cwd="/work", branch="perf"))

    events = source.parse_line(
        rollout_line(
            "compacted",
            {
                "message": "Summary: refactored the fetch client to retry on 5xx.",
                "window_number": 2,
                "window_id": "0199a1b2-0000-7000-8000-000000000001",
            },
        )
    )

    (event,) = events
    assert event.role == "assistant"
    assert event.kind == "text"
    assert event.content == "Summary: refactored the fetch client to retry on 5xx."
    assert event.agent_session_id == "sess-9"
    assert event.cwd == "/work"
    assert event.git_branch == "perf"
    assert source.skipped_bookkeeping == 0


def test_compacted_line_with_empty_message_produces_no_event() -> None:
    source = CodexSource()
    source.parse_line(session_meta())

    events = source.parse_line(rollout_line("compacted", {"message": "   "}))

    assert events == []


def test_unknown_response_item_type_is_skipped_not_raised() -> None:
    """Codex's format keeps adding variants; an unrecognized one must not
    take the whole parse down."""
    source = CodexSource()
    source.parse_line(session_meta())
    events = source.parse_line(response_item({"type": "web_search_call", "status": "completed"}))

    assert events == []
    assert source.skipped_unknown_response_item == 1


def test_unrecognized_message_role_is_skipped_not_raised() -> None:
    source = CodexSource()
    source.parse_line(session_meta())
    events = source.parse_line(
        response_item({"type": "message", "role": "developer", "content": [{"type": "input_text", "text": "x"}]})
    )

    assert events == []


def test_malformed_json_is_counted_not_raised() -> None:
    source = CodexSource()

    assert source.parse_line("{not json") == []
    assert source.parse_line("") == []
    assert source.malformed_lines == 1


def test_empty_content_produces_no_events() -> None:
    source = CodexSource()
    source.parse_line(session_meta())
    events = source.parse_line(
        response_item({"type": "message", "role": "user", "content": [{"type": "input_text", "text": "   "}]})
    )

    assert events == []
