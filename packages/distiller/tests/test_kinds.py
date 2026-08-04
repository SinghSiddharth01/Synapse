"""Event-kind filtering — which parts of a Segment reach the model."""

from __future__ import annotations

import pytest
from synapse_contracts import LocalBinding, Segment
from synapse_providers import FakeProvider

from synapse_distiller import Distiller, load_pack_by_name
from synapse_distiller.config import ConfigError, load_config
from synapse_distiller.fixtures import load_segment
from synapse_distiller.prompt import (
    DEFAULT_KINDS,
    DEFAULT_RENDER_STYLE,
    render_segment,
)

PACK = load_pack_by_name("v3-text")
BINDING = LocalBinding(
    agent_session_id="as-fixture-001",
    shared_id="s",
    contributor="aditya",
    agent="claude-code",
)


def test_defaults_are_text_only_and_labelled() -> None:
    """labelled, not content: measured back-to-back, content saved 12 tokens
    (0.7%) and inverted a fact on seg-001."""
    assert DEFAULT_KINDS == ("text",)
    assert DEFAULT_RENDER_STYLE == "labelled"


def test_content_style_strips_role_labels() -> None:
    segment = load_segment("seg-001")
    content = render_segment(segment, ["text"], "content")
    labelled = render_segment(segment, ["text"], "labelled")

    assert "agent:" not in content
    assert "developer:" not in content
    assert "agent:" in labelled
    # The prose itself is untouched either way.
    assert "Switching to session pooling" in content
    assert len(content) < len(labelled)


def test_labelled_style_keeps_who_said_what() -> None:
    """Developer turns carry intent and correction; agent turns carry reasoning.
    A finding's type often depends on which one it came from."""
    rendered = render_segment(load_segment("seg-001"), ["text"], "labelled")

    assert "developer: The API is opening too many Postgres connections" in rendered
    assert "agent: Transaction mode is a dead end here" in rendered


def test_role_labels_do_not_collide_with_the_chat_envelope() -> None:
    """The segment rides inside a role=user message and the few-shots above it
    are literal user/assistant turns, so those two words must not also label
    transcript lines. CONTEXT.md lists both under _Avoid_."""
    rendered = render_segment(load_segment("seg-001"), ["text"], "labelled")

    assert "user:" not in rendered
    assert "assistant:" not in rendered


def test_content_style_preserves_every_block() -> None:
    """Stripping labels must not drop events, only their prefixes."""
    segment = load_segment("seg-001")
    rendered = render_segment(segment, ["text"], "content")

    text_events = [e for e in segment.events if e.kind == "text"]
    assert len(rendered.split("\n\n")) == len(text_events)
    for event in text_events:
        assert event.content in rendered


def test_unknown_render_style_raises() -> None:
    with pytest.raises(ValueError, match="Unknown render style"):
        render_segment(load_segment("seg-001"), ["text"], "freeform")


