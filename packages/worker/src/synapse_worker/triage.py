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
LINT_CLEAN_RE = re.compile(
    r"(?i)(\(\d+ fixed, 0 remaining\)|all checks passed|0 errors|nothing to fix)"
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


def triage(segment: Segment) -> TriageDecision:
    prose = " ".join(e.content for e in segment.events
                     if e.kind == "text" and e.role == "assistant")

    # ── keep-signals, in order of confidence ────────────────────────────────
    if any(e.kind == "thinking" for e in segment.events):
        return TriageDecision(True, "thinking-present")
    if any(e.role == "system" and e.kind == "text" for e in segment.events):
        return TriageDecision(True, "system-note")  # compaction summaries etc.
    if DECISION_RE.search(prose) or any(
        e.kind == "thinking" and DECISION_RE.search(e.content) for e in segment.events
    ):
        return TriageDecision(True, "decision-language")

    tool_results = [e for e in segment.events if e.kind == "tool_result"]
    real_errors = [e for e in tool_results
                   if ERROR_RE.search(e.content) and not LINT_CLEAN_RE.search(e.content)]
    if real_errors:
        return TriageDecision(True, "error-signal")

    # ── skip-signatures — only reachable with zero keep-signals above ───────
    if any(LINT_CLEAN_RE.search(e.content) for e in tool_results):
        return TriageDecision(False, "lint-clean")
    tool_uses = [e for e in segment.events if e.kind == "tool_use"]
    if (tool_uses
            and all((e.tool_name or "") in READONLY_TOOLS for e in tool_uses)
            and len(prose) < SUBSTANTIAL_PROSE_CHARS):
        return TriageDecision(False, "readonly-run")

    return TriageDecision(True, "default-keep")
