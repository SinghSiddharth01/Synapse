from datetime import datetime, timezone

from synapse_contracts import AgentEvent, Segment
from synapse_worker.triage import TriageDecision, triage

TS = datetime(2026, 8, 4, tzinfo=timezone.utc)


def _seg(events: list[AgentEvent]) -> Segment:
    return Segment(id="t-001", agent_session_id="as-t", events=events,
                   started_at=TS, ended_at=TS)


def _ev(role="assistant", kind="text", content="", tool_name=None) -> AgentEvent:
    return AgentEvent(role=role, kind=kind, content=content, tool_name=tool_name,
                      ts=TS, agent_session_id="as-t")


def test_error_in_tool_result_is_kept():
    d = triage(_seg([_ev(role="user", kind="tool_result", tool_name="Bash",
                         content="Traceback (most recent call last): ...\nExit code 1")]))
    assert d.keep and d.reason == "error-signal"


def test_thinking_block_is_kept():
    d = triage(_seg([_ev(kind="thinking", content="hmm, maybe the offset is stale")]))
    assert d.keep and d.reason == "thinking-present"


def test_decision_language_is_kept():
    d = triage(_seg([_ev(content="that approach is a dead end, switching to polling instead")]))
    assert d.keep and d.reason == "decision-language"


def test_lint_clean_run_is_skipped():
    events = [
        _ev(role="user", kind="text", content="fix the imports"),
        _ev(kind="tool_use", tool_name="Bash", content="ruff check --fix ."),
        _ev(role="user", kind="tool_result", tool_name="Bash",
            content="Found 3 errors (3 fixed, 0 remaining)."),
        _ev(content="Done — ruff fixed the import order."),
    ]
    d = triage(_seg(events))
    assert not d.keep and d.reason == "lint-clean"


def test_lint_clean_is_not_mistaken_for_an_error():
    # "Found 3 errors" contains the word errors; the clean-report shape wins.
    d = triage(_seg([_ev(role="user", kind="tool_result", tool_name="Bash",
                         content="Found 3 errors (3 fixed, 0 remaining).")]))
    assert not d.keep


def test_readonly_run_with_short_prose_is_skipped():
    events = [
        _ev(role="user", kind="text", content="what's in the config dir"),
        _ev(kind="tool_use", tool_name="LS", content="config/"),
        _ev(role="user", kind="tool_result", tool_name="LS", content="synapse.toml\nprompts/"),
        _ev(content="Two entries: synapse.toml and a prompts directory."),
    ]
    d = triage(_seg(events))
    assert not d.keep and d.reason == "readonly-run"


def test_plain_conversation_defaults_to_keep():
    # seg-002's shape: no error, no tool, no decision keyword. Recall says keep.
    d = triage(_seg([_ev(role="user", kind="text", content="should we cache tokens?"),
                     _ev(content="Token lifetime is short, so caching buys nothing here.")]))
    assert d.keep and d.reason == "default-keep"


def test_readonly_with_substantial_prose_is_kept():
    long_prose = "The layout tells us something important about the module boundaries. " * 10
    events = [
        _ev(kind="tool_use", tool_name="Read", content="loop.py"),
        _ev(role="user", kind="tool_result", tool_name="Read", content="…file contents…"),
        _ev(content=long_prose),
    ]
    d = triage(_seg(events))
    assert d.keep  # substantial analysis after reading is insight, not noise
