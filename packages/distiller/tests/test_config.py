"""Config tests — model, prompt pack and budget are configurable and validated."""

from __future__ import annotations

import pytest

from synapse_distiller.capability import CapabilityError
from synapse_distiller.config import load_config

CONFIG = """
[distiller]
model = "test-model"
prompt_pack = "v2-hardened"
max_seconds_per_call = 30.0

[provider]
base_url = "http://127.0.0.1:9999/v1"
max_tokens = 512

[capability."test-model"]
usable_context = 4096
prefill_toks_per_sec = 250.0
response_reserve = 500
"""


def _write(tmp_path, body: str = CONFIG):
    path = tmp_path / "synapse.toml"
    path.write_text(body, encoding="utf-8")
    return path


def test_loads_model_pack_and_provider_from_file(tmp_path) -> None:
    config = load_config(_write(tmp_path))

    assert config.model == "test-model"
    assert config.prompt_pack.name == "v2-hardened"
    assert config.provider.base_url == "http://127.0.0.1:9999/v1"
    assert config.provider.max_tokens == 512


def test_env_overrides_the_file(tmp_path, monkeypatch) -> None:
    """So a bake-off can sweep models without editing a tracked file."""
    monkeypatch.setenv("SYNAPSE_PROMPT_PACK", "v1-baseline")
    monkeypatch.setenv("SYNAPSE_MAX_TOKENS", "256")

    config = load_config(_write(tmp_path))

    assert config.prompt_pack.name == "v1-baseline"
    assert config.provider.max_tokens == 256


def test_budget_is_resolved_from_record_and_pack(tmp_path) -> None:
    config = load_config(_write(tmp_path))
    expected = config.record.segment_budget(
        config.prompt_pack.overhead_tokens, max_seconds_per_call=30.0
    )

    assert config.segment_budget == expected


def test_switching_prompt_pack_changes_the_budget(tmp_path, monkeypatch) -> None:
    """The end-to-end version of 'overhead is derived': change the prompt, and
    the segmenter's budget moves with it."""
    path = _write(tmp_path)

    monkeypatch.setenv("SYNAPSE_PROMPT_PACK", "v1-baseline")
    v1_budget = load_config(path).segment_budget
    monkeypatch.setenv("SYNAPSE_PROMPT_PACK", "v2-hardened")
    v2_budget = load_config(path).segment_budget

    assert v1_budget > v2_budget  # v2 is the longer prompt


def test_unknown_model_fails_with_a_useful_message(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("SYNAPSE_MODEL", "not-a-real-model")
    config = load_config(_write(tmp_path))

    with pytest.raises(CapabilityError, match="No capability record"):
        _ = config.record


def test_override_is_honoured_when_valid(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("SYNAPSE_SEGMENT_BUDGET", "1200")

    assert load_config(_write(tmp_path)).segment_budget == 1200


def test_oversized_override_is_rejected_at_resolution(tmp_path, monkeypatch) -> None:
    """Fails when the budget is resolved, not silently at call time."""
    monkeypatch.setenv("SYNAPSE_SEGMENT_BUDGET", "9000")
    config = load_config(_write(tmp_path))

    with pytest.raises(CapabilityError, match="would be truncated"):
        _ = config.segment_budget


def test_describe_names_every_input_to_the_budget(tmp_path) -> None:
    """A reported number that cannot name its model and prompt pack is not
    comparable to any other run."""
    described = load_config(_write(tmp_path)).describe()

    for expected in ("test-model", "v2-hardened", "segment_budget", "prompt_overhead"):
        assert expected in described


def test_describe_reports_calibration_provenance(tmp_path) -> None:
    """Whether the overhead was measured or estimated changes how much the
    reported budget can be trusted, so it travels with the number."""
    described = load_config(_write(tmp_path)).describe()

    assert "calibrated against" in described
    assert "Qwen3-4B" in described
