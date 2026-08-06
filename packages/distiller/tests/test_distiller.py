"""Distiller tests — Plan B Task B.1."""

from __future__ import annotations

import json

import pytest
from synapse_contracts import FindingType, LocalBinding, Provenance
from synapse_providers import FakeProvider

from synapse_distiller import Distiller, PromptDropError, load_pack_by_name
from synapse_distiller.distiller import RETRY_NUDGE
from synapse_distiller.fixtures import load_goldens, load_segment
from synapse_distiller.prompt import build_messages, render_segment

PACK = load_pack_by_name("v2-hardened")

BINDING = LocalBinding(
    agent_session_id="as-fixture-001",
    shared_id="shared-1",
    contributor="aditya",
    agent="claude-code",
)


def _scripted(findings: list[dict]) -> dict:
    return {"findings": findings}


async def test_fixture_segment_yields_schema_valid_findings() -> None:
    segment = load_segment("seg-001")
    goldens = load_goldens("seg-001")
    provider = FakeProvider(
        scripts=[_scripted([{"type": g.type.value, "text": g.text} for g in goldens])]
    )

    findings, stats = await Distiller(provider, BINDING).distil(segment)

    assert len(findings) == len(goldens)
    assert [f.type for f in findings] == [g.type for g in goldens]
    assert stats.attempts == 1


async def test_every_finding_type_is_reachable() -> None:
    segment = load_segment("seg-001")
    provider = FakeProvider(
        scripts=[_scripted([{"type": t.value, "text": f"a {t.value} finding"} for t in FindingType])]
    )

    findings, _ = await Distiller(provider, BINDING).distil(segment)

    assert {f.type for f in findings} == set(FindingType)


async def test_attribution_is_stamped_from_the_binding() -> None:
    segment = load_segment("seg-001")
    provider = FakeProvider(scripts=[_scripted([{"type": "learning", "text": "x"}])])

    findings, _ = await Distiller(provider, BINDING).distil(segment)

    (attribution,) = findings[0].attributions
    assert attribution.contributor == "aditya"
    assert attribution.agent_session == "as-fixture-001"
    assert attribution.agent == "claude-code"


async def test_producer_leaves_service_written_fields_at_defaults() -> None:
    segment = load_segment("seg-001")
    provider = FakeProvider(scripts=[_scripted([{"type": "learning", "text": "x"}])])

    findings, _ = await Distiller(provider, BINDING).distil(segment)

    assert findings[0].provenance is Provenance.DISTILLED
    assert findings[0].merged_from == []
    assert findings[0].merged_into is None


async def test_all_noise_fixture_yields_empty_array() -> None:
    """seg-004's golden is an empty array. Small models invent findings for
    boring input — this is the guard, and it is failure mode #3."""
    segment = load_segment("seg-004")
    assert load_goldens("seg-004") == []
    provider = FakeProvider(scripts=[_scripted([])])

    findings, _ = await Distiller(provider, BINDING).distil(segment)

    assert findings == []


async def test_malformed_output_retries_exactly_once_then_drops() -> None:
    segment = load_segment("seg-001")
    provider = FakeProvider(scripts=["not json at all", "still not json"])

    findings, stats = await Distiller(provider, BINDING).distil(segment)

    assert findings == []
    assert stats.attempts == 2
    assert provider.calls == 2


async def test_malformed_then_valid_recovers_on_the_retry() -> None:
    segment = load_segment("seg-001")
    provider = FakeProvider(
        scripts=["garbage", _scripted([{"type": "decision", "text": "recovered"}])]
    )

    findings, stats = await Distiller(provider, BINDING).distil(segment)

    assert [f.text for f in findings] == ["recovered"]
    assert stats.attempts == 2
    assert stats.dropped_malformed == 1


async def test_the_retry_sends_a_different_prompt_than_the_first_attempt() -> None:
    """A retry that resends byte-identical messages to a temperature-0 model is
    not a retry — the decode is deterministic, so it reproduces the first
    failure exactly and spends a second run of NPU time doing it. Observed
    live: a malformed response came back in the same shape twice. So attempt 2
    appends a corrective instruction, which is the only lever available here
    (temperature belongs to the provider, not this call)."""

    class _Recording(FakeProvider):
        def __init__(self, scripts):
            super().__init__(scripts=scripts)
            self.seen: list[list[dict]] = []

        async def complete(self, messages, response_schema=None):
            self.seen.append(list(messages))
            return await super().complete(messages, response_schema)

    segment = load_segment("seg-001")
    provider = _Recording(["garbage", _scripted([{"type": "learning", "text": "ok"}])])

    findings, stats = await Distiller(provider, BINDING).distil(segment)

    assert stats.attempts == 2 and [f.text for f in findings] == ["ok"]
    assert provider.seen[0] != provider.seen[1], "the second call must differ"
    assert provider.seen[1][-1] == RETRY_NUDGE
    assert provider.seen[1][:-1] == provider.seen[0], "only the nudge is added"


