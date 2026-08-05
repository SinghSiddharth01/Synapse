"""synapse-orchestrator CLI — argument parsing and wiring, without a real server.

`uvicorn.run()` opens a real socket and blocks forever serving requests, so
every serve-path test replaces it with a stub that just records how it was
called. `main`/`cmd_resync` also accept a test-only `transport` kwarg so the
CLI's own httpx calls (Relay, build_briefing, register_tools) never open a
real socket to the default --service-url either — a test that relied on
nothing listening on 127.0.0.1:8899 would silently change behaviour the day
something local claims that port. That is what is worth mocking here:
everything else (argument parsing, Relay/binding wiring, exit code) is real.
"""

from __future__ import annotations

from datetime import datetime, timezone

import httpx

import synapse_orchestrator.cli as cli
from synapse_contracts.binding import SessionBinding, write_binding


def _never_called(request: httpx.Request) -> httpx.Response:
    raise AssertionError(f"no real network call expected, got {request.url}")


def test_defaults_to_localhost_8787(monkeypatch, tmp_path) -> None:
    calls = []
    monkeypatch.setattr(cli.uvicorn, "run", lambda app, **kw: calls.append(kw))

    exit_code = cli.main(["--state-dir", str(tmp_path)])

    assert exit_code == 0
    assert calls == [{"host": "127.0.0.1", "port": 8787}]


def test_host_and_port_are_applied_to_the_server(monkeypatch, tmp_path) -> None:
    calls = []
    monkeypatch.setattr(cli.uvicorn, "run", lambda app, **kw: calls.append(kw))

    cli.main(["--host", "0.0.0.0", "--port", "9999", "--state-dir", str(tmp_path)])

    assert calls == [{"host": "0.0.0.0", "port": 9999}]


def test_transport_is_http_via_uvicorn_never_stdio(monkeypatch, tmp_path) -> None:
    """ADR 0001: stdio spawns one server process per client, which would give
    one Orchestrator per Agent and dissolve the single-egress property. This
    CLI has no stdio code path at all — the app is always served over HTTP by
    uvicorn."""
    calls = []
    monkeypatch.setattr(cli.uvicorn, "run", lambda app, **kw: calls.append(kw))

    cli.main(["--state-dir", str(tmp_path)])

    assert len(calls) == 1
    assert "host" in calls[0] and "port" in calls[0]  # an HTTP bind, not a transport kwarg


def test_verbose_flag_is_accepted(monkeypatch, tmp_path) -> None:
    monkeypatch.setattr(cli.uvicorn, "run", lambda app, **kw: None)

    exit_code = cli.main(["-v", "--state-dir", str(tmp_path)])

    assert exit_code == 0


def test_unknown_flag_exits_nonzero(monkeypatch, tmp_path) -> None:
    monkeypatch.setattr(cli.uvicorn, "run", lambda app, **kw: None)

    try:
        cli.main(["--not-a-real-flag"])
        raised = False
    except SystemExit as exc:
        raised = True
        assert exc.code != 0
    assert raised, "argparse should reject an unknown flag"


def test_serve_falls_back_to_unbound_session_with_no_binding(monkeypatch, tmp_path, capsys) -> None:
    monkeypatch.setattr(cli.uvicorn, "run", lambda app, **kw: None)

    cli.main(["--state-dir", str(tmp_path)])

    assert "session: unbound" in capsys.readouterr().out


def test_serve_uses_the_joined_session_when_a_binding_exists(monkeypatch, tmp_path, capsys) -> None:
    write_binding(
        tmp_path / "bindings" / "claude-code.json",
        SessionBinding(
            agent_session_id="as-1",
            shared_id="sh-joined",
            contributor="aditya",
            agent="claude-code",
            transcript_path="/tmp/t.jsonl",
            pinned_at=datetime(2026, 8, 4, tzinfo=timezone.utc),
        ),
    )
    monkeypatch.setattr(cli.uvicorn, "run", lambda app, **kw: None)

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/v1/sessions/sh-joined/watermark"
        return httpx.Response(200, json={"version": 1, "new_since": 0,
                                         "by_type": {}, "conflicts": 0})

    cli.main(["--state-dir", str(tmp_path)], transport=httpx.MockTransport(handler))

    assert "session: sh-joined" in capsys.readouterr().out


def test_serve_falls_back_to_unbound_when_the_bindings_dir_has_no_readable_binding(
    monkeypatch, tmp_path, capsys
) -> None:
    """A bindings/ dir that exists but holds nothing readable (e.g. every file
    corrupt) must fall back to unbound, same as no dir at all."""
    bindings_dir = tmp_path / "bindings"
    bindings_dir.mkdir(parents=True)
    (bindings_dir / "claude-code.json").write_text("not valid json")
    monkeypatch.setattr(cli.uvicorn, "run", lambda app, **kw: None)

    cli.main(["--state-dir", str(tmp_path)])

    assert "session: unbound" in capsys.readouterr().out


