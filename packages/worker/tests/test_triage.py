import json
from datetime import datetime, timezone
from pathlib import Path

from synapse_contracts import AgentEvent, Segment
from synapse_worker.triage import TriageDecision, triage

TS = datetime(2026, 8, 4, tzinfo=timezone.utc)
FIXTURES_DIR = Path(__file__).resolve().parents[3] / "fixtures"


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


def test_system_note_is_kept():
    # Unreachable via ClaudeCodeSource today (see the comment on this branch
    # in triage.py) -- role=="system" never survives that adapter. The rule
    # is forward-looking for a Source that does emit one, and this test pins
    # its behaviour at the triage() level so it stays covered and correct
    # even though nothing in this repo can produce the input yet.
    d = triage(_seg([_ev(role="system", kind="text", content="conversation compacted")]))
    assert d.keep and d.reason == "system-note"


def test_decision_language_is_kept():
    d = triage(_seg([_ev(content="that approach is a dead end, switching to polling instead")]))
    assert d.keep and d.reason == "decision-language"


def test_seg_004_corpus_fixture_is_the_canonical_lint_clean_skip():
    """seg-004 is the corpus's ONLY `"expected": "skip"` entry and this
    module's own docstring names it directly ("match the clean shape BEFORE
    the error words, or seg-004 style runs read as failures"). Its three
    tool_results are an `ls` listing, the ruff clean report, and a `git diff
    --stat` -- only one of the three is clean-report text, so a rule that
    required ALL of them to match (rather than ANY) would never fire on this
    fixture and every real multi-tool-call lint turn would leak through as
    default-keep.
    """
    raw = json.loads((FIXTURES_DIR / "segments" / "seg-004.json").read_text())
    segment = Segment.model_validate(raw)

    d = triage(segment)

    assert not d.keep and d.reason == "lint-clean"


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


def test_decision_language_in_user_text_is_kept_even_during_a_readonly_run():
    # Recall inversion: DECISION_RE was only ever searched over assistant
    # prose, but the human stating the decision -- then having the assistant
    # go read the one call site to confirm it -- is a completely ordinary
    # shape, and the skip-signatures below apply to the WHOLE segment
    # (including the user's text) even though the keep-signals above did not.
    events = [
        _ev(role="user", kind="text",
            content="We're dropping Redis and switching to in-process "
                    "memoization instead — the cache never survived a deploy."),
        _ev(kind="tool_use", tool_name="Read", content="cache.py"),
        _ev(role="user", kind="tool_result", tool_name="Read", content="…file contents…"),
        _ev(content="Right, that is the only call site."),
    ]
    d = triage(_seg(events))
    assert d.keep and d.reason == "decision-language"


def test_error_reported_in_user_text_is_kept_even_during_a_readonly_run():
    # Recall inversion: the error keep-signal only ever looked at
    # tool_results, so a human reporting a failure in their own words (rather
    # than pasting a traceback) while the assistant reads code to investigate
    # it was reachable by the readonly-run skip-signature.
    events = [
        _ev(role="user", kind="text",
            content="prod is throwing a Traceback on every login since the token change"),
        _ev(kind="tool_use", tool_name="Read", content="auth.py"),
        _ev(role="user", kind="tool_result", tool_name="Read", content="…file contents…"),
        _ev(content="I see it."),
    ]
    d = triage(_seg(events))
    assert d.keep and d.reason == "error-signal"


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