async def test_ids_are_unique_within_a_batch() -> None:
    segment = load_segment("seg-001")
    provider = FakeProvider(
        scripts=[_scripted([{"type": "learning", "text": f"finding {i}"} for i in range(5)])]
    )

    findings, _ = await Distiller(provider, BINDING).distil(segment)

    assert len({f.id for f in findings}) == 5


async def test_id_survives_serialization_so_replay_is_idempotent() -> None:
    """The id is stamped at distil time so the worker's write-ahead log can
    replay an unsent finding with an identical id. Ingest upserts by id."""
    segment = load_segment("seg-001")
    provider = FakeProvider(scripts=[_scripted([{"type": "learning", "text": "x"}])])

    findings, _ = await Distiller(provider, BINDING).distil(segment)
    original = findings[0]
    replayed = type(original).model_validate_json(original.model_dump_json())

    assert replayed.id == original.id


async def test_unknown_finding_type_is_dropped_not_coerced() -> None:
    segment = load_segment("seg-001")
    provider = FakeProvider(
        scripts=[
            _scripted(
                [
                    {"type": "insight", "text": "invented type"},
                    {"type": "learning", "text": "valid one"},
                ]
            )
        ]
    )

    findings, stats = await Distiller(provider, BINDING).distil(segment)

    assert [f.text for f in findings] == ["valid one"]
    assert stats.dropped_invalid_type == 1


async def test_empty_text_findings_are_dropped() -> None:
    segment = load_segment("seg-001")
    provider = FakeProvider(
        scripts=[_scripted([{"type": "learning", "text": "   "}, {"type": "learning", "text": "real"}])]
    )

    findings, stats = await Distiller(provider, BINDING).distil(segment)

    assert [f.text for f in findings] == ["real"]
    assert stats.dropped_empty_text == 1


async def test_dropped_prompt_raises_before_any_finding_is_built() -> None:
    """The highest-severity guard, exercised through the real distiller path."""
    segment = load_segment("seg-001")
    provider = FakeProvider(
        scripts=[_scripted([{"type": "learning", "text": "invented from nothing"}])],
        input_tokens=1,
    )

    with pytest.raises(PromptDropError):
        await Distiller(provider, BINDING).distil(segment)


def test_render_is_deterministic_across_repeated_calls() -> None:
    segment = load_segment("seg-001")
    assert render_segment(segment) == render_segment(segment)
    assert build_messages(segment, PACK) == build_messages(segment, PACK)


def test_different_packs_produce_different_prompts() -> None:
    segment = load_segment("seg-001")
    v1 = build_messages(segment, load_pack_by_name("v1-baseline"))
    v2 = build_messages(segment, load_pack_by_name("v2-hardened"))

    assert v1 != v2
    assert v1[0]["content"] != v2[0]["content"]


def test_task_suffix_rides_the_same_message_as_the_segment() -> None:
    """A separate message would put a turn boundary between the restated task
    and the data it refers to, which is the thing the suffix exists to avoid."""
    messages = build_messages(load_segment("seg-001"), PACK)

    assert messages[-1]["role"] == "user"
    assert PACK.task_suffix.strip()[:40] in messages[-1]["content"]
    assert "pgbouncer" in messages[-1]["content"]


def test_rendered_prompt_carries_the_error_line_and_the_pivot() -> None:
    """A dead_end requires linking an error to the later pivot. If rendering
    loses either, failure mode #4 is guaranteed regardless of the model."""
    rendered = render_segment(load_segment("seg-001"))

    assert "prepared statement" in rendered
    assert "session" in rendered


def test_few_shots_are_valid_json_and_include_an_empty_answer() -> None:
    messages = build_messages(load_segment("seg-004"), PACK)
    assistant_turns = [m for m in messages if m["role"] == "assistant"]

    payloads = [json.loads(m["content"]) for m in assistant_turns]
    assert {"findings": []} in payloads
    assert any(p["findings"] for p in payloads)


async def test_stats_record_which_prompt_pack_produced_the_result() -> None:
    """A finding whose prompt version is unknown cannot be compared across runs,
    and the pack is expected to change daily."""
    provider = FakeProvider(scripts=[_scripted([{"type": "learning", "text": "x"}])])

    _, stats = await Distiller(provider, BINDING, PACK).distil(load_segment("seg-001"))

    assert stats.prompt_pack == "v2-hardened"
