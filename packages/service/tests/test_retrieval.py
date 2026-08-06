"""Invariant 3, at the definition.

⟨RE-KEYED 2026-08-06, session lifecycle spec⟩ Every test in this file used to
express the asker's identity as an Agent Session id (`asking_agent_session=
"as-me"`, attributions built from a list of agent_sessions with a fixed
contributor "c"). Suppression is keyed on the CONTRIBUTOR now -- see
retrieval.py's module docstring for why -- so the fixtures name humans. The
properties asserted are unchanged, one for one; only the identity they are
written in moved. One test is RENAMED --
`test_own_session_findings_are_suppressed_before_the_model_sees_them` ->
`test_own_findings_are_suppressed_before_the_model_sees_them`, same body, same
assertion -- because "own session" now names the thing suppression explicitly
does not read. One is ADDED: the two-agent-sessions-one-contributor case,
which is the regression the re-key exists for and which nothing here could
have expressed before.
"""
from datetime import datetime, timezone

from synapse_contracts import Attribution, Finding, SessionContext
from synapse_providers import FakeProvider

from synapse_service.retrieval import query_findings

TS = datetime(2026, 8, 4, tzinfo=timezone.utc)
CTX = SessionContext(shared_id="sh-1", purpose="fec decode", working_memory="wm")


def _f(fid: str, text: str, contributors: list[str]) -> Finding:
    """One Attribution per contributor. `agent_session` is derived from the
    name and is deliberately NOT what anything here compares on -- if a
    future edit re-keys suppression back onto it by accident, these fixtures
    make that visible rather than accidentally still passing."""
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


async def test_the_same_contributor_is_suppressed_across_two_agent_sessions():
    """Spec Testing item 5, at the definition. The re-key's whole point: one
    human running Claude Code in one window and Codex in another is ONE
    contributor, and findings they produced in either conversation are already
    in their head. Keyed on the Agent Session id -- as this was until
    2026-08-06 -- the second window was shown the first window's findings as
    if a teammate had written them.

    The two attributions carry DIFFERENT agent_session values on purpose: if
    suppression ever reads that field again, this test is what goes red."""
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


async def test_bogus_indices_are_dropped():
    candidates = [_f("f-1", "a", ["aditya"])]
    fake = FakeProvider(scripts=[{"ranked": [0, 7, -2, 0]}])
    ranked = await query_findings(fake, context=CTX, candidates=candidates,
                                  query="?", asking_contributor="siddsing")
    assert [f.id for f in ranked] == ["f-1"]  # deduped, in-range only
