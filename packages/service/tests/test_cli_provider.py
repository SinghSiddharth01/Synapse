"""Which synthesizer the service builds, and why the NPU needs its own arm."""

from __future__ import annotations

from synapse_providers import FakeProvider, NPUProvider

from synapse_service.cli import _provider


def test_npu_arm_speaks_the_only_endpoint_geniex_actually_serves(monkeypatch) -> None:
    """`geniex serve` exposes /chat/completions, /v1/models and
    /v1/models/{model} — and no /completions at all. AIC100Provider needs
    /completions by design (Cirrascale's /chat/completions eats emitted JSON
    into empty tool_calls, so aic100.py routes around it), which makes
    aic100-against-GenieX a 410 Gone on every synthesis call.

    Observed live 2026-08-06: a host started with --npu answered queries with
    an empty findings list and a 200, while its own /debug dashboard listed the
    findings, because retrieval.query_findings() catches the failure and fails
    closed. NPUProvider is an OpenAICompatibleProvider, so it posts to
    /chat/completions and the synthesizer works.
    """
    monkeypatch.setenv("SYNAPSE_SYNTHESIZER", "npu")
    monkeypatch.setenv("SYNAPSE_BASE_URL", "http://127.0.0.1:18181/v1")

    provider = _provider()

    assert isinstance(provider, NPUProvider)
    assert provider.base_url == "http://127.0.0.1:18181/v1"


def test_npu_arm_falls_back_to_the_providers_own_defaults(monkeypatch) -> None:
    """serve_local.py passes SYNAPSE_BASE_URL, but a hand-started service
    should not need it — the default is where `geniex serve` listens anyway."""
    monkeypatch.setenv("SYNAPSE_SYNTHESIZER", "npu")
    monkeypatch.delenv("SYNAPSE_BASE_URL", raising=False)
    monkeypatch.delenv("SYNAPSE_MODEL", raising=False)

    provider = _provider()

    assert isinstance(provider, NPUProvider)
    assert provider.base_url == "http://127.0.0.1:18181/v1"


def test_unset_still_boots_on_the_fake(monkeypatch) -> None:
    """The default has to boot anywhere — a service that needs a model present
    just to start cannot be brought up on a laptop with nothing installed."""
    monkeypatch.delenv("SYNAPSE_SYNTHESIZER", raising=False)

    assert isinstance(_provider(), FakeProvider)
