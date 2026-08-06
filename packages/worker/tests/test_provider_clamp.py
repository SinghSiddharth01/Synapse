"""The worker-side max_tokens clamp, and the identity that makes it mean
something (7418a63).

`SynapseConfig.effective_max_tokens` had two tests. The line that USES it did
not:

    max_tokens = config.effective_max_tokens          # worker/cli.py

`test_cli.py::test_run_wraps_the_distiller_provider_when_debug_enabled`
executes that line and asserts nothing about it — revert it to
`config.provider.max_tokens` and the whole suite stayed green while the NPU box
went back to asking for 900 output tokens against a 500-token reserve. The
orchestrator's copy of the same line is asserted (`test_cli.py:900`); the
worker's, which is the one the demo actually runs, was not.

The clamp is only meaningful because of an identity, so this file asserts the
identity too. `segment_budget = usable_context - prompt_overhead -
response_reserve` makes the reserve a PROMISE: the segmenter packed prompts
assuming no more than that many output tokens would be asked for on top. On
the shipped config those three numbers sum to exactly 4096 with zero slack
(FLOW.md §2-3), so requesting 900 put a full segment at 4496 against a 4096
ceiling — measured 2026-08-06, and the response came back truncated or
degenerate.
"""

from __future__ import annotations

import logging

import pytest
from synapse_distiller.config import load_config
from synapse_providers import NPUProvider

from synapse_worker import cli

# FLOW.md §2-3, EFFECTIVE values on the shipped config with no SYNAPSE_* env.
SHIPPED_USABLE_CONTEXT = 4096
SHIPPED_SEGMENT_BUDGET = 2787
SHIPPED_PROMPT_OVERHEAD = 809
SHIPPED_RESPONSE_RESERVE = 500
SHIPPED_RAW_MAX_TOKENS = 900

CONFIG = """
[distiller]
model = "test-model"
prompt_pack = "v2-hardened"
max_seconds_per_call = 30.0

[provider]
max_tokens = {max_tokens}

[capability."test-model"]
usable_context = 4096
prefill_toks_per_sec = 250.0
response_reserve = {reserve}
"""


@pytest.fixture(autouse=True)
def _no_env_overrides(monkeypatch):
    """Every SYNAPSE_* key `load_config` reads, cleared. Otherwise a stray
    export in the runner's environment silently rewrites the numbers this file
    exists to pin."""
    for key in (
        "MODEL", "PROMPT_PACK", "MAX_TOKENS", "SEGMENT_BUDGET", "BASE_URL",
        "MAX_SECONDS_PER_CALL", "DISTILLER",
    ):
        monkeypatch.delenv(f"SYNAPSE_{key}", raising=False)


def _config(tmp_path, *, max_tokens: int, reserve: int):
    path = tmp_path / "synapse.toml"
    path.write_text(
        CONFIG.format(max_tokens=max_tokens, reserve=reserve), encoding="utf-8"
    )
    return load_config(path)


# --- the shipped numbers, end to end ---------------------------------------------


def test_the_worker_asks_the_npu_for_the_reserve_not_the_configured_900() -> None:
    """Against the REAL committed config/synapse.toml, not a tmp_path fixture —
    the point is what the NPU box is asked for tonight, and a fixture cannot
    say that.

    Reverting `config.effective_max_tokens` to `config.provider.max_tokens` in
    `_build_distiller_provider` makes this the only failing test in the suite.
    """
    config = load_config()
    provider = cli._build_distiller_provider(config)

    assert isinstance(provider, NPUProvider)
    assert provider.max_tokens == SHIPPED_RESPONSE_RESERVE
    assert config.provider.max_tokens == SHIPPED_RAW_MAX_TOKENS, (
        "the raw config value is deliberately left at 900 — the clamp is what "
        "protects the NPU, so if this ever equals the reserve the clamp stops "
        "being exercised in production and this file stops proving anything"
    )
    assert provider.max_tokens < config.provider.max_tokens


def test_the_full_prompt_fits_the_context_exactly_with_the_clamp_and_not_without() -> (
    None
):
    """The identity the clamp exists to preserve, and the failure it prevents,
    asserted as arithmetic on the shipped config rather than as a fixture.

    budget + overhead + effective  ==  usable_context   (4096, zero slack)
    budget + overhead + raw        >   usable_context   (4496, the bug)
    """
    config = load_config()
    overhead = config.prompt_pack.overhead_tokens

    assert config.segment_budget == SHIPPED_SEGMENT_BUDGET
    assert overhead == SHIPPED_PROMPT_OVERHEAD
    assert config.record.usable_context == SHIPPED_USABLE_CONTEXT

    assert (
        config.segment_budget + overhead + config.effective_max_tokens
        == config.record.usable_context
    ), "the reserve is a promise the segmenter already spent"
    assert (
        config.segment_budget + overhead + config.provider.max_tokens
        > config.record.usable_context
    ), "without the clamp a full segment overruns the ceiling — this is the bug"


def test_the_clamp_names_both_numbers_when_it_bites(tmp_path, caplog) -> None:
    """A clamp that silently rewrites a configured value is a config that lies
    about itself. The log is the only place someone who set 900 finds out they
    are getting 500."""
    config = _config(tmp_path, max_tokens=900, reserve=500)

    with caplog.at_level(logging.INFO, logger="synapse_worker.cli"):
        cli._build_distiller_provider(config)

    [log] = [r.getMessage() for r in caplog.records if "clamping" in r.getMessage()]
    assert "900" in log
    assert "500" in log
    assert "test-model" in log


