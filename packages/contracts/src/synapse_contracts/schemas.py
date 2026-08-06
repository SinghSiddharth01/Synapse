"""Frozen cross-track schemas.

These types define the handoff points between components:
- AgentEvent: what the Source adapter produces per raw transcript line (internal to worker)
- Segment: bounded run of AgentEvents; the Distiller's input (worker → distiller boundary)
- Attribution: where a Finding came from — Contributor / Agent Session / Agent
- Finding: distilled unit of team intelligence (distiller → service boundary)
- SynapseSession / LocalBinding: Shared Session identity and per-machine attribution
- SessionStatus: whether a Shared Session is still open (ACTIVE) or closed (ENDED)
- Conflict: two Findings the synthesizer judged to disagree
- SessionContext: Working Memory + conflicts, returned by synthesis
- ModelResult: what any ModelProvider.complete() returns; usage/latency baked in

Vocabulary is defined in /CONTEXT.md. In particular: an *Agent Session* is one
conversation with a coding agent; a *Shared Session* is the collaboration unit.
Never call either one just "session".

Shared Memory is two things, deliberately: the Working Memory (bounded prose,
on SessionContext) keeps the merge prompt fixed-cost, while the Finding Log
(every Finding, behind the service's storage interface) is what retrieval ranks
over. Retrieval must read the Log, not the prose — otherwise synthesis's dedup
and trivia filter protect nothing a teammate actually sees.
"""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import Any, Literal

from pydantic import BaseModel, Field


class AgentEvent(BaseModel):
    """Normalized event emitted by a Source adapter (agent-agnostic)."""

    role: Literal["user", "assistant", "system"]
    kind: Literal["text", "thinking", "tool_use", "tool_result"]
    content: str
    tool_name: str | None = None
    ts: datetime
    agent_session_id: str
    cwd: str | None = None
    git_branch: str | None = None


class Segment(BaseModel):
    """A bounded run of AgentEvents split on turn boundary AND token budget.

    This is the distiller's input. Hand-authored fixture Segments in
    fixtures/segments/*.json pin the boundary between the segmenter (Plan A)
    and the distiller (Plan B); the segmenter must reproduce them exactly.

    The budget is derived per model from the resolved distiller's measured
    capability record — never a shared hard-coded constant.
    """

    id: str
    agent_session_id: str
    events: list[AgentEvent]
    started_at: datetime
    ended_at: datetime


FindingId = str


class FindingType(StrEnum):
    """Provisional taxonomy — all four are epistemic. Real output may later
    need referential/procedural/artifactual kinds. Adding a member is a
    one-line change all tracks pick up on the next pull; do not build
    tolerance machinery for it. See /CONTEXT.md.
    """

    LEARNING = "learning"
    DECISION = "decision"
    DEAD_END = "dead_end"
    OPEN_QUESTION = "open_question"


class Provenance(StrEnum):
    """How a Finding was produced — orthogonal to who it came from."""

    DISTILLED = "distilled"      # worker, from a transcript Segment
    CONTRIBUTED = "contributed"  # agent prose via contribute(), locally distilled
    SYNTHESIZED = "synthesized"  # written by synthesis when merging (service-side)


class FindingStatus(StrEnum):
    """Synthesis's quality verdict. Merge state is expressed by merged_into, not here."""

    KEPT = "kept"        # stands on its own (default until synthesis says otherwise)
    TRIVIAL = "trivial"  # restates an action without insight; filtered from retrieval


class SessionStatus(StrEnum):
    """Whether a Shared Session still accepts reads and writes.

    Two members, deliberately. "Fully closed" is the decision the lifecycle
    spec records (2026-08-06): once a session ends, reads AND writes are both
    rejected and the log persists for audit/replay only. A third member
    (`ARCHIVED`, read-only) was considered and rejected — the moment reads
    stay open on a closed session, every caller has to learn which of two
    closed states it is looking at, and the service has to keep suppression,
    watermarks and briefings honest for a session nobody can contribute to.

    Not a producer-writable field. The service derives it by folding the log
    (`SessionEnded`) and projects it onto `SessionContext` on the way out, the
    same Option A shape `adr/0004` closed for `Finding.merged_into`/`status`.
    """

    ACTIVE = "active"
    ENDED = "ended"


class Attribution(BaseModel):
    """Where a Finding came from, at three levels.

    Carried as one value so the levels cannot drift apart. Read at different
    levels for different jobs: `contributor` for attribution and conflicts,
    `agent_session` for awareness suppression, `agent` for the cross-agent story.
    """

    contributor: str      # the human, e.g. "aditya"
    agent_session: str    # their Agent Session id
    agent: str            # the Agent product, e.g. "claude-code" | "codex"


