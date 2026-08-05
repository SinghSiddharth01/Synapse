"""Compaction — Plan A.5, deterministic code, not a model. Runs BEFORE triage.

Segments routinely exceed the segment budget on the axis that costs nothing
to read and everything to render: a `tool_result` is often a raw log dump,
most of which is neither an error nor a conclusion. `adr/0003` already put
compression on the distiller's shoulders, but the distiller only ever sees
what actually reaches it — a segment too large simply gets clipped at the
model's context boundary, silently, with no say in what survives. This module
makes that choice deliberately, in code, before the model (or triage) ever
sees the segment:

  - `tool_result` is head/tail-truncated (first/last `HEAD_TAIL_LINES`) —
    errors and conclusions overwhelmingly live at the edges of a log.
  - a trivial tool call — a READ-ONLY tool (mirrors `triage.READONLY_TOOLS`)
    whose result is both small and clean — is dropped entirely, `tool_use`
    and its `tool_result` together.
  - binary/base64-looking runs are stripped and replaced with a short marker.
  - `thinking` is trimmed to its first `THINKING_LINES` lines.

`compact(segment) -> Segment` is pure: it never mutates its argument, and
running it twice on its own output is a no-op (every transformation below is
gated on a marker that only its own output carries, so a second pass sees
"already compacted" and stops).

WHY THIS RUNS BEFORE TRIAGE (A.5b's "reads the full Segment" note, `triage.py`
module docstring). Triage keys on `AgentEvent.kind` being PRESENT (`any(e.kind
== "thinking" ...)`) and on error text living in `tool_result`/`text` content
— and it runs over the Segment as segmented, unfiltered by `distil_kinds`
(that filter is `render_segment`'s job, downstream of both). Compaction must
therefore never make a kind vanish outright the way dropping a trivial call
does for tool_use/tool_result — trivial-by-construction pairs are, by
definition, clean and small, so nothing triage keys on was living there — and
truncating `tool_result`/`thinking` CONTENT must not erase the signal triage
looks for. That is what the buried-error preservation below is for: a naive
first/last-N-lines rule would silently drop an error sitting in the middle of
a long log, which is exactly `seg-003`'s shape (~52% through a 118-line dump)
and exactly the false-negative triage's own docstring calls unrecoverable —
the follower never re-reads a transcript position, so a line compaction drops
here is gone as permanently as one triage skips.

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
"""

from __future__ import annotations

import re

from synapse_contracts import AgentEvent, Segment

from synapse_worker.triage import READONLY_TOOLS

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

# Every marker below only ever appears in THIS module's own output, never in
# raw transcript content by construction -- that is what makes each
# transformation's idempotency check ("does the marker already appear?")
# sound rather than a coincidence of the fixture corpus.
_OMISSION_MARKER = "⋯ compacted ⋯"
_THINKING_TRIMMED_MARKER = "⋯ (thinking trimmed) ⋯"


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
    keeps every line of itself, not just the first hit."""
    if _OMISSION_MARKER in content:
        return content  # already compacted -- idempotent by construction
    lines = content.splitlines()
    if len(lines) <= 2 * HEAD_TAIL_LINES:
        return content
    head = lines[:HEAD_TAIL_LINES]
    tail = lines[-HEAD_TAIL_LINES:]
    middle = lines[HEAD_TAIL_LINES: len(lines) - HEAD_TAIL_LINES]
    survivors = [line for line in middle if _LIKELY_ERROR_LINE_RE.search(line)]
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


def _is_trivial_readonly_pair(tool_use: AgentEvent, tool_result: AgentEvent) -> bool:
    """A read-only tool call whose result is both small and clean. Mirrors
    triage.READONLY_TOOLS deliberately — the same set of tools triage
    already treats as "browsing, not analysis" is the set compaction treats
    as safe to drop outright when the result confirms nothing happened."""
    if (tool_use.tool_name or "") not in READONLY_TOOLS:
        return False
    if len(tool_result.content) > TRIVIAL_RESULT_MAX_CHARS:
        return False
    return not _LIKELY_ERROR_LINE_RE.search(tool_result.content)


def compact(segment: Segment) -> Segment:
    """Deterministic compaction. Pure — never mutates `segment`; returns a
    new `Segment`. Idempotent — see each helper's own marker-gated check.

    Runs before triage in `WorkerLoop.tick` (see the module docstring for
    why order matters here)."""
    events = segment.events
    kept: list[AgentEvent] = []
    i = 0
    while i < len(events):
        event = events[i]
        if (
            event.kind == "tool_use"
            and i + 1 < len(events)
            and events[i + 1].kind == "tool_result"
            and _is_trivial_readonly_pair(event, events[i + 1])
        ):
            i += 2  # drop both -- the read-only call and its tiny, clean result
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
