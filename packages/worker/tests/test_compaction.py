"""Compaction (Plan A.5) — deterministic, runs after triage's keep decision.
See compaction.py's module docstring for the four behaviors, why each
exists, and why the ordering relative to triage is load-bearing."""

from __future__ import annotations

from datetime import datetime, timezone

from synapse_contracts import AgentEvent, Segment
from synapse_distiller.capability import NPU_QWEN3_4B_INSTRUCT_2507
from synapse_distiller.fixtures import load_segment
from synapse_distiller.promptpack import load_pack_by_name

from synapse_worker.compaction import (
    HEAD_TAIL_LINES,
    MAX_SURVIVOR_LINES,
    THINKING_LINES,
    TRIVIAL_RESULT_MAX_CHARS,
    compact,
)
from synapse_worker.triage import triage

TS = datetime(2026, 8, 4, tzinfo=timezone.utc)


def _seg(events: list[AgentEvent]) -> Segment:
    return Segment(id="t-001", agent_session_id="as-t", events=events,
                   started_at=TS, ended_at=TS)


def _ev(role="assistant", kind="text", content="", tool_name=None) -> AgentEvent:
    return AgentEvent(role=role, kind=kind, content=content, tool_name=tool_name,
                      ts=TS, agent_session_id="as-t")


def test_compact_is_a_pure_function():
    original = _seg([_ev(kind="thinking", content="a\nb\nc\nd")])
    before = original.model_copy(deep=True)

    compact(original)

    assert original == before


def test_trivial_readonly_tool_call_result_is_hollowed_but_kinds_survive():
    """Fixer amendment (blocker): the pair used to be DROPPED entirely. That
    erased the `tool_use` kind from the segment, which is exactly what broke
    triage.readonly-run (see test_readonly_run_skip_signature_survives_
    compaction below) — triage's skip-signature is gated on `tool_use`
    events merely being present, not on their content. Now only the tiny,
    clean `tool_result` CONTENT collapses; both events, and their kinds,
    stay."""
    segment = _seg([
        _ev(role="user", kind="text", content="what does config.py say"),
        _ev(kind="tool_use", tool_name="Read", content="config.py"),
        _ev(role="user", kind="tool_result", tool_name="Read", content="DEBUG = True"),
        _ev(content="Debug mode is on."),
    ])

    compacted = compact(segment)

    kinds = [e.kind for e in compacted.events]
    assert kinds == ["text", "tool_use", "tool_result", "text"]
    assert len(compacted.events) == 4
    result_content = compacted.events[2].content
    assert result_content != "DEBUG = True"  # collapsed to the trivial marker
    assert "DEBUG" not in result_content  # not just truncated -- the actual
    # secret-shaped content is gone, not merely shortened
    assert compacted.events[1].content == "config.py"  # tool_use itself untouched


def test_a_non_readonly_tool_call_survives_even_with_a_tiny_result():
    """Bash isn't in READONLY_TOOLS -- a tiny result doesn't make an
    arbitrary command trivial the way a Read/Grep/Glob/LS/WebSearch/WebFetch
    peek does."""
    segment = _seg([
        _ev(kind="tool_use", tool_name="Bash", content="rm -rf build/"),
        _ev(role="user", kind="tool_result", tool_name="Bash", content="ok"),
    ])

    compacted = compact(segment)

    assert [e.kind for e in compacted.events] == ["tool_use", "tool_result"]


def test_a_readonly_call_with_an_error_result_survives():
    """A tiny result is only "trivial" if it's also clean -- an error-shaped
    result from a read-only tool must not be thrown away."""
    segment = _seg([
        _ev(kind="tool_use", tool_name="Read", content="missing.py"),
        _ev(role="user", kind="tool_result", tool_name="Read",
            content="Error: file not found"),
    ])

    compacted = compact(segment)

    assert [e.kind for e in compacted.events] == ["tool_use", "tool_result"]


def test_a_readonly_call_with_a_large_result_survives():
    """"Tiny" is load-bearing -- a big Read/Grep result is exactly the
    signal-carrying content compaction exists to keep (truncated, not
    hollowed to the trivial marker). `"x" * 5000` was deliberately NOT used
    here -- a long run of a single repeated letter matches the binary/base64
    heuristic (`_BASE64_RUN_RE`) and gets stripped to a short placeholder
    before triviality is even judged, which made the original version of
    this test pass for the wrong reason (kind survives, but so would an
    accidentally-dropped one)."""
    big_content = "line of real read output, not a repeated character " * 20
    assert len(big_content) > TRIVIAL_RESULT_MAX_CHARS
    segment = _seg([
        _ev(kind="tool_use", tool_name="Read", content="big.py"),
        _ev(role="user", kind="tool_result", tool_name="Read", content=big_content),
    ])

    compacted = compact(segment)

    assert [e.kind for e in compacted.events] == ["tool_use", "tool_result"]
    assert compacted.events[1].content == big_content  # single line: under the
    # HEAD_TAIL_LINES line-count threshold, so untouched -- and NOT collapsed
    # to the trivial marker, because it's over TRIVIAL_RESULT_MAX_CHARS