def test_error_count_ending_in_zero_is_not_read_as_a_clean_report():
    # Regression: LINT_CLEAN_RE's old "0 errors" alternative had no left word
    # boundary, so it matched inside "10 errors", "130 errors", etc. -- any
    # count ending in 0. This has to be a SINGLE-line tool_result: a two-line
    # construction (a detail line plus a separate summary line) is already
    # caught by the per-line split in `_has_uncleared_error` regardless of
    # the boundary, since the detail line alone trips ERROR_RE with no clean
    # phrase on it -- that shape pins the per-line split, not the boundary.
    # This single line is both the count AND (via the substring "0 errors"
    # inside "10 errors") the clean-report match, which is what actually
    # distinguishes `0 errors` from `\b0 errors\b`, and it is the real shape
    # mypy's own summary line takes.
    d = triage(_seg([
        _ev(role="user", kind="tool_result", tool_name="Bash",
            content="Found 10 errors in 3 files (checked 40 source files)"),
    ]))
    assert d.keep and d.reason == "error-signal"


def test_clean_lint_line_does_not_veto_a_real_error_on_another_line():
    # One Bash call that lints then tests is the most common shape of tool
    # invocation in real transcripts -- a clean lint phrase on one line of the
    # SAME tool_result must not suppress a real failure on another line.
    d = triage(_seg([
        _ev(role="user", kind="tool_result", tool_name="Bash",
            content="ruff: All checks passed!\n\n"
                    "FAILED tests/test_loop.py::test_offset - AssertionError: 3 != 4"),
    ]))
    assert d.keep and d.reason == "error-signal"


def test_clean_error_count_does_not_veto_a_real_error_on_another_line():
    d = triage(_seg([
        _ev(role="user", kind="tool_result", tool_name="Bash",
            content="webpack: 0 errors, 12 warnings\n"
                    "npm ERR! build script failed, exit code 1"),
    ]))
    assert d.keep and d.reason == "error-signal"


def test_one_clean_lint_result_does_not_veto_a_real_edit_in_the_same_segment():
    # A clean `ruff check .` sitting next to a real Edit's tool_result must not
    # skip the whole segment -- "lint-clean" requires the segment be NOTHING
    # BUT a clean report, not merely contain one somewhere.
    long_prose = "Working through why the crash only happens under heavy load. " * 12
    events = [
        _ev(kind="tool_use", tool_name="Bash", content="ruff check ."),
        _ev(role="user", kind="tool_result", tool_name="Bash", content="All checks passed!"),
        _ev(kind="tool_use", tool_name="Edit", content="loop.py"),
        _ev(role="user", kind="tool_result", tool_name="Edit",
            content="The file /repo/src/loop.py has been edited."),
        _ev(content=long_prose),
    ]
    d = triage(_seg(events))
    assert d.keep


def test_clean_and_failing_status_on_the_SAME_line_reads_as_a_failure():
    # Adjudicated fix for the same-line false negative: task runners commonly
    # print one status line per stage, pipe- or semicolon-joined ("just check"
    # style: "lint: all checks passed | tests: 3 failed"). Before this fix,
    # `_has_uncleared_error` vetoed the whole LINE the instant any clean
    # phrase appeared on it, even when an unrelated stage on the very same
    # line had genuinely failed. Per-line scanning has to become per-clause:
    # a real failure and a clean phrase in different clauses of one line must
    # still read as a failure -- the clean phrase only vetoes an error match
    # that shares ITS clause.
    d = triage(_seg([
        _ev(role="user", kind="tool_result", tool_name="Bash",
            content="lint: all checks passed | tests: 3 failed"),
    ]))
    assert d.keep and d.reason == "error-signal"


def test_lint_clean_phrase_in_unrelated_log_does_not_swallow_substantial_analysis():
    # The phrase "all checks passed" appearing in a generic CI log (not an
    # actual lint tool_result) must not silently skip real analysis sitting
    # right next to it -- same prose-length carve-out the readonly-run rule
    # already gets.
    long_prose = "The OOM points at the batch size, not the model itself. " * 10
    events = [
        _ev(kind="tool_use", tool_name="Read", content="ci.log"),
        _ev(role="user", kind="tool_result", tool_name="Read",
            content="build step 3: all checks passed\nstep 4: OOM killed"),
        _ev(content=long_prose),
    ]
    d = triage(_seg(events))
    assert d.keep
