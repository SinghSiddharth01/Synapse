"""`--npu --live`: the flag pair that used to be silently dropped.

Until 2026-08-06 `--live` was consulted only in the non-`--npu` branch, so
`--npu --live` started plain GenieX and said nothing about it. Whoever asked
for the live 70B got the NPU synthesizing and no way to tell from the banner —
the same silent-seam class as the empty-200 and the typo'd
SYNAPSE_SYNTHESIZER, and the reason W1 exists.

The pair's honest meaning is the real production topology on one machine:
distil on the NPU, synthesize on the actual 70B. These tests pin the two
things that can go wrong with that — refusing to start it without
credentials, and pointing the service at the wrong endpoint once it has them.

The credential resolution itself is exercised against a temporary
`secrets.jsonc`-shaped file, never the real one, and no key value is ever
asserted or printed.
"""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent


def _load():
    spec = importlib.util.spec_from_file_location(
        "serve_local", REPO / "scripts" / "serve_local.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def sl():
    return _load()


@pytest.fixture(autouse=True)
def _no_ambient_credentials(monkeypatch):
    """The author's own machine has these exported. A test that passed only
    there would be worse than no test."""
    monkeypatch.delenv("INFERENCE_CLOUD_API_KEY", raising=False)
    monkeypatch.delenv("INFERENCE_CLOUD_BASE_URL", raising=False)


def test_credentials_come_from_the_environment_first(sl, monkeypatch):
    monkeypatch.setenv("INFERENCE_CLOUD_API_KEY", "env-key")
    monkeypatch.setenv("INFERENCE_CLOUD_BASE_URL", "https://env.example/v1")

    key, base_url = sl._inference_credentials()

    assert key == "env-key"
    assert base_url == "https://env.example/v1"


def test_credentials_fall_back_to_the_secrets_block(sl, monkeypatch, tmp_path):
    """`secrets.jsonc` is where every other credential in this project lives,
    so a teammate who puts it there must not be told there is none."""
    secrets = tmp_path / "secrets.jsonc"
    secrets.write_text(json.dumps({
        "inference_cloud": {"api_key": "file-key",
                            "base_url": "https://file.example/v1"}}))
    monkeypatch.setattr(sl, "REPO", tmp_path)

    key, base_url = sl._inference_credentials()

    assert key == "file-key"
    assert base_url == "https://file.example/v1"


def test_a_commented_out_instance_is_not_a_credential(sl, monkeypatch, tmp_path):
    """Comments are stripped before parsing, so a block the team has
    deliberately commented out stays unused rather than being silently
    resurrected by this reader."""
    secrets = tmp_path / "secrets.jsonc"
    secrets.write_text(
        "{\n"
        '  // "inference_cloud": { "api_key": "old", "base_url": "https://old/v1" },\n'
        '  "anthropic": { "api_key": "sk-ant-x" }\n'
        "}\n")
    monkeypatch.setattr(sl, "REPO", tmp_path)

    assert sl._inference_credentials() == (None, None)


def test_a_malformed_secrets_file_is_no_credentials_not_a_crash(sl, monkeypatch, tmp_path):
    secrets = tmp_path / "secrets.jsonc"
    secrets.write_text("{ this is not json")
    monkeypatch.setattr(sl, "REPO", tmp_path)

    assert sl._inference_credentials() == (None, None)


def _main_argv(*extra: str) -> list[str]:
    return ["--npu", "--live", *extra]


def test_npu_live_without_credentials_exits_naming_both_alternatives(
        sl, monkeypatch, tmp_path, capsys):
    """It exits BEFORE claiming ports or spawning anything: a missing key
    discovered after three processes are up is a mess to unwind, and the
    operator who typed the wrong flags wants their terminal back."""
    monkeypatch.setattr(sl, "REPO", tmp_path)          # no secrets.jsonc here
    monkeypatch.setattr(sl, "claim_ports", lambda ports: pytest.fail(
        "exited too late — ports were claimed before the credential check"))
    monkeypatch.setattr(sl, "spawn", lambda *a, **k: pytest.fail(
        "exited too late — a process was spawned before the credential check"))

    with pytest.raises(SystemExit) as exit_info:
        sl.main(_main_argv())

    message = str(exit_info.value)
    assert "INFERENCE_CLOUD_API_KEY" in message
    assert "INFERENCE_CLOUD_BASE_URL" in message
    assert "secrets.jsonc" in message
    # Both working alternatives, because "this does not work" without "here is
    # what does" is how someone ends up not running the demo at all.
    assert "--npu    alone" in message
    assert "--live   alone" in message


def test_npu_live_with_only_a_key_says_which_half_is_missing(sl, monkeypatch, tmp_path):
    """Naming the ONE thing that is absent, not both — an error that lists
    everything it might need makes the reader re-check what they already set."""
    monkeypatch.setattr(sl, "REPO", tmp_path)
    monkeypatch.setenv("INFERENCE_CLOUD_API_KEY", "k")

    with pytest.raises(SystemExit) as exit_info:
        sl.main(_main_argv())

    message = str(exit_info.value)
    assert "INFERENCE_CLOUD_BASE_URL" in message
    assert "INFERENCE_CLOUD_API_KEY" not in message


def test_npu_live_points_the_service_at_the_cloud_and_the_distiller_at_the_npu(
        sl, monkeypatch, tmp_path, capsys):
    """The whole point of the mode, asserted on the env each child is given.

    The SERVICE must speak aic100 to Cirrascale — with the derived 1600/180
    operating point ADR-0005 mandates, not AIC100Provider's own 800/60
    defaults — while the ORCHESTRATOR's distiller stays on the local GenieX at
    :18181. Getting this backwards is invisible at runtime: both
    configurations boot, and the only symptom is which machine is doing which
    half of the work."""
    monkeypatch.setattr(sl, "REPO", tmp_path)
    monkeypatch.setenv("INFERENCE_CLOUD_API_KEY", "secret-key")
    monkeypatch.setenv("INFERENCE_CLOUD_BASE_URL", "https://cloud.example/v1")

    spawned: dict[str, dict] = {}

    class _Fake:
        pid = 1
        returncode = None

        def poll(self):
            return None

    def fake_spawn(name, argv, env=None):
        spawned[name] = dict(env or {})
        return _Fake()

    monkeypatch.setattr(sl, "claim_ports", lambda ports: None)
    monkeypatch.setattr(sl, "spawn", fake_spawn)
    monkeypatch.setattr(sl, "start_or_adopt_geniex", lambda url: _Fake())
    monkeypatch.setattr(sl, "wait_for", lambda *a, **k: True)
    monkeypatch.setattr(sl, "http", lambda method, url, body=None, timeout=30.0: {
        "shared_id": "sh-test", "created_by": "me", "purpose": "p"})
    monkeypatch.setattr(sl, "STATE", tmp_path / ".synapse")
    # The supervision loop would otherwise never return.
    monkeypatch.setattr(sl.time, "sleep",
                        lambda seconds: (_ for _ in ()).throw(KeyboardInterrupt()))

    assert sl.main(_main_argv("--contributor", "tester")) == 0

    service_env = spawned["service"]
    assert service_env["SYNAPSE_SYNTHESIZER"] == "aic100"
    assert service_env["INFERENCE_CLOUD_BASE_URL"] == "https://cloud.example/v1"
    assert service_env["INFERENCE_CLOUD_MAX_TOKENS"] == "1600"
    assert service_env["INFERENCE_CLOUD_TIMEOUT"] == "180"
    # Present, and asserted only for presence — this test never reads a key's
    # value into an assertion message.
    assert "INFERENCE_CLOUD_API_KEY" in service_env

    orchestrator_env = spawned["orchestrator"]
    assert orchestrator_env["SYNAPSE_BASE_URL"] == sl.NPU_URL

    # ...and the banner tells the operator which half is where, because a
    # DUAL run that looks identical to a plain --npu run on screen is exactly
    # the confusion this mode used to cause by dropping the flag.
    printed = capsys.readouterr().out
    assert "DUAL" in printed
    assert "cloud.example" in printed
    assert "secret-key" not in printed          # never, anywhere


def test_npu_alone_still_synthesizes_on_the_npu(sl, monkeypatch, tmp_path):
    """The negative control: --npu without --live is untouched by any of this
    and must keep pointing the service at the local seam."""
    monkeypatch.setattr(sl, "REPO", tmp_path)
    spawned: dict[str, dict] = {}

    class _Fake:
        pid = 1
        returncode = None

        def poll(self):
            return None

    def fake_spawn(name, argv, env=None):
        spawned[name] = dict(env or {})
        return _Fake()

    monkeypatch.setattr(sl, "claim_ports", lambda ports: None)
    monkeypatch.setattr(sl, "spawn", fake_spawn)
    monkeypatch.setattr(sl, "start_or_adopt_geniex", lambda url: _Fake())
    monkeypatch.setattr(sl, "wait_for", lambda *a, **k: True)
    monkeypatch.setattr(sl, "http", lambda method, url, body=None, timeout=30.0: {
        "shared_id": "sh-test", "created_by": "me", "purpose": "p"})
    monkeypatch.setattr(sl, "STATE", tmp_path / ".synapse")
    monkeypatch.setattr(sl.time, "sleep",
                        lambda seconds: (_ for _ in ()).throw(KeyboardInterrupt()))

    assert sl.main(["--npu", "--contributor", "tester"]) == 0

    assert spawned["service"]["SYNAPSE_SYNTHESIZER"] == "npu"
    assert spawned["service"]["SYNAPSE_BASE_URL"] == sl.NPU_URL
