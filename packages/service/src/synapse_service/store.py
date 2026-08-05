"""The storage seam (Plan C.2). In-memory first pass, deliberately.

RETRIEVABLE moved to fold.py in Plan E Task E.5 and lives there now, and only
there.

Upsert is FIRST-WRITE-WINS by Finding.id. The worker's write-ahead log replays
on retry and resync, and a replayed original must never clobber the tombstone
or trivia verdict synthesis wrote meanwhile. Producers never mutate a finding
after stamping it, so ignoring the replay is lossless.

Durability note: a service restart wipes this — by design, the recovery path
is every orchestrator's retained log + resync (amendment F Q5), which is why
upsert idempotency is tested harder than anything else here.
"""

from __future__ import annotations

import logging
import uuid
from dataclasses import replace

from synapse_contracts import (Conflict, Finding, FindingId, FindingStatus,
                               SessionContext, SynapseSession)

from synapse_service.fold import SupersessionCycleError, View
from synapse_service.lanes import DEFAULT_TOP_K, DEFAULT_TOPIC_LANE, CandidateSet
from synapse_service.memory import SharedMemory, TopicSummary

logger = logging.getLogger(__name__)


class InMemoryStore:
    """The multi-session REGISTRY. One SharedMemory per Shared Session.

    RETRIEVABLE is no longer defined here -- it lives in fold.py and only
    there. What lives here is the projection (adr/0004, Option A, closed
    2026-08-05): supersession and trivia are derived from the log, and the
    read accessors copy them back onto every Finding handed out so that
    `Finding.merged_into` still means what every consumer thinks it means.

    The projection is in the read accessors rather than at the ingest
    boundary (which is what adr/0004's Follow-up asks for) because this is the
    only component holding the View, so it is the narrowest place the
    projection can live WHERE NO CALLER CAN FORGET IT. See the ADR's
    Amendment for the full deviation record.
    """

    def __init__(self) -> None:
        self._sessions: dict[str, SynapseSession] = {}
        self._contexts: dict[str, SessionContext] = {}
        self._memories: dict[str, SharedMemory] = {}
        self._last_seen: dict[tuple[str, str], int] = {}

    # ── sessions ────────────────────────────────────────────────────────────
    def create_session(self, purpose: str, created_by: str, *,
                       shared_id: str | None = None) -> SynapseSession:
        """Create, or return an EXISTING session unchanged.

        Before this, the id was minted server-side only: after a restart the
        old sh-... 404s and cannot be recreated by construction, so every
        teammate has to re-join a brand-new session mid-demo. The documented
        recovery path (every orchestrator resyncs its retained log into the
        SAME shared_id) was not merely unbuilt -- it was impossible.

        Returning the existing session UNCHANGED is what makes this safe to
        call unconditionally, which is what `cmd_resync` does: a recovering
        client does not know whether the service still has the session, and
        must not overwrite a live one's purpose if it does."""
        if shared_id is not None and shared_id in self._sessions:
            return self._sessions[shared_id]
        shared_id = shared_id or f"sh-{uuid.uuid4().hex[:8]}"
        session = SynapseSession(shared_id=shared_id, purpose=purpose,
                                 members=[], created_by=created_by)
        self._sessions[shared_id] = session
        self._contexts[shared_id] = SessionContext(
            shared_id=shared_id, purpose=purpose, working_memory="")
        self._memories[shared_id] = SharedMemory(shared_id=shared_id, purpose=purpose)
        return session

    def get_session(self, shared_id: str) -> SynapseSession | None:
        return self._sessions.get(shared_id)

    def add_member(self, shared_id: str, contributor: str) -> None:
        session = self._sessions[shared_id]
        if contributor not in session.members:
            session.members.append(contributor)

    def session_ids(self) -> list[str]:
        """Every Shared Session this store knows about, creation order.

        Debug-only accessor (E6): the `/debug` session selector is the one
        caller that needs to enumerate sessions rather than look one up.
        """
        return list(self._sessions)

    # ── debug-only reads (E6) ────────────────────────────────────────────────
    # Two narrow additions for the /debug dashboard. Both are read accessors
    # onto structures this module already computes -- no new state, no write
    # path -- added here rather than reached into from synapse_service.debug
    # because this module's own docstring calls itself "the storage seam,
    # narrow on purpose", and a debug page reaching past that seam into
    # `_memories[sid]` would be exactly the kind of second entrance this
    # module exists to prevent.
    def log_entries(self, shared_id: str):
        """The raw append-only log, in order. CONTEXT.md: "the log IS the
        merge/topic feed" -- the debug page's log tail reads this directly
        rather than building a second feed alongside it."""
        return list(self._memories[shared_id].log)

    def view(self, shared_id: str) -> View:
        """The current fold. Debug-only: every product route reads through
        the narrower `retrievable`/`get`/`all_findings` projections above;
        the dashboard is the one place that wants the fold's own counts
        (visible/superseded/trivial) directly."""
        return self._memories[shared_id].view()

    # ── the projection (adr/0004, Option A) ─────────────────────────────────
    @staticmethod
    def _project(view: View, finding: Finding) -> Finding:
        """DEEP copy, deliberately. `model_copy(update=...)` alone is shallow:
        the result shares `attributions`, `refs` and `merged_from` with the
        record inside the fold, so `store.get(sid, x).attributions.append(...)`
        writes through into the log -- the same mutation-through-reference
        class Task 1 removed from synthesis, reintroduced one layer down.
        Synthesis reads `s.attributions` when composing a Synthesized Finding,
        so it is one line away from mattering."""
        return finding.model_copy(deep=True, update={
            "merged_into": view.superseded_by.get(finding.id),
            "status": (FindingStatus.TRIVIAL if finding.id in view.trivial
                       else FindingStatus.KEPT),
        })

    # ── findings ────────────────────────────────────────────────────────────
    def upsert(self, shared_id: str, findings: list[Finding]) -> int:
        """Append every finding; return the count of ids NOT PREVIOUSLY SEEN.

        Not "entries appended". The log records a resend because it happened;
        `accepted` stays 0 because nothing new arrived, and api.py's
        `if accepted:` is the only thing keeping a replayed POST off the
        provider.

        ONE fold for the whole batch: `seen` is computed once here and handed
        to each `append` as `is_new=`, rather than each append re-folding a
        log that the previous append just invalidated."""
        memory = self._memories[shared_id]
        seen = set(memory.view().findings)
        new = 0
        for finding in findings:
            is_new = finding.id not in seen
            if is_new:
                seen.add(finding.id)
                new += 1
            memory.append(finding, is_new=is_new)
        return new

    def get(self, shared_id: str, finding_id: str) -> Finding | None:
        memory = self._memories[shared_id]
        view = memory.view()
        finding = view.findings.get(finding_id)
        return None if finding is None else self._project(view, finding)

    def all_findings(self, shared_id: str) -> list[Finding]:
        view = self._memories[shared_id].view()
        return [self._project(view, f) for f in view.findings.values()]

    def retrievable(self, shared_id: str) -> list[Finding]:
        view = self._memories[shared_id].view()
        return [self._project(view, f) for f in view.visible()]

    def candidates(self, shared_id: str, text: str, *, top_k: int = DEFAULT_TOP_K,
                   exclude: frozenset[FindingId] = frozenset(),
                   topic_lane: bool = DEFAULT_TOPIC_LANE) -> CandidateSet:
        """The one lookup. Synthesis passes a finding's text; retrieval passes
        a teammate's question. Projected like every other read -- api.query
        serialises `c.finding` straight into the response body."""
        memory = self._memories[shared_id]
        result = memory.candidates(text, top_k=top_k, exclude=exclude,
                                   topic_lane=topic_lane)
        view = memory.view()
        return replace(result, candidates=tuple(
            replace(c, finding=self._project(view, c.finding))
            for c in result.candidates))

    def topic_summaries(self, shared_id: str, *, limit: int = 3,
                        only: frozenset[FindingId] | None = None) -> list[TopicSummary]:
        return self._memories[shared_id].topic_summaries(limit=limit, only=only)

    def resolve_forward(self, shared_id: str, finding_id: FindingId) -> FindingId:
        """Follow supersession forward to the live id a Conflict should name.

        The ONE resolver. synthesis.py carried its own copy with no depth cap
        while `View.resolve()` -- capped at MAX_SUPERSESSION_DEPTH=64 and
        raising rather than hanging -- was called by nothing. A malformed chain
        degrades to 'leave the conflict where it is', which is strictly better
        than a hung request in front of an audience."""
        view = self._memories[shared_id].view()
        try:
            return view.resolve(finding_id)
        except SupersessionCycleError:
            logger.warning("Supersession chain from %s is malformed; leaving the "
                           "conflict unresolved", finding_id)
            return finding_id

    # ── verdicts ────────────────────────────────────────────────────────────
    def supersede(self, shared_id: str, sources: list[FindingId],
                  result: Finding) -> None:
        """Land `result` and supersede every LIVE source.

        The `live` filter is what makes first-successor-wins true at this
        layer while the fold underneath is last-merge-wins (test_fold.py's
        test_the_fold_is_last_merge_wins_when_a_source_is_claimed_twice). When
        every named source is already superseded this becomes
        `merge(result, ())` -- an empty-sources Merged entry, which lands the
        result and supersedes nothing. Reachable only through a DIRECT call
        with all-dead sources (synthesis.py filters `merged_into is None` and
        `continue`s on empty sources, so the product never gets here); pinned
        by test_memory.py's
        test_merging_with_no_live_sources_still_records_the_result so the
        empty-tuple contract is stated rather than discovered.

        ⟨ACCEPTED CONSEQUENCE, 2026-08-05⟩ On that same empty-tuple path
        `Indexes.add` takes its `symbols.add` branch rather than
        `add_merged` (lanes.py:140-144), so the result does NOT inherit its
        sources' symbols. Left as-is deliberately: the path is unreachable
        from the product, and passing the ORIGINAL (dead) sources through
        would change branch behaviour on the live path too, two days out."""
        memory = self._memories[shared_id]
        view = memory.view()
        live = tuple(fid for fid in sources
                     if fid in view.findings and fid not in view.superseded_by)
        memory.merge(result, live)

    def mark_trivial(self, shared_id: str, finding_ids: list[FindingId]) -> None:
        memory = self._memories[shared_id]
        view = memory.view()
        live = tuple(fid for fid in finding_ids
                     if fid in view.findings
                     and fid not in view.superseded_by
                     and fid not in view.trivial)
        for missing in (set(finding_ids) - set(view.findings)):
            logger.warning("Trivial verdict for unknown id %s; ignored", missing)
        memory.mark_trivial(live)

    def set_context(self, shared_id: str, *, working_memory: str | None = None,
                    conflicts: list[Conflict] | None = None) -> None:
        """Write only the keyword arguments given. `None` means LEAVE ALONE --
        which is what preserves synthesis's behaviour when a verdict omits the
        working-memory rewrite (a schema gate that demands only ONE required key
        makes that a real case, not a hypothetical)."""
        ctx = self._contexts[shared_id]
        if working_memory is not None:
            ctx.working_memory = working_memory
        if conflicts is not None:
            ctx.conflicts = conflicts

    # ── context / versioning ────────────────────────────────────────────────
    def get_context(self, shared_id: str) -> SessionContext:
        return self._contexts[shared_id]

    def bump_version(self, shared_id: str) -> int:
        ctx = self._contexts[shared_id]
        ctx.memory_version += 1
        return ctx.memory_version

    def last_seen(self, shared_id: str, agent_session: str) -> int:
        return self._last_seen.get((shared_id, agent_session), 0)

    def mark_seen(self, shared_id: str, agent_session: str) -> None:
        self._last_seen[(shared_id, agent_session)] = (
            self._contexts[shared_id].memory_version)
