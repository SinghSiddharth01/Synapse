"""Compaction — Plan A.5, deterministic code, not a model. Runs AFTER triage's
keep decision, on the Segments triage has already decided are worth sending
to the model.

Segments routinely exceed the segment budget on the axis that costs nothing
to read and everything to render: a `tool_result` is often a raw log dump,
most of which is neither an error nor a conclusion. `adr/0003` already put
compression on the distiller's shoulders, but the distiller only ever sees
what actually reaches it — a segment too large simply gets clipped at the
model's context boundary, silently, with no say in what survives. This module
makes that choice deliberately, in code, before the model ever sees the
segment (but after `triage` already has — see the AMENDMENT below):

  - `tool_result` is head/tail-truncated (first/last `HEAD_TAIL_LINES`) —
    errors and conclusions overwhelmingly live at the edges of a log.
  - a trivial tool call — a READ-ONLY tool (mirrors `triage.READONLY_TOOLS`)
    whose result is both small and clean — has its `tool_result` content
    collapsed to a short, fixed marker. **The `tool_use`/`tool_result`
    EVENTS themselves are never removed** — see the amendment below for why.
  - binary/base64-looking runs are stripped and replaced with a short marker.
  - `thinking` is trimmed to its first `THINKING_LINES` lines.

`compact(segment) -> Segment` is pure: it never mutates its argument, and
running it twice on its own output is a no-op. `thinking` truncation is
idempotent the way the module originally shipped it (gated on a marker that
only its own output carries). `tool_result` compaction (both the ordinary
head/tail path and the trivial-pair path below) is idempotent by a different,
stronger argument: the trivial/non-trivial CLASSIFICATION is made on the
content *after* head/tail truncation and binary-stripping have already run,
not on the raw input — so a second pass sees exactly the content the first
pass already decided about, reaches the identical verdict, and produces
identical output. See `_compact_readonly_tool_result`.

WHY THIS RUNS AFTER TRIAGE (A.5b's "reads the full Segment" note, `triage.py`
module docstring, taken literally — fixer amendment, blocker). The module
shipped first with compaction running BEFORE triage, on the reasoning that
triage keys on `AgentEvent.kind` being PRESENT and compaction must therefore
never make a kind vanish outright. That reasoning is true but insufficient:
triage's keep-signals also key on error TEXT living inside `tool_result`/
`text` CONTENT (`_has_uncleared_error`), and compaction's own head/tail
truncation — even with the buried-error-preservation and survivor-ranking
below — is a lossy view of that content by construction. A segment can be
built (and was, in review: a lint-clean report's own count noun sitting
ahead of a real, uncleared failure within the truncated middle) where
compaction's ranked survivor list crowds the real failure out of the budget
entirely, silently flipping triage's verdict from keep to skip on a Segment
that never should have been skipped at all — a false negative exactly as
permanent as one triage produces on its own, since the follower never
re-reads a transcript position. Running compaction second closes that class
of bug structurally rather than by out-guessing every future decoy shape:
triage's keep/skip judgment is now always made against the Segment exactly
as segmented, the ONE piece of evidence that was actually in the transcript,
never against a view compaction chose to show it. Compaction's own
buried-error preservation and survivor-ranking (see the AMENDMENTs below)
still matter after this reorder — not to protect triage's verdict anymore,
but so the model, which only ever sees the compacted form, isn't the one
that loses the buried error instead. That is what the buried-error
preservation below is for: a naive first/last-N-lines rule would silently
drop an error sitting in the middle of a long log, which is exactly
`seg-003`'s shape (~52% through a 118-line dump) and exactly the kind of
knowledge loss worth guarding against even downstream of triage's decision.

AMENDMENT (fixer pass, blocker): the first version of this module dropped a
trivial read-only pair ENTIRELY — both the `tool_use` and its `tool_result`
vanished from `segment.events`. That is exactly the "kind vanishing outright"
the paragraph above forbids, and it was missed here because a trivial-by-
construction pair's *content* is indeed clean and small (nothing triage's
content-scanning keep-signals key on was living there) — but `triage.py`'s
`readonly-run` SKIP-signature keys on `AgentEvent.kind == "tool_use"` merely
being PRESENT (`tool_uses = [... if e.kind == "tool_use"]`, gated on that list
being non-empty). Deleting every `tool_use` event from an all-read-only
segment made that list empty, so the skip-signature could never fire and the
segment fell through to `default-keep` — turning a pure-browsing turn that
triage was designed to skip into one that reaches the NPU, the exact false
positive A.5b exists to prevent. Fixed by keeping both events always and only
ever collapsing the `tool_result`'s CONTENT to `_TRIVIAL_RESULT_MARKER` — the
`kind`s survive unconditionally, so `triage.tool_uses` sees the same shape it
would have seen without compaction in front of it.

The error-line heuristic below (`_LIKELY_ERROR_LINE_RE`) is deliberately
BROADER than `triage.ERROR_RE`, not the same pattern reused. `triage.ERROR_RE`
requires `\\berror\\b` as its own word, tuned for triage's per-clause
clean-vs-real-failure judgment (see `_has_uncleared_error`); it does NOT match
"ConnectionResetError" — a single CamelCase identifier with no word boundary
before "Error" — which is precisely seg-003's buried line. Compaction is
choosing what to KEEP, not judging durability the way triage does: a false
positive here just keeps one extra line for free, while a false negative
would permanently discard it before triage or the model ever see the segment.
Recall-biased on purpose, same asymmetry, deliberately looser pattern.

AMENDMENT (fixer pass, major): breadth on its own was not enough.
`_LIKELY_ERROR_LINE_RE` matches lines triage itself would call CLEARED — a
lint report's own count noun ("Found 3 errors (3 fixed, 0 remaining)") trips
"errors" — and `_truncate_tool_result_lines` used to take `matches[:
MAX_SURVIVOR_LINES]` in plain document order with no regard for that. On a
log with enough clean stage reports ahead of a real, uncleared failure (a
`fatal:` line, say), the decoys filled the whole survivor budget and the one
line that actually mattered got truncated away. Fixed by ranking survivors,
not just capping them: every match is scored by `triage.LINT_CLEAN_RE` — the
identical clean-phrase veto `triage._has_uncleared_error` itself uses to
decide a clause is cleared — and every UNCLEARED match wins the budget over
every CLEARED one, `MAX_SURVIVOR_LINES` applied only after that reordering.
Order is preserved within each group (first occurrence first), so this
changes nothing about which lines survive when there's no decoy competition
— it only changes who loses when the budget is actually contested.

Originally written when compaction still ran before triage, where a lost
survivor could flip triage's own verdict from keep to skip — the blocker
reorder above (WHY THIS RUNS AFTER TRIAGE) closed that specific failure
mode structurally, since triage no longer ever sees this module's output.
This ranking stays in place anyway: it is now the model's own protection,
not triage's. Once a Segment has already earned a `keep`, the compacted
`tool_result` is the ONLY view of it the distiller ever gets — the follower
never re-reads a transcript position, so a real failure this ranking failed
to protect would be exactly as permanently lost to the model as one triage
mistakenly skips. This is compaction's own "keep what triage would keep"
contract, now applied to what survives for the model's sake rather than
triage's: a survivor list can no longer be filled entirely by lines triage
would call clean while a real failure sitting right next to them gets cut.
"""

