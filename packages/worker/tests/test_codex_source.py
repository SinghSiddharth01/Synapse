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


def test_function_call_arguments_json_string_is_reparsed_not_left_as_raw_json() -> None:
    """`function_call.arguments` is a JSON-encoded STRING (the Responses API
    convention), not an already-parsed object -- `_stringify` re-parses it
    back into a dict before flattening, the branch that deliberately departs
    from a naive "value is already a string, just use it" implementation.

    Content is pinned exactly, including the nested-list-loses-key-
    association quirk the module docstring calls out: a list value
    (`command`) is flattened by joining its own items with newlines, so
    `"command: " + <joined items>` reads as if `-lc` and `pytest -q` were
    their own top-level keys. That is worth pinning deliberately (the
    finding's own words: "worth pinning deliberately rather than by
    accident") rather than only ever checked with an `in` substring, which a
    mutant that skips re-parsing (`return value` unconditionally) also
    satisfies -- `"pytest" in content` is true of the raw JSON text too.
    """
    source = CodexSource()
    source.parse_line(session_meta())

    arguments = json.dumps(
        {"command": ["bash", "-lc", "pytest -q"], "workdir": "/repo", "timeout_ms": 120000}
    )
    (event,) = source.parse_line(
        response_item(
            {"type": "function_call", "name": "shell", "arguments": arguments, "call_id": "call_shell"}
        )
    )

    assert event.content == "command: bash\n-lc\npytest -q\nworkdir: /repo\ntimeout_ms: 120000"
    # The raw JSON syntax must be gone -- a mutant that skips re-parsing
    # would leave the braces/quotes/colons of the JSON text itself in place.
    assert "{" not in event.content
    assert '"' not in event.content


def test_function_call_output_json_object_string_is_also_reparsed() -> None:
    """The same re-parse applies to function_call_output.output when IT
    happens to be a JSON-object string too -- not just function_call.
    arguments -- per the module docstring's `_stringify` note."""
    source = CodexSource()
    source.parse_line(session_meta())
    source.parse_line(
        response_item({"type": "function_call", "name": "shell", "arguments": "{}", "call_id": "call_3"})
    )

    output = json.dumps({"exit_code": 0, "stdout": "3 passed"})
    (event,) = source.parse_line(
        response_item({"type": "function_call_output", "call_id": "call_3", "output": output})
    )

    assert event.content == "exit_code: 0\nstdout: 3 passed"
    assert "{" not in event.content


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


# --- Codex's own machine-injected "user"-role context, module docstring's
# "Codex's own machine-injected context" section. These mirror what a real
# rollout's first response_item message actually contains -- see
# fixtures/raw_lines/codex/README.md.


def test_environment_context_bundle_is_skipped_not_treated_as_a_prompt() -> None:
    """The demo-path case: a fresh session's first response_item message is
    Codex's own <environment_context> bundle, role="user" -- indistinguishable
    on the wire from something the developer typed. It must never become an
    AgentEvent."""
    source = CodexSource()
    source.parse_line(session_meta())
    events = source.parse_line(
        response_item(
            {
                "type": "message",
                "role": "user",
                "content": [
                    {"type": "input_text", "text": "<environment_context>\n<cwd>/repo</cwd>\n</environment_context>"}
                ],
            }
        )
    )

    assert events == []
    assert source.skipped_injected_context == 1
    # Not double-counted against the unrelated skip paths.
    assert source.skipped_unknown_response_item == 0
    assert source.skipped_bookkeeping == 0


def test_agents_md_instructions_bundle_is_skipped() -> None:
    """UserInstructions -- AGENTS.md content, injected the same way, marked
    "# AGENTS.md instructions" ... "</INSTRUCTIONS>"."""
    source = CodexSource()
    source.parse_line(session_meta())
    events = source.parse_line(
        response_item(
            {
                "type": "message",
                "role": "user",
                "content": [
                    {
                        "type": "input_text",
                        "text": "# AGENTS.md instructions for /repo\n\n<INSTRUCTIONS>\nUse pytest, not unittest.\n</INSTRUCTIONS>",
                    }
                ],
            }
        )
    )

    assert events == []
    assert source.skipped_injected_context == 1


