"""Which synthesizer the service builds, why the NPU needs its own arm, and
what each way of misconfiguring it now says out loud at boot.

⟨2026-08-06, W1⟩ The two NPU tests below monkeypatch `_npu_answers`. That is
not a convenience: `_provider()` now probes the seam at startup, and a test
that let the real probe run would (a) depend on whether a `geniex serve`
happens to be running on this machine, and (b) reach out to :18181, a port
this repo's live-verification rules reserve for the real stack. The design
note for W1 predicted these tests would be unaffected because they "use
build_app directly, never cli.main" — that is true of the API suites and
false here, since this file calls `_provider()` itself. Recorded rather than
quietly patched over.
"""

from __future__ import annotations

import pytest
from synapse_providers import AIC100Provider, FakeProvider, NPUProvider

from synapse_service import cli
from synapse_service.cli import _provider


@pytest.fixture
def seam_answers(monkeypatch):
    """Pretend a GenieX is listening. Returns a list of the probed base urls
    so a test can assert the probe targeted the right one."""
    probed: list[str] = []

    def fake_probe(base_url: str) -> bool:
        probed.append(base_url)
        return True

    monkeypatch.setattr(cli, "_npu_answers", fake_probe)
    return probed


def test_npu_arm_speaks_the_only_endpoint_geniex_actually_serves(
        monkeypatch, seam_answers) -> None:
    """`geniex serve` exposes /chat/completions, /v1/models and
    /v1/models/{model} — and no /completions at all. AIC100Provider needs
    /completions by design (Cirrascale's /chat/completions eats emitted JSON
    into empty tool_calls, so aic100.py routes around it), which makes
    aic100-against-GenieX a 410 Gone on every synthesis call.

    Observed live 2026-08-06: a host started with --npu answered queries with
    an empty findings list and a 200, while its own /debug dashboard listed the
    findings — because retrieval.query_findings() caught the failure and
    returned `[]`, which is indistinguishable from "nothing matched".
    NPUProvider is an OpenAICompatibleProvider, so it posts to
    /chat/completions and the synthesizer works.

    ⟨AMENDED, decision 008⟩ The masking half of that story is gone: the same
    410 now raises `RetrievalUnavailable` and the route answers 503
    `retrieval_unavailable`, so a wrongly-wired synthesizer announces itself
    on the first query instead of looking like an empty memory. This test is
    still worth having — it pins that the arm is wired to the endpoint GenieX
    actually serves, which is what stops the 503 from happening at all.
    """
    monkeypatch.setenv("SYNAPSE_SYNTHESIZER", "npu")
    monkeypatch.setenv("SYNAPSE_BASE_URL", "http://127.0.0.1:18181/v1")

    provider = _provider()

    assert isinstance(provider, NPUProvider)
    assert provider.base_url == "http://127.0.0.1:18181/v1"
    assert seam_answers == ["http://127.0.0.1:18181/v1"]


def test_npu_arm_falls_back_to_the_providers_own_defaults(
        monkeypatch, seam_answers) -> None:
    """serve_local.py passes SYNAPSE_BASE_URL, but a hand-started service
    should not need it — the default is where `geniex serve` listens anyway."""
    monkeypatch.setenv("SYNAPSE_SYNTHESIZER", "npu")
    monkeypatch.delenv("SYNAPSE_BASE_URL", raising=False)
    monkeypatch.delenv("SYNAPSE_MODEL", raising=False)

    provider = _provider()

    assert isinstance(provider, NPUProvider)
    assert provider.base_url == "http://127.0.0.1:18181/v1"


def test_unset_still_boots_on_the_fake(monkeypatch, capsys) -> None:
    """The default has to boot anywhere — a service that needs a model present
    just to start cannot be brought up on a laptop with nothing installed.

    ⟨W1⟩ Still boots. What changed is that it no longer does it silently: the
    banner is the boot-side half of the same honesty fix decision 008 made at
    runtime, and it names the consequence (a 503, not an empty answer) so
    whoever sees it knows what their agents will experience."""
    monkeypatch.delenv("SYNAPSE_SYNTHESIZER", raising=False)

    assert isinstance(_provider(), FakeProvider)

    warned = capsys.readouterr().err
    assert "SYNAPSE_SYNTHESIZER is not set" in warned
    assert "503" in warned
    assert "aic100|npu|anthropic" in warned


