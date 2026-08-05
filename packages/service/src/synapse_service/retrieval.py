"""LLM-as-retriever over the curated Finding Log — never the prose (Plan C.5).

Suppression happens HERE, before the model sees the candidates: a finding is
excluded only when EVERY attribution is the asking agent's own Agent Session.
One person's two agents still learn from each other; a Synthesized finding
carrying any teammate contribution is always shown.
"""

from __future__ import annotations

import logging

from synapse_contracts import Finding, SessionContext
from synapse_providers import ModelProvider

logger = logging.getLogger(__name__)

RANK_SCHEMA = {
    "type": "object",
    "properties": {"ranked": {"type": "array", "items": {"type": "integer"}}},
    "required": ["ranked"],
}

RETRIEVER_SYSTEM = (
    "You are a retrieval system over a team's shared findings. Given the session "
    "purpose, working memory, a QUERY, and an indexed list of FINDINGS, return the "
    "indices of relevant findings, best first, as JSON: {\"ranked\": [..]}. "
    "Return an empty list rather than stretching relevance."
)


def _visible_to(candidates: list[Finding], asking_agent_session: str) -> list[Finding]:
    return [f for f in candidates
            if not all(a.agent_session == asking_agent_session for a in f.attributions)]


async def query_findings(provider: ModelProvider, *, context: SessionContext,
                         candidates: list[Finding], query: str,
                         asking_agent_session: str) -> list[Finding]:
    visible = _visible_to(candidates, asking_agent_session)
    if not visible:
        return []

    listing = "\n".join(f"[{i}] ({f.type.value}) {f.text}" for i, f in enumerate(visible))
    messages = [
        {"role": "system", "content": RETRIEVER_SYSTEM},
        {"role": "user", "content":
            f"PURPOSE: {context.purpose}\nWORKING MEMORY:\n{context.working_memory}\n\n"
            f"QUERY:\n{query}\n\nFINDINGS:\n{listing}"},
    ]
    try:
        result = await provider.complete(messages, response_schema=RANK_SCHEMA)
        indices = result.data.get("ranked", [])
    except Exception:                                    # noqa: BLE001
        logger.exception("Retrieval model call failed; returning nothing rather than everything")
        return []

    seen: set[int] = set()
    ranked: list[Finding] = []
    for i in indices:
        if isinstance(i, int) and 0 <= i < len(visible) and i not in seen:
            seen.add(i)
            ranked.append(visible[i])
    return ranked
