"""Log tests. Append is the only verb; position is the only ordering."""

from __future__ import annotations

from datetime import UTC, datetime

from synapse_contracts import Attribution, Finding, FindingType

from synapse_service.log import FindingAppended, Log, Merged, TopicAssigned

TS = datetime(2026, 8, 4, 12, 0, tzinfo=UTC)


def _finding(finding_id: str, text: str = "a finding") -> Finding:
    return Finding(
        id=finding_id,
        type=FindingType.LEARNING,
        text=text,
        attributions=[
            Attribution(
                contributor="aditya", agent_session="sess", agent="claude-code"
            )
        ],
        ts=TS,
    )


def test_append_returns_the_position() -> None:
    log = Log(shared_id="s")

    assert log.append(FindingAppended(finding=_finding("a"))) == 0
    assert log.append(FindingAppended(finding=_finding("b"))) == 1


def test_version_is_the_entry_count() -> None:
    """Advances on every entry, so 'anything new?' needs no model and no diffing."""
    log = Log(shared_id="s")
    assert log.version == 0

    log.append(TopicAssigned(finding_id="a", topic_id="t0001", founded=True))

    assert log.version == 1


def test_findings_yields_appended_and_merged_results_in_order() -> None:
    """Index rebuilds need superseded findings too — a merge can be reversed."""
    log = Log(shared_id="s")
    log.extend(
        [
            FindingAppended(finding=_finding("a")),
            FindingAppended(finding=_finding("b")),
            Merged(result=_finding("c"), sources=("a", "b")),
            TopicAssigned(finding_id="c", topic_id="t0001"),
        ]
    )

    assert [finding.id for finding in log.findings()] == ["a", "b", "c"]


def test_entries_are_iterable_and_counted() -> None:
    log = Log(shared_id="s")
    log.extend([FindingAppended(finding=_finding("a"))])

    assert len(log) == 1
    assert [type(entry) for entry in log] == [FindingAppended]


def test_created_at_is_the_first_finding_timestamp() -> None:
    log = Log(shared_id="s")
    assert log.created_at() is None

    log.append(FindingAppended(finding=_finding("a")))

    assert log.created_at() == TS


def test_finding_appended_exposes_its_id() -> None:
    entry = FindingAppended(finding=_finding("a"))

    assert entry.finding_id == "a"
    assert entry.kind == "finding"
