"""synapse-server: keys-file parsing, key verdicts, env assembly, and the
refuse-to-boot-on-dead-pool gate."""

from __future__ import annotations

import pytest

from synapse_service import server_cli


@pytest.fixture(autouse=True)
def _home(tmp_path, monkeypatch):
    monkeypatch.setenv("SYNAPSE_HOME", str(tmp_path / "home"))
    # The assembly must never inherit a developer's real keys from the
    # ambient environment during tests.
    for var in ("INFERENCE_CLOUD_API_KEY", "INFERENCE_CLOUD_API_KEYS",
                "INFERENCE_CLOUD_BASE_URL", "INFERENCE_CLOUD_MAX_TOKENS",
                "INFERENCE_CLOUD_TIMEOUT"):
        monkeypatch.delenv(var, raising=False)
    yield


def _write_keys(tmp_path, text):
    path = tmp_path / "keys.txt"
    path.write_text(text, encoding="utf-8")
    return path


class TestReadKeysFile:
    def test_one_per_line_with_comments_and_blanks(self, tmp_path):
        path = _write_keys(tmp_path, "# team pool\nkey-one\n\nkey-two\n")
        assert server_cli.read_keys_file(path) == ["key-one", "key-two"]

    def test_order_preserved(self, tmp_path):
        path = _write_keys(tmp_path, "b\na\nc\n")
        assert server_cli.read_keys_file(path) == ["b", "a", "c"]

    def test_missing_file_is_a_loud_exit(self, tmp_path):
        with pytest.raises(SystemExit, match="keys file not found"):
            server_cli.read_keys_file(tmp_path / "nope.txt")

    def test_empty_file_is_a_loud_exit(self, tmp_path):
        path = _write_keys(tmp_path, "# only a comment\n\n")
        with pytest.raises(SystemExit, match="holds no keys"):
            server_cli.read_keys_file(path)

    def test_comma_in_a_line_is_rejected(self, tmp_path):
        # Keys are joined with commas for INFERENCE_CLOUD_API_KEYS; a comma
        # inside one would silently split it into two broken keys.
        path = _write_keys(tmp_path, "key-one,key-two\n")
        with pytest.raises(SystemExit, match="one key per line"):
            server_cli.read_keys_file(path)


class TestAssembleEnv:
    def test_aic100_without_keys_refuses(self):
        cfg = {"server.synthesizer": "aic100"}
        with pytest.raises(SystemExit, match="needs keys"):
            server_cli._assemble_env(cfg, skip_key_check=True, force=False)

    def test_aic100_joins_keys_and_sets_operating_point(self, tmp_path):
        path = _write_keys(tmp_path, "k1\nk2\n")
        cfg = {"server.synthesizer": "aic100",
               "server.keys_file": str(path),
               "server.base_url": "http://cloud:1/v1",
               "server.model": "Llama-3.3-70B"}
        env = server_cli._assemble_env(cfg, skip_key_check=True, force=False)
        assert env["INFERENCE_CLOUD_API_KEYS"] == "k1,k2"
        assert env["INFERENCE_CLOUD_BASE_URL"] == "http://cloud:1/v1"
        assert env["INFERENCE_CLOUD_MODEL"] == "Llama-3.3-70B"
        # The ADR-0005 operating point serve_local always injected — a
        # hand-started service used to lose it and truncate every synthesis.
        assert env["INFERENCE_CLOUD_MAX_TOKENS"] == "1600"
        assert env["INFERENCE_CLOUD_TIMEOUT"] == "180"

    def test_ambient_operating_point_wins(self, tmp_path, monkeypatch):
        monkeypatch.setenv("INFERENCE_CLOUD_MAX_TOKENS", "3000")
        path = _write_keys(tmp_path, "k1\n")
        cfg = {"server.synthesizer": "aic100", "server.keys_file": str(path)}
        env = server_cli._assemble_env(cfg, skip_key_check=True, force=False)
        assert env["INFERENCE_CLOUD_MAX_TOKENS"] == "3000"

    def test_fake_is_inferred_without_keys(self):
        assert server_cli._resolve_synthesizer({}) == "fake"

    def test_aic100_is_inferred_with_keys_file(self):
        cfg = {"server.keys_file": "/somewhere/keys.txt"}
        assert server_cli._resolve_synthesizer(cfg) == "aic100"

    def test_dead_pool_refuses_and_force_overrides(self, tmp_path, monkeypatch):
        path = _write_keys(tmp_path, "k1\n")
        cfg = {"server.synthesizer": "aic100", "server.keys_file": str(path),
               "server.base_url": "http://cloud:1/v1"}
        monkeypatch.setattr(server_cli, "preflight_keys",
                            lambda base, keys: False)
        with pytest.raises(SystemExit, match="zero usable keys"):
            server_cli._assemble_env(cfg, skip_key_check=False, force=False)
        env = server_cli._assemble_env(cfg, skip_key_check=False, force=True)
        assert env["INFERENCE_CLOUD_API_KEYS"] == "k1"


class TestCheckKey:
    def test_unauthorized_and_rate_limited_verdicts(self, monkeypatch):
        import urllib.error

        def raise_http(code):
            def _open(request, timeout):
                raise urllib.error.HTTPError(request.full_url, code, "", {}, None)
            return _open

        monkeypatch.setattr(server_cli.urllib.request, "urlopen", raise_http(401))
        verdict, _ = server_cli.check_key("http://x/v1", "k")
        assert verdict == "unauthorized"

        monkeypatch.setattr(server_cli.urllib.request, "urlopen", raise_http(429))
        verdict, detail = server_cli.check_key("http://x/v1", "k")
        assert verdict == "rate-limited"
        assert "valid" in detail

    def test_connection_refused_is_unreachable(self):
        verdict, _ = server_cli.check_key("http://127.0.0.1:9/v1", "k",
                                          timeout=0.5)
        assert verdict == "unreachable"


def test_mask_never_shows_a_whole_key():
    assert server_cli._mask("sk-test-aaaa1111bbbb") == "sk-t…bbbb"
    assert "short" not in server_cli._mask("short")