def test_no_clamp_log_when_the_configured_value_already_honours_the_reserve(
    tmp_path, caplog
) -> None:
    """The mirror. A notice on every startup is a notice nobody reads, and the
    one that matters goes past with it."""
    config = _config(tmp_path, max_tokens=400, reserve=500)

    with caplog.at_level(logging.INFO, logger="synapse_worker.cli"):
        provider = cli._build_distiller_provider(config)

    assert provider.max_tokens == 400, "the clamp lowers; it must never raise"
    assert [r for r in caplog.records if "clamping" in r.getMessage()] == []


# --- the bound, swept ------------------------------------------------------------


@pytest.mark.parametrize("max_tokens", [1, 250, 499, 500, 501, 900, 4096, 100_000])
@pytest.mark.parametrize("reserve", [500, 512, 1024])
def test_the_request_never_exceeds_the_reserve_at_any_configured_value(
    tmp_path, max_tokens: int, reserve: int
) -> None:
    """Property-style, because the bug was a single value getting through, and
    a single-fixture test would only ever have caught the one value it chose.

    Two directions, both asserted: the request never EXCEEDS the reserve (the
    bound), and it never falls BELOW a configured value that already fits (the
    clamp must not become a blanket lowering, which would waste output room on
    every arm whose reserve is generous).
    """
    config = _config(tmp_path, max_tokens=max_tokens, reserve=reserve)

    provider = cli._build_distiller_provider(config)

    assert provider.max_tokens <= reserve
    assert provider.max_tokens == min(max_tokens, reserve)
    assert provider.max_tokens > 0


@pytest.mark.parametrize("reserve", [500, 512, 1024, 2048])
def test_whatever_the_reserve_the_prompt_plus_the_request_still_fits(
    tmp_path, reserve: int
) -> None:
    """The identity above, held across reserves rather than at the shipped one.

    `segment_budget` is DERIVED from the reserve, so raising the reserve
    shrinks the budget by the same amount and the sum is invariant. That is the
    whole reason clamping to the reserve is safe — asserted, because if the
    derivation ever stopped tracking, the clamp would go on looking correct
    while protecting nothing.
    """
    config = _config(tmp_path, max_tokens=100_000, reserve=reserve)
    provider = cli._build_distiller_provider(config)

    total = (
        config.segment_budget + config.prompt_pack.overhead_tokens + provider.max_tokens
    )
    assert total <= config.record.usable_context
    assert total == config.record.usable_context, (
        "zero slack is the design — any gap here means budget derivation and "
        "the clamp have drifted apart"
    )


# --- the cloud arms are deliberately NOT clamped ---------------------------------


def test_the_anthropic_arm_keeps_its_own_much_larger_budget(
    tmp_path, monkeypatch
) -> None:
    """The clamp is keyed to the NPU's 4096 ceiling and returns BEFORE it for
    the cloud arms — they have their own reserves, orders of magnitude larger,
    and clamping a 1M-context model to 500 output tokens would cripple it.

    Protected by branch order alone (FLOW.md:136): `config.model` stays the NPU
    model whatever `SYNAPSE_DISTILLER` says, so if the arm checks were ever
    moved below the clamp, this arm would silently inherit the NPU's reserve.
    """
    monkeypatch.setenv("SYNAPSE_DISTILLER", "anthropic")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key-not-used")
    config = _config(tmp_path, max_tokens=900, reserve=500)

    provider = cli._build_distiller_provider(config)

    assert provider.provider_id == "anthropic"
    assert provider.max_tokens > SHIPPED_RESPONSE_RESERVE
    assert provider.max_tokens > config.provider.max_tokens


def test_the_claude_cli_arm_is_handed_the_raw_value_unclamped(
    tmp_path, monkeypatch
) -> None:
    """Also above the clamp, and given `config.provider.max_tokens` rather than
    the effective one. Pinned because it looks like an oversight and is not:
    ClaudeCliProvider ignores max_tokens entirely (the `claude` binary takes no
    such flag), so the value is inert — but the day it stops being inert, this
    test is what says which number it should then get.
    """
    monkeypatch.setenv("SYNAPSE_DISTILLER", "claude-cli")
    config = _config(tmp_path, max_tokens=900, reserve=500)

    provider = cli._build_distiller_provider(config)

    assert provider.max_tokens == 900
    assert provider.max_tokens == config.provider.max_tokens
    assert provider.max_tokens != config.effective_max_tokens


def test_an_unset_distiller_env_still_selects_the_npu(tmp_path, monkeypatch) -> None:
    """Nobody who leaves SYNAPSE_DISTILLER unset sees any change — the demo is
    the NPU story, and the cloud arms exist only to run the loop in parallel."""
    monkeypatch.delenv("SYNAPSE_DISTILLER", raising=False)

    provider = cli._build_distiller_provider(
        _config(tmp_path, max_tokens=900, reserve=500)
    )

    assert isinstance(provider, NPUProvider)
    assert provider.max_tokens == 500