from __future__ import annotations

import re

from synapse_contracts import AgentEvent, Segment

from synapse_worker.triage import LINT_CLEAN_RE, READONLY_TOOLS

HEAD_TAIL_LINES = 15
THINKING_LINES = 2

# Below this, a read-only tool's result is a peek, not a payload worth
# keeping whole -- "tiny" is load-bearing: see test_a_readonly_call_with_a_
# large_result_survives (a big Read/Grep result is exactly the
# signal-carrying content this module exists to keep, truncated rather than
# dropped) and test_a_readonly_call_with_an_error_result_survives (an error
# result of any size is never trivial).
TRIVIAL_RESULT_MAX_CHARS = 200

# See the module docstring: deliberately broader than triage.ERROR_RE.
_LIKELY_ERROR_LINE_RE = re.compile(
    r"(?i)(error|exception|traceback|failed|fatal|panic|denied|refused|"
    r"reset by peer|exit code [1-9]|non-zero exit)"
)

# A long run of base64-alphabet characters, optional padding. Long enough
# (80+) that ordinary identifiers/URLs essentially never trip it by accident
# -- a URL's `.`/`:` and most prose's spaces/punctuation break up any run
# this long, and this is a keep-more-if-unsure heuristic like the error regex
# above, not a precision-tuned one.
_BASE64_RUN_RE = re.compile(r"[A-Za-z0-9+/]{80,}={0,2}")

