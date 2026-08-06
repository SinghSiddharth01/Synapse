"""Distiller — Segment → Finding[].

The most expensive step in the system (~14 tok/s at 4B on the NPU) and an
unrepeatable one: the worker never re-reads a transcript position, so a finding
lost here is device work permanently gone. That is why the caller persists
findings the moment this returns, *before* attempting any send.

The egress rule lives upstream of this module, but this is where it is enforced
in practice: raw transcript content goes in, and only abstracted `Finding`s come
out. Nothing else in the system ever sees the Segment.
"""

from __future__ import annotations

import logging
import uuid
from collections.abc import Collection
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

from synapse_contracts import (
    Attribution,
    Finding,
    FindingType,
    LocalBinding,
    Provenance,
    Segment,
)
from synapse_providers import ModelProvider

from synapse_distiller.guards import assert_prompt_conditioned
from synapse_distiller.prompt import (
    DEFAULT_KINDS,
    DEFAULT_RENDER_STYLE,
    RESPONSE_SCHEMA,
    build_messages,
)
from synapse_distiller.promptpack import PromptPack, load_pack_by_name

logger = logging.getLogger(__name__)

# One retry, then drop. Not a tunable: a second malformed response from a
# temperature-0 model is evidence the prompt or the model is wrong, and
# retrying past that burns NPU time that the next Segment needs.
MAX_ATTEMPTS = 2

# Appended to the SECOND attempt only. See the call site for why a retry has to
# change the prompt to be a retry at all.
RETRY_NUDGE = {
    "role": "user",
    "content": (
        "Your previous reply was not valid JSON. Reply with ONLY the JSON "
        'object — {"findings": [...]} — and no prose, no code fence, and no '
        "text before or after it."
    ),
}

_VALID_TYPES = {t.value for t in FindingType}

# --- why a segment was dropped ---------------------------------------------------
#
# `dropped_malformed` counts three genuinely different failures identically, and
# each has a different fix:
#
#   OVER_BUDGET   the response was cut off at the provider's max_tokens. The
#                 model was fine; the budget was wrong. Fix: raise the reserve
#                 or shrink the segment. This is the failure 7418a63's clamp and
#                 c077a51's salvage both exist for.
#   DEGENERATE    the model looped — the same phrase repeated until the cap or
#                 until it stopped. A small model near its context ceiling does
#                 this, and raising max_tokens makes it WORSE (more loop). Fix:
#                 the prompt, the model, or less context.
#   MALFORMED     the text was neither; it just did not parse. Prose around the
#                 JSON, a stray fence, a refusal.
#
# Raising the cap in response to a DEGENERATE drop is the specific wrong move
# this distinction is here to prevent, which is why the two are never collapsed.
DROP_OVER_BUDGET = "over-budget"
DROP_DEGENERATE = "degenerate-repetition"
DROP_MALFORMED = "malformed"

# Shingle width for the repetition measure. 6 words is long enough that ordinary
# English (and ordinary JSON key sequences) do not repeat a shingle by accident,
# and short enough that a loop is caught within a couple of cycles.
_SHINGLE_WORDS = 6
# Below this fraction of DISTINCT shingles, the text is a loop rather than
# prose. Measured against the two shapes in the wild: a truncated findings
# object runs ~0.97-1.0 distinct, and `"not json " * 200` runs ~0.01.
_DEGENERATE_DISTINCT_RATIO = 0.5
# Under this many shingles the ratio is noise, so short text is never called
# degenerate — a 20-word reply that happens to repeat is not a loop.
_MIN_SHINGLES_TO_JUDGE = 12


def distinct_shingle_ratio(text: str, width: int = _SHINGLE_WORDS) -> float:
    """Fraction of the text's word-shingles that are distinct.

    1.0 means nothing repeated; near 0 means the model emitted the same window
    over and over. Deliberately cheap — this runs on a failure path where the
    expensive work (the model call) has already been spent, but it must not
    itself become a cost on a long response, so it is one pass and a set.

    Returns 1.0 for text too short to judge, so `is_degenerate` never fires on
    a fragment (see `_MIN_SHINGLES_TO_JUDGE`).
    """
    words = text.split()
    if len(words) < width + _MIN_SHINGLES_TO_JUDGE:
        return 1.0
    shingles = [
        " ".join(words[i : i + width]) for i in range(len(words) - width + 1)
    ]
    return len(set(shingles)) / len(shingles)


