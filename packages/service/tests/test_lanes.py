"""Lane and fusion tests.

The behaviour that matters here is that the lanes are *unioned*. Every knob in
candidate selection is set toward returning more, because a missed candidate is
a merge that never happens and nothing reports it, while a spurious one costs
fifty tokens and a "no".
"""

from __future__ import annotations

from datetime import UTC, datetime

from synapse_contracts import Attribution, Finding, FindingType

from synapse_service.lanes import Lane, select
from synapse_service.store import Store

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


def _store(*pairs: tuple[str, str]) -> Store:
    store = Store(shared_id="s")
    for finding_id, text in pairs:
        store.append(_finding(finding_id, text))
    return store


def test_the_flagship_pair_retrieves_on_the_symbol_lane() -> None:
    """Aditya's half and Akhil's half, seven minutes apart, must find each other."""
    store = _store(
        ("a", "The timing window is 40 ms."),
        ("noise", "The follower was rebuilt after the rotation."),
    )

    result = store.candidates("It fails when the delay exceeds ~40 ms under load.")

    assert "a" in result.ids()
    found = next(c for c in result.candidates if c.finding_id == "a")
    assert Lane.SYMBOLS in found.lanes
    assert "40ms" in found.shared_symbols


def test_a_candidate_found_by_one_lane_still_appears() -> None:
    """Union, not intersection. One weak signal is enough to be shown."""
    store = _store(("only-lexical", "the connection pool is created per worker"))

    result = store.candidates("each worker builds its own connection pool")

    assert "only-lexical" in result.ids()


def test_being_found_by_several_lanes_ranks_higher() -> None:
    """RRF's one real property: agreement across weak lanes beats one strong hit."""
    store = _store(
        ("both", "default_pool_size in the connection pool is untouched"),
        ("lexical-only", "the connection pool is shared between workers"),
    )

    result = store.candidates("default_pool_size in the connection pool")
    ranked = result.ids()

    assert ranked.index("both") < ranked.index("lexical-only")


def test_superseded_findings_are_never_candidates() -> None:
    """The visibility predicate is applied once, in the fold, and holds here."""
    store = _store(
        ("a", "The timing window is 40 ms."),
        ("b", "It fails above 40 ms under load."),
    )
    store.merge(_finding("c", "The window is 40 ms under load."), ("a", "b"))

    result = store.candidates("40 ms under load")

    assert "a" not in result.ids()
    assert "b" not in result.ids()
    assert "c" in result.ids()


def test_recent_lane_surfaces_findings_no_other_lane_would() -> None:
    """The hedge for findings too new to have settled anywhere."""
    store = _store(("unrelated", "Documentation for the sink no longer matches."))

    result = store.candidates("something completely different about tls certs")

    assert "unrelated" in result.ids()
    found = next(c for c in result.candidates if c.finding_id == "unrelated")
    assert Lane.RECENT in found.lanes


def test_top_k_bounds_the_result() -> None:
    store = _store(*((f"f{i}", f"finding number {i} about pooling") for i in range(40)))

    result = store.candidates("pooling", top_k=5)

    assert len(result) == 5


def test_exclude_keeps_a_finding_from_matching_itself() -> None:
    store = _store(("self", "The timing window is 40 ms."))

    result = store.candidates(
        "The timing window is 40 ms.", exclude=frozenset({"self"})
    )

    assert "self" not in result.ids()


def test_empty_store_returns_nothing_rather_than_erroring() -> None:
    store = Store(shared_id="s")

    result = store.candidates("anything at all")

    assert len(result) == 0
    assert result.searched == 0


def test_every_lane_runs_even_when_it_contributes_nothing() -> None:
    store = _store(("a", "the pool is exhausted"))

    result = store.candidates("unrelated query")

    assert result.lanes_run == frozenset(Lane)


def test_coverage_line_reports_what_was_searched() -> None:
    """So 'I found no match' is calibrated rather than confident."""
    store = _store(*((f"f{i}", f"finding {i}") for i in range(30)))

    line = store.candidates("finding", top_k=4).coverage_line()

    assert "searched 30 findings" in line
    assert "showing top 4" in line


def test_rendered_candidate_carries_its_lane_provenance() -> None:
    store = _store(("a", "The timing window is 40 ms."))

    rendered = store.candidates("40 ms under load").candidates[0].render()

    assert "#a" in rendered
    assert "symbols" in rendered
    assert "40ms" in rendered


def test_select_is_usable_without_a_store() -> None:
    """The seam is a convenience, not a requirement — lanes take a plain view."""
    store = _store(("a", "the pool is exhausted"))

    result = select("pool exhausted", store.view(), store.indexes)

    assert "a" in result.ids()