def test_fake_asked_for_explicitly_is_still_announced(monkeypatch, capsys) -> None:
    """`fake` on purpose is a legitimate choice and boots; it is warned about
    anyway, because the thing that hurts is not the choice, it is nobody in
    the room knowing which brain is running. The headline distinguishes it
    from unset so the banner never tells an operator they forgot something
    they did deliberately."""
    monkeypatch.setenv("SYNAPSE_SYNTHESIZER", "fake")

    assert isinstance(_provider(), FakeProvider)

    warned = capsys.readouterr().err
    assert "SYNAPSE_SYNTHESIZER=fake" in warned
    assert "is not set" not in warned


def test_a_typo_in_the_mode_is_an_exit_not_a_silent_fake(monkeypatch) -> None:
    """`SYNAPSE_SYNTHESIZER=aic-100` used to fall through to the FakeProvider:
    the service booted clean, printed "synthesizer: aic-100" on its own
    startup line, and knew nothing. Same silent-seam class as the empty-200 —
    an operator with a wrong mental model and no contradicting signal."""
    monkeypatch.setenv("SYNAPSE_SYNTHESIZER", "aic-100")

    with pytest.raises(SystemExit) as exit_info:
        _provider()

    message = str(exit_info.value)
    assert "aic-100" in message                      # names what they typed
    for mode in ("fake", "aic100", "npu", "anthropic"):
        assert mode in message                       # ...and every valid value


def test_aic100_without_a_key_exits_at_boot_rather_than_401ing_per_call(
        monkeypatch) -> None:
    """Without a key the provider builds fine, boots fine, and 401s on every
    synthesis AND every retrieval ranking — so the failure surfaces as a
    service that is up and knows nothing, hours after whoever started it
    walked away. Both env vars are named because the rotation pool
    (INFERENCE_CLOUD_API_KEYS) is the one people forget is separate."""
    monkeypatch.setenv("SYNAPSE_SYNTHESIZER", "aic100")
    monkeypatch.delenv("INFERENCE_CLOUD_API_KEY", raising=False)
    monkeypatch.delenv("INFERENCE_CLOUD_API_KEYS", raising=False)

    with pytest.raises(SystemExit) as exit_info:
        _provider()

    message = str(exit_info.value)
    assert "INFERENCE_CLOUD_API_KEY" in message
    assert "INFERENCE_CLOUD_API_KEYS" in message
    assert "secrets.jsonc" in message


def test_aic100_with_only_the_rotation_pool_set_still_boots(monkeypatch) -> None:
    """The pool alone is a complete credential — AIC100Provider reads
    INFERENCE_CLOUD_API_KEYS first. A preflight that demanded the singular
    var would break the multi-key setup the ~20-req/hour budget exists for."""
    monkeypatch.setenv("SYNAPSE_SYNTHESIZER", "aic100")
    monkeypatch.delenv("INFERENCE_CLOUD_API_KEY", raising=False)
    monkeypatch.setenv("INFERENCE_CLOUD_API_KEYS", "k1,k2")

    assert isinstance(_provider(), AIC100Provider)


def test_npu_with_nothing_listening_exits_and_names_the_fix(monkeypatch) -> None:
    """The hand-started service, pointed at a GenieX that is not there or has
    already gone idle-dead. serve_local verifies the seam before it spawns
    this process, so this exists for the other path — and it must name
    serve_local, because that is now the command that launches and supervises
    the seam rather than merely requiring one."""
    monkeypatch.setenv("SYNAPSE_SYNTHESIZER", "npu")
    monkeypatch.setenv("SYNAPSE_BASE_URL", "http://127.0.0.1:19181/v1")
    monkeypatch.setattr(cli, "_npu_answers", lambda base_url: False)

    with pytest.raises(SystemExit) as exit_info:
        _provider()

    message = str(exit_info.value)
    assert "http://127.0.0.1:19181/v1/models" in message
    assert "geniex serve" in message
    assert "serve_local.py --npu" in message
