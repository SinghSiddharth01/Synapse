"""SharedMemory tests, including the one that defends the central claim.

`test_rebuild_reproduces_every_index` is the important one: every structure in
this package is supposed to be derived from the log, and the moment something
survives only in memory it has become a second source of truth without anyone
deciding that it should.
"""

from __future__ import annotations

from datetime import UTC, datetime

from synapse_contracts import Attribution, Finding, FindingType

from synapse_service.memory import SharedMemory

TS = datetime(2026, 8, 4, 12, 0, tzinfo=UTC)


def _finding(finding_id: str, text: str, contributor: str = "aditya") -> Finding:
    return Finding(
        id=finding_id,
        type=FindingType.LEARNING,
        text=text,
        attributions=[
            Attribution(
                contributor=contributor,
                agent_session=f"sess-{contributor}",
                agent="claude-code",
            )
        ],
        ts=TS,
    )


def test_append_returns_a_topic_and_flags_the_first_member() -> None:
    """`topic_founded` is the only signal that owes a model call."""
    store = SharedMemory(shared_id="s")

    first = store.append(_finding("a", "the connection pool is exhausted"))

    assert first.topic_founded is True
    assert first.topic_id


def test_appending_the_same_finding_twice_indexes_it_once() -> None:
    store = SharedMemory(shared_id="s")
    store.append(_finding("a", "the timing window is 40 ms"))
    store.append(_finding("a", "the timing window is 40 ms"))

    assert store.view().visible_ids == ("a",)
    assert len(store.indexes.vectors.vectors) == 1


def test_merge_supersedes_and_the_result_is_retrievable() -> None:
    store = SharedMemory(shared_id="s")
    store.append(_finding("a", "the window is 40 ms"))
    store.append(_finding("b", "fails above 40 ms under load", "akhil"))

    store.merge(_finding("c", "the window is 40 ms under load"), ("a", "b"))

    assert store.view().visible_ids == ("c",)
    assert "c" in store.candidates("40 ms").ids()


def test_a_merged_finding_keeps_its_sources_symbols() -> None:
    store = SharedMemory(shared_id="s")
    store.append(_finding("a", "default_pool_size is untouched"))
    store.append(_finding("b", "the window is 40 ms", "akhil"))
    store.merge(_finding("c", "it fails under load"), ("a", "b"))

    assert "default_pool_size" in store.indexes.symbols.symbols_of["c"]
    assert "40ms" in store.indexes.symbols.symbols_of["c"]


def test_rebuild_reproduces_every_index() -> None:
    """The log alone is sufficient. Anything that does not come back was not derived."""
    store = SharedMemory(shared_id="s")
    store.append(_finding("a", "the connection pool is exhausted at 40 ms"))
    store.append(_finding("b", "gateway.yaml caps retries at 3", "akhil"))
    store.merge(_finding("c", "pool exhaustion at 40 ms in gateway.yaml"), ("a", "b"))
    store.append(_finding("d", "the follower survived a rotation"))

    before_view = store.view()
    before_symbols = dict(store.indexes.symbols.symbols_of)
    before_vectors = dict(store.indexes.vectors.vectors)
    before_candidates = store.candidates("40 ms pooling").ids()

    store.rebuild()

    assert store.view().visible_ids == before_view.visible_ids
    assert store.indexes.symbols.symbols_of == before_symbols
    assert store.indexes.vectors.vectors == before_vectors
    assert store.candidates("40 ms pooling").ids() == before_candidates


def test_version_advances_on_every_entry() -> None:
    """Including topic assignment — the watermark answers 'anything new?' cheaply."""
    store = SharedMemory(shared_id="s")
    first = store.append(_finding("a", "one"))
    second = store.append(_finding("b", "two"))

    assert second.version > first.version


def test_unhealthy_topics_are_reported_when_one_swallows_the_corpus() -> None:
    """Collapse looks like working: it still returns candidates, just useless ones."""
    store = SharedMemory(shared_id="s")
    for index in range(70):
        store.append(_finding(f"f{index}", "the connection pool is exhausted"))

    assert store.unhealthy_topics()


def test_splitting_a_topic_re_maps_membership_without_re_tagging() -> None:
    store = SharedMemory(shared_id="s")
    for index in range(6):
        store.append(_finding(f"p{index}", "the connection pool is exhausted"))
    for index in range(6):
        store.append(_finding(f"t{index}", "the connection pool is exhausted slowly"))

    topic_id = store.view().topic_of["p0"]
    left, right = store.split_topic(topic_id)

    view = store.view()
    assert set(view.topic_of.values()) >= {left, right}
    assert view.findings["p0"].text == "the connection pool is exhausted"