def test_config_rejects_unknown_render_style(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("SYNAPSE_RENDER_STYLE", "bare")
    path = tmp_path / "synapse.toml"
    path.write_text("", encoding="utf-8")

    with pytest.raises(ConfigError, match="Unknown render_style"):
        load_config(path)


async def test_stats_record_the_render_style() -> None:
    provider = FakeProvider(scripts=[{"findings": []}])

    _, stats = await Distiller(provider, BINDING, PACK, ["text"], "content").distil(
        load_segment("seg-001")
    )

    assert stats.render_style == "content"


def test_text_only_render_drops_tool_and_thinking_content() -> None:
    segment = load_segment("seg-001")
    rendered = render_segment(segment, ["text"])

    # From tool_use / tool_result / thinking — must not survive.
    assert "__asyncpg_stmt_3__" not in rendered
    assert "pgbouncer.ini" not in rendered
    assert "23 passed" not in rendered
    # From assistant prose — must survive.
    assert "dead end" in rendered
    assert "Switching to session pooling" in rendered


def test_text_only_render_is_substantially_smaller() -> None:
    segment = load_segment("seg-001")

    assert len(render_segment(segment, ["text"])) < len(render_segment(segment)) * 0.7


def test_all_four_goldens_remain_recoverable_from_prose_alone() -> None:
    """The load-bearing check for this whole setting. If the prose does not carry
    the dead_end, the decision, the tradeoff and the open risk, then text-only
    is discarding findings rather than discarding noise."""
    rendered = render_segment(load_segment("seg-001"), ["text"]).lower()

    assert "dead end" in rendered                       # dead_end
    assert "switching to session pooling" in rendered   # decision
    assert "statement cache" in rendered                # its rejected alternative
    assert "reuse ratio is much lower" in rendered      # learning
    assert "load test" in rendered                      # open_question


def test_widening_kinds_restores_the_dropped_content() -> None:
    segment = load_segment("seg-001")
    rendered = render_segment(segment, ["text", "tool_result"])

    assert "prepared statement" in rendered
    assert "pool_mode = transaction" not in rendered  # that is tool_use, still excluded


def test_none_means_every_kind() -> None:
    segment = load_segment("seg-001")

    assert render_segment(segment) == render_segment(
        segment, ["text", "thinking", "tool_use", "tool_result"]
    )


async def test_segment_with_no_matching_kind_skips_the_model_entirely() -> None:
    """An empty segment body would leave only instructions and few-shots in the
    prompt, from which a small model will invent a finding about the few-shots."""
    segment = load_segment("seg-001")
    tool_only = Segment(
        id="tool-only",
        agent_session_id=segment.agent_session_id,
        events=[e for e in segment.events if e.kind == "tool_use"],
        started_at=segment.started_at,
        ended_at=segment.ended_at,
    )
    provider = FakeProvider(scripts=[])  # any call would raise "exhausted"

    findings, stats = await Distiller(provider, BINDING, PACK, ["text"]).distil(tool_only)

    assert findings == []
    assert stats.skipped_empty is True
    assert stats.attempts == 0
    assert provider.calls == 0


async def test_stats_record_the_kinds_used() -> None:
    provider = FakeProvider(scripts=[{"findings": []}])

    _, stats = await Distiller(provider, BINDING, PACK, ["text"]).distil(
        load_segment("seg-001")
    )

    assert stats.kinds == ("text",)
    assert stats.skipped_empty is False


def test_config_parses_kinds_from_toml(tmp_path) -> None:
    path = tmp_path / "synapse.toml"
    path.write_text(
        '[distiller]\nmodel = "m"\ndistil_kinds = ["text", "tool_result"]\n\n'
        '[capability."m"]\nusable_context = 4096\nprefill_toks_per_sec = 250.0\n'
        "response_reserve = 500\n",
        encoding="utf-8",
    )

    assert load_config(path).distil_kinds == ("text", "tool_result")


def test_config_parses_kinds_from_env(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("SYNAPSE_DISTIL_KINDS", "text,thinking")
    path = tmp_path / "synapse.toml"
    path.write_text("", encoding="utf-8")

    assert load_config(path).distil_kinds == ("text", "thinking")


def test_unknown_kind_is_rejected(tmp_path, monkeypatch) -> None:
    """A typo would silently narrow the input rather than raise."""
    monkeypatch.setenv("SYNAPSE_DISTIL_KINDS", "text,tool_results")
    path = tmp_path / "synapse.toml"
    path.write_text("", encoding="utf-8")

    with pytest.raises(ConfigError, match="Unknown distil_kinds"):
        load_config(path)


def test_empty_kinds_is_rejected(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("SYNAPSE_DISTIL_KINDS", " ")
    path = tmp_path / "synapse.toml"
    path.write_text('[distiller]\ndistil_kinds = []\n', encoding="utf-8")

    with pytest.raises(ConfigError, match="empty"):
        load_config(path)
