from datetime import datetime, timezone

from synapse_contracts import Attribution, Finding, FindingStatus
from synapse_service.store import InMemoryStore

TS = datetime(2026, 8, 4, tzinfo=timezone.utc)


def _finding(fid: str, text: str = "x", contributor: str = "aditya",
             agent_session: str = "as-1") -> Finding:
    return Finding(
        id=fid, type="learning", text=text,
        attributions=[Attribution(contributor=contributor,
                                  agent_session=agent_session, agent="claude-code")],
        ts=TS,
    )


def _store_with_session() -> tuple[InMemoryStore, str]:
    store = InMemoryStore()
    session = store.create_session(purpose="debug the fec failure", created_by="siddsing")
    return store, session.shared_id


def test_upsert_is_first_write_wins():
    store, sid = _store_with_session()
    assert store.upsert(sid, [_finding("f-1", text="original")]) == 1
    assert store.upsert(sid, [_finding("f-1", text="replayed variant")]) == 0
    assert store.get(sid, "f-1").text == "original"


def test_replayed_original_never_clobbers_a_tombstone():
    """THE stop-gate for the store swap (Plan E Task E.6). Same property the
    E3 version asserted; a different mechanism asserts it. The old version set
    `merged_into` through a reference `get()` handed back -- which is exactly
    the thing that silently stops working under a store that returns copies."""
    store, sid = _store_with_session()
    store.upsert(sid, [_finding("f-1")])
    syn = _finding("syn-1", text="merged")
    store.supersede(sid, ["f-1"], syn)                 # synthesis tombstoned it

    assert store.upsert(sid, [_finding("f-1")]) == 0   # the worker's WAL replays

    assert store.get(sid, "f-1").merged_into == "syn-1"
    assert "f-1" not in [f.id for f in store.retrievable(sid)]


def test_retrievable_excludes_tombstones_and_trivia():
    store, sid = _store_with_session()
    store.upsert(sid, [_finding("f-k"), _finding("f-t"), _finding("f-x")])
    store.supersede(sid, ["f-t"], _finding("syn-1", text="merged"))
    store.mark_trivial(sid, ["f-x"])

    assert sorted(f.id for f in store.retrievable(sid)) == ["f-k", "syn-1"]
    assert len(store.all_findings(sid)) == 4           # nothing was deleted


def test_context_versioning_and_members():
    store, sid = _store_with_session()
    store.add_member(sid, "aditya")
    store.add_member(sid, "aditya")                  # idempotent
    assert store.get_session(sid).members == ["aditya"]
    assert store.get_context(sid).memory_version == 0
    assert store.bump_version(sid) == 1
    assert store.get_context(sid).memory_version == 1


def test_last_seen_tracking():
    store, sid = _store_with_session()
    store.bump_version(sid)
    assert store.last_seen(sid, "as-9") == 0
    store.mark_seen(sid, "as-9")
    assert store.last_seen(sid, "as-9") == 1


def test_unknown_session_is_none_not_keyerror():
    assert InMemoryStore().get_session("nope") is None


def test_a_forged_verdict_on_ingest_has_no_effect_on_visibility():
    """The property adr/0004 buys today and does not claim (Amendment,
    argument 2). `api.push_findings` runs only Finding.model_validate and the
    Relay POSTs to the service directly, so a producer can send any value in
    these two fields. Under the fold, visibility does not read them at all --
    and the projection normalises them back on the way out."""
    store, sid = _store_with_session()
    forged = _finding("f-forged")
    forged.status = FindingStatus.TRIVIAL
    forged.merged_into = "whatever"

    store.upsert(sid, [forged])

    assert [f.id for f in store.retrievable(sid)] == ["f-forged"]
    handed_back = store.get(sid, "f-forged")
    assert handed_back.status is FindingStatus.KEPT
    assert handed_back.merged_into is None


def test_a_resend_is_accepted_zero_and_changes_no_topic_membership():
    """Pins the duplicate guard through upsert's contract AND through what the
    guard is actually FOR. Deleting the guard today leaves 75 tests green,
    because it was checked against an index rather than against the view."""
    store, sid = _store_with_session()
    store.upsert(sid, [_finding("f-1", text="the connection pool is exhausted")])
    before = store._memories[sid].view().topic_of.copy()

    assert store.upsert(sid, [_finding("f-1", text="the connection pool is exhausted")]) == 0

    assert store._memories[sid].view().topic_of == before
    assert len(store.all_findings(sid)) == 1


