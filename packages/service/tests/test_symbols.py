"""Symbol lane tests.

The privacy property is the one worth staring at: this module has no entry point
that accepts a Segment, and it must not grow one. Symbols extracted from raw
transcript content would carry redacted material across the device boundary in
tag form while `verbatim_overlap` still reported clean.
"""

from __future__ import annotations

import inspect

from synapse_service import symbols
from synapse_service.symbols import SymbolIndex, extract


def test_numbers_carry_their_unit() -> None:
    """`40 ms` and `40 requests` must not collide on the bare number."""
    assert "40ms" in extract("the timing window is 40 ms")
    assert "40ms" not in extract("we saw 40 requests queue up")


def test_units_normalise_across_spellings() -> None:
    assert extract("40 ms") == extract("40ms") == extract("40 MSEC")


def test_bare_integers_are_not_symbols() -> None:
    """`3` appears everywhere; indexing it makes the lane return the corpus."""
    assert extract("retries are capped at 3") == frozenset()


def test_identifiers_paths_versions_and_codes() -> None:
    found = extract(
        "default_pool_size in gateway.yaml broke after 1.9.4 with ECONNRESET"
    )

    assert "default_pool_size" in found
    assert "gateway.yaml" in found
    assert "1.9.4" in found
    assert "econnreset" in found


def test_paths_normalise_separators() -> None:
    assert extract(r"src\synapse\log.py") == extract("src/synapse/log.py")


def test_search_ranks_rarer_shared_symbols_higher() -> None:
    index = SymbolIndex()
    index.add("common-1", "the run took 10 ms")
    index.add("common-2", "another pass took 10 ms")
    index.add("rare", "default_pool_size was never changed")

    ranked = index.search("10 ms with default_pool_size untouched")

    assert ranked[0][0] == "rare"


def test_search_returns_nothing_when_the_query_has_no_symbols() -> None:
    index = SymbolIndex()
    index.add("a", "the pool is exhausted")

    assert index.search("something went wrong") == []


def test_a_merged_finding_inherits_its_sources_symbols() -> None:
    """A summary can drop a symbol a source carried; losing it loses a path in."""
    index = SymbolIndex()
    index.add("a", "the window is 40 ms")
    index.add("b", "default_pool_size is untouched under load")

    merged = index.add_merged("c", "it fails under load", ("a", "b"))

    assert "40ms" in merged
    assert "default_pool_size" in merged
    assert "c" in index.postings["40ms"]


def test_exclude_filters_results() -> None:
    index = SymbolIndex()
    index.add("a", "40 ms")
    index.add("b", "40 ms")

    ranked = index.search("40 ms", exclude=frozenset({"a"}))

    assert [fid for fid, _ in ranked] == ["b"]


def test_module_exposes_no_segment_entry_point() -> None:
    """Extraction must never be reachable from raw transcript content.

    A guard rather than a comment, because the tempting refactor — 'extract
    symbols at capture time, it is cheaper' — is exactly what turns this index
    into a channel around the distiller's redaction.
    """
    signatures = [
        str(inspect.signature(obj))
        for name, obj in vars(symbols).items()
        if inspect.isfunction(obj) and not name.startswith("_")
    ]

    assert not any("Segment" in signature for signature in signatures)