def test_a_realistic_small_result_still_collapses_below_the_max_chars_threshold():
    """Fixer-major finding: only the UPPER direction of TRIVIAL_RESULT_MAX_CHARS
    was pinned (the test right above this one, 200 -> 50000 fails it) --
    nothing pinned the lower direction, so shrinking the threshold all the
    way down to 20 chars left the whole suite green. A content string
    comfortably between 20 and TRIVIAL_RESULT_MAX_CHARS -- realistic for a
    short but real Read/Grep peek, not a toy one-liner -- must still
    collapse to the trivial marker."""
    content = "line one of a short but real result\nline two, still nothing notable"
    assert 20 < len(content) < TRIVIAL_RESULT_MAX_CHARS
    segment = _seg([
        _ev(kind="tool_use", tool_name="Read", content="notes.txt"),
        _ev(role="user", kind="tool_result", tool_name="Read", content=content),
    ])

    compacted = compact(segment)

    assert compacted.events[1].content != content
    assert len(compacted.events[1].content) < len(content)


def test_a_small_clean_readonly_result_never_grows_the_segment():
    """Fixer-major finding: f483448's whole fix
    (`_TRIVIAL_RESULT_MARKER = ""`) had no test pinning the property its own
    commit message claims -- that collapsing a small, clean read-only
    result to the trivial marker never GROWS the segment. The prior 38-char
    prose marker ("XX (trivial read-only result omitted) XX") is longer
    than most real trivial results ("DEBUG = True" is 12 chars), so
    reverting to it (measured through a real WorkerLoop.tick() as "4/4
    events kept · 26 chars added" on exactly this scenario, per f483448's
    own commit message) passed the whole suite silently -- nothing asserted
    the total shrank or even stayed flat. This compacts the canonical
    small-read-config demo turn through `compact()` directly and asserts
    the property by construction, not by pinning today's exact marker
    value: chars out <= chars in."""
    segment = _seg([
        _ev(kind="tool_use", tool_name="Read", content="config.py"),
        _ev(role="user", kind="tool_result", tool_name="Read", content="DEBUG = True"),
    ])
    original_chars = sum(len(e.content) for e in segment.events)

    compacted = compact(segment)

    compacted_chars = sum(len(e.content) for e in compacted.events)
    assert compacted_chars <= original_chars
    assert "DEBUG" not in compacted.events[1].content


def test_thinking_is_trimmed_to_first_n_lines_but_the_kind_survives():
    long_thinking = "\n".join(f"line {i}" for i in range(20))
    segment = _seg([_ev(kind="thinking", content=long_thinking)])

    compacted = compact(segment)

    [event] = compacted.events
    assert event.kind == "thinking"  # triage keys on the kind being PRESENT
    kept_lines = event.content.splitlines()[:THINKING_LINES]
    assert kept_lines == long_thinking.splitlines()[:THINKING_LINES]
    assert len(event.content) < len(long_thinking)


def test_short_thinking_is_left_alone():
    segment = _seg([_ev(kind="thinking", content="line 0\nline 1")])

    compacted = compact(segment)

    assert compacted.events[0].content == "line 0\nline 1"


def test_binary_looking_content_is_stripped():
    blob = "QUJDREVGR0hJSktMTU5PUFFSU1RVVldYWVphYmNkZWZnaGlqa2xtbm9wcXJzdHV2d3h5ejAxMjM0" * 3
    segment = _seg([_ev(role="user", kind="tool_result", tool_name="Bash",
                        content=f"uploading...\n{blob}\ndone")])

    compacted = compact(segment)

    content = compacted.events[0].content
    assert blob not in content
    assert "uploading" in content and "done" in content
    assert "stripped" in content.lower()


def test_a_buried_error_line_survives_head_tail_truncation():
    lines = [f"benign line {i}" for i in range(100)]
    lines[50] = "ConnectionResetError: [Errno 104] Connection reset by peer"
    content = "\n".join(lines)
    segment = _seg([_ev(role="user", kind="tool_result", tool_name="Bash", content=content)])

    compacted = compact(segment)

    result_content = compacted.events[0].content
    assert "ConnectionResetError" in result_content
    assert len(result_content) < len(content)  # truncation actually happened
    assert len(result_content.splitlines()) < len(lines)


