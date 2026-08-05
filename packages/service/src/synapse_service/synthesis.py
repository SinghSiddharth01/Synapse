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


def _resolve_forward(store: InMemoryStore, shared_id: str, finding_id: str) -> str:
    """Follow merged_into pointers to the live (or most-recently-synthesized)
    id a Conflict should reference, so it never dangles at a tombstone."""
    seen: set[str] = set()
    current = finding_id
    while current not in seen:
        seen.add(current)
        finding = store.get(shared_id, current)
        if finding is None or finding.merged_into is None:
            return current
        current = finding.merged_into
    return current                                       # cycle guard; should not happen


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

        raw_merges = verdicts.get("merges", [])
        if not isinstance(raw_merges, list):
            logger.warning("Synthesis verdict's 'merges' was %s, not a list; ignoring all "
                           "of it rather than crashing", type(raw_merges).__name__)
            raw_merges = []

        for merge in raw_merges:
            # AIC100Provider's schema gate (_satisfies_schema) only checks the
            # TOP-level object's required keys; it never inspects array items.
            # A schema_valid=True verdict can still carry a merge entry that
            # isn't a dict at all, or is missing source_ids/text/type, or has
            # source_ids of the wrong type. Skip just that entry rather than
            # crash mid-application -- a KeyError/TypeError here would leave
            # earlier tombstones written and memory_version un-bumped,
            # contradicting this module's own "zero loss" docstring.
            if (not isinstance(merge, dict)
                    or not isinstance(merge.get("source_ids"), list)
                    or not isinstance(merge.get("text"), str)
                    or "type" not in merge):
                logger.warning("Malformed merge verdict entry %r; skipping", merge)
                continue
            sources = [store.get(shared_id, fid) for fid in merge["source_ids"]
                       if isinstance(fid, str) and fid in known]
            sources = [s for s in sources if s is not None and s.merged_into is None]
            # Plan semantics (restored 2026-08-04, see docs/plans/exec/
            # 2026-08-04-e3-service.md L495-499): an unknown or already-
            # merged id in source_ids is logged and ignored, but does NOT
            # veto the ids that DID resolve -- "applying to the N valid
            # source(s)" is the plan's own log line. A prior round required
            # len(sources) >= 2 here (ADR 0002's "two or more"), which is a
            # reasonable-sounding rule but inverts a test the plan wrote out
            # by hand; the round-2 adjudication is to follow the plan
            # exactly, not the ADR's prose read narrowly.
            if len(sources) < len(merge["source_ids"]):
                logger.warning("Merge verdict referenced source_ids %s but only %d "
                               "resolved to a live finding; applying to the %d valid "
                               "source(s)", merge["source_ids"], len(sources), len(sources))
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

        raw_trivial = verdicts.get("trivial_ids", [])
        if not isinstance(raw_trivial, list):
            logger.warning("Synthesis verdict's 'trivial_ids' was %s, not a list; ignoring",
                           type(raw_trivial).__name__)
            raw_trivial = []
        for fid in raw_trivial:
            if not isinstance(fid, str):
                logger.warning("Malformed trivial id %r; skipping", fid)
                continue
            finding = store.get(shared_id, fid)
            if finding is None:
                logger.warning("Trivial verdict for unknown id %s; ignored", fid)
                continue
            if finding.merged_into is None:
                finding.status = FindingStatus.TRIVIAL

        raw_conflicts = verdicts.get("conflicts", [])
        if not isinstance(raw_conflicts, list):
            logger.warning("Synthesis verdict's 'conflicts' was %s, not a list; ignoring",
                           type(raw_conflicts).__name__)
            raw_conflicts = []
        for c in raw_conflicts:
            if (not isinstance(c, dict) or not isinstance(c.get("a"), str)
                    or not isinstance(c.get("b"), str) or not isinstance(c.get("description"), str)):
                logger.warning("Malformed conflict verdict entry %r; skipping", c)
                continue
            if c["a"] not in known or c["b"] not in known or c["a"] == c["b"]:
                continue
            ctx.conflicts.append(Conflict(finding_a=c["a"], finding_b=c["b"],
                                          description=c["description"]))

        # Resolve EVERY stored conflict -- old rounds' and this round's new
        # ones alike -- forward through merged_into, then dedup. Resolving
        # only THIS round's newly-reported pairs (and leaving conflicts
        # already sitting in ctx.conflicts untouched) left a Conflict
        # recorded in an EARLIER round dangling at a tombstone the moment a
        # LATER round merged that finding away -- exactly the case ADR 0002
        # names tombstones as needing to survive for ("Conflicts must
        # follow it forward" is not an audit nicety). Dedup runs on the
        # RESOLVED ids, after this pass, so a re-reported pair naming the
        # old id collides with the entry already resolved to the live id
        # instead of accumulating a duplicate -- the bounded candidate
        # window makes re-reporting the expected case, not a rare one.
        resolved: list[Conflict] = []
        seen_pairs: set[frozenset[str]] = set()
        for conflict in ctx.conflicts:
            ra = _resolve_forward(store, shared_id, conflict.finding_a)
            rb = _resolve_forward(store, shared_id, conflict.finding_b)
            if ra == rb:
                continue                  # both sides converged into the same finding
            key = frozenset((ra, rb))
            if key in seen_pairs:
                continue
            seen_pairs.add(key)
            resolved.append(Conflict(finding_a=ra, finding_b=rb,
                                     description=conflict.description))
        ctx.conflicts = resolved

        ctx.working_memory = verdicts.get("working_memory", ctx.working_memory)
        store.bump_version(shared_id)                               # 4. exactly once
        return ctx
