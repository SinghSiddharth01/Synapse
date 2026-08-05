from datetime import datetime, timezone

from synapse_contracts import Attribution, Finding, SessionContext
from synapse_providers import FakeProvider

from synapse_service.retrieval import query_findings

TS = datetime(2026, 8, 4, tzinfo=timezone.utc)
CTX = SessionContext(shared_id="sh-1", purpose="fec decode", working_memory="wm")


def _f(fid: str, text: str, agent_sessions: list[str]) -> Finding:
    return Finding(id=fid, type="learning", text=text,
                   attributions=[Attribution(contributor="c", agent_session=s,
                                             agent="claude-code") for s in agent_sessions],
                   ts=TS)


async def test_returns_findings_in_model_rank_order():
    candidates = [_f("f-1", "irrelevant", ["as-a"]), _f("f-2", "timing window 40ms", ["as-a"])]
    fake = FakeProvider(scripts=[{"ranked": [1, 0]}])
    ranked = await query_findings(fake, context=CTX, candidates=candidates,
                                  query="what do we know about timing?",
                                  asking_agent_session="as-z")
    assert [f.id for f in ranked] == ["f-2", "f-1"]


async def test_own_session_findings_are_suppressed_before_the_model_sees_them():
    mine = _f("f-mine", "my own discovery", ["as-me"])
    theirs = _f("f-theirs", "teammate insight", ["as-them"])
    shared = _f("f-shared", "merged from both of us", ["as-me", "as-them"])
    fake = FakeProvider(scripts=[{"ranked": [0, 1]}])
    ranked = await query_findings(fake, context=CTX, candidates=[mine, theirs, shared],
                                  query="anything", asking_agent_session="as-me")
    # suppressed only when EVERY attribution is the asker's own session
    assert {f.id for f in ranked} == {"f-theirs", "f-shared"}


async def test_empty_candidates_short_circuits_without_a_model_call():
    fake = FakeProvider(scripts=[])          # a call would raise: scripts exhausted
    assert await query_findings(fake, context=CTX, candidates=[],
                                query="?", asking_agent_session="as-z") == []


async def test_bogus_indices_are_dropped():
    candidates = [_f("f-1", "a", ["as-a"])]
    fake = FakeProvider(scripts=[{"ranked": [0, 7, -2, 0]}])
    ranked = await query_findings(fake, context=CTX, candidates=candidates,
                                  query="?", asking_agent_session="as-z")
    assert [f.id for f in ranked] == ["f-1"]  # deduped, in-range only
