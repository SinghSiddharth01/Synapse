"""LLM-as-retriever over the curated Finding Log — never the prose (Plan C.5).

Suppression happens HERE, before the model sees the candidates: a finding is
excluded only when EVERY attribution is the asking AGENT SESSION's own. A
Synthesized finding carrying any other conversation's contribution -- a
teammate's, or the same human's other window -- is always shown.

⟨SPLIT 2026-08-06, decisions/001; partially reverses the same day's
contributor re-key⟩ This field has now been both. Keyed on `agent_session`
alone, leave-and-rejoin replayed the memory: a new conversation is a new id, so
`last_seen` fell to 0 and your own earlier findings came back as team
knowledge. Keyed on `contributor` alone -- the fix for that -- the same human's
second window could never see the first's, which is W2's entire use case
(one agent invocation is one Agent Session; two windows are two participants).

One key cannot do both, because the two concerns are about different things.
"Is this already in the context window asking?" is a fact about ONE
CONVERSATION, and that is suppression, here. "How much have I not seen?" is a
fact about ONE PERSON, and that is the watermark, which stays keyed on the
Contributor in `store.last_seen`/`mark_seen`. So: suppression by
`agent_session`, watermark by `contributor`, each keyed by what it is about.

`asking_contributor` survives as the FALLBACK for a request that carries no
`agent_session` at all -- an anonymous or contributor-only client. The old
`_legacy_agent_session` escape hatch in api.py is gone with this change: the
un-upgraded client's field is the primary key again, so there is nothing left
to special-case.
"""

from __future__ import annotations

import logging
from typing import Callable

from synapse_contracts import Finding, ModelUsage, SessionContext
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


class RetrievalUnavailable(RuntimeError):
    """The retrieval MODEL CALL failed. Distinct by type from 'ranked nothing':
    an empty list is an answer; this is the absence of one.

    ⟨decision 008, 2026-08-06⟩ This type exists because the alternative — the
    `except Exception: return []` that stood here — made a dead model backend
    indistinguishable from a memory with nothing relevant in it. The route
    answered 200 with `{"findings": []}` and the orchestrator rendered "Team
    memory has nothing relevant to that. (Checked — not skipped.)", which is a
    confident lie told by a system whose whole product is honest recall.

    An exception rather than a sentinel or a status field, deliberately: a
    `None` invites the next caller to `or []` it straight back into silence,
    and a `degraded: true` on a 200 keeps un-upgraded clients working
    *silently wrong*. An exception is the only shape that cannot be ignored by
    accident, which is the entire point of the change.
    """

    def __init__(self, provider_id: str, cause: Exception) -> None:
        self.provider_id = provider_id
        self.cause_name = type(cause).__name__
        super().__init__(
            f"retrieval model call failed on {provider_id}: "
            f"{self.cause_name}: {cause}")


def visible_to(candidates: list[Finding], *,
               asking_agent_session: str | None = None,
               asking_contributor: str | None = None) -> list[Finding]:
    """Invariant 3, defined once. Everything that suppresses reads this.

    ⟨SPLIT 2026-08-06, decisions/001⟩ The primary comparison is
    `a.agent_session` against the ASKING CONVERSATION; `a.contributor` is the
    fallback for a request that named no conversation. The GUARD did not move
    and must not: it is about how many attributions there are, not about which
    field they are compared on, so it is unchanged by either re-key and
    invariant 3 is unchanged with it.

    An asker who identifies as neither suppresses nothing. That is deliberate:
    "anonymous" is not an identity that can own a finding, and silently
    treating it as one would hide arbitrary findings from every anonymous
    request. Both parameters are keyword-only so no existing positional call
    site can slide an Agent Session id into the Contributor slot or the
    reverse -- a mix-up that yields zero suppression and no error, and which
    the parameter name is the only thing at this seam that would catch.
    """
    # A Finding is suppressed only when it HAS attributions and every one of
    # them is the asking conversation's own. `all(...)` over an empty
    # attributions list is vacuously True, so without the explicit
    # `f.attributions and` guard a zero-attribution Finding would be
    # suppressed for every possible asker -- the opposite of invariant 3.
    def is_own(attribution) -> bool:
        if asking_agent_session:
            return attribution.agent_session == asking_agent_session
        if asking_contributor:
            return attribution.contributor == asking_contributor
        return False

    return [f for f in candidates
            if not (f.attributions and all(is_own(a) for a in f.attributions))]


async def query_findings(provider: ModelProvider, *, context: SessionContext,
                         candidates: list[Finding],
                         query: str,
                         asking_agent_session: str | None = None,
                         asking_contributor: str | None = None,
                         on_usage: Callable[[ModelUsage | None], None] | None = None,
                         ) -> list[Finding]:
    """Rank what the asker is allowed to see.

    Both identities are threaded here as well as applied at the lanes seam
    because api.query applies the predicate twice on purpose (belt and
    braces), and a key honoured in only one of the two places would suppress
    at candidate selection and then hand the model the finding back anyway.

    `on_usage` is the METERING seam (2026-08-06, W3b). Retrieval shares one
    provider object -- hence one API key and one hourly ceiling -- with
    synthesis, and until now nothing charged this call to anything, so N
    queries burned N of the key's 20 requests/hour while the service's
    governor still answered "affordable" (FLOW.md §1.5). It is a CALLBACK
    rather than a changed return type on purpose: this function's contract is
    `list[Finding]` and ~15 test call sites plus `visible_to`'s two-layer
    application depend on that; a metering hook that forces every reader to
    unpack a tuple would be paid for by every one of them.

    It fires exactly once per call that reached the provider, INCLUDING the
    failing one, and is handed `None` when the provider raised before
    reporting usage -- the caller decides what an un-costed call costs. It
    does not fire at all when there was nothing to rank, because then there
    was no request. Same three states `api._record_spend` already
    distinguishes for synthesis, for the same reason.
    """
    visible = visible_to(candidates, asking_agent_session=asking_agent_session,
                         asking_contributor=asking_contributor)
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
        if on_usage is not None:
            on_usage(result.usage)
        indices = result.data.get("ranked", [])
    except Exception as exc:                             # noqa: BLE001
        # Charged BEFORE the raise: the request went out and the key paid for
        # it whether or not anything usable came back. Not charging a failed
        # retrieval is the same mistake `_record_spend`'s ⟨CORRECTED⟩ note
        # describes from the other direction.
        if on_usage is not None:
            on_usage(None)
        # The log line stays; the SWALLOW goes (decision 008). `return []`
        # here was the whole bug: it is the same value a model returns when it
        # honestly ranked nothing, so the caller could not tell an outage from
        # an answer. The two `return []`s above and below this block are
        # untouched — those are real empty answers.
        logger.exception("Retrieval model call failed; raising RetrievalUnavailable")
        raise RetrievalUnavailable(
            getattr(provider, "provider_id", "unknown"), exc) from exc

    seen: set[int] = set()
    ranked: list[Finding] = []
    for i in indices:
        if isinstance(i, int) and 0 <= i < len(visible) and i not in seen:
            seen.add(i)
            ranked.append(visible[i])
    return ranked
