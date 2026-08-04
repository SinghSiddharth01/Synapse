"""Frozen cross-track contracts for Synapse."""

# NOTE: the import list below intentionally differs from the block in
# docs/brainstorming/2026-07-25-plan-0-foundation.md Task 2 Step 4. That block
# exports Attribution, FindingId, FindingStatus and Provenance in __all__ but
# omits them from the import — `from synapse_contracts import Attribution`
# would raise ImportError. Fixed here; the schemas themselves are verbatim.

from synapse_contracts.schemas import (
    AgentEvent,
    Attribution,
    Conflict,
    Finding,
    FindingId,
    FindingStatus,
    FindingType,
    LocalBinding,
    ModelResult,
    ModelUsage,
    Provenance,
    Segment,
    SessionContext,
    SynapseSession,
)

__all__ = [
    "AgentEvent",
    "Attribution",
    "Conflict",
    "Finding",
    "FindingId",
    "FindingStatus",
    "FindingType",
    "LocalBinding",
    "ModelResult",
    "ModelUsage",
    "Provenance",
    "Segment",
    "SessionContext",
    "SynapseSession",
]