class Finding(BaseModel):
    """A single distilled unit of team intelligence.

    `text` is abstracted, not verbatim code. The distiller redacts by design
    so that raw work stays on the device — only sanitized findings cross the
    device boundary.

    `id` is stamped client-side at distil time so a retried push is idempotent
    (ingest upserts by id) and so Conflict and lineage can reference findings
    rather than embedding copies.

    `attributions` is a list because a Synthesized Finding carries every source
    it was merged from. Awareness suppresses a Finding only when *every*
    attribution is the asking agent's own Agent Session.
    """

    id: FindingId
    type: FindingType
    text: str
    attributions: list[Attribution]
    ts: datetime
    refs: list[str] = Field(default_factory=list)
    provenance: Provenance = Provenance.DISTILLED

    # Written by synthesis, service-side. Producers leave these at defaults.
    #
    # A merged-away Finding becomes a TOMBSTONE: merged_into is set, and it keeps
    # its text and attributions but is excluded from retrieval. It is not deleted,
    # for three reasons that are about correctness rather than history:
    #   1. ingest upserts by id, so a 5xx retry must find a known id or it
    #      re-inserts a finding that has already been merged away;
    #   2. Conflict references FindingIds and must be able to follow merged_into
    #      forward rather than dangle;
    #   3. the merge decision is made by an 8B model we expect to be imperfect,
    #      and it is the only irreversible action in the system.
    #
    # RETRIEVABLE  ==  merged_into is None and status is KEPT
    status: FindingStatus = FindingStatus.KEPT
    merged_from: list[FindingId] = Field(default_factory=list)  # set on a SYNTHESIZED finding
    merged_into: FindingId | None = None                        # set on a tombstone


class SynapseSession(BaseModel):
    """A Shared Session — the collaboration unit."""

    shared_id: str
    purpose: str
    members: list[str]  # Contributors (humans)
    created_by: str


class LocalBinding(BaseModel):
    """Maps one Agent Session to one Shared Session, carrying the Contributor.

    Effectively the Attribution template for an Agent Session plus the Shared
    Session it feeds. Owned by the orchestrator, which stamps it onto every
    Finding arriving from any local producer. Local state, not a service
    contract — one laptop can hold several, one per Agent Session.
    """

    agent_session_id: str
    shared_id: str
    contributor: str
    agent: str


class Conflict(BaseModel):
    """Two Findings the synthesizer judged to disagree.

    References ids rather than embedding copies, so the Finding Log stays the
    single source of truth and a conflict can later be updated or resolved.
    """

    finding_a: FindingId
    finding_b: FindingId
    description: str


class SessionContext(BaseModel):
    """Working Memory + conflicts, organized by purpose. Returned by synthesis.

    This is only half of Shared Memory. `working_memory` is bounded prose,
    rewritten each merge to keep the merge prompt fixed-cost. The Finding Log
    lives behind the service's storage interface and is what retrieval ranks
    over — its shape is a deliberate first pass and expected to evolve.

    ⟨CORRECTION, corrected 2026-08-05⟩ Not "once per merge": `memory_version`
    increments once per VERDICT ROUND APPLIED. `Synthesizer.merge` calls
    `bump_version` at the end of every structurally-valid verdict, including
    a no-op one with `"merges": []` (synthesis.py; `test_full_flow_push_
    watermark_query` pins a no-op verdict still bumping the version). It is
    the whole staleness calculation for the awareness layer: the watermark
    endpoint compares it against each member's last-seen value.
    """

    shared_id: str
    purpose: str
    working_memory: str
    memory_version: int = 0
    conflicts: list[Conflict] = Field(default_factory=list)

    # DERIVED, never sent by a producer (added 2026-08-06 with the session
    # lifecycle spec). `ACTIVE` is the default so that every SessionContext
    # constructed anywhere — including the ones the distiller and the
    # orchestrator build for a prompt — is live unless the service says
    # otherwise. The service folds `SessionEnded` out of the log and writes
    # the answer here in exactly one place (`InMemoryStore.get_context`);
    # storing a mutable flag instead was rejected for the reason `adr/0004`
    # gives: a flag is a second source of truth, and it would come back
    # ACTIVE after a restart + resync because it never travelled in the log.
    status: SessionStatus = SessionStatus.ACTIVE


class ModelUsage(BaseModel):
    input_tokens: int
    output_tokens: int


class ModelResult(BaseModel):
    """Return type of ModelProvider.complete().

    `data` is the raw text (str) when no response_schema was supplied,
    or the validated structured object (dict/list) when one was.
    """

    data: Any
    usage: ModelUsage
    latency_ms: int
    provider_id: str
    schema_valid: bool
