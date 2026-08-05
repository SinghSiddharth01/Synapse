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

import uuid

from synapse_contracts import Finding, FindingStatus, SessionContext, SynapseSession


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
