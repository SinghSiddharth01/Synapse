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
    store, sid = _store_with_session()
    store.upsert(sid, [_finding("f-1")])
    store.get(sid, "f-1").merged_into = "syn-1"     # synthesis tombstoned it
    store.upsert(sid, [_finding("f-1")])            # the worker's WAL replays
    assert store.get(sid, "f-1").merged_into == "syn-1"


def test_retrievable_excludes_tombstones_and_trivia():
    store, sid = _store_with_session()
    kept, tomb, triv = _finding("f-k"), _finding("f-t"), _finding("f-x")
    store.upsert(sid, [kept, tomb, triv])
    store.get(sid, "f-t").merged_into = "syn-1"
    store.get(sid, "f-x").status = FindingStatus.TRIVIAL
    assert [f.id for f in store.retrievable(sid)] == ["f-k"]
    assert len(store.all_findings(sid)) == 3        # nothing was deleted


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
