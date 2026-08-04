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

_VALID_TYPES = {t.value for t in FindingType}


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
            result = await self.provider.complete(
                messages=messages, response_schema=RESPONSE_SCHEMA
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
            logger.warning(
                "Distiller: unparseable output for segment %s on attempt %d/%d",
                segment.id,
                attempt,
                MAX_ATTEMPTS,
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