# AMENDMENT (fixer pass, major): `_LIKELY_ERROR_LINE_RE` is deliberately
# broad (see above) -- on an error-DENSE dump (a failing test-suite log,
# where nearly every line contains "failed"/"error"/"exception") the old
# code kept every single matching middle line with no cap, so compaction
# could come out LARGER than its input: measured on a 400-line pytest
# failure dump, 30,979 chars in -> 31,011 chars out. That defeats the whole
# point of head/tail truncation and made `WorkerLoop._compact`'s reported
# "chars saved" go negative. Capping survivors bounds the worst case to
# `2 * HEAD_TAIL_LINES + MAX_SURVIVOR_LINES` lines regardless of how
# error-dense the input is -- large enough that a handful of real errors
# never get trimmed away, small enough that a whole log of them can't
# reintroduce unbounded growth.
MAX_SURVIVOR_LINES = HEAD_TAIL_LINES

# Every marker below only ever appears in THIS module's own output, never in
# raw transcript content by construction -- that is what makes each
# transformation's idempotency check ("does the marker already appear?")
# sound rather than a coincidence of the fixture corpus.
_OMISSION_MARKER = "⋯ compacted ⋯"
_THINKING_TRIMMED_MARKER = "⋯ (thinking trimmed) ⋯"

# What a trivial read-only tool_result's content collapses to.
#
# AMENDMENT (fixer pass, major): this was originally a 38-char prose marker
# ("⋯ (trivial read-only result omitted) ⋯"). Most trivial results -- the
# common case this branch exists for -- are themselves well under 38 chars
# ("DEBUG = True" is 12), so replacing them with a longer marker made
# compaction GROW the segment on the single most common turn shape,
# measured through a real WorkerLoop.tick() as "4/4 events kept · 26 chars
# added" on exactly the scenario test_stats.py's ordering test uses. Empty
# is the only value that can never grow anything, by construction (`len("")
# == 0 <= len(content)` for every possible `content`, including empty
# results) -- still well under TRIVIAL_RESULT_MAX_CHARS, still matches no
# `_LIKELY_ERROR_LINE_RE` word, so classifying it is still always a no-op
# verdict on a second pass. See the module docstring's idempotence argument.
_TRIVIAL_RESULT_MARKER = ""


def _strip_binary(content: str) -> str:
    """Replace any long base64-looking run with a short placeholder. Runs
    before line truncation below: a single giant base64 blob is often ONE
    long line, so line-count truncation alone would never touch it."""

    def _replace(match: re.Match[str]) -> str:
        return f"[{len(match.group(0))} chars of binary/base64 stripped]"

    return _BASE64_RUN_RE.sub(_replace, content)


def _truncate_tool_result_lines(content: str) -> str:
    """Head/tail-truncate to `HEAD_TAIL_LINES`, keeping any line in the
    omitted middle that looks like an error (see the module docstring) —
    those never count against the omitted total, so a large error region
    keeps every line of itself, up to `MAX_SURVIVOR_LINES` (see that
    constant's comment: uncapped survivors let an error-dense dump defeat
    truncation entirely)."""
    if _OMISSION_MARKER in content:
        return content  # already compacted -- idempotent by construction
    lines = content.splitlines()
    if len(lines) <= 2 * HEAD_TAIL_LINES:
        return content
    head = lines[:HEAD_TAIL_LINES]
    tail = lines[-HEAD_TAIL_LINES:]
    middle = lines[HEAD_TAIL_LINES: len(lines) - HEAD_TAIL_LINES]
    matches = [line for line in middle if _LIKELY_ERROR_LINE_RE.search(line)]
    # Rank before capping (see the module docstring's AMENDMENT): an
    # uncleared match — the same test `triage._has_uncleared_error` applies
    # per clause — always wins the budget over one `triage.LINT_CLEAN_RE`
    # would call cleared, so a real failure can never be crowded out by a
    # clean report's own count noun. First-occurrence order is preserved
    # within each group; only the two groups are reordered relative to
    # each other.
    uncleared = [line for line in matches if not LINT_CLEAN_RE.search(line)]
    cleared = [line for line in matches if LINT_CLEAN_RE.search(line)]
    survivors = (uncleared + cleared)[:MAX_SURVIVOR_LINES]
    omitted = len(middle) - len(survivors)
    parts = [*head, f"{_OMISSION_MARKER} ({omitted} lines omitted)", *survivors, *tail]
    return "\n".join(parts)


