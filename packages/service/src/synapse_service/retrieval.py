"""LLM-as-retriever over the curated Finding Log — never the prose (Plan C.5).

Suppression happens HERE, before the model sees the candidates: a finding is
excluded only when EVERY attribution is the asking CONTRIBUTOR's own. A
Synthesized finding carrying any teammate contribution is always shown.

⟨RE-KEYED 2026-08-06, session lifecycle spec⟩ The key was the Agent Session
id, and the sentence here used to read "one person's two agents still learn
from each other". That was the bug, not the feature: an Agent Session id is a
transcript filename stem, so it is one Claude Code window. Keyed that way, the
same human's second window was shown that human's own findings back as if a
teammate had written them -- the awareness layer's whole job, inverted, and
loudest for the person running two agents at once, which is the demo. Findings
already in one of your conversations are still yours in the next one, so the
Contributor is the identity the rule has to be written in.
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


def visible_to(candidates: list[Finding], asking_contributor: str, *,
               asking_agent_session: str | None = None) -> list[Finding]:
    """Invariant 3, defined once. Everything that suppresses reads this.

    ⟨2026-08-06⟩ The comparison moved from `a.agent_session` to
    `a.contributor` (module docstring for why). The GUARD did not move and
    must not: it is about how many attributions there are, not about which
    field they are compared on, so it is unchanged by the re-key and
    invariant 3 is unchanged with it.

    `asking_agent_session` is set ONLY for an un-upgraded client -- one that
    sent `agent_session` and no `contributor` at all (`api._legacy_agent_session`)
    -- and switches the comparison back to `a.agent_session` for that request
    alone. Without it the re-key was additive on the WIRE but not in BEHAVIOUR:
    an old client's `agent_session` value was read as its identity and then
    compared against `a.contributor`, which it never equals, so its own
    findings came back to it as team knowledge and `query()` rendered them
    credited to itself. Verified against `build_app` 2026-08-06 -- an old-shaped
    request got back BOTH its own finding and the teammate's where before the
    re-key it got only the teammate's. The two processes are on separate laptops
    and deploy in either order, so "old client keeps exactly today's behaviour"
    has to be true of the suppression and not only of the field name.
    """
    # A Finding is suppressed only when it HAS attributions and every one of
    # them is the asking Contributor's own. `all(...)` over an empty
    # attributions list is vacuously True, so without the explicit
    # `f.attributions and` guard a zero-attribution Finding would be
    # suppressed for every possible asker -- the opposite of invariant 3.
    def is_own(attribution) -> bool:
        if asking_agent_session is not None:
            return attribution.agent_session == asking_agent_session
        return attribution.contributor == asking_contributor

    return [f for f in candidates
            if not (f.attributions and all(is_own(a) for a in f.attributions))]


async def query_findings(provider: ModelProvider, *, context: SessionContext,
                         candidates: list[Finding], query: str,
                         asking_contributor: str,
                         asking_agent_session: str | None = None) -> list[Finding]:
    """Rank what the asker is allowed to see. `asking_contributor` was
    `asking_agent_session` until 2026-08-06; the parameter is renamed rather
    than left alone because a caller passing an Agent Session id into a
    Contributor comparison gets zero suppression and no error, and the name is
    the only thing at this seam that would have told them.

    `asking_agent_session` is the un-upgraded-client escape hatch and is passed
    straight through to `visible_to` -- see its docstring. It is threaded here
    as well as applied at the lanes seam because api.query applies the
    predicate twice on purpose (belt and braces), and a fallback honoured in
    only one of the two places would suppress at candidate selection and then
    hand the model the finding back anyway."""
    visible = visible_to(candidates, asking_contributor,
                         asking_agent_session=asking_agent_session)
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