def test_marked_fragment_matching_is_case_insensitive() -> None:
    """`matches_marked_text` compares markers case-insensitively -- Codex
    itself compares this way (`eq_ignore_ascii_case`), so this adapter must
    too or a fragment differing only in tag case would leak through as a
    fake user prompt."""
    source = CodexSource()
    source.parse_line(session_meta())

    events = source.parse_line(
        response_item(
            {
                "type": "message",
                "role": "user",
                "content": [{"type": "input_text", "text": "<ENVIRONMENT_CONTEXT>\n<cwd>/repo</cwd>\n</Environment_Context>"}],
            }
        )
    )

    assert events == []
    assert source.skipped_injected_context == 1


def test_marked_fragment_matching_requires_the_closing_marker_too() -> None:
    """Text that opens the tag but never closes it (a torn/streamed write, or
    prose that merely starts the same way) must not match -- both the start
    AND end marker are required, not just a prefix check."""
    source = CodexSource()
    source.parse_line(session_meta())

    events = source.parse_line(
        response_item(
            {
                "type": "message",
                "role": "user",
                "content": [{"type": "input_text", "text": "<environment_context>\n<cwd>/repo</cwd>\nunterminated"}],
            }
        )
    )

    assert [e.content for e in events] == ["<environment_context>\n<cwd>/repo</cwd>\nunterminated"]
    assert source.skipped_injected_context == 0


def test_real_prompt_that_merely_mentions_a_marker_word_is_not_filtered() -> None:
    """A developer genuinely asking about `environment_context` in prose must
    survive -- the filter matches on the whole trimmed message starting AND
    ending with the exact marker pair, not a substring `in` check anywhere in
    the text. A mutant that widened the check to `marker in text` would wrongly
    drop this."""
    source = CodexSource()
    source.parse_line(session_meta())
    events = source.parse_line(
        response_item(
            {
                "type": "message",
                "role": "user",
                "content": [
                    {"type": "input_text", "text": "please fix the bug in build_environment_context, it crashes"}
                ],
            }
        )
    )

    assert [(e.role, e.kind, e.content) for e in events] == [
        ("user", "text", "please fix the bug in build_environment_context, it crashes")
    ]
    assert source.skipped_injected_context == 0


def test_real_prompt_containing_both_marker_substrings_mid_sentence_is_not_filtered() -> None:
    """Both the open and close marker text can appear SOMEWHERE in a real
    prompt (e.g. a developer quoting/discussing the tags) without the message
    starting and ending with them as a wrapping pair. This must survive -- a
    mutant that checked `start_marker in text and end_marker in text` instead
    of position-anchored prefix/suffix matching would wrongly treat this as
    injected and drop it, and a substring-anywhere check cannot be told apart
    from the correct implementation by any test whose text contains at most
    one of the two markers."""
    source = CodexSource()
    source.parse_line(session_meta())
    text = (
        "I noticed both <environment_context> and </environment_context> "
        "show up verbatim in the rollout file — is that expected?"
    )
    events = source.parse_line(
        response_item({"type": "message", "role": "user", "content": [{"type": "input_text", "text": text}]})
    )

    assert [e.content for e in events] == [text]
    assert source.skipped_injected_context == 0


def test_injected_context_filter_only_applies_to_user_role() -> None:
    """The filter is scoped to role="user" -- an assistant message that
    happens to echo one of these tags back (e.g. quoting environment context
    in its own reply) must not be silently dropped; only Codex's own
    machine-injected *user*-role turns are in scope."""
    source = CodexSource()
    source.parse_line(session_meta())
    events = source.parse_line(
        response_item(
            {
                "type": "message",
                "role": "assistant",
                "content": [
                    {"type": "output_text", "text": "<environment_context>\n<cwd>/repo</cwd>\n</environment_context>"}
                ],
            }
        )
    )

    assert [(e.role, e.kind) for e in events] == [("assistant", "text")]
    assert source.skipped_injected_context == 0


