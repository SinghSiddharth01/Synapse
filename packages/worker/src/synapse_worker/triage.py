"""Deterministic keep/skip before the NPU — Plan A.5b, mandated by adr/0003.

The distiller compresses; it does not judge. This module is the upstream half
of the judgment it gave up (synthesis's trivia filter is the downstream half).

RECALL-TUNED, DELIBERATELY. Rules are ordered so every keep-signal is checked
before any skip-signature, and the default is keep. The asymmetry: a false
positive costs ~10s of NPU time; a false negative is knowledge permanently
lost with nothing anywhere to notice. fixtures/triage.json records two
ACCEPTED FALSE POSITIVES (seg-006, seg-007) as the price of that choice.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from synapse_contracts import Segment

# A clean lint/format report contains the word "errors" — match the clean shape
# BEFORE the error words, or seg-004 style runs read as failures.
# `\b0 errors\b`, not `0 errors`: without the left boundary this also matches
# "Found 10 errors", "130 errors" etc. — any count ending in 0 — which both
# vetoes the real error-signal keep and satisfies this skip-signature.
LINT_CLEAN_RE = re.compile(
    r"(?i)(\(\d+ fixed, 0 remaining\)|all checks passed|\b0 errors\b|nothing to fix)"
)
ERROR_RE = re.compile(
    r"(?i)(\btraceback\b|\bexception\b|\berror(s)?\b|\bfailed\b|\bfatal\b"
    r"|exit code [1-9]|non-zero exit)"
)
DECISION_RE = re.compile(
    r"(?i)(\binstead\b|\brather than\b|\bdead.?end\b|\bswitch(ing|ed)? to\b"
    r"|\brevert(ing|ed)?\b|\bturns out\b|\bgave up\b|\bwon'?t work\b|\bdoesn'?t work\b"
    r"|\babandon(ing|ed)?\b)"
)
READONLY_TOOLS = frozenset({"Read", "Grep", "Glob", "LS", "WebSearch", "WebFetch"})
# Below this much assistant prose, a read-only run is browsing, not analysis.
SUBSTANTIAL_PROSE_CHARS = 300


@dataclass(frozen=True)
class TriageDecision:
    keep: bool
    reason: str


def _has_uncleared_error(content: str) -> bool:
    """True if some LINE of `content` reads as an error not cleared on that
    same line.

    Per-line, not per-content: one Bash call routinely interleaves a clean
    lint pass with a failing test run (or a clean-count phrase like "0 errors"
    on one line and a real failure on another), and a clean-report phrase
    anywhere in the content must not veto a real error sitting on a different
    line of the same tool_result.
    """
    return any(
        ERROR_RE.search(line) and not LINT_CLEAN_RE.search(line)
        for line in content.splitlines()
    )


def triage(segment: Segment) -> TriageDecision:
    prose = " ".join(e.content for e in segment.events
                     if e.kind == "text" and e.role == "assistant")
    # Every line of dialogue, either role. Decision language and error reports
    # are just as durable when the HUMAN states them -- a user typing "we're
    # switching to X instead" or "prod is throwing a Traceback" is, in
    # practice, one of the two most common ways this knowledge enters a
    # transcript at all, and the skip-signatures below are evaluated over the
    # WHOLE segment. A keep-signal that only looked at assistant prose /
    # tool_results would be narrower than the skip rules it exists to
    # outrank, letting a human-stated decision or a human-reported error slip
    # through as readonly-run or lint-clean.
    all_text = " ".join(e.content for e in segment.events if e.kind == "text")

    # ── keep-signals, in order of confidence ────────────────────────────────
    if any(e.kind == "thinking" for e in segment.events):
        return TriageDecision(True, "thinking-present")
    if any(e.role == "system" and e.kind == "text" for e in segment.events):
        return TriageDecision(True, "system-note")  # compaction summaries etc.
    if DECISION_RE.search(all_text) or any(
        e.kind == "thinking" and DECISION_RE.search(e.content) for e in segment.events
    ):
        return TriageDecision(True, "decision-language")

    error_sources = [e for e in segment.events if e.kind in ("tool_result", "text")]
    real_errors = [e for e in error_sources if _has_uncleared_error(e.content)]
    if real_errors:
        return TriageDecision(True, "error-signal")

    tool_results = [e for e in segment.events if e.kind == "tool_result"]

    # ── skip-signatures — only reachable with zero keep-signals above ───────
    # `any`, not `all`, over tool_results: seg-004 (the corpus's canonical
    # lint-clean case) also has an `ls` and a `git diff --stat` tool_result
    # alongside the clean lint report -- neither of those reads as a
    # clean-report phrase, so requiring ALL tool_results to match means this
    # rule can only ever fire on a segment whose tool_results are exclusively
    # clean-report text, a shape that does not occur on real multi-tool-call
    # turns. A clean phrase anywhere still cannot veto a real error: the
    # error-signal keep-signal above already runs `_has_uncleared_error` over
    # every tool_result first. The prose-length gate is what stops a
    # lint-clean phrase embedded in unrelated content (a CI log that happens
    # to contain "all checks passed") from silently swallowing substantial
    # analysis sitting next to it.
    if (tool_results
            and any(LINT_CLEAN_RE.search(e.content) for e in tool_results)
            and len(prose) < SUBSTANTIAL_PROSE_CHARS):
        return TriageDecision(False, "lint-clean")
    tool_uses = [e for e in segment.events if e.kind == "tool_use"]
    if (tool_uses
            and all((e.tool_name or "") in READONLY_TOOLS for e in tool_uses)
            and len(prose) < SUBSTANTIAL_PROSE_CHARS):
        return TriageDecision(False, "readonly-run")

    return TriageDecision(True, "default-keep")