def _compact_tool_result_content(content: str) -> str:
    return _truncate_tool_result_lines(_strip_binary(content))


def _compact_thinking_content(content: str) -> str:
    if _THINKING_TRIMMED_MARKER in content:
        return content  # already compacted -- idempotent by construction
    lines = content.splitlines()
    if len(lines) <= THINKING_LINES:
        return content
    kept = lines[:THINKING_LINES]
    return "\n".join([*kept, _THINKING_TRIMMED_MARKER])


def _is_trivial_readonly_result(tool_name: str | None, content: str) -> bool:
    """A read-only tool call whose result is both small and clean. Mirrors
    triage.READONLY_TOOLS deliberately — the same set of tools triage
    already treats as "browsing, not analysis" is the set compaction treats
    as safe to collapse to a marker when the result confirms nothing
    happened.

    `content` is the result AFTER `_compact_tool_result_content` has already
    run on it, never the raw input — see `_compact_readonly_tool_result`
    and the module docstring's idempotence argument for why that ordering is
    load-bearing, not incidental."""
    if (tool_name or "") not in READONLY_TOOLS:
        return False
    if len(content) > TRIVIAL_RESULT_MAX_CHARS:
        return False
    return not _LIKELY_ERROR_LINE_RE.search(content)


def _compact_readonly_tool_result(tool_name: str | None, content: str) -> str:
    """The full compaction pipeline for a `tool_result` paired with a
    read-only `tool_use`: truncate/strip first (itself idempotent), THEN
    decide triviality on that already-stable content. Doing it in this order
    — rather than classifying the raw content up front — is what makes the
    whole thing idempotent: a truncated-but-not-yet-trivial result that
    shrinks under `TRIVIAL_RESULT_MAX_CHARS` is classified as trivial HERE,
    on pass one, instead of surviving pass one as borderline-sized content
    that a hypothetical pass two would then reclassify and shrink further.
    See the module docstring."""
    truncated = _compact_tool_result_content(content)
    if _is_trivial_readonly_result(tool_name, truncated):
        return _TRIVIAL_RESULT_MARKER
    return truncated


def compact(segment: Segment) -> Segment:
    """Deterministic compaction. Pure — never mutates `segment`; returns a
    new `Segment`. Idempotent — see the module docstring.

    Runs AFTER triage's keep decision in `WorkerLoop.tick` (see the module
    docstring's "WHY THIS RUNS AFTER TRIAGE" for why order matters here).
    Never removes a `tool_use` or `tool_result` event outright, even a
    trivial one — only ever collapses `tool_result` CONTENT — because
    triage's `readonly-run` skip-signature keys on `tool_use` events merely
    being present (see the module docstring's AMENDMENT)."""
    events = segment.events
    kept: list[AgentEvent] = []
    i = 0
    while i < len(events):
        event = events[i]
        if (
            event.kind == "tool_use"
            and i + 1 < len(events)
            and events[i + 1].kind == "tool_result"
        ):
            tool_result = events[i + 1]
            new_content = _compact_readonly_tool_result(event.tool_name, tool_result.content)
            kept.append(event)
            kept.append(tool_result.model_copy(update={"content": new_content}))
            i += 2
            continue
        if event.kind == "tool_result":
            event = event.model_copy(
                update={"content": _compact_tool_result_content(event.content)}
            )
        elif event.kind == "thinking":
            event = event.model_copy(
                update={"content": _compact_thinking_content(event.content)}
            )
        kept.append(event)
        i += 1
    return segment.model_copy(update={"events": kept})
