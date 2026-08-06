"""`--npu` decides which synthesizer the service builds (282fd07, second half).

`packages/service/tests/` covers `_provider()` — what the service does once
SYNAPSE_SYNTHESIZER is set. Nothing covered who sets it. `serve_local.py` is the
script every developer actually starts the stack with, and the branch choosing
`npu` over `aic100` from `args.npu` had no test: grep for `serve_local` across
`packages/` and `tests/` returned exactly one hit, and it was a docstring
reference, not an import.

That is the half of the fix that matters at the command line. `_provider()`
speaking the right endpoint is no help if `--npu` still exports
`SYNAPSE_SYNTHESIZER=aic100` — the service would build AIC100Provider, post to
/completions, and GenieX would answer 410 on every synthesis call while
`query_findings()` swallowed it into an empty result with a 200.

Loaded by path because `scripts/` is not a package (same as
test_local_model_server.py).
"""

from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
MODEL_URL = "http://127.0.0.1:18181/v1"


def _load():
    spec = importlib.util.spec_from_file_location(
        "serve_local", REPO / "scripts" / "serve_local.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def serve_local():
    return _load()


# --- the branch itself -----------------------------------------------------------


def test_npu_selects_the_synthesizer_that_speaks_chat_completions(serve_local) -> None:
    """`--npu` must export the arm `_provider()` routes to NPUProvider, which is
    the only one that posts to /chat/completions — the sole completions-shaped
    route `geniex serve` exposes."""
    env = serve_local.service_env_for(npu=True, model_url=MODEL_URL)

    assert env["SYNAPSE_SYNTHESIZER"] == "npu"
    assert env["SYNAPSE_BASE_URL"] == MODEL_URL


def test_without_npu_the_stand_in_arm_is_selected(serve_local) -> None:
    """The default. The stand-in serves BOTH shapes, so aic100 — which needs
    /completions by design — works against it."""
    env = serve_local.service_env_for(npu=False, model_url=MODEL_URL)

    assert env["SYNAPSE_SYNTHESIZER"] == "aic100"
    assert env["INFERENCE_CLOUD_BASE_URL"] == MODEL_URL


def test_the_two_arms_never_produce_the_same_synthesizer(serve_local) -> None:
    """Stated directly. Each test above checks only its own branch, so both
    would still pass if the flag stopped changing anything at all — which is
    exactly the regression this file exists for."""
    npu = serve_local.service_env_for(npu=True, model_url=MODEL_URL)
    standin = serve_local.service_env_for(npu=False, model_url=MODEL_URL)

    assert npu["SYNAPSE_SYNTHESIZER"] != standin["SYNAPSE_SYNTHESIZER"]


def test_each_arm_sets_only_its_own_base_url_variable(serve_local) -> None:
    """The two arms read DIFFERENT variables — `_provider()`'s npu branch reads
    SYNAPSE_BASE_URL, AIC100Provider reads INFERENCE_CLOUD_BASE_URL. Leaking the
    other arm's key would point a correctly-selected provider at the wrong host,
    or leave a stale value from a previous run in play.
    """
    npu = serve_local.service_env_for(npu=True, model_url=MODEL_URL)
    standin = serve_local.service_env_for(npu=False, model_url=MODEL_URL)

    assert "INFERENCE_CLOUD_BASE_URL" not in npu
    assert "SYNAPSE_BASE_URL" not in standin


@pytest.mark.parametrize(
    "model_url",
    [
        "http://127.0.0.1:18181/v1",
        "http://127.0.0.1:10787/v1",
        "http://192.168.1.20:18181/v1",
    ],
)
@pytest.mark.parametrize("npu", [True, False])
def test_whichever_arm_is_chosen_is_pointed_at_the_url_it_was_given(
    serve_local, npu: bool, model_url: str
) -> None:
    """Swept over both arms and several hosts, because the failure was a
    provider pointed at a host it could not speak to — selecting the right arm
    and then handing it the wrong URL is the same outage from the other side.

    Exactly one of the two base-url keys must carry it, whichever arm is live.
    """
    env = serve_local.service_env_for(npu=npu, model_url=model_url)

    urls = [env.get("SYNAPSE_BASE_URL"), env.get("INFERENCE_CLOUD_BASE_URL")]
    assert [u for u in urls if u is not None] == [model_url]


# --- the stand-in arm's budget travels with it -----------------------------------


def test_the_stand_in_arm_carries_the_derived_budget_not_the_providers_defaults(
    serve_local,
) -> None:
    """AIC100Provider's own 800/60s defaults are what broke synthesis on
    2026-08-06: a 500-word working memory is ~850 tokens, so the memory alone
    overran the cap and every round came back truncated while findings kept
    landing. Both numbers have to be exported here or the provider silently
    falls back to them (`demo_local.py` still does, per FLOW.md).
    """
    env = serve_local.service_env_for(npu=False, model_url=MODEL_URL)

    assert env["INFERENCE_CLOUD_MAX_TOKENS"] == str(serve_local.SYNTHESIS_MAX_TOKENS)
    assert env["INFERENCE_CLOUD_TIMEOUT"] == str(serve_local.SYNTHESIS_TIMEOUT_S)
    assert int(env["INFERENCE_CLOUD_MAX_TOKENS"]) == 1600
    assert float(env["INFERENCE_CLOUD_TIMEOUT"]) == 180


def test_the_raised_budget_is_actually_a_raise_over_the_providers_default(
    serve_local,
) -> None:
    """The ADR-0005 check on the test above: if SYNTHESIS_MAX_TOKENS were ever
    set back to 800, that test keeps passing while the truncation bug returns.
    Asserted against AIC100Provider's real constructor defaults, so the two
    cannot drift apart silently.
    """
    import inspect

    from synapse_providers import AIC100Provider

    defaults = inspect.signature(AIC100Provider.__init__).parameters
    assert serve_local.SYNTHESIS_MAX_TOKENS > defaults["max_tokens"].default
    assert serve_local.SYNTHESIS_TIMEOUT_S > defaults["timeout"].default


def test_every_value_is_a_string_because_the_environment_has_no_other_type(
    serve_local,
) -> None:
    """`spawn()` passes this straight into the subprocess environment, and a
    non-str value raises TypeError there rather than here — at stack startup,
    with a traceback that names `subprocess`, not this dict."""
    for npu in (True, False):
        env = serve_local.service_env_for(npu=npu, model_url=MODEL_URL)
        assert all(isinstance(v, str) for v in env.values()), env