def test_get_returns_a_deep_copy_so_no_mutation_through_it_reaches_the_log():
    """Option A's free consequence, asserted for the fields it is actually
    free for. `model_copy(update=...)` is SHALLOW: the copy's `attributions`
    list IS the list inside the fold, so `.append()` through it writes into the
    record the log holds -- exactly the class of mutation-through-reference
    Task 1 exists to eliminate, and invisible to a test that only touches
    scalars. `deep=True` is what makes the claim true."""
    store, sid = _store_with_session()
    store.upsert(sid, [_finding("f-1")])

    handed_back = store.get(sid, "f-1")
    handed_back.merged_into = "syn-ghost"                       # scalar
    handed_back.attributions.append(
        Attribution(contributor="mallory", agent_session="as-x", agent="claude-code"))
    handed_back.merged_from.append("f-ghost")

    fresh = store.get(sid, "f-1")
    assert fresh.merged_into is None
    assert [a.contributor for a in fresh.attributions] == ["aditya"]
    assert fresh.merged_from == []
    assert [f.id for f in store.retrievable(sid)] == ["f-1"]


def test_candidates_are_projected_like_every_other_read():
    """`api.query` hands `c.finding` straight to the model and then serialises
    the ranked result. If candidates skipped the projection, a forged verdict
    would leak into the /query response body -- Option A's 'no caller can
    forget it' would be false at the one call site that matters most."""
    store, sid = _store_with_session()
    forged = _finding("f-forged", text="the timing window is 40 ms")
    forged.status = FindingStatus.TRIVIAL
    store.upsert(sid, [forged])

    result = store.candidates(sid, "40 ms timing window")

    [candidate] = [c for c in result.candidates if c.finding.id == "f-forged"]
    assert candidate.finding.status is FindingStatus.KEPT


def test_a_second_upsert_of_the_same_batch_costs_the_log_nothing():
    """What ONE push of N findings actually costs the log, said out loud.

    ⟨AMENDED 2026-08-06⟩ This used to pin 3N and describe the cause as
    out-of-scope to fix. It is now 2N, and the amendment is the point.

    `api.push_findings` calls `store.upsert(sid, findings)` and then
    `synthesizer.merge` calls `store.upsert(shared_id, new_findings)` AGAIN,
    so every finding was appended twice. That was deliberate ("the duplicate
    is recorded in the log (it happened)") and it was wrong in practice: the
    dashboard's log tail showed every finding twice, which is how it surfaced,
    and the write-ahead log RETRIES by design so a flaky upstream multiplied
    the noise further. An identical resend is not an event.

    `upsert` now skips the append when the stored finding compares equal, so
    the second upsert costs zero entries. The comparison is free -- it reads
    the single folded view `upsert` already computes for the whole batch --
    and only EXACT duplicates are dropped, which
    test_a_resend_whose_content_changed_is_still_recorded pins from the other
    side. Dropping merge()'s own upsert remains the deeper fix and remains out
    of scope; it is no longer costing anything.

    2N per push: N FindingAppended + N TopicAssigned, both from the first
    upsert. `rebuild()` cost and `Log.version` scale with that number."""
    store, sid = _store_with_session()
    batch = [_finding("f-1"), _finding("f-2"), _finding("f-3")]

    assert store.upsert(sid, batch) == 3
    after_first = len(store._memories[sid].log.entries)
    topics_before = dict(store._memories[sid].view().topic_of)

    assert store.upsert(sid, batch) == 0          # ids not previously seen: none

    after_second = len(store._memories[sid].log.entries)
    assert after_first == 6                        # 3 FindingAppended + 3 TopicAssigned
    assert after_second == 6                       # the resend adds nothing at all
    assert len(store.all_findings(sid)) == 3       # nothing was duplicated in the VIEW
    assert store._memories[sid].view().topic_of == topics_before


def test_conflicts_resolve_forward_through_the_view_not_a_second_walker():
    """synthesis.py used to carry its own `_resolve_forward`, walking
    `store.get(...).merged_into` with a `seen` set and NO depth cap, while
    `View.resolve()` -- depth-capped at 64, raising SupersessionCycleError
    'rather than a hung service' -- was called by nothing. Two resolvers, no
    test pinning them to agree. There is one now, and it is the View's."""
    store, sid = _store_with_session()
    store.upsert(sid, [_finding("f-1"), _finding("f-2")])
    store.supersede(sid, ["f-1"], _finding("syn-1", text="first merge"))
    store.supersede(sid, ["syn-1"], _finding("syn-2", text="second merge"))

    assert store.resolve_forward(sid, "f-1") == "syn-2"      # two hops
    assert store.resolve_forward(sid, "f-2") == "f-2"        # live, unchanged
    assert store.resolve_forward(sid, "f-UNKNOWN") == "f-UNKNOWN"