def is_degenerate(text: str) -> bool:
    """Whether the response is a repetition loop rather than damaged JSON."""
    return distinct_shingle_ratio(text) < _DEGENERATE_DISTINCT_RATIO


def classify_drop(text: str, output_tokens: int, max_tokens: int | None) -> str:
    """Which of the three failures produced this unparseable response.

    Over-budget is checked FIRST and on the token count, not on the text: a
    response cut at the cap is over-budget whatever its shape, and a loop that
    ran until the cap is still, operationally, a response that needed a bigger
    budget to have any chance — reporting it as a loop would send the reader
    after the prompt when the cap is what truncated the evidence.

    `max_tokens` is None for a provider that does not expose one (FakeProvider,
    ClaudeCliProvider), in which case over-budget is simply not knowable from
    here and the text-shaped distinction still applies.
    """
    if max_tokens is not None and output_tokens >= max_tokens > 0:
        return DROP_OVER_BUDGET
    if is_degenerate(text):
        return DROP_DEGENERATE
    return DROP_MALFORMED


@dataclass
class DistillStats:
    """What happened, for the eval harness. Not part of any contract."""

    attempts: int = 0
    dropped_malformed: int = 0
    dropped_invalid_type: int = 0
    dropped_empty_text: int = 0
    latency_ms: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    raw_outputs: list[str] = field(default_factory=list)
    # Which prompt produced this. A result that cannot name its pack cannot be
    # compared against another run, and the pack changes daily by design.
    prompt_pack: str = ""
    # Which AgentEvent kinds reached the model. Same reasoning: the input
    # distribution is now a setting, so a result that cannot name it is not
    # comparable to one taken under different kinds.
    kinds: tuple[str, ...] = ()
    render_style: str = ""
    # True when the kind filter left nothing to distil and no call was made.
    skipped_empty: bool = False
    # Why each unparseable attempt was unparseable, one entry per increment of
    # `dropped_malformed`. See `classify_drop` — a counter alone cannot tell a
    # segment that overran its output budget (raise the reserve, or shrink the
    # segment) from one the model looped on (a prompt or model problem) from one
    # it simply wrapped in prose (a parser problem). All three used to land here
    # as the same number and the same sentence.
    drop_reasons: list[str] = field(default_factory=list)


