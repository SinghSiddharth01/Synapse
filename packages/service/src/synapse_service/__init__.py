"""Synapse Service — Shared Memory.

An append-only log, a view derived by folding it, and candidate selection that
stays a fixed size no matter how large the log gets.

    from synapse_service import Store

    store = Store(shared_id="checkout-timeouts", purpose="fix flaky timeouts")
    store.append(finding)
    result = store.candidates("fails above 40 ms under load")

`candidates()` is the one interesting operation, and it is deliberately one:
synthesis passes an incoming finding, retrieval passes a teammate's question.
Two callers, one lookup.

Nothing here calls a model. That is on purpose — the parts that fail silently
(identity, supersession, visibility, retrieval recall) are deterministic and
testable, and they come first. The merge call sits on top once recall is known.
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
from synapse_service.semantic import (
    Embedder,
    HashingEmbedder,
    TopicHealth,
    TopicIndex,
    VectorIndex,
    cosine,
)
from synapse_service.store import Appended, Store
from synapse_service.symbols import SymbolIndex, extract

__all__ = [
    "Appended",
    "Candidate",
    "CandidateSet",
    "Embedder",
    "Entry",
    "FindingAppended",
    "HashingEmbedder",
    "Indexes",
    "Lane",
    "LexicalIndex",
    "Log",
    "Merged",
    "Store",
    "SupersessionCycleError",
    "SymbolIndex",
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