def test_short_tool_result_is_left_alone():
    short = "\n".join(f"line {i}" for i in range(2 * HEAD_TAIL_LINES))
    segment = _seg([_ev(role="user", kind="tool_result", tool_name="Bash", content=short)])

    compacted = compact(segment)

    assert compacted.events[0].content == short


def test_compaction_is_idempotent():
    lines = [f"benign line {i}" for i in range(100)]
    lines[50] = "ConnectionResetError: reset by peer"
    segment = _seg([
        _ev(kind="tool_use", tool_name="Read", content="x"),
        _ev(role="user", kind="tool_result", tool_name="Read", content="ok"),
        _ev(kind="thinking", content="\n".join(f"t{i}" for i in range(10))),
        _ev(role="user", kind="tool_result", tool_name="Bash", content="\n".join(lines)),
    ])

    once = compact(segment)
    twice = compact(once)

    assert twice == once


def test_compaction_is_idempotent_when_truncation_crosses_the_trivial_threshold():
    """Regression for the fixer's major finding: the original code classified
    trivial-vs-not on the RAW content, so a read-only result just over
    TRIVIAL_RESULT_MAX_CHARS -- not trivial on pass one, truncated -- could
    come out UNDER the threshold once truncated, making it trivial (and
    droppable) on a hypothetical pass two. `compact(compact(s)) == compact(s)`
    requires pass one alone to already be the fixed point.

    60 clean 3-char lines reproduces it exactly: 239 raw chars (just over
    the 200-char trivial threshold, so pass one truncates rather than treats
    it as already-trivial) that head/tail truncation shrinks well under 200."""
    lines = [f"L{i:02d}" for i in range(60)]  # "L00".."L59", 3 chars each
    content = "\n".join(lines)
    assert len(content) > TRIVIAL_RESULT_MAX_CHARS  # not trivial by a RAW reading
    segment = _seg([
        _ev(kind="tool_use", tool_name="Read", content="x"),
        _ev(role="user", kind="tool_result", tool_name="Read", content=content),
    ])

    once = compact(segment)
    twice = compact(once)

    assert twice == once
    # Also pin that the kind survives either way -- the old "drop" behaviour
    # would fail this on pass two even where idempotence happened to hold.
    assert [e.kind for e in twice.events] == ["tool_use", "tool_result"]


def test_error_dense_tool_result_still_shrinks_and_stays_bounded():
    """Fixer major finding: `_LIKELY_ERROR_LINE_RE` is broad by design (see
    the module docstring) and on a genuinely error-dense dump (a failing
    test-suite log) nearly every line matches it. Uncapped, the old code
    kept every single match, so compaction could come out LARGER than its
    input -- measured on a 400-line pytest failure dump, 30,979 chars in
    became 31,011 chars out. `MAX_SURVIVOR_LINES` bounds the worst case
    regardless of how error-dense the input is."""
    lines = [
        f"FAILED tests/test_mod{i}.py::test_case_{i} - AssertionError: boom"
        for i in range(400)
    ]
    content = "\n".join(lines)
    segment = _seg([_ev(role="user", kind="tool_result", tool_name="Bash", content=content)])

    compacted = compact(segment)

    result_content = compacted.events[0].content
    assert len(result_content) < len(content)  # net shrink, not growth
    kept_lines = result_content.splitlines()
    assert len(kept_lines) <= 2 * HEAD_TAIL_LINES + 1 + MAX_SURVIVOR_LINES


def test_readonly_run_skip_signature_survives_compaction():
    """Blocker fix regression. `triage.readonly-run` is gated on `tool_uses`
    (`kind == "tool_use"` events) being NON-EMPTY -- see triage.py's
    skip-signatures. Before the fix, compaction dropped every trivial
    read-only tool_use/tool_result pair outright, so an all-browsing segment
    had ZERO tool_use events left after compaction and fell through to
    triage's default-keep -- reaching the NPU for exactly the false positive
    A.5b exists to prevent. See test_loop.py for the same regression pinned
    through a real WorkerLoop.tick()."""
    segment = _seg([
        _ev(role="user", kind="text", content="what's in these files"),
        _ev(kind="tool_use", tool_name="Read", content="a.py"),
        _ev(role="user", kind="tool_result", tool_name="Read", content="x = 1"),
        _ev(kind="tool_use", tool_name="Grep", content="TODO"),
        _ev(role="user", kind="tool_result", tool_name="Grep", content="no matches"),
        _ev(content="Nothing interesting here."),
    ])

    pre = triage(segment)
    assert (pre.keep, pre.reason) == (False, "readonly-run")

    post = triage(compact(segment))
    assert (post.keep, post.reason) == (False, "readonly-run")