def test_serve_discovers_a_codex_only_binding(monkeypatch, tmp_path, capsys) -> None:
    """cli.py used to hardcode bindings/claude-code.json — a Codex-only join
    (Plan D.2: one binding file per Agent product) was invisible: no tools, no
    briefing, egress to the fabricated 'unbound' session."""
    write_binding(
        tmp_path / "bindings" / "codex.json",
        SessionBinding(
            agent_session_id="as-codex-1",
            shared_id="sh-codex",
            contributor="akhil",
            agent="codex",
            transcript_path="/tmp/codex.jsonl",
            pinned_at=datetime(2026, 8, 4, tzinfo=timezone.utc),
        ),
    )
    monkeypatch.setattr(cli.uvicorn, "run", lambda app, **kw: None)

    cli.main(["--state-dir", str(tmp_path)],
            transport=httpx.MockTransport(lambda r: httpx.Response(200, json={})))

    assert "session: sh-codex" in capsys.readouterr().out


def test_serve_picks_the_most_recently_joined_binding_when_several_exist(
    monkeypatch, tmp_path, capsys
) -> None:
    write_binding(
        tmp_path / "bindings" / "claude-code.json",
        SessionBinding(agent_session_id="as-1", shared_id="sh-older", contributor="aditya",
                       agent="claude-code", transcript_path="/tmp/t.jsonl",
                       pinned_at=datetime(2026, 8, 1, tzinfo=timezone.utc)),
    )
    write_binding(
        tmp_path / "bindings" / "codex.json",
        SessionBinding(agent_session_id="as-2", shared_id="sh-newer", contributor="akhil",
                       agent="codex", transcript_path="/tmp/c.jsonl",
                       pinned_at=datetime(2026, 8, 4, tzinfo=timezone.utc)),
    )
    monkeypatch.setattr(cli.uvicorn, "run", lambda app, **kw: None)

    cli.main(["--state-dir", str(tmp_path)],
            transport=httpx.MockTransport(lambda r: httpx.Response(200, json={})))

    assert "session: sh-newer" in capsys.readouterr().out


def test_resync_with_nothing_pending_never_touches_the_network(tmp_path, capsys) -> None:
    """No findings were ever recorded, so `_all_findings()` is empty and Relay's
    own short-circuit means `resync` makes zero HTTP calls — the CLI test stays
    fully offline without needing to mock a transport."""
    exit_code = cli.main(["--state-dir", str(tmp_path), "resync"])

    assert exit_code == 0
    out = capsys.readouterr().out
    assert "re-pushed 0 finding(s)" in out
    assert "unbound" in out


def test_resync_reports_the_joined_session(tmp_path, capsys) -> None:
    write_binding(
        tmp_path / "bindings" / "claude-code.json",
        SessionBinding(
            agent_session_id="as-1",
            shared_id="sh-joined",
            contributor="aditya",
            agent="claude-code",
            transcript_path="/tmp/t.jsonl",
            pinned_at=datetime(2026, 8, 4, tzinfo=timezone.utc),
        ),
    )

    exit_code = cli.main(["--state-dir", str(tmp_path), "resync"])

    assert exit_code == 0
    assert "sh-joined" in capsys.readouterr().out


def test_resync_accepts_state_dir_after_the_subcommand(tmp_path, capsys) -> None:
    """`resync --state-dir X` used to fail with 'unrecognized arguments' —
    only the options-before-subcommand order worked, because --state-dir was
    declared on the top-level parser only. Both orders must work."""
    exit_code = cli.main(["resync", "--state-dir", str(tmp_path)])

    assert exit_code == 0
    out = capsys.readouterr().out
    assert "re-pushed 0 finding(s)" in out


def test_resync_still_honours_state_dir_given_before_the_subcommand(tmp_path, capsys) -> None:
    """The fix for the above must not break the form that already worked."""
    write_binding(
        tmp_path / "bindings" / "claude-code.json",
        SessionBinding(agent_session_id="as-1", shared_id="sh-joined", contributor="aditya",
                       agent="claude-code", transcript_path="/tmp/t.jsonl",
                       pinned_at=datetime(2026, 8, 4, tzinfo=timezone.utc)),
    )

    exit_code = cli.main(["--state-dir", str(tmp_path), "resync"])

    assert exit_code == 0
    assert "sh-joined" in capsys.readouterr().out


