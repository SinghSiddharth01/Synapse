"""BM25 lane tests.

The property worth defending is that the index never scans: only findings
sharing a term with the query are ever touched. That is what keeps this lane
usable when the log is far larger than any context window.
"""

from __future__ import annotations

from synapse_service.lexical import LexicalIndex, tokenize


def test_tokenizer_drops_stopwords_and_single_characters() -> None:
    assert tokenize("the pool is a mess") == ["pool", "mess"]


def test_tokenizer_keeps_qualifier_words() -> None:
    """'not' and 'under' carry meaning in findings; dropping them costs recall."""
    tokens = tokenize("does not reproduce under load")

    assert "not" in tokens
    assert "under" in tokens


def test_reworded_finding_with_shared_vocabulary_ranks_first() -> None:
    index = LexicalIndex()
    index.add("match", "the connection pool is created per worker")
    index.add("other", "tls certificates expire on the internal host")

    ranked = index.search("each worker creates its own connection pool")

    assert ranked[0][0] == "match"


def test_rare_terms_outweigh_common_ones() -> None:
    index = LexicalIndex()
    for i in range(20):
        index.add(f"common{i}", "the pool was checked again")
    index.add("rare", "the pool exhausted during compaction")

    ranked = index.search("pool compaction")

    assert ranked[0][0] == "rare"


def test_search_touches_only_findings_sharing_a_term() -> None:
    """The inverted index is why this lane does not grow with the corpus."""
    index = LexicalIndex()
    index.add("shares", "connection pool exhausted")
    for i in range(50):
        index.add(f"unrelated{i}", "certificate rotation completed cleanly")

    ranked = index.search("connection pool")

    assert [fid for fid, _ in ranked] == ["shares"]


def test_empty_index_and_empty_query_return_nothing() -> None:
    assert LexicalIndex().search("anything") == []

    index = LexicalIndex()
    index.add("a", "the connection pool")
    assert index.search("the a is") == []


def test_exclude_filters_results() -> None:
    index = LexicalIndex()
    index.add("a", "connection pool exhausted")
    index.add("b", "connection pool exhausted")

    ranked = index.search("connection pool", exclude=frozenset({"a"}))

    assert [fid for fid, _ in ranked] == ["b"]


def test_size_and_average_length_track_the_corpus() -> None:
    index = LexicalIndex()
    assert index.average_length == 0.0

    index.add("a", "connection pool exhausted")

    assert index.size == 1
    assert index.average_length == 3.0