def test_a_real_error_is_not_crowded_out_by_lint_clean_decoys_within_the_survivor_cap():
    """Fixer-major finding: `_LIKELY_ERROR_LINE_RE` is deliberately broad
    enough to match a CLEARED clean-report line too -- "Found 3 errors (3
    fixed, 0 remaining)" trips it on the word "errors" alone, the exact
    phrase `triage.LINT_CLEAN_RE`/`triage._has_uncleared_error` exist to
    veto. Before the fix, `_truncate_tool_result_lines` took the first
    `MAX_SURVIVOR_LINES` matches in plain document order with no regard for
    which ones triage would actually call cleared -- so enough clean stage
    reports ahead of a real, uncleared failure (here, one `fatal:` line)
    filled the whole survivor budget and the real failure was truncated
    away before triage or the model ever saw it. This is deliberately the
    same shape the finding was verified against: 15 kept head lines, then
    (inside the truncated middle) 15 lint-clean decoys ahead of one real
    `fatal:` line, then filler, then a kept tail -- 16 matches competing for
    a 15-line budget.

    Written when compaction still ran before triage, where this crowding-out
    could flip triage's own verdict -- the adjudicated blocker reorder
    (compaction.py's "WHY THIS RUNS AFTER TRIAGE") has since closed that
    specific failure mode structurally. This unit-level check on `compact()`
    stays regardless: post-reorder, a crowded-out real error would still be
    permanently lost to the MODEL on a Segment triage already kept, which is
    exactly as unrecoverable."""
    head = [f"checking module {i}... ok, nothing notable" for i in range(HEAD_TAIL_LINES)]
    lint_clean_decoys = [
        f"eslint stage {i}: {i} errors ({i} fixed, 0 remaining)"
        for i in range(MAX_SURVIVOR_LINES)
    ]
    real_error = ["fatal: unable to access repo: Could not resolve host"]
    filler = [f"cleanup step {i}... ok, nothing notable" for i in range(14)]
    tail = [f"trailing status {i}... ok, nothing notable" for i in range(HEAD_TAIL_LINES)]
    content = "\n".join(head + lint_clean_decoys + real_error + filler + tail)

    segment = _seg([_ev(role="user", kind="tool_result", tool_name="Bash", content=content)])

    # Counterfactual: triage on the RAW segment reads the real, uncleared
    # failure and keeps -- establishing the finding survives triage's own
    # judgment when compaction hasn't run in front of it yet.
    raw_decision = triage(segment)
    assert (raw_decision.keep, raw_decision.reason) == (True, "error-signal")

    compacted = compact(segment)
    result_content = compacted.events[0].content

    assert "fatal:" in result_content  # the real failure must not be a casualty
    # of the budget cap -- see the module docstring's AMENDMENT.
    compacted_decision = triage(compacted)
    assert (compacted_decision.keep, compacted_decision.reason) == (True, "error-signal")


def test_seg003_oversized_tool_result_compacts_within_budget_and_the_buried_error_survives():
    """Plan A.5's first failing test, and the plan-0 fixture's original
    assertion (test_fixture_corpus.py's
    test_seg003_buried_error_reaches_the_model_only_via_prose_under_shipped_
    config documented that this could not be pinned until A.5 landed) —
    finally exercised for real. The tool_result is 118 lines with
    "ConnectionResetError" buried at ~52% -- well outside a naive first/last
    15-line window -- so this also pins that compaction's error-preservation
    is not merely "keep the head and tail"."""
    segment = load_segment("seg-003")

    compacted = compact(segment)

    tool_results = [e for e in compacted.events if e.kind == "tool_result"]
    assert any("ConnectionResetError" in e.content for e in tool_results)

    original_chars = sum(len(e.content) for e in segment.events)
    compacted_chars = sum(len(e.content) for e in compacted.events)
    assert compacted_chars < original_chars

    pack = load_pack_by_name("v4-condense")
    budget = NPU_QWEN3_4B_INSTRUCT_2507.segment_budget(
        pack.overhead_tokens, max_seconds_per_call=30.0
    )
    estimated_tokens = compacted_chars / 3.5
    assert estimated_tokens < budget
