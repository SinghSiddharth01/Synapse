"""The storage seam (Plan C.2). In-memory first pass, deliberately.

RETRIEVABLE is defined here and only here:
    merged_into is None and status is KEPT

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

from synapse_contracts import (Conflict, Finding, FindingId, FindingStatus,
                               SessionContext, SynapseSession)

logger = logging.getLogger(__name__)


def is_retrievable(finding: Finding) -> bool:
    return finding.merged_into is None and finding.status == FindingStatus.KEPT


class InMemoryStore:
    def __init__(self) -> None:
        self._sessions: dict[str, SynapseSession] = {}
        self._contexts: dict[str, SessionContext] = {}
        self._findings: dict[str, dict[str, Finding]] = {}
        self._last_seen: dict[tuple[str, str], int] = {}

    # ── sessions ────────────────────────────────────────────────────────────
    def create_session(self, purpose: str, created_by: str) -> SynapseSession:
        shared_id = f"sh-{uuid.uuid4().hex[:8]}"
        session = SynapseSession(shared_id=shared_id, purpose=purpose,
                                 members=[], created_by=created_by)
        self._sessions[shared_id] = session
        self._contexts[shared_id] = SessionContext(
            shared_id=shared_id, purpose=purpose, working_memory="")
        self._findings[shared_id] = {}
        return session

    def get_session(self, shared_id: str) -> SynapseSession | None:
        return self._sessions.get(shared_id)

    def add_member(self, shared_id: str, contributor: str) -> None:
        session = self._sessions[shared_id]
        if contributor not in session.members:
            session.members.append(contributor)

    # ── findings ────────────────────────────────────────────────────────────
    def upsert(self, shared_id: str, findings: list[Finding]) -> int:
        table = self._findings[shared_id]
        new = 0
        for finding in findings:
            if finding.id not in table:
                table[finding.id] = finding
                new += 1
        return new

    def get(self, shared_id: str, finding_id: str) -> Finding | None:
        return self._findings[shared_id].get(finding_id)

    def all_findings(self, shared_id: str) -> list[Finding]:
        return list(self._findings[shared_id].values())

    def retrievable(self, shared_id: str) -> list[Finding]:
        return [f for f in self._findings[shared_id].values() if is_retrievable(f)]

    # ── verdicts (the seam: Plan E Task E.1) ────────────────────────────────
    # Synthesis used to apply every verdict by mutating objects `get()` handed
    # back. That works only while the store hands back the live object; a store
    # that returns copies, frozen records or a projected view discards all of it
    # silently, with every API-level test still green. These three methods are
    # the entire write path, and test_storage_seam.py reads the source to keep
    # them the ONLY one.
    def supersede(self, shared_id: str, sources: list[FindingId],
                  result: Finding) -> None:
        """Land `result`, then tombstone every LIVE source (ADR 0002).

        An already-superseded source keeps pointing at its first successor: a
        merge is the only irreversible act in the system, and re-pointing it
        would rewrite lineage a human may need to read back."""
        self.upsert(shared_id, [result])
        table = self._findings[shared_id]
        for finding_id in sources:
            finding = table.get(finding_id)
            if finding is None or finding.merged_into is not None:
                continue
            finding.merged_into = result.id

    def mark_trivial(self, shared_id: str, finding_ids: list[FindingId]) -> None:
        """Apply the trivia verdict, skipping anything already tombstoned.

        Unknown ids are ignored rather than fatal -- an 8B inventing an id must
        not crash ingest (synthesis.py's own docstring)."""
        table = self._findings[shared_id]
        for finding_id in finding_ids:
            finding = table.get(finding_id)
            if finding is None:
                logger.warning("Trivial verdict for unknown id %s; ignored", finding_id)
                continue
            if finding.merged_into is None:
                finding.status = FindingStatus.TRIVIAL

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
