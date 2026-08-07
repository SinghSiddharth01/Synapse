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

import difflib
import logging
import re
import uuid
from collections.abc import Collection, Sequence
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


# --- why a FINDING was dropped ---------------------------------------------------
#
# The three above classify a whole unparseable *response*. This one classifies a
# single finding inside a well-formed one, alongside `dropped_invalid_type` and
# `dropped_empty_text`, and it is the only one of those three that is a content
# judgement rather than a schema one.
#
# WHAT IT IS FOR. Measured live on 2026-08-06 (docs/overnight/w7-live-evidence.md,
# F1): one contributed paragraph produced nine findings, and six of them were
# v4-condense's own few-shot outputs paraphrased — durable brokers and template
# caches the contributor never wrote a word about — landing in shared memory as
# `Provenance.CONTRIBUTED` under a real engineer's name. They were schema-valid,
# typed correctly, fluent, and passed every guard we had:
# `assert_prompt_conditioned` asks whether the model read its prompt (it did, far
# too well), and `test_fixture_contamination.py` checks the inverse direction —
# that no fixture duplicates a pack. Nothing checked the direction that failed.
#
# The proximate cause was the claude-cli arm flattening the few-shot turns into an
# unmarked blob (fixed in `claude_cli_provider._flatten_prompt`). This guard is
# here because that fix is unverifiable without a live call and applies to one arm:
# any model, on any arm, that reads its examples too literally produces the same
# poisoning, and the parse path is the last place to catch it before a Finding is
# minted and attributed.
DROP_EXAMPLE_ECHO = "example_echo"

# The measure: order-aware token similarity against the pack's own example
# outputs (`PromptPack.example_finding_texts`, derived from the few-shots so new
# examples are covered without touching this file).
#
# Order-aware rather than a bag of words, because a paraphrase keeps the sentence
# walking the same way and an unrelated finding that happens to reuse the
# vocabulary does not. Measured against the real W7 payload — the six echoes, the
# three genuine findings returned in the same response, six constructed findings
# about genuine queue/broker/cache/render/latency work, and every golden finding
# in the shipped fixture corpus scored against every shipped pack
# (packages/distiller/tests/test_example_echo.py carries all of them):
#
#   the six observed echoes                 0.667 - 0.938
#   the three genuine findings, same run    0.103 - 0.163
#   genuine work on the examples' topics    0.118 - 0.462
#   every shipped golden vs every pack      <= 0.160
#
# So the honest bar sits anywhere in 0.462 - 0.667, and 0.60 is placed nearer the
# top of that gap on purpose: a wrongly-kept echo is visible in the log line below
# and in the next query, while a wrongly-dropped finding is unrecoverable — the
# worker never re-reads a transcript position, as this module's docstring says.
ECHO_SIMILARITY_THRESHOLD = 0.60

# Below this many words a note is exempt entirely. Short strings collide by
# accident: "Page latency has not been measured." scores 0.600 against an example
# it shares no intent with, and "The queue was switched." is a subsequence of one.
# The pack asks for one or two sentences, so the observed echoes all run 15-28
# words and lose nothing to this floor, while the false-positive band sits under
# it. A guard that eats short real findings to catch short fake ones is a bad
# trade in the only direction that cannot be undone.
ECHO_MIN_WORDS = 10

_ECHO_WORD = re.compile(r"[a-z0-9]+")


def _echo_words(text: str) -> list[str]:
    """Words only, lowercased. Punctuation, possessives and hyphenation are what
    a paraphrase changes first, so none of them may carry weight here."""
    return _ECHO_WORD.findall(text.lower())


def example_echo_match(text: str, examples: Sequence[str]) -> tuple[float, str]:
    """Closest of the pack's own example outputs, and how close it sits.

    Returns `(0.0, "")` for a note too short to judge — see `ECHO_MIN_WORDS` —
    so the caller never has to special-case length, exactly as
    `distinct_shingle_ratio` returns 1.0 for text too short to call a loop.
    """
    words = _echo_words(text)
    if len(words) < ECHO_MIN_WORDS:
        return 0.0, ""

    best, nearest = 0.0, ""
    for example in examples:
        other = _echo_words(example)
        if not other:
            continue
        score = difflib.SequenceMatcher(None, words, other).ratio()
        if score > best:
            best, nearest = score, example
        if best >= 1.0:
            break
    return best, nearest


def is_example_echo(text: str, examples: Sequence[str]) -> bool:
    """Whether this finding is the prompt's own example wearing a contributor's name."""
    return example_echo_match(text, examples)[0] >= ECHO_SIMILARITY_THRESHOLD


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
    # Findings that were the pack's own few-shot output handed back as extraction.
    # A counter AND the texts, because the counter alone cannot tell an operator
    # whether the guard is doing its job or eating real work — and the eval
    # harness needs to be able to say which example was echoed. See
    # DROP_EXAMPLE_ECHO.
    dropped_example_echo: int = 0
    example_echoes: list[str] = field(default_factory=list)
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
    #
    # Example-echo drops are deliberately NOT appended here: this list is one
    # entry per increment of `dropped_malformed` (a whole response that would not
    # parse), and an echo is one finding inside a response that parsed fine. They
    # share the `reason=` log convention so an operator greps for both the same
    # way; they do not share a counter, because a reader comparing
    # `len(drop_reasons)` against `dropped_malformed` must keep getting the same
    # number.
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
        # Read once, off whichever pack is wired in, so the echo guard covers a
        # pack swapped at runtime and any example added to a pack's TOML without
        # anyone remembering this line exists.
        self._example_texts = self.pack.example_finding_texts

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

            # LAST GATE BEFORE A Finding EXISTS. Everything past this point is
            # attributed to a named human and treated downstream as that human's
            # verified experience, so the pack's own fiction has to stop here.
            similarity, nearest = example_echo_match(text, self._example_texts)
            if similarity >= ECHO_SIMILARITY_THRESHOLD:
                stats.dropped_example_echo += 1
                stats.example_echoes.append(text)
                logger.warning(
                    "Distiller: dropping finding from segment %s; reason=%s "
                    "similarity=%.2f threshold=%.2f pack=%s "
                    "echoed_example=%r text=%r",
                    segment.id,
                    DROP_EXAMPLE_ECHO,
                    similarity,
                    ECHO_SIMILARITY_THRESHOLD,
                    self.pack.name,
                    nearest[:120],
                    text[:160],
                )
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
