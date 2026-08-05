"""Fold tests — the invariants that fail silently if they break.

Every test here encodes something that produces no error when it goes wrong.
A superseded finding that reappears, a resync that undoes a merge, a cycle that
hangs the fold: none of them raise, and none of them show up in a log.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from synapse_contracts import Attribution, Finding, FindingStatus, FindingType

from synapse_service.fold import SupersessionCycleError, fold
from synapse_service.log import (FindingAppended, Log, MarkedTrivial, Merged,
                                 TopicAssigned, TopicSplit)
from synapse_service.memory import SharedMemory

TS = datetime(2026, 8, 4, 12, 0, tzinfo=UTC)


def _finding(
    finding_id: str,
    text: str = "a finding",
    *,
    contributor: str = "aditya",
    status: FindingStatus = FindingStatus.KEPT,
) -> Finding:
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
        status=status,
    )


def test_appended_findings_are_visible_in_order() -> None:
    log = Log(shared_id="s")
    log.append(FindingAppended(finding=_finding("a")))
    log.append(FindingAppended(finding=_finding("b")))

    view = fold(log)

    assert view.visible_ids == ("a", "b")


def test_merge_hides_its_sources_without_touching_them() -> None:
    """The originals stay readable — that is what makes a bad merge reversible."""
    log = Log(shared_id="s")
    log.append(FindingAppended(finding=_finding("a", "the window is 40 ms")))
    log.append(FindingAppended(finding=_finding("b", "40 ms under load")))
    log.append(
        Merged(result=_finding("c", "40 ms under load"), sources=("a", "b"))
    )

    view = fold(log)

    assert view.visible_ids == ("c",)
    assert view.findings["a"].text == "the window is 40 ms"
    assert view.findings["b"].text == "40 ms under load"


def test_resend_after_a_merge_does_not_resurrect_the_original() -> None:
    """The whole argument for append-only, as one test.

    Under update-in-place this is the silent catastrophe: the worker's copy
    carries status/merged_into at their defaults because it never had a verdict,
    so a whole-object write un-hides every merged-away finding in the session.
    """
    log = Log(shared_id="s")
    log.append(FindingAppended(finding=_finding("a")))
    log.append(FindingAppended(finding=_finding("b")))
    log.append(Merged(result=_finding("c"), sources=("a", "b")))

    log.append(FindingAppended(finding=_finding("a")))  # worker resync

    view = fold(log)

    assert view.visible_ids == ("c",)
    assert "a" not in view.visible_ids


def test_resend_advances_the_version_but_changes_nothing_derived() -> None:
    log = Log(shared_id="s")
    log.append(FindingAppended(finding=_finding("a")))
    before = fold(log)

    log.append(FindingAppended(finding=_finding("a")))
    after = fold(log)

    assert after.version > before.version
    assert after.visible_ids == before.visible_ids


def test_first_write_of_an_id_wins() -> None:
    """A replayed entry must not overwrite what the service already holds."""
    log = Log(shared_id="s")
    log.append(FindingAppended(finding=_finding("a", "original text")))
    log.append(FindingAppended(finding=_finding("a", "different text")))

    view = fold(log)

    assert view.findings["a"].text == "original text"


def test_resolve_follows_a_chain_forward() -> None:
    """A Conflict holds ids, and the finding it names may since have merged."""
    log = Log(shared_id="s")
    for fid in ("a", "b", "e"):
        log.append(FindingAppended(finding=_finding(fid)))
    log.append(Merged(result=_finding("c"), sources=("a", "b")))
    log.append(Merged(result=_finding("d"), sources=("c", "e")))

    view = fold(log)

    assert view.resolve("a") == "d"
    assert view.resolve("c") == "d"
    assert view.resolve("d") == "d"
    assert view.visible_ids == ("d",)


def test_a_supersession_cycle_raises_rather_than_hanging() -> None:
    """A fold that spins takes the whole service with it."""
    log = Log(shared_id="s")
    log.append(FindingAppended(finding=_finding("a")))
    log.append(FindingAppended(finding=_finding("b")))
    log.append(Merged(result=_finding("b"), sources=("a",)))
    log.append(Merged(result=_finding("a"), sources=("b",)))

    view = fold(log)

    with pytest.raises(SupersessionCycleError):
        view.resolve("a")


def test_topic_assignment_and_split_resolve_in_the_fold() -> None:
    """A split re-maps the grouping. No finding is ever re-tagged."""
    log = Log(shared_id="s")
    log.append(FindingAppended(finding=_finding("a")))
    log.append(FindingAppended(finding=_finding("b")))
    log.append(TopicAssigned(finding_id="a", topic_id="t0001", founded=True))
    log.append(TopicAssigned(finding_id="b", topic_id="t0001"))
    log.append(
        TopicSplit(
            topic_id="t0001",
            into=("t0002", "t0003"),
            assignments=(("a", "t0002"), ("b", "t0003")),
        )
    )

    view = fold(log)

    assert view.topic_of["a"] == "t0002"
    assert view.topic_of["b"] == "t0003"
    assert view.members_of == {"t0002": ("a",), "t0003": ("b",)}


def test_folding_twice_gives_the_same_view() -> None:
    log = Log(shared_id="s")
    log.append(FindingAppended(finding=_finding("a")))
    log.append(Merged(result=_finding("c"), sources=("a",)))

    assert fold(log) == fold(log)


def test_marked_trivial_removes_a_finding_from_the_view_without_deleting_it():
    log = Log(shared_id="s")
    log.append(FindingAppended(finding=_finding("a")))
    log.append(FindingAppended(finding=_finding("b")))
    log.append(MarkedTrivial(finding_ids=("a",)))

    view = fold(log)

    assert view.visible_ids == ("b",)
    assert "a" in view.findings                      # retained, not deleted
    assert view.trivial == frozenset({"a"})


def test_a_producer_supplied_trivial_status_does_not_hide_a_finding():
    """The fold no longer reads Finding.status AT ALL -- that field is
    producer-writable and `api.push_findings` runs only model_validate, so a
    first push carrying status=trivial used to exclude itself from retrieval
    forever with no way to correct it (adr/0004 Amendment, argument 2).

    Replaces test_trivial_findings_are_stored_but_not_visible, which asserted
    the opposite against the OLD mechanism."""
    log = Log(shared_id="s")
    log.append(FindingAppended(finding=_finding("a", status=FindingStatus.TRIVIAL)))
    log.append(FindingAppended(finding=_finding("b")))

    view = fold(log)

    assert view.visible_ids == ("a", "b")


def test_a_finding_both_merged_and_marked_trivial_is_absent_once_not_twice():
    log = Log(shared_id="s")
    log.append(FindingAppended(finding=_finding("a")))
    log.append(Merged(result=_finding("syn"), sources=("a",)))
    log.append(MarkedTrivial(finding_ids=("a",)))

    view = fold(log)

    assert view.visible_ids == ("syn",)
    assert list(view.visible_ids).count("syn") == 1


def test_marked_trivial_round_trips_through_rebuild():
    """The pin on adr/0004's central claim: 'the log is the only thing that
    has to survive.' If a MarkedTrivial entry does not come back from a replay,
    the trivia verdict has become a second source of truth hiding as a cache."""
    memory = SharedMemory(shared_id="s")
    memory.append(_finding("a"))
    memory.append(_finding("b"))
    memory.mark_trivial(("a",))
    before = memory.view()

    memory.rebuild()
    after = memory.view()

    assert after.visible_ids == before.visible_ids == ("b",)
    assert after.trivial == before.trivial == frozenset({"a"})
    assert set(after.findings) == set(before.findings)


def test_the_fold_is_last_merge_wins_when_a_source_is_claimed_twice():
    """The LOG's semantics, said out loud. `_apply` writes
    `superseded_by[source] = entry.result.id` unconditionally, so a second
    Merged entry naming the same source re-points it.

    The registry's `supersede` gives the OPPOSITE answer (first successor
    wins) and does it by pre-filtering to live sources before it ever reaches
    here -- see test_store.py's
    test_supersede_leaves_an_already_superseded_source_pointing_at_its_first_successor.
    Both are intentional; this test is what stops the next reader assuming the
    fold enforces the registry's rule."""
    log = Log(shared_id="s")
    log.append(FindingAppended(finding=_finding("a")))
    log.append(Merged(result=_finding("syn-1"), sources=("a",)))
    log.append(Merged(result=_finding("syn-2"), sources=("a",)))

    view = fold(log)

    assert view.superseded_by["a"] == "syn-2"
    assert view.resolve("a") == "syn-2"
