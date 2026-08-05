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

from pydantic import BaseModel, Field, ValidationError

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


class _MergeVerdict(BaseModel):
    source_ids: list[str]
    text: str
    type: str


class _ConflictVerdict(BaseModel):
    a: str
    b: str
    description: str


class _SynthesisVerdicts(BaseModel):
    """Structural shape of one synthesis verdict, validated as ONE unit
    before any mutation touches the store (round-2 adjudication on finding
    "malformed verdicts crashing merge mid-application").

    AIC100Provider's schema gate (_satisfies_schema) only checks the
    TOP-level object's required keys; it never inspects array items, so a
    schema_valid=True verdict can still carry a merge/conflict entry that's
    missing a key or holds the wrong type. The previous approach defended
    each nested access individually and skipped just the bad entry --
    which sounds tolerant but is actually PARTIAL APPLICATION: entries
    before the bad one land, entries after it are silently dropped, and
    which entries ran first is an implementation accident (list order),
    not a decision anyone made. That contradicts this module's own "zero
    loss" docstring and invariant 5's premise that a merge is the only
    irreversible action in the system.

    Validating the whole payload up front makes the failure mode binary
    and honest: either the ENTIRE verdict is structurally sound and all of
    it applies, or none of it does and findings stay exactly as durably
    landed by step 1 -- the same "model failed, quality degrades, nothing
    is lost" path already used for a provider exception or
    schema_valid=False. `working_memory` is optional (None) rather than
    required so a verdict that answers everything else but omits the
    working-memory rewrite (the schema gate only demands ONE required key
    be present, by design -- see aic100._satisfies_schema) still validates;
    the merge loop below leaves ctx.working_memory untouched in that case.
    """

    working_memory: str | None = None
    merges: list[_MergeVerdict] = Field(default_factory=list)
    trivial_ids: list[str] = Field(default_factory=list)
    conflicts: list[_ConflictVerdict] = Field(default_factory=list)



