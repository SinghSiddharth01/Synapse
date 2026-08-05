"""Vector and topic tests.

Note what these do *not* assert. Under `HashingEmbedder` there is no paraphrase
signal at all, so no test here claims the vector lane catches paraphrase — that
claim needs a real embedder and is not available offline. What is tested is the
plumbing: determinism, normalisation, centroid maintenance, and that a split is
reproducible from the log rather than depending on randomness.
"""

from __future__ import annotations

import pytest

from synapse_service.semantic import (
    HashingEmbedder,
    TopicIndex,
    VectorIndex,
    cosine,
    mean,
    normalise,
)


def test_embedding_is_deterministic() -> None:
    """A fold that cannot be replayed identically is not a fold."""
    embedder = HashingEmbedder()

    assert embedder.embed("the pool is exhausted") == embedder.embed(
        "the pool is exhausted"
    )


def test_embeddings_are_unit_length() -> None:
    vector = HashingEmbedder().embed("the connection pool is exhausted")

    assert cosine(vector, vector) == pytest.approx(1.0)


def test_shared_vocabulary_scores_higher_than_none() -> None:
    embedder = HashingEmbedder()
    query = embedder.embed("the connection pool is exhausted")
    near = embedder.embed("the connection pool ran out")
    far = embedder.embed("tls certificate expired on the host")

    assert cosine(query, near) > cosine(query, far)


def test_empty_text_does_not_divide_by_zero() -> None:
    assert normalise([0.0, 0.0]) == (0.0, 0.0)
    assert mean([]) == ()


def test_vector_search_excludes_and_drops_anti_correlation() -> None:
    index = VectorIndex(embedder=HashingEmbedder())
    index.add("a", "the connection pool is exhausted")
    index.add("b", "the connection pool is exhausted")

    ranked = index.search("connection pool exhausted", exclude=frozenset({"a"}))

    assert [fid for fid, _ in ranked] == ["b"]
    assert all(score > 0.0 for _, score in ranked)


def test_first_finding_founds_a_topic() -> None:
    embedder = HashingEmbedder()
    topics = TopicIndex()

    topic_id, founded = topics.assign("a", embedder.embed("pool exhausted"))

    assert founded is True
    assert topics.topics[topic_id].members == ["a"]


def test_a_similar_finding_joins_rather_than_founding() -> None:
    embedder = HashingEmbedder()
    topics = TopicIndex(threshold=0.2)
    first, _ = topics.assign("a", embedder.embed("the connection pool is exhausted"))

    second, founded = topics.assign(
        "b", embedder.embed("the connection pool is exhausted again")
    )

    assert founded is False
    assert second == first


def test_a_dissimilar_finding_founds_its_own_topic() -> None:
    embedder = HashingEmbedder()
    topics = TopicIndex(threshold=0.5)
    first, _ = topics.assign("a", embedder.embed("the connection pool is exhausted"))

    second, founded = topics.assign(
        "b", embedder.embed("tls certificate expired for the host")
    )

    assert founded is True
    assert second != first


def test_health_reports_size_share_and_spread() -> None:
    embedder = HashingEmbedder()
    topics = TopicIndex(threshold=0.1)
    vectors = {}
    for index in range(5):
        vector = embedder.embed("the connection pool is exhausted")
        vectors[f"f{index}"] = vector
        topics.assign(f"f{index}", vector)

    report = topics.health(vectors)

    assert report[0].size == 5
    assert report[0].share == pytest.approx(1.0)
    assert report[0].spread == pytest.approx(0.0, abs=1e-6)


def test_a_collapsed_topic_is_flagged() -> None:
    embedder = HashingEmbedder()
    topics = TopicIndex(threshold=0.1)
    vectors = {}
    for index in range(80):
        vector = embedder.embed(f"pool exhausted variant {index}")
        vectors[f"f{index}"] = vector
        topics.assign(f"f{index}", vector)

    flagged = [
        h
        for h in topics.health(vectors)
        if h.is_collapsed(max_share=0.35, max_size=60)
    ]

    assert flagged


def test_split_is_deterministic_and_partitions_every_member() -> None:
    """Seeding is farthest-first, never random: a fold must replay identically."""
    embedder = HashingEmbedder()
    topics = TopicIndex(threshold=0.05)
    vectors = {}
    texts = ["the connection pool is exhausted"] * 5 + [
        "tls certificate expired for the host"
    ] * 5
    for index, text in enumerate(texts):
        vector = embedder.embed(f"{text} {index % 2}")
        vectors[f"f{index}"] = vector
        topics.assign(f"f{index}", vector)

    topic_id = max(topics.topics, key=lambda t: topics.topics[t].size)
    expected = set(topics.topics[topic_id].members)
    (left, right), assignments = topics.split(topic_id, vectors)

    assert {finding for finding, _ in assignments} == expected
    assert {t for _, t in assignments} == {left, right}
    assert topic_id not in topics.topics


def test_splitting_a_single_member_topic_is_rejected() -> None:
    embedder = HashingEmbedder()
    topics = TopicIndex()
    vector = embedder.embed("only one")
    topic_id, _ = topics.assign("a", vector)

    with pytest.raises(ValueError, match="cannot split"):
        topics.split(topic_id, {"a": vector})


def test_topic_search_returns_the_nearest_cluster() -> None:
    embedder = HashingEmbedder()
    topics = TopicIndex(threshold=0.3)
    topic_of = {}
    for index in range(3):
        vector = embedder.embed("the connection pool is exhausted")
        topic_id, _ = topics.assign(f"pool{index}", vector)
        topic_of[f"pool{index}"] = topic_id
    other = embedder.embed("tls certificate expired for the host")
    topic_of["tls"] = topics.assign("tls", other)[0]

    members = topics.search(
        embedder.embed("the connection pool is exhausted"), topic_of
    )

    assert {fid for fid, _ in members} == {"pool0", "pool1", "pool2"}


def test_topic_search_on_an_empty_index_returns_nothing() -> None:
    assert TopicIndex().search((1.0, 0.0), {}) == []
