"""Lane and fusion tests.

The behaviour that matters here is that the lanes are *unioned*. Every knob in
candidate selection is set toward returning more, because a missed candidate is
a merge that never happens and nothing reports it, while a spurious one costs
fifty tokens and a "no".
"""

from __future__ import annotations

from datetime import UTC, datetime

from synapse_contracts import Attribution, Finding, FindingType

from synapse_service.lanes import DEFAULT_TOPIC_LANE, Lane, select
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


def _store(*pairs: tuple[str, str]) -> SharedMemory:
    store = SharedMemory(shared_id="s")
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
    store = SharedMemory(shared_id="s")

    result = store.candidates("anything at all")

    assert len(result) == 0
    assert result.searched == 0


def test_every_ENABLED_lane_runs_even_when_it_contributes_nothing() -> None:
    """Was `test_every_lane_runs_even_when_it_contributes_nothing`, asserting
    `lanes_run == frozenset(Lane)`. `lanes_run` now means the lanes that RAN,
    and the topic lane is behind a flag -- so the set is Lane minus TOPIC when
    the flag is off and all of Lane when it is on. The original property (a
    lane that finds nothing still REPORTS that it ran) is what all three
    assertions below preserve.

    The DEFAULT half compares against DEFAULT_TOPIC_LANE, not against a
    hardcoded outcome. Step 5 of this same task takes the measurement that
    sets that constant; writing `- {Lane.TOPIC}` here would have made the
    lane-ON outcome unreachable without editing a test that is not in
    Tests-expected-to-change for it."""
    store = _store(("a", "the pool is exhausted"))
    expected_default = (frozenset(Lane) if DEFAULT_TOPIC_LANE
                        else frozenset(Lane) - {Lane.TOPIC})

    assert store.candidates("unrelated query").lanes_run == expected_default
    assert (store.candidates("unrelated query", topic_lane=True).lanes_run
            == frozenset(Lane))
    assert (store.candidates("unrelated query", topic_lane=False).lanes_run
            == frozenset(Lane) - {Lane.TOPIC})


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


def test_the_reserved_floor_backfills_instead_of_shrinking_the_result() -> None:
    """`test_top_k_bounds_the_result` uses a symbol-free query, so the symbol
    reservation is empty and the count comes out right BY ACCIDENT. With a
    symbol-bearing query the reserved ids are already in `chosen` from the
    fusion, each one costs a budget slot, and nothing takes its place:
    measured 12 of 14 on a 40-finding corpus, in a module whose whole thesis
    is that every knob is set toward returning MORE."""
    store = _store(*((f"f{i}", f"the pool exhausts above 40 ms under load, case {i}")
                     for i in range(40)))

    result = store.candidates("40 ms pool exhaustion", top_k=14)

    assert len(result) == 14


def test_backfill_never_exceeds_top_k() -> None:
    store = _store(*((f"f{i}", f"the pool exhausts above 40 ms, case {i}")
                     for i in range(40)))

    assert len(store.candidates("40 ms pool", top_k=5)) == 5


def test_backfill_cannot_invent_candidates_that_do_not_exist() -> None:
    store = _store(("a", "The timing window is 40 ms."))

    assert len(store.candidates("40 ms", top_k=14)) == 1


def test_the_topic_lane_contributes_nothing_when_it_is_off() -> None:
    """Measured at 0 partners and 0 uniquely, at 422 findings and at 2,022.
    A lane that returns a whole 40-member cluster into an RRF fusion is not
    free: those members take rank credit that can outvote real matches.

    Passes `topic_lane=` EXPLICITLY, so this assertion is true whichever way
    Step 5's measurement sets DEFAULT_TOPIC_LANE."""
    store = _store(*((f"f{i}", f"the pool exhausts under load, case {i}")
                     for i in range(20)))

    result = store.candidates("pool exhaustion", topic_lane=False)

    assert all(Lane.TOPIC not in c.lanes for c in result.candidates)
    assert Lane.TOPIC not in result.lanes_run


def test_the_topic_lane_runs_when_it_is_on() -> None:
    store = _store(*((f"f{i}", f"the pool exhausts under load, case {i}")
                     for i in range(20)))

    result = store.candidates("pool exhaustion", topic_lane=True)

    assert Lane.TOPIC in result.lanes_run


def test_coverage_line_names_only_the_lanes_that_ran() -> None:
    """`lanes_run` means LANES THAT RAN THIS CALL, not lanes that exist -- the
    only reading coverage_line()'s job supports, since a lane that did not run
    contributed no coverage. The literal string is pinned in both flag states
    because it WOULD be a model-facing surface the moment anyone renders it
    into a prompt, and a silent 5->4 is exactly the kind of change that reaches
    a model with no test noticing. (In the shipped integration nothing renders
    it -- see the scope note in this task's preamble. This is a tripwire on a
    surface that is currently dead, and it is labelled as one.)

    Both states are passed EXPLICITLY: neither half depends on the default."""
    store = _store(*((f"f{i}", f"finding {i} about pooling") for i in range(10)))

    off = store.candidates("pooling", top_k=14, topic_lane=False).coverage_line()
    on = store.candidates("pooling", top_k=14, topic_lane=True).coverage_line()

    assert "· 4 lanes ·" in off
    assert "· 5 lanes ·" in on


def test_default_recent_above_the_reserved_floor_changes_nothing() -> None:
    """DEFAULT_RECENT is inert above `max(1, top_k // RESERVE_DIVISOR)`:
    only that many of the collected ids are ever used. Measured identical at
    recent=2, 8 and 20 on a 422-finding corpus. The fix for the docstring's
    claim is the docstring, not the number."""
    store = _store(*((f"f{i}", f"the pool exhausts under load, case {i}")
                     for i in range(40)))

    assert (store.candidates("pool", top_k=14, recent=2).ids()
            == store.candidates("pool", top_k=14, recent=8).ids())