class Synthesizer:
    def __init__(self, provider: ModelProvider) -> None:
        self.provider = provider

    async def merge(self, store: InMemoryStore, shared_id: str,
                    new_findings: list[Finding]) -> SessionContext:
        store.upsert(shared_id, new_findings)                       # 1. durability first
        ctx = store.get_context(shared_id)

        # CANDIDATE_WINDOW bounds the merge prompt against LOG GROWTH (Plan
        # C.4's fixed-cost property) -- it must never bound it against the
        # CURRENT push. Candidates are the union of every finding just
        # accepted in THIS call (regardless of how many that is) plus up
        # to CANDIDATE_WINDOW of the most recent OTHER retrievable
        # findings. A worker flushing a backlog after being offline is the
        # normal case for the write-ahead log this system is built around
        # and can easily exceed the window in one push; those findings are
        # not "old", they are simply new to synthesis for the first time,
        # and a pure recency slice of the whole Log silently drops them
        # forever (they age out of the window the moment anything newer
        # arrives, in this round or any later one).
        retrievable = store.retrievable(shared_id)
        new_ids = {f.id for f in new_findings}
        pushed = [f for f in retrievable if f.id in new_ids]

        if pushed:
            # One candidate lookup per pushed finding, unioned and deduped.
            # A pure recency slice made an old near-duplicate permanently
            # unmergeable -- the ADR 0002 merge stopped happening as the
            # session grew, silently, with nothing reporting the loss.
            #
            # COST, named: this is O(pushed x log) select() sweeps. ~2.5 ms per
            # sweep at 422 findings, so microseconds-to-milliseconds at demo
            # scale. The PROMPT is fixed-cost; the compute is not.
            best: dict[str, tuple[float, Finding]] = {}
            # Why a candidate was surfaced, kept for the prompt. A shared rare
            # identifier is close to proof, and telling the model so costs a
            # few tokens per line (lanes.Candidate.render's own argument, which
            # otherwise reaches no prompt in this integration).
            marks: dict[str, frozenset[str]] = {}
            for finding in pushed:
                result = store.candidates(shared_id, finding.text,
                                          exclude=frozenset(new_ids))
                for candidate in result.candidates:
                    prior = best.get(candidate.finding.id)
                    if prior is None or candidate.score > prior[0]:
                        best[candidate.finding.id] = (candidate.score, candidate.finding)
                    if candidate.shared_symbols:
                        marks[candidate.finding.id] = (
                            marks.get(candidate.finding.id, frozenset())
                            | candidate.shared_symbols)
            # Rank the UNION before truncating. Truncating dict-insertion order
            # instead lets the first two pushed findings fill all 20 slots, so
            # findings 3..N of a backlog flush contribute no partners at all --
            # the same starvation the recency slice caused, differently shaped.
            others = [f for _, f in
                      sorted(best.values(), key=lambda sf: (-sf[0], sf[1].id))
                      ][:CANDIDATE_WINDOW]
        else:
            # The /synthesize self-heal path passes no new findings, so there
            # is no text to search WITH. Recency is the only rule available,
            # and this is exactly what that route did before lanes existed.
            others = [f for f in retrievable if f.id not in new_ids][-CANDIDATE_WINDOW:]
            marks = {}

        candidates = pushed + others
        if not candidates:
            return ctx

        def _line(f: Finding) -> str:
            base = f"[{f.id}] ({f.type.value}) {f.text}"
            shared = marks.get(f.id)
            # Parentheses, never brackets: test helpers parse `[...]` out of
            # this listing as finding ids, and a bracketed marker would read as
            # a phantom id in every one of them.
            return base + (f"  (shares: {', '.join(sorted(shared))})" if shared else "")

        listing = "\n".join(_line(f) for f in candidates)
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

        # Validate the ENTIRE verdict as one unit before touching the store.
        # An 8B's schema_valid=True only proves the top-level keys are
        # present (see _SynthesisVerdicts' docstring) -- a malformed nested
        # entry anywhere invalidates the whole verdict, never just that
        # entry. No partial application, ever.
        try:
            verdicts = _SynthesisVerdicts.model_validate(result.data)
        except ValidationError:
            logger.warning("Synthesis verdict for %s failed structural validation; "
                           "findings are landed, memory unchanged", shared_id, exc_info=True)
            return ctx

        known = {f.id for f in store.all_findings(shared_id)}

        for merge in verdicts.merges:
            sources = [store.get(shared_id, fid) for fid in merge.source_ids if fid in known]
            sources = [s for s in sources if s is not None and s.merged_into is None]
            # Plan semantics (restored 2026-08-04, see docs/plans/exec/
            # 2026-08-04-e3-service.md L495-499): an unknown or already-
            # merged id in source_ids is logged and ignored, but does NOT
            # veto the ids that DID resolve -- "applying to the N valid
            # source(s)" is the plan's own log line.
            if len(sources) < len(merge.source_ids):
                logger.warning("Merge verdict referenced source_ids %s but only %d "
                               "resolved to a live finding; applying to the %d valid "
                               "source(s)", merge.source_ids, len(sources), len(sources))
            if not sources:
                continue
            attributions: list[Attribution] = []
            for s in sources:
                for a in s.attributions:
                    if a not in attributions:
                        attributions.append(a)
            try:
                ftype = FindingType(merge.type)
            except ValueError:
                ftype = FindingType.LEARNING       # distiller types are best-effort anyway
            synthesized = Finding(
                id=f"syn-{uuid.uuid4().hex[:8]}",
                type=ftype,
                text=merge.text,
                attributions=attributions,
                ts=datetime.now(timezone.utc),
                provenance=Provenance.SYNTHESIZED,
                merged_from=[s.id for s in sources],
            )
            store.supersede(shared_id, [s.id for s in sources], synthesized)

        store.mark_trivial(shared_id, list(verdicts.trivial_ids))

        # Accumulate locally. `ctx.conflicts.append(...)` was a mutation
        # through a reference just as much as an assignment was; it survives a
        # mutable store and vanishes on a projecting one.
        pending: list[Conflict] = list(ctx.conflicts)
        for c in verdicts.conflicts:
            if c.a not in known or c.b not in known or c.a == c.b:
                continue
            pending.append(Conflict(finding_a=c.a, finding_b=c.b,
                                    description=c.description))

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
        for conflict in pending:                # was: for conflict in ctx.conflicts
            ra = store.resolve_forward(shared_id, conflict.finding_a)
            rb = store.resolve_forward(shared_id, conflict.finding_b)
            if ra == rb:
                continue                  # both sides converged into the same finding
            key = frozenset((ra, rb))
            if key in seen_pairs:
                continue
            seen_pairs.add(key)
            resolved.append(Conflict(finding_a=ra, finding_b=rb,
                                     description=conflict.description))

        store.set_context(shared_id, working_memory=verdicts.working_memory,
                          conflicts=resolved)
        store.bump_version(shared_id)           # 4. exactly once per VERDICT ROUND
        return store.get_context(shared_id)
