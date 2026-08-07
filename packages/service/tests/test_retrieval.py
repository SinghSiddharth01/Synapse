"""Invariant 3, at the definition.

⟨SPLIT 2026-08-06, `docs/overnight/decisions/001`⟩ Suppression is keyed on the
asking AGENT SESSION, with the Contributor as the fallback for a request that
names no conversation. Both keys are exercised here, and which one a test
means is now visible in its call: a test that passes only `asking_contributor`
is pinning the fallback, one that passes `asking_agent_session` is pinning the
primary key.

The file has been through both keys, and the history is the point: keyed on
the Agent Session alone, leave-and-rejoin replayed the memory; keyed on the
Contributor alone, one human's two windows could not see each other at all.
Each concern is now keyed by what it is about — suppression by the
conversation, the watermark (`store.last_seen`, not this module) by the person.
"""
from datetime import datetime, timezone

import pytest
from synapse_contracts import Attribution, Finding, SessionContext
from synapse_providers import FakeProvider

from synapse_service.retrieval import RetrievalUnavailable, query_findings

TS = datetime(2026, 8, 4, tzinfo=timezone.utc)
CTX = SessionContext(shared_id="sh-1", purpose="fec decode", working_memory="wm")


def _f(fid: str, text: str, contributors: list[str]) -> Finding:
    """One Attribution per contributor, with `agent_session` derived from the
    name — so a fixture built this way has exactly one conversation per human
    and the two keys agree. Tests that need them to DISAGREE (the interesting
    case, and the one the split is about) build their attributions inline."""
    return Finding(id=fid, type="learning",
                   text=text,
                   attributions=[Attribution(contributor=c, agent_session=f"as-{c}",
                                             agent="claude-code") for c in contributors],
                   ts=TS)


async def test_returns_findings_in_model_rank_order():
    candidates = [_f("f-1", "irrelevant", ["aditya"]),
                  _f("f-2", "timing window 40ms", ["aditya"])]
    fake = FakeProvider(scripts=[{"ranked": [1, 0]}])
    ranked = await query_findings(fake, context=CTX, candidates=candidates,
                                  query="what do we know about timing?",
                                  asking_contributor="siddsing")
    assert [f.id for f in ranked] == ["f-2", "f-1"]


async def test_own_findings_are_suppressed_before_the_model_sees_them():
    mine = _f("f-mine", "my own discovery", ["me"])
    theirs = _f("f-theirs", "teammate insight", ["them"])
    shared = _f("f-shared", "merged from both of us", ["me", "them"])
    fake = FakeProvider(scripts=[{"ranked": [0, 1]}])
    ranked = await query_findings(fake, context=CTX, candidates=[mine, theirs, shared],
                                  query="anything", asking_contributor="me")
    # suppressed only when EVERY attribution is the asker's own
    assert {f.id for f in ranked} == {"f-theirs", "f-shared"}


async def test_the_contributor_fallback_suppresses_across_conversations():
    """The FALLBACK arm: a request that names no conversation at all falls
    back to comparing the Contributor, which is the only identity it gave.

    The two attributions carry DIFFERENT agent_session values on purpose —
    with no asking conversation to compare them against, neither can match,
    and only the contributor comparison can suppress this finding."""
    across = Finding(
        id="f-both", type="learning", text="I found this twice, in two windows",
        attributions=[
            Attribution(contributor="aditya", agent_session="as-window-1",
                        agent="claude-code"),
            Attribution(contributor="aditya", agent_session="as-window-2",
                        agent="codex"),
        ], ts=TS)
    teammates = _f("f-theirs", "and this is akhil's", ["akhil"])

    fake = FakeProvider(scripts=[{"ranked": [0]}])
    ranked = await query_findings(fake, context=CTX, candidates=[across, teammates],
                                  query="anything", asking_contributor="aditya")

    assert [f.id for f in ranked] == ["f-theirs"]


async def test_the_asking_conversation_wins_over_the_contributor():
    """The PRIMARY key, and the whole of the split at the definition. One
    human, two windows: what window 2 may not be told about is what window 2
    produced. Window 1's work is not in window 2's context window, so it is
    news there — the same way a teammate's is.

    Keyed on the Contributor, as this was between the two 2026-08-06 changes,
    `f-w1` comes back suppressed and this goes red."""
    mine_here = Finding(
        id="f-w2", type="learning", text="learned in the window asking",
        attributions=[Attribution(contributor="aditya", agent_session="as-window-2",
                                  agent="claude-code")], ts=TS)
    mine_there = Finding(
        id="f-w1", type="learning", text="learned in my other window",
        attributions=[Attribution(contributor="aditya", agent_session="as-window-1",
                                  agent="claude-code")], ts=TS)
    theirs = _f("f-theirs", "and this is akhil's", ["akhil"])

    fake = FakeProvider(scripts=[{"ranked": [0, 1, 2]}])
    ranked = await query_findings(fake, context=CTX,
                                  candidates=[mine_here, mine_there, theirs],
                                  query="anything",
                                  asking_agent_session="as-window-2",
                                  asking_contributor="aditya")

    assert {f.id for f in ranked} == {"f-w1", "f-theirs"}


