"""Semantic merge — ADR 0002 made executable.

Order of operations is load-bearing:
  1. upsert the new findings           (durability before intelligence)
  2. one model call over WM + a BOUNDED candidate window
  3. apply verdicts: new SYNTHESIZED findings, tombstones, TRIVIAL, conflicts
  4. bump memory_version exactly once

A model failure after step 1 leaves findings landed and version unchanged —
degraded quality, zero loss. Unknown ids in verdicts are logged and ignored:
an 8B inventing an id must not crash ingest.
"""

from __future__ import annotations

import logging
import uuid
from datetime import datetime, timezone

from synapse_contracts import (Attribution, Conflict, Finding, FindingStatus,
                               FindingType, Provenance, SessionContext)
from synapse_providers import ModelProvider

from synapse_service.store import InMemoryStore

logger = logging.getLogger(__name__)

# The fixed-cost property (Plan C.4): the merge prompt sees the Working Memory
# plus a bounded window of recent candidates, never the whole Log — otherwise
# merge cost grows with session length and the WM's whole reason to exist dies.
CANDIDATE_WINDOW = 20

SYNTH_SCHEMA = {
    "type": "object",
    "properties": {
        "working_memory": {"type": "string"},
        "merges": {"type": "array", "items": {"type": "object", "properties": {
            "source_ids": {"type": "array", "items": {"type": "string"}},
            "text": {"type": "string"},
            "type": {"type": "string"},
        }, "required": ["source_ids", "text", "type"]}},
        "trivial_ids": {"type": "array", "items": {"type": "string"}},
        "conflicts": {"type": "array", "items": {"type": "object", "properties": {
            "a": {"type": "string"}, "b": {"type": "string"},
            "description": {"type": "string"},
        }, "required": ["a", "b", "description"]}},
    },
    "required": ["working_memory", "merges", "trivial_ids", "conflicts"],
}

SYNTH_SYSTEM = (
    "You maintain one team's shared working memory. Rewrite the working memory "
    "(under 500 words) from the current memory plus the new findings. Where two "
    "findings state the same fact, merge them: emit ONE merged text capturing the "
    "essence of BOTH — never drop a qualifier one of them adds. Mark findings that "
    "merely restate actions without insight as trivial. Where two findings "
    "contradict each other, report a conflict — do not resolve it. Return JSON "
    "matching the schema exactly, using finding ids verbatim."
)


class Synthesizer:
    def __init__(self, provider: ModelProvider) -> None:
        self.provider = provider

    async def merge(self, store: InMemoryStore, shared_id: str,
                    new_findings: list[Finding]) -> SessionContext:
        store.upsert(shared_id, new_findings)                       # 1. durability first
        ctx = store.get_context(shared_id)
        candidates = store.retrievable(shared_id)[-CANDIDATE_WINDOW:]   # bounded, always
        if not candidates:
            return ctx

        listing = "\n".join(f"[{f.id}] ({f.type.value}) {f.text}" for f in candidates)
        messages = [
            {"role": "system", "content": SYNTH_SYSTEM},
            {"role": "user", "content":
                f"PURPOSE: {ctx.purpose}\n\nCURRENT WORKING MEMORY:\n"
                f"{ctx.working_memory or '(empty)'}\n\nFINDINGS:\n{listing}"},
        ]
        try:
            result = await self.provider.complete(messages, response_schema=SYNTH_SCHEMA)
        except Exception:                                           # noqa: BLE001
            logger.exception("Synthesis failed for %s; findings are landed, memory unchanged",
                             shared_id)
            return ctx

        # Every real provider's documented failure path hands back something
        # that isn't a verdicts dict: AIC100Provider returns data=None with
        # schema_valid=False once its retry is exhausted, and
        # OpenAICompatibleProvider/NPUProvider return the raw text string when
        # tolerant parsing fails. Only FakeProvider always returns a dict, so
        # this must be checked explicitly rather than trusted.
        if not result.schema_valid or not isinstance(result.data, dict):
            logger.warning("Synthesis returned schema_valid=%s data of type %s for %s; "
                           "findings are landed, memory unchanged",
                           result.schema_valid, type(result.data).__name__, shared_id)
            return ctx
        verdicts = result.data

        known = {f.id for f in store.all_findings(shared_id)}

        for merge in verdicts.get("merges", []):
            sources = [store.get(shared_id, fid) for fid in merge["source_ids"]
                       if fid in known]
            sources = [s for s in sources if s is not None and s.merged_into is None]
            if len(sources) < 2 and len(merge["source_ids"]) >= 2:
                logger.warning("Merge verdict referenced unknown/merged ids %s; applying "
                               "to the %d valid source(s)", merge["source_ids"], len(sources))
            if not sources:
                continue
            attributions: list[Attribution] = []
            for s in sources:
                for a in s.attributions:
                    if a not in attributions:
                        attributions.append(a)
            try:
                ftype = FindingType(merge["type"])
            except ValueError:
                ftype = FindingType.LEARNING       # distiller types are best-effort anyway
            synthesized = Finding(
                id=f"syn-{uuid.uuid4().hex[:8]}",
                type=ftype,
                text=merge["text"],
                attributions=attributions,
                ts=datetime.now(timezone.utc),
                provenance=Provenance.SYNTHESIZED,
                merged_from=[s.id for s in sources],
            )
            store.upsert(shared_id, [synthesized])
            for s in sources:
                s.merged_into = synthesized.id                     # tombstone: text stays

        for fid in verdicts.get("trivial_ids", []):
            finding = store.get(shared_id, fid)
            if finding is None:
                logger.warning("Trivial verdict for unknown id %s; ignored", fid)
                continue
            if finding.merged_into is None:
                finding.status = FindingStatus.TRIVIAL

        for c in verdicts.get("conflicts", []):
            if c["a"] in known and c["b"] in known:
                ctx.conflicts.append(Conflict(finding_a=c["a"], finding_b=c["b"],
                                              description=c["description"]))

        ctx.working_memory = verdicts.get("working_memory", ctx.working_memory)
        store.bump_version(shared_id)                               # 4. exactly once
        return ctx
