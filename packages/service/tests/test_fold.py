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
from synapse_service.log import FindingAppended, Log, Merged, TopicAssigned, TopicSplit

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


def test_trivial_findings_are_stored_but_not_visible() -> None:
    log = Log(shared_id="s")
    log.append(
        FindingAppended(finding=_finding("a", status=FindingStatus.TRIVIAL))
    )
    log.append(FindingAppended(finding=_finding("b")))

    view = fold(log)

    assert view.visible_ids == ("b",)
    assert "a" in view.findings


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