class Distiller:
    """Turns one Segment into validated Findings, stamped with Attribution."""

    def __init__(
        self,
        provider: ModelProvider,
        binding: LocalBinding,
        pack: PromptPack | None = None,
        kinds: Collection[str] | None = None,
        render_style: str = DEFAULT_RENDER_STYLE,
    ) -> None:
        self.provider = provider
        self.binding = binding
        # Defaulting keeps the common case a two-argument call, but the pack is
        # always explicit in the record so a result is never ambiguous.
        self.pack = pack if pack is not None else load_pack_by_name("v2-hardened")
        self.kinds = tuple(kinds) if kinds is not None else DEFAULT_KINDS
        self.render_style = render_style

    async def distil(self, segment: Segment) -> tuple[list[Finding], DistillStats]:
        stats = DistillStats(
            prompt_pack=self.pack.name,
            kinds=self.kinds,
            render_style=self.render_style,
        )

        # If the kind filter leaves nothing, there is nothing to distil. Calling
        # the model with an empty segment would spend NPU time on a prompt that
        # contains only instructions and few-shots — from which a small model
        # will happily invent a finding about the few-shots themselves.
        if not any(event.kind in set(self.kinds) for event in segment.events):
            stats.skipped_empty = True
            logger.info(
                "Distiller: segment %s has no events of kind %s; skipping",
                segment.id,
                list(self.kinds),
            )
            return [], stats

        messages = build_messages(segment, self.pack, self.kinds, self.render_style)

        parsed: dict[str, Any] | None = None
        for attempt in range(1, MAX_ATTEMPTS + 1):
            stats.attempts = attempt
            # Resending byte-identical messages to a temperature-0 model cannot
            # produce a different decode — it is the same call, and the retry
            # was observed reproducing the first failure exactly. So the second
            # attempt changes the prompt, which is the only lever available
            # without varying temperature (a config-level property here).
            # aic100.py states the same principle for the same reason.
            attempt_messages = messages if attempt == 1 else messages + [RETRY_NUDGE]
            result = await self.provider.complete(
                messages=attempt_messages, response_schema=RESPONSE_SCHEMA
            )

            # Before anything is read from the response. A model that dropped its
            # prompt produces fluent, schema-plausible findings invented from
            # nothing, and they would flow straight into shared team memory.
            assert_prompt_conditioned(
                result, context=f"segment {segment.id}, attempt {attempt}"
            )

            stats.latency_ms += result.latency_ms
            stats.input_tokens += result.usage.input_tokens
            stats.output_tokens += result.usage.output_tokens
            stats.raw_outputs.append(
                result.data if isinstance(result.data, str) else ""
            )

            if isinstance(result.data, dict) and "findings" in result.data:
                parsed = result.data
                break

            stats.dropped_malformed += 1
            raw = stats.raw_outputs[-1] if stats.raw_outputs else ""
            # WHICH of the three failures this was. The counter cannot say, and
            # the three have opposite fixes — raising max_tokens is the right
            # move for over-budget and the wrong one for a repetition loop,
            # which only gets longer with more room. See `classify_drop`.
            reason = classify_drop(
                raw, result.usage.output_tokens, getattr(self.provider, "max_tokens", None)
            )
            stats.drop_reasons.append(reason)
            # The raw text is the rest of the diagnosis — truncated mid-object,
            # prose wrapped around the JSON, or the repetition a small model
            # falls into near its context ceiling all look identical without it,
            # and it was already being captured into stats and then never shown.
            logger.warning(
                "Distiller: unparseable output for segment %s on attempt %d/%d; "
                "reason=%s raw[:200]=%r",
                segment.id,
                attempt,
                MAX_ATTEMPTS,
                reason,
                raw[:200],
            )

        if parsed is None:
            logger.error(
                "Distiller: dropping segment %s after %d attempts", segment.id, MAX_ATTEMPTS
            )
            return [], stats

        return self._to_findings(parsed, segment, stats), stats

    def _to_findings(
        self, parsed: dict[str, Any], segment: Segment, stats: DistillStats
    ) -> list[Finding]:
        raw_findings = parsed.get("findings")
        if not isinstance(raw_findings, list):
            stats.dropped_malformed += 1
            return []

        attribution = Attribution(
            contributor=self.binding.contributor,
            agent_session=self.binding.agent_session_id,
            agent=self.binding.agent,
        )
        now = datetime.now(timezone.utc)

        findings: list[Finding] = []
        for item in raw_findings:
            if not isinstance(item, dict):
                stats.dropped_malformed += 1
                continue

            finding_type = str(item.get("type", "")).strip().lower()
            if finding_type not in _VALID_TYPES:
                # A small model inventing a fifth type is a prompt failure, not a
                # taxonomy signal. Drop rather than coerce — coercing would file
                # the finding under a type its author did not mean.
                stats.dropped_invalid_type += 1
                logger.warning(
                    "Distiller: dropping finding with unknown type %r (segment %s)",
                    item.get("type"),
                    segment.id,
                )
                continue

            text = str(item.get("text", "")).strip()
            if not text:
                stats.dropped_empty_text += 1
                continue

            findings.append(
                Finding(
                    # Client-assigned at distil time so a retried push is idempotent
                    # (ingest upserts by id) and lineage can reference it.
                    id=str(uuid.uuid4()),
                    type=FindingType(finding_type),
                    text=text,
                    attributions=[attribution],
                    ts=now,
                    provenance=Provenance.DISTILLED,
                    # status / merged_from / merged_into are service-written.
                    # Producers leave them at defaults.
                )
            )
        return findings