def test_one_matching_content_item_drops_the_entire_message_not_just_that_item() -> None:
    """Mirrors Codex's own parse_user_message: a single matching fragment
    disqualifies the WHOLE message. A message with a real line of text
    alongside an injected fragment must still be dropped entirely, not
    partially kept -- a mutant that filtered per-item instead of per-message
    would wrongly emit the real-looking line."""
    source = CodexSource()
    source.parse_line(session_meta())
    events = source.parse_line(
        response_item(
            {
                "type": "message",
                "role": "user",
                "content": [
                    {"type": "input_text", "text": "some real-looking text"},
                    {"type": "input_text", "text": "<turn_aborted>\nThe user interrupted the previous turn on purpose.\n</turn_aborted>"},
                ],
            }
        )
    )

    assert events == []
    assert source.skipped_injected_context == 1


def test_additional_context_fragment_is_skipped() -> None:
    """AdditionalContextUserFragment -- <external_KEY>...</external_KEY>,
    hook- or extension-supplied out-of-band context; the close tag must echo
    the same key or it does not match (checked separately below)."""
    source = CodexSource()
    source.parse_line(session_meta())
    events = source.parse_line(
        response_item(
            {
                "type": "message",
                "role": "user",
                "content": [{"type": "input_text", "text": "<external_ticket>JIRA-123: fix the retry loop</external_ticket>"}],
            }
        )
    )

    assert events == []
    assert source.skipped_injected_context == 1


def test_additional_context_fragment_requires_matching_key_on_both_tags() -> None:
    """A mismatched close-tag key is not a valid AdditionalContextUserFragment
    -- must not match, or any text vaguely shaped like <external_x>...</...>
    would be swept up regardless of whether the keys agree."""
    source = CodexSource()
    source.parse_line(session_meta())
    events = source.parse_line(
        response_item(
            {
                "type": "message",
                "role": "user",
                "content": [{"type": "input_text", "text": "<external_ticket>JIRA-123</external_other>"}],
            }
        )
    )

    assert [e.content for e in events] == ["<external_ticket>JIRA-123</external_other>"]
    assert source.skipped_injected_context == 0


def test_internal_model_context_fragment_is_skipped() -> None:
    """InternalModelContextFragment -- hidden extension-owned model steering,
    <codex_internal_context source="...">...</codex_internal_context>."""
    source = CodexSource()
    source.parse_line(session_meta())
    events = source.parse_line(
        response_item(
            {
                "type": "message",
                "role": "user",
                "content": [
                    {
                        "type": "input_text",
                        "text": '<codex_internal_context source="planner">next step: refactor</codex_internal_context>',
                    }
                ],
            }
        )
    )

    assert events == []
    assert source.skipped_injected_context == 1


def test_legacy_goal_context_fragment_is_skipped() -> None:
    source = CodexSource()
    source.parse_line(session_meta())
    events = source.parse_line(
        response_item(
            {
                "type": "message",
                "role": "user",
                "content": [{"type": "input_text", "text": "<goal_context>ship the retry helper</goal_context>"}],
            }
        )
    )

    assert events == []
    assert source.skipped_injected_context == 1


def test_legacy_apply_patch_warning_is_skipped() -> None:
    source = CodexSource()
    source.parse_line(session_meta())
    events = source.parse_line(
        response_item(
            {
                "type": "message",
                "role": "user",
                "content": [
                    {
                        "type": "input_text",
                        "text": "Warning: apply_patch was requested via exec_command. Use the apply_patch tool instead of exec_command.",
                    }
                ],
            }
        )
    )

    assert events == []
    assert source.skipped_injected_context == 1


def test_hook_prompt_fragment_is_skipped() -> None:
    """A hook's injected prompt fragment
    (`<hook_prompt hook_run_id="...">...</hook_prompt>`), the Codex-side
    analogue of the additionalContext this repo's own Claude Code awareness
    pack (Chain C) injects via a UserPromptSubmit hook."""
    source = CodexSource()
    source.parse_line(session_meta())
    events = source.parse_line(
        response_item(
            {
                "type": "message",
                "role": "user",
                "content": [
                    {"type": "input_text", "text": '<hook_prompt hook_run_id="hook-run-1">Retry with tests.</hook_prompt>'}
                ],
            }
        )
    )

    assert events == []
    assert source.skipped_injected_context == 1
