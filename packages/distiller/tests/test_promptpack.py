"""Prompt-pack tests — configurable prompts must not become configurable bugs."""

from __future__ import annotations

import pytest

from synapse_distiller.promptpack import (
    PromptPackError,
    available_packs,
    load_pack,
    load_pack_by_name,
)


def test_both_shipped_packs_load() -> None:
    packs = available_packs()

    assert {"v1-baseline", "v2-hardened"} <= set(packs)
    for name in packs:
        assert load_pack_by_name(name).system


def test_overhead_is_derived_from_the_text_not_configured() -> None:
    """The property the whole design rests on: a longer pack costs more, with
    nobody updating a number by hand."""
    v1 = load_pack_by_name("v1-baseline")
    v2 = load_pack_by_name("v2-hardened")

    assert v2.estimated_overhead_tokens > v1.estimated_overhead_tokens


def test_shipped_packs_are_calibrated_against_the_real_model() -> None:
    """Both were measured on 2026-08-04 via scripts/calibrate_prompt.py. An
    uncalibrated pack falls back to a chars/4 estimate, which over-counted by
    164 tokens on v2 — safe, but it wastes budget on a 4096-token bundle."""
    for name, expected in (("v1-baseline", 497), ("v2-hardened", 719)):
        pack = load_pack_by_name(name)
        assert pack.is_calibrated is True
        assert pack.overhead_tokens == expected
        assert "Qwen3-4B" in pack.calibrated_against


def test_uncalibrated_pack_falls_back_to_the_estimate(tmp_path) -> None:
    path = tmp_path / "raw.toml"
    path.write_text('name = "raw"\nsystem = "some system prompt"\n', encoding="utf-8")
    pack = load_pack(path)

    assert pack.is_calibrated is False
    assert pack.overhead_tokens == pack.estimated_overhead_tokens


def test_v1_declares_its_contamination_and_v2_does_not() -> None:
    """v1's second few-shot duplicates fixture seg-004, so any seg-004 score
    from it is void. The pack says so rather than relying on someone remembering."""
    assert load_pack_by_name("v1-baseline").is_contaminated_for("seg-004") is True
    assert load_pack_by_name("v1-baseline").is_contaminated_for("seg-001") is False
    assert load_pack_by_name("v2-hardened").is_contaminated_for("seg-004") is False


def test_v2_noise_few_shot_does_not_reuse_the_seg004_scenario() -> None:
    """The actual decontamination, asserted on content rather than on a flag."""
    v2 = load_pack_by_name("v2-hardened")
    noise_shot = v2.few_shots[-1].input.lower()

    assert "ruff" not in noise_shot
    assert "import" not in noise_shot
    assert noise_shot.strip()


def test_v2_holds_abstraction_and_granularity_together() -> None:
    """Pushing granularity caused identifier quoting in a live probe, so the
    suffix must restate both rules, not just the new one."""
    suffix = load_pack_by_name("v2-hardened").task_suffix.lower()

    assert "one finding per distinct insight" in suffix
    assert "identifier" in suffix


def test_active_packs_label_roles_the_way_the_renderer_does() -> None:
    """A few-shot that says 'assistant:' while the rendered segment says 'agent:'
    teaches from a format the real input never has. This mismatch was introduced
    once by renaming the role labels and forgetting the packs."""
    from synapse_distiller.prompt import ROLE_LABELS

    # v1-baseline is excluded: it is kept frozen so the contaminated-few-shot
    # episode stays reproducible, not because it is fit to run.
    for name in (n for n in available_packs() if n != "v1-baseline"):
        for index, shot in enumerate(load_pack_by_name(name).few_shots):
            where = f"{name} few_shots[{index}]"
            assert "assistant:" not in shot.input, f"{where} uses the old label"
            assert "user:" not in shot.input, f"{where} uses the old label"
            assert any(
                f"{label}:" in shot.input for label in ROLE_LABELS.values()
            ), f"{where} labels no role at all"


def test_judgment_packs_and_compression_packs_declare_which_they_are() -> None:
    """The eval harness scores an all-noise fixture differently depending on
    this, so a pack that compresses only must say so rather than be reported as
    failing a job it was never asked to do."""
    assert load_pack_by_name("v4-condense").judges_durability is False
    assert load_pack_by_name("v2-hardened").judges_durability is True


def test_condense_pack_states_the_fidelity_rule() -> None:
    """The rule that fixed the factual inversion. If it is edited away, the
    inversion is expected to return."""
    system = load_pack_by_name("v4-condense").system.lower()

    assert "never reverse a comparison" in system
    assert "follow the session" in system


def test_missing_pack_raises_a_clear_error(tmp_path) -> None:
    with pytest.raises(PromptPackError, match="not found"):
        load_pack(tmp_path / "nope.toml")


def test_pack_without_a_system_prompt_is_rejected(tmp_path) -> None:
    path = tmp_path / "bad.toml"
    path.write_text('name = "bad"\nsystem = ""\n', encoding="utf-8")

    with pytest.raises(PromptPackError, match="missing required key"):
        load_pack(path)


def test_few_shot_with_invalid_json_output_is_rejected(tmp_path) -> None:
    """A few-shot modelling malformed output teaches the model to emit it."""
    path = tmp_path / "bad.toml"
    path.write_text(
        'name = "bad"\nsystem = "s"\n\n[[few_shots]]\ninput = "i"\noutput = "not json"\n',
        encoding="utf-8",
    )

    with pytest.raises(PromptPackError, match="not valid JSON"):
        load_pack(path)


def test_calibrated_overhead_wins_over_the_estimate(tmp_path) -> None:
    path = tmp_path / "cal.toml"
    path.write_text(
        'name = "cal"\nsystem = "some system prompt text"\n\n'
        '[calibration]\noverhead_tokens = 123\nmeasured_against = "some-model"\n',
        encoding="utf-8",
    )

    pack = load_pack(path)

    assert pack.is_calibrated is True
    assert pack.overhead_tokens == 123
    assert pack.overhead_tokens != pack.estimated_overhead_tokens
