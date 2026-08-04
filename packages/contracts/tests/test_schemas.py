"""Contract tests — the first failing tests from Plan 0 Task 0.2."""

from __future__ import annotations

from datetime import datetime, timezone

import pytest
from pydantic import ValidationError

from synapse_contracts import (
    AgentEvent,
    Attribution,
    Conflict,
    Finding,
    FindingStatus,
    FindingType,
    Provenance,
    Segment,
)

TS = datetime(2026, 8, 4, 12, 0, 0, tzinfo=timezone.utc)


def _attribution(contributor: str = "aditya", agent_session: str = "as-1") -> Attribution:
    return Attribution(
        contributor=contributor, agent_session=agent_session, agent="claude-code"
    )


def retrievable(finding: Finding) -> bool:
    """The invariant, stated once: RETRIEVABLE == merged_into is None and status is KEPT."""
    return finding.merged_into is None and finding.status is FindingStatus.KEPT


def test_finding_round_trips_json_with_multiple_attributions() -> None:
    original = Finding(
        id="f-1",
        type=FindingType.LEARNING,
        text="pgbouncer transaction mode breaks prepared statements",
        attributions=[_attribution("aditya", "as-1"), _attribution("akhil", "as-2")],
        ts=TS,
        provenance=Provenance.SYNTHESIZED,
        merged_from=["f-a", "f-b"],
    )

    restored = Finding.model_validate_json(original.model_dump_json())

    assert restored == original
    assert len(restored.attributions) == 2
    assert [a.contributor for a in restored.attributions] == ["aditya", "akhil"]


def test_retrievable_holds_for_kept_tombstone_and_trivial() -> None:
    kept = Finding(
        id="f-kept", type=FindingType.DECISION, text="use session mode",
        attributions=[_attribution()], ts=TS,
    )
    tombstone = Finding(
        id="f-tomb", type=FindingType.DECISION, text="use session mode",
        attributions=[_attribution()], ts=TS, merged_into="f-synth",
    )
    trivial = Finding(
        id="f-triv", type=FindingType.LEARNING, text="ran the test suite",
        attributions=[_attribution()], ts=TS, status=FindingStatus.TRIVIAL,
    )

    assert retrievable(kept) is True
    assert retrievable(tombstone) is False
    assert retrievable(trivial) is False


def test_producer_defaults_leave_synthesis_fields_untouched() -> None:
    """status / merged_from / merged_into are service-written. Producers leave defaults."""
    produced = Finding(
        id="f-2", type=FindingType.OPEN_QUESTION, text="does the pool leak under retry?",
        attributions=[_attribution()], ts=TS,
    )

    assert produced.provenance is Provenance.DISTILLED
    assert produced.status is FindingStatus.KEPT
    assert produced.merged_from == []
    assert produced.merged_into is None


def test_conflict_references_ids_not_finding_objects() -> None:
    assert Conflict(finding_a="f-1", finding_b="f-2", description="disagree on mode")

    embedded = Finding(
        id="f-1", type=FindingType.DECISION, text="x",
        attributions=[_attribution()], ts=TS,
    )
    with pytest.raises(ValidationError):
        Conflict(finding_a=embedded, finding_b="f-2", description="nope")


def test_segment_and_agent_event_use_agent_session_id_never_session_id() -> None:
    event = AgentEvent(
        role="assistant", kind="text", content="hello",
        ts=TS, agent_session_id="as-1",
    )
    segment = Segment(
        id="seg-001", agent_session_id="as-1", events=[event],
        started_at=TS, ended_at=TS,
    )

    for model in (event, segment):
        fields = set(type(model).model_fields)
        assert "agent_session_id" in fields
        assert "session_id" not in fields


def test_every_finding_type_is_constructible() -> None:
    """All four types must be reachable — the distiller is measured on covering them."""
    for finding_type in FindingType:
        finding = Finding(
            id=f"f-{finding_type.value}", type=finding_type, text="t",
            attributions=[_attribution()], ts=TS,
        )
        assert finding.type is finding_type