async def test_an_asker_who_names_neither_identity_suppresses_nothing():
    """"Anonymous" is not an identity that can own a Finding. Treating it as
    one would hide arbitrary findings from every anonymous request — the same
    class of silent, invisible withholding both re-keys were about."""
    fake = FakeProvider(scripts=[{"ranked": [0, 1]}])
    ranked = await query_findings(fake, context=CTX,
                                  candidates=[_f("f-1", "a", ["aditya"]),
                                              _f("f-2", "b", ["akhil"])],
                                  query="anything")

    assert {f.id for f in ranked} == {"f-1", "f-2"}


async def test_empty_candidates_short_circuits_without_a_model_call():
    # Scripted with a real (usable) response rather than an empty script list:
    # an empty script list raises *before* incrementing FakeProvider's call
    # counter (scripts-exhausted-before-first-call), so it cannot distinguish
    # "never called" from "called and blew up" -- fake.calls would read 0
    # either way. A usable script makes a wrongful call observable: it would
    # succeed and bump fake.calls to 1.
    fake = FakeProvider(scripts=[{"ranked": []}])
    assert await query_findings(fake, context=CTX, candidates=[],
                                query="?", asking_contributor="siddsing") == []
    assert fake.calls == 0         # proves the model was never reached, not just the result


async def test_a_finding_with_zero_attributions_is_never_suppressed():
    """Invariant 3 suppresses a Finding only when EVERY attribution is the
    asker's own. With zero attributions, `all(...)` over an empty generator
    is vacuously True, which used to suppress the finding for every possible
    asker -- the opposite of the invariant.

    The `f.attributions and` guard this pins is UNCHANGED by the 2026-08-06
    re-key: it counts attributions, it does not compare them."""
    orphan = _f("f-orphan", "no attributions at all", [])
    fake = FakeProvider(scripts=[{"ranked": [0]}])
    ranked = await query_findings(fake, context=CTX, candidates=[orphan],
                                  query="anything", asking_contributor="anyone")
    assert [f.id for f in ranked] == ["f-orphan"]


class _DeadProvider(FakeProvider):
    """A model seam that is up enough to be called and not up enough to
    answer — the shape of the GenieX idle death (W1): the process is alive,
    the socket accepts, and the request never completes."""

    provider_id = "npu"

    def __init__(self) -> None:
        super().__init__(scripts=[])

    async def complete(self, messages, response_schema=None):
        raise TimeoutError("read timed out after 30s")


async def test_a_dead_model_raises_instead_of_returning_an_empty_list():
    """⟨decision 008⟩ THE pin for the contract change. `[]` is what a model
    returns when it honestly ranked nothing, so returning it on failure made
    an outage and an answer the same value — and the orchestrator rendered
    "Team memory has nothing relevant to that. (Checked — not skipped.)" over
    a backend that was not answering at all.

    The candidate list is non-empty on purpose: an empty one short-circuits
    before the model and would pass this test for the wrong reason."""
    candidates = [_f("f-1", "timing window 40ms", ["aditya"])]

    with pytest.raises(RetrievalUnavailable) as raised:
        await query_findings(_DeadProvider(), context=CTX, candidates=candidates,
                             query="what do we know about timing?",
                             asking_contributor="siddsing")

    # The two fields the 503 body is built from — a message alone would leave
    # the route guessing which backend to name.
    assert raised.value.provider_id == "npu"
    assert raised.value.cause_name == "TimeoutError"
    assert "npu" in str(raised.value)
    # Chained, so `logger.exception` and any traceback keep the real cause.
    assert isinstance(raised.value.__cause__, TimeoutError)


async def test_a_model_that_ranked_nothing_still_returns_an_empty_list():
    """The other half of the contract, and the reason it is a TYPE and not a
    flag: "the model read everything and none of it was relevant" is a real
    answer and must stay a plain `[]` with a 200 behind it."""
    candidates = [_f("f-1", "unrelated", ["aditya"])]
    fake = FakeProvider(scripts=[{"ranked": []}])
    assert await query_findings(fake, context=CTX, candidates=candidates,
                                query="anything at all",
                                asking_contributor="siddsing") == []


async def test_everything_suppressed_is_an_empty_list_not_an_outage():
    """The early return above the model call. Every candidate is the asker's
    own, so there is nothing to rank; that is an answer, not a failure, and it
    must not raise even though no model was consulted."""
    fake = FakeProvider(scripts=[{"ranked": [0]}])
    assert await query_findings(fake, context=CTX,
                                candidates=[_f("f-mine", "mine", ["me"])],
                                query="anything", asking_contributor="me") == []
    assert fake.calls == 0


async def test_bogus_indices_are_dropped():
    candidates = [_f("f-1", "a", ["aditya"])]
    fake = FakeProvider(scripts=[{"ranked": [0, 7, -2, 0]}])
    ranked = await query_findings(fake, context=CTX, candidates=candidates,
                                  query="?", asking_contributor="siddsing")
    assert [f.id for f in ranked] == ["f-1"]  # deduped, in-range only