def test_resync_fails_loudly_when_the_push_does_not_succeed(tmp_path, capsys) -> None:
    """A pending finding that fails to push must NOT print the same
    're-pushed 0 finding(s)' shape as 'nothing was pending' — that was
    indistinguishable from success, with exit code 0 either way."""
    write_binding(
        tmp_path / "bindings" / "claude-code.json",
        SessionBinding(agent_session_id="as-1", shared_id="sh-joined", contributor="aditya",
                       agent="claude-code", transcript_path="/tmp/t.jsonl",
                       pinned_at=datetime(2026, 8, 4, tzinfo=timezone.utc)),
    )
    # Seed the relay's durable log directly, as if a finding had been recorded
    # by an earlier producer POST and never acknowledged.
    from synapse_contracts import Attribution, Finding
    relay_dir = tmp_path / "relay"
    relay_dir.mkdir(parents=True)
    finding = Finding(id="f-stuck", type="learning", text="insight",
                      attributions=[Attribution(contributor="aditya", agent_session="as-1",
                                                agent="claude-code")],
                      ts=datetime(2026, 8, 4, tzinfo=timezone.utc))
    (relay_dir / "findings.jsonl").write_text(finding.model_dump_json() + "\n")

    def down(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("down")
    exit_code = cli.main(["--state-dir", str(tmp_path), "resync"],
                         transport=httpx.MockTransport(down))

    assert exit_code == 1
    out = capsys.readouterr().out
    assert "FAILED" in out
    assert "re-pushed 0 finding(s)" not in out


def test_resync_with_nothing_bound_never_egresses_to_a_fabricated_session(tmp_path, capsys) -> None:
    """No binding at all + something pending: resync must not invent a session
    id ('unbound') to post real Findings to."""
    from synapse_contracts import Attribution, Finding
    relay_dir = tmp_path / "relay"
    relay_dir.mkdir(parents=True)
    finding = Finding(id="f-stuck", type="learning", text="insight",
                      attributions=[Attribution(contributor="aditya", agent_session="as-1",
                                                agent="claude-code")],
                      ts=datetime(2026, 8, 4, tzinfo=timezone.utc))
    (relay_dir / "findings.jsonl").write_text(finding.model_dump_json() + "\n")

    exit_code = cli.main(["--state-dir", str(tmp_path), "resync"],
                         transport=httpx.MockTransport(_never_called))

    assert exit_code == 1
    assert "unbound" in capsys.readouterr().out


def test_build_npu_distiller_matches_the_workers_config_pack_and_model(monkeypatch) -> None:
    """The plan's 'one distiller' property: contribute()'s round trip must use
    the identical NPU model, base config and prompt pack as the passive
    (synapse-worker run) path. Previously a closure inside main() that no test
    ever called — a signature drift in NPUProvider or load_pack_by_name would
    only surface at runtime on the NPU box."""
    import synapse_distiller
    import synapse_providers
    from synapse_contracts import LocalBinding

    class FakeProviderConfig:
        base_url = "http://fake-npu/v1"
        max_tokens = 111
        temperature = 0.5
        timeout_s = 7.0

    class FakeConfig:
        model = "fake-model"
        prompt_pack_name = "fake-pack"
        distil_kinds = ("learning", "decision")
        render_style = "labelled"
        provider = FakeProviderConfig()

    fake_pack = object()
    captured_pack_name = []
    captured_provider_kwargs = {}

    def fake_load_pack_by_name(name):
        captured_pack_name.append(name)
        return fake_pack

    class FakeNPUProvider:
        def __init__(self, **kwargs):
            captured_provider_kwargs.update(kwargs)

    monkeypatch.setattr(synapse_distiller, "load_config", lambda: FakeConfig())
    monkeypatch.setattr(synapse_distiller, "load_pack_by_name", fake_load_pack_by_name)
    monkeypatch.setattr(synapse_providers, "NPUProvider", FakeNPUProvider)

    binding = LocalBinding(agent_session_id="as-1", shared_id="sh-1",
                           contributor="aditya", agent="claude-code")
    distiller = cli.build_npu_distiller(binding)

    assert distiller.binding is binding
    assert distiller.pack is fake_pack
    assert captured_pack_name == ["fake-pack"]
    assert distiller.kinds == ("learning", "decision")
    assert distiller.render_style == "labelled"
    assert isinstance(distiller.provider, FakeNPUProvider)
    assert captured_provider_kwargs == {
        "base_url": "http://fake-npu/v1", "model": "fake-model",
        "max_tokens": 111, "temperature": 0.5, "timeout": 7.0,
    }
