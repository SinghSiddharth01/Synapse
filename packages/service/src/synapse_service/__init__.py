"""Synapse Service — the remote half of Synapse, and its Shared Memory.

Two layers, deliberately named apart (Plan E Task E.2):

    InMemoryStore   the multi-session REGISTRY: sessions, members, contexts,
                    watermark bookkeeping, and one SharedMemory per Shared
                    Session. This is what `api.py` and `synthesis.py` hold.

    SharedMemory    ONE Shared Session's memory: an append-only Log plus the
                    indexes derived from it. Nothing above this layer sees a
                    Log, a View or an Entry.

Together they are CONTEXT.md's **Shared Memory**.

`Appended` is exported because it is the branch's public return type for
`append`/`merge`. Nothing consumes it. Its `.version` field carries
`Log.version`, which is NOT `SessionContext.memory_version` and must never be
reported as a watermark -- see Task 5 Step 3.
"""

from __future__ import annotations

from synapse_service.fold import SupersessionCycleError, View, fold
from synapse_service.lanes import Candidate, CandidateSet, Indexes, Lane, select
from synapse_service.lexical import LexicalIndex
from synapse_service.log import (
    Entry,
    FindingAppended,
    Log,
    Merged,
    TopicAssigned,
    TopicId,
    TopicSplit,
)
from synapse_service.memory import Appended, SharedMemory
from synapse_service.semantic import (
    Embedder,
    HashingEmbedder,
    TopicHealth,
    TopicIndex,
    VectorIndex,
    cosine,
)
from synapse_service.store import InMemoryStore
from synapse_service.symbols import SymbolIndex, extract
from synapse_service.synthesis import Synthesizer

__all__ = [
    "Appended",
    "Candidate",
    "CandidateSet",
    "Embedder",
    "Entry",
    "FindingAppended",
    "HashingEmbedder",
    "InMemoryStore",
    "Indexes",
    "Lane",
    "LexicalIndex",
    "Log",
    "Merged",
    "SharedMemory",
    "SupersessionCycleError",
    "SymbolIndex",
    "Synthesizer",
    "TopicAssigned",
    "TopicHealth",
    "TopicId",
    "TopicIndex",
    "TopicSplit",
    "VectorIndex",
    "View",
    "cosine",
    "extract",
    "fold",
    "select",
]
