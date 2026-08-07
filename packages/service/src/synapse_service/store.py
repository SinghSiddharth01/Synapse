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
                               SessionContext, SessionStatus, SynapseSession)

from synapse_service.fold import SupersessionCycleError, View
from synapse_service.log import FindingAppended
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
        # Departures this PROCESS has watched, per session. Not the member
        # list and not the log -- see `remove_member` for why departure is in
        # neither. It exists so that "not in `members`" can be told apart from
        # "left", which are three different situations wearing one shape:
        # never registered, registered elsewhere before a restart, and actually
        # departed. Anything that reads this must treat absence as UNKNOWN
        # rather than as "did not leave".
        #
        # Keyed `(contributor, agent_session)` since 2026-08-06, where the
        # agent session is the WINDOW that left and `None` means "the person,
        # every window of them". Two Claude Code windows are two participants
        # (W2) and one of them leaving is not the other leaving -- but
        # `members` is a list of contributor STRINGS, so the DELETE that
        # detached one window marked both. Observed: a `leave_session` from one
        # window flipped both of that person's rows on /debug to LEFT while the
        # other window was still bound and still pushing. The roster stays
        # person-level (it gates `end_session`, and "is this human still here?"
        # is the question it answers); only departure gained the second
        # dimension, because it is the only one that has to distinguish them.
        self._departed: dict[str, set[tuple[str, str | None]]] = {}
        # How many findings had EVER entered a session's memory when this
        # contributor last read it (W5). A second watermark alongside
        # `_last_seen`, and deliberately not a replacement for it: they answer
        # different questions and only one of them can answer each.
        # `_last_seen` holds a `memory_version`, which counts VERDICT ROUNDS
        # and is what "the memory has moved N versions" means; it cannot say
        # WHICH findings moved, because a finding carries no version stamp and
        # is queryable the instant it is pushed, whole synthesis rounds before
        # any version bump covers it. This counts arrivals, so "what landed
        # since you last looked" is a slice rather than an inference. Keyed by
        # CONTRIBUTOR for the same reason `_last_seen` is (decisions/001): a
        # new conversation is the same person and must not replay the memory.
        self._seen_count: dict[tuple[str, str], int] = {}

    # ── sessions ────────────────────────────────────────────────────────────
    def create_session(self, purpose: str, created_by: str | None, *,
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
        must not overwrite a live one's purpose if it does.

        `created_by` may be None since 2026-08-06 -- "recreated after a restart
        and this machine has no record of who created it" (see
        `SynapseSession.created_by` for why that is a None and not a sentinel).
        The unchanged-on-return rule above is what makes accepting it safe:
        a resync that genuinely knows nothing POSTs None, and if the service is
        actually LIVE the existing session comes back with its real creator
        intact. Recovery must never DOWNGRADE a live session's ownership to
        unknown, and that property is this one `return` rather than a check --
        pinned by test_lifecycle.py's
        test_recreating_a_live_session_with_created_by_none_does_not_downgrade_it.
        """
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

    def add_member(self, shared_id: str, contributor: str,
                   agent_session: str | None = None) -> None:
        """Join. `agent_session` names the WINDOW joining, when the caller
        knows it.

        Re-joining retracts the departure: someone who left and came back is
        present, not "left", and leaving the marker would make the next
        DELETE-less absence read as a second departure that never happened.
        WHICH departure it retracts is the whole reason for the argument. A
        named window retracts only its own; an unnamed join retracts only the
        person-level marker, and deliberately leaves per-window ones standing.

        That asymmetry is load-bearing. `Relay._register_members` POSTs the
        contributor alone on the push path, with no window in hand -- so if an
        unnamed join cleared every window's marker, window A's `leave_session`
        would be undone by window B's very next push, which is the same
        both-windows-are-one-person bug this keying exists to remove, arriving
        from the other direction.
        """
        session = self._sessions[shared_id]
        if contributor not in session.members:
            session.members.append(contributor)
        self._departed.get(shared_id, set()).discard((contributor, agent_session))

    def _windows_of(self, shared_id: str, contributor: str) -> set[str]:
        """Every Agent Session this contributor has pushed under, from the log.

        There is no roster of windows anywhere in this system -- nothing on the
        join path carries one (`add_member` takes a contributor, and the relay
        has only that) -- so the log's `Attribution.agent_session` is the same
        and only durable evidence `debug._participants` builds its rows from.
        Using it here is what keeps the two agreeing: a window the page shows
        as a row is a window this can count.

        A window that joined and has pushed NOTHING is invisible to this, which
        is the identical blind spot `_participants` documents and handles by
        listing a contributor-only row. The consequence is bounded and stated
        at the one call site: `remove_member` can retire a contributor from the
        roster while a silent sibling window is still bound. Its next push
        re-registers them (`Relay._register_members` is idempotent), so the
        roster self-heals, and the departed marker written per window is not
        disturbed by that.
        """
        windows: set[str] = set()
        memory = self._memories.get(shared_id)
        if memory is None:
            return windows
        for entry in memory.log:
            if not isinstance(entry, FindingAppended):
                continue
            for attribution in entry.finding.attributions:
                if attribution.contributor == contributor:
                    windows.add(attribution.agent_session)
        return windows

    def remove_member(self, shared_id: str, contributor: str,
                      agent_session: str | None = None) -> None:
        """Detach one member. Leaving is not ending (2026-08-06 spec): the
        Shared Session stays open for everyone else and every finding this
        Contributor already pushed stays in the log, attributed to them.

        `agent_session` names the WINDOW that left. Given one, the contributor
        leaves the ROSTER only once every window of theirs this store can see
        has departed -- because two windows of one person are two participants
        and the first to leave must not evict the second, which is exactly what
        this route did until 2026-08-06. Omitted, it stays what it always was:
        the person leaves, whole, every window with them. That is the honest
        reading of a DELETE that names no window, and it is what an un-upgraded
        client sends.

        NOT a log entry, unlike `end_session` -- and that asymmetry is
        deliberate. Membership has never been in the log (`add_member` writes
        the list on `SynapseSession` and always has), so making only the
        REMOVAL an entry would split one concept across two representations
        and give the fold a half-picture it could not repair: it would see
        members leaving it never saw arrive. If membership moves into the log
        it should move as a pair, `MemberJoined`/`MemberLeft`, and that is a
        bigger change than this pass.

        Idempotent, because a DELETE that is retried after a dropped response
        must not be an error.

        `_last_seen` is deliberately LEFT ALONE. A member who leaves and
        re-joins keeps their place in the memory -- that is the whole point of
        keying the watermark on the Contributor rather than on a conversation
        id (see `last_seen` below), and clearing it here would reinstate the
        exact "everything is new again" briefing the re-key removes.

        `_departed` IS written, and it is not a second representation of
        membership -- it is this process's record that it watched the DELETE
        arrive. `members` alone cannot answer "did they leave?", because an
        absent contributor may equally never have registered (nothing on the
        ingest path calls `add_member`; only the relay does) or have
        registered against a service that has since restarted. Callers that
        need to tell those apart ask `has_departed`; callers that only need
        the roster keep reading `members`.
        """
        session = self._sessions[shared_id]
        departed = self._departed.setdefault(shared_id, set())
        departed.add((contributor, agent_session))
        if agent_session is not None:
            remaining = self._windows_of(shared_id, contributor) | {agent_session}
            if any((contributor, window) not in departed for window in remaining):
                return
        if contributor in session.members:
            session.members.remove(contributor)

    def has_departed(self, shared_id: str, contributor: str,
                     agent_session: str | None = None) -> bool:
        """Did THIS PROCESS observe a `DELETE /members/{contributor}`?

        `False` is not "they are still here" -- it is "no departure was
        observed", which after a restart is true of everyone. The only honest
        reading of `False` for a non-member is UNKNOWN.

        Named an `agent_session`, this answers for THAT WINDOW: its own
        departure, or a person-level one, which covers every window they have.
        Unnamed, it answers for the person and any departure of theirs counts
        -- which is what every pre-2026-08-06 caller means by the question.
        """
        departed = self._departed.get(shared_id, set())
        if (contributor, None) in departed:
            return True
        if agent_session is not None:
            return (contributor, agent_session) in departed
        return any(who == contributor for who, _ in departed)

    # ── lifecycle ───────────────────────────────────────────────────────────
    def end_session(self, shared_id: str, ended_by: str) -> str:
        """Close a Shared Session for everyone. Returns the Contributor the
        closure is attributed to -- which is `ended_by` for the first call and
        the ORIGINAL closer for any repeat, because the fold takes the first
        `SessionEnded` entry.

        An EVENT, per `adr/0004`: nothing is written onto the session record,
        and `SessionContext.status` is a fold over the log (see `get_context`).
        `SynapseSession.ended = True` was the obvious alternative and is
        rejected there and in `log.SessionEnded` -- in one sentence, a flag
        does not survive the restart + resync path that is this system's
        entire recovery story, and an entry will.

        Caller-side gating (creator-only, refuse while teammates are still
        members) lives at the routes and in the orchestrator, not here: this is
        the storage seam and it records what happened. The 403 needs
        `SynapseSession.created_by`, which the route already has.
        """
        self._memories[shared_id].end(ended_by)
        return self._memories[shared_id].view().ended_by or ended_by

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
        view = memory.view()
        seen = dict(view.findings)
        new = 0
        for finding in findings:
            is_new = finding.id not in seen
            if is_new:
                seen[finding.id] = finding
                new += 1
                memory.append(finding, is_new=True)
                continue
            # AN IDENTICAL RESEND IS NOT AN EVENT. Skipped entirely rather
            # than appended, which is what stopped one push of N from costing
            # 3N log entries (N FindingAppended + N TopicAssigned here, then N
            # more FindingAppended when synthesis.merge upserts the SAME list
            # again). Every finding showed up twice in the dashboard's log
            # tail, which is how this surfaced -- and the write-ahead log
            # RETRIES by design, so a flaky upstream multiplied it further.
            #
            # Only exact duplicates are dropped. A resend whose CONTENT
            # changed is still a real event and still appended: the log is the
            # record, and silently discarding a changed finding because its id
            # was seen before would lose data rather than noise.
            #
            # The comparison is free: `seen` already holds the folded view
            # this method computes once for the whole batch, so nothing here
            # re-folds -- the O(N**2) trap `memory.append`'s `is_new` hint
            # exists to avoid.
            if seen[finding.id] != finding:
                memory.append(finding, is_new=False)
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
        """The Working Memory half of Shared Memory, with `status` PROJECTED
        from the fold on the way out (2026-08-06, session lifecycle spec).

        Same Option A shape as `_project` above and for the same reason:
        termination lives in the log, `SessionContext.status` is what every
        consumer already reads, and this is the one accessor no caller can go
        around. Writing the flag at `end_session` time instead would leave two
        places that decide whether a session is closed, and the one that is
        NOT the log is the one that loses its answer on restart.

        This assigns onto the retained context rather than handing back a copy,
        which is what `set_context` already does to the same object -- and
        `test_storage_seam.py` allows a `.status` write in this module and
        nowhere else, which is exactly the fence this projection wants.
        """
        ctx = self._contexts[shared_id]
        ctx.status = (SessionStatus.ENDED
                      if self._memories[shared_id].view().ended_by is not None
                      else SessionStatus.ACTIVE)
        return ctx

    def bump_version(self, shared_id: str) -> int:
        ctx = self._contexts[shared_id]
        ctx.memory_version += 1
        return ctx.memory_version

    def last_seen(self, shared_id: str, contributor: str) -> int:
        """How far this CONTRIBUTOR had read when they last queried.

        ⟨RE-KEYED 2026-08-06, from `agent_session` to `contributor`⟩ The old
        key was the Agent Session id, which is the transcript filename stem
        (`worker/discovery.py:112`) -- it changes every time you open a new
        Claude Code window. So the ordinary act of closing a conversation and
        starting another on the same machine produced an unknown key, a
        `last_seen` of 0, and a briefing that reported the entire Shared
        Memory as new to someone who had just read it. Nothing errored; the
        watermark simply stopped meaning "how much have I not seen yet".

        The Contributor is the identity that is actually stable across
        conversations, and it is the one the spec picked for both this and
        self-suppression so that the two cannot disagree about who you are.
        """
        return self._last_seen.get((shared_id, contributor), 0)

    def has_looked(self, shared_id: str, contributor: str) -> bool:
        """Has this CONTRIBUTOR ever read this session's memory?

        Distinct from `last_seen(...) == 0`, which is also what a person who
        read an untouched session gets. The arrival summary needs the
        difference: "everything here is new to you" is true for a first-ever
        joiner and false — and misleading — for someone returning to a session
        that simply has not moved.
        """
        return (shared_id, contributor) in self._last_seen

    def seen_count(self, shared_id: str, contributor: str) -> int:
        """How many findings had entered this memory when they last read it."""
        return self._seen_count.get((shared_id, contributor), 0)

    def arrived_after(self, shared_id: str, count: int) -> list[Finding]:
        """The RETRIEVABLE findings recorded after the first `count` were.

        `count` is a `seen_count`. The fold records every finding once, in
        arrival order, and never reorders — `View.findings` is keyed in that
        same first-write order (`fold._record`) — so a position in it is a
        stable "when did this arrive" and slicing past `count` is exactly
        "everything since". Filtered back through `visible_ids` so a finding
        that arrived and was then merged away or marked trivial is not
        announced to a joiner as news, and projected like every other read.
        """
        view = self._memories[shared_id].view()
        arrived = set(list(view.findings)[count:])
        return [self._project(view, view.findings[fid])
                for fid in view.visible_ids if fid in arrived]

    def read_position(self, shared_id: str) -> tuple[int, int]:
        """Where the memory stands RIGHT NOW: (memory_version, findings ever
        recorded). What `mark_seen` would write if called at this instant.

        Exists so a caller whose read takes measurable time can take the
        snapshot at the moment the reader's view was actually fixed, rather
        than at the moment the response is written — see `mark_seen`'s `at=`.

        Reads `_contexts` directly rather than `get_context` for the version,
        deliberately: this wants the stored number, not the status projection
        `get_context` performs (which folds the log for a value nothing here
        reads). The fold IS taken for the count, because there is no cheaper
        way to count findings than the structure that holds them, and every
        caller has already folded and left the result cached on
        `SharedMemory._view`.
        """
        return (self._contexts[shared_id].memory_version,
                len(self._memories[shared_id].view().findings))

    def mark_seen(self, shared_id: str, contributor: str, *,
                  at: tuple[int, int] | None = None) -> None:
        """Move this contributor's watermark to `at`, or to right now.

        ⟨`at=` ADDED 2026-08-06, adversarial review finding #3⟩ `/query`'s only
        caller used to let this read the store AFTER awaiting the ranking
        model — a call docs/FLOW.md measures at 12.6–52.8 seconds. Anything a
        teammate pushed inside that window was counted as seen by an asker who
        was never shown it, and for `_seen_count` that is not self-correcting:
        `_last_seen` is a coarse version delta that the next verdict round
        moves past anyway, but `_seen_count` is an ARRIVAL INDEX, so those
        findings were excluded from that person's NEW SINCE slice permanently.
        Reproduced end to end: push f-1, start /query as aditya, push f-2 while
        the ranking is blocked, release — /arrival then reported `new: 0` with
        `accumulated.total: 2`.

        Passing a position taken before the await is the whole fix. `at=None`
        keeps the old behaviour for callers whose read is instantaneous.
        """
        version, count = at if at is not None else self.read_position(shared_id)
        self._last_seen[(shared_id, contributor)] = version
        self._seen_count[(shared_id, contributor)] = count
