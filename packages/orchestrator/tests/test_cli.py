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
from starlette.testclient import TestClient

import synapse_orchestrator.cli as cli
from synapse_contracts.binding import SessionBinding, write_binding

_PRODUCER_FINDING = {"id": "f-wiring", "type": "learning", "text": "x",
                     "attributions": [{"contributor": "aditya", "agent_session": "as-1",
                                       "agent": "claude-code"}],
                     "ts": "2026-08-04T12:00:00Z", "refs": [], "provenance": "distilled",
                     "status": "kept", "merged_from": [], "merged_into": None}


def test_main_wires_resolve_binding_into_the_app_so_a_post_boot_join_takes_effect(
    monkeypatch, tmp_path
) -> None:
    """Kills the mutant `build_app(relay, server)` (dropping the keyword
    `resolve_binding=resolve_binding`) — round 2 review's blocker finding:
    'Reverting cli.main's resolve_binding wiring ... leaves all 229 tests
    passing.' Without it wired live into the app, the producer endpoint
    takes the legacy branch (app.py: `if resolve_binding is not None`) —
    no 503 gate at all, and a `synapse-worker join` run after this process
    started would never be picked up on the egress path, contradicting both
    app.py's own docstring and cli.py's `_resolve_binding` docstring. This
    test drives the REAL `app` object handed to `uvicorn.run`, not a mock of
    `build_app`, so it exercises the actual composition in `main()`."""
    captured: dict = {}
    monkeypatch.setattr(cli.uvicorn, "run", lambda app, **kw: captured.setdefault("app", app))

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"accepted": 1, "memory_version": 1})

    # Boot with NO binding at all.
    cli.main(["--state-dir", str(tmp_path)], transport=httpx.MockTransport(handler))
    # One TestClient context for both requests: the MCP session manager
    # underneath `app` can only run its ASGI lifespan once per instance, so
    # both the before-join and after-join requests must share it — that is
    # exactly the "no restart" property this test is pinning.
    with TestClient(captured["app"]) as client:
        resp = client.post("/producer/findings", json={"findings": [_PRODUCER_FINDING]})
        assert resp.status_code == 503    # only true if resolve_binding is live-wired

        # A `synapse-worker join` after boot — write the binding file now,
        # with the SAME `app` object still in hand (no restart).
        write_binding(
            tmp_path / "bindings" / "claude-code.json",
            SessionBinding(agent_session_id="as-1", shared_id="sh-late", contributor="aditya",
                           agent="claude-code", transcript_path="/tmp/t.jsonl",
                           pinned_at=datetime(2026, 8, 4, tzinfo=timezone.utc)),
        )
        resp = client.post("/producer/findings", json={"findings": [_PRODUCER_FINDING]})
        assert resp.status_code == 200    # picked up live, without a restart


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


def test_serve_wires_register_tools_unconditionally_with_a_live_resolver(
    monkeypatch, tmp_path
) -> None:
    """Pins Task 4's whole CLI composition as an observable effect, not just a
    printed banner line. Two independent mutations previously survived every
    test in the suite: cli.main never calling register_tools at all, and
    cli.main handing create_mcp the wrong (or no) briefing — both regress
    silently unless something asserts on what main() actually wires together,
    not just what it prints.

    Round 3 review, finding #11: `register_tools` used to be called only
    `if binding is not None` — an orchestrator started before any join
    served a permanently tool-less MCP server, contradicting the very
    instructions it handed the agent ("run `synapse-worker join` ... to
    connect one"). It is now called EVERY time, unbound or not, with a
    `resolve_binding` callable rather than a frozen `binding` value — this
    test also proves that callable is LIVE (re-reads disk), not a closure
    over the binding resolved at the moment `main()` ran."""
    monkeypatch.setattr(cli.uvicorn, "run", lambda app, **kw: None)

    create_mcp_calls: list[str | None] = []
    register_tools_calls: list[dict] = []
    real_create_mcp = cli.create_mcp
    monkeypatch.setattr(cli, "create_mcp",
                        lambda instructions=None: (create_mcp_calls.append(instructions)
                                                   or real_create_mcp(instructions)))
    monkeypatch.setattr(cli, "register_tools",
                        lambda server, **kw: register_tools_calls.append(kw))

    # Unbound: register_tools IS still called (unconditionally), and the
    # briefing handed to create_mcp is the plain default — not the
    # joined-session template.
    cli.main(["--state-dir", str(tmp_path)])
    assert len(register_tools_calls) == 1
    assert register_tools_calls[0]["resolve_binding"]() is None
    assert "No session is bound yet" in create_mcp_calls[-1]

    # Bound: register_tools is called again (once per main() run), with a
    # resolve_binding that reflects the resolved binding — and the briefing
    # handed to create_mcp is the live, binding-specific one, not a
    # generic/default string a mutated call could smuggle through instead.
    write_binding(
        tmp_path / "bindings" / "claude-code.json",
        SessionBinding(agent_session_id="as-1", shared_id="sh-wired", contributor="aditya",
                       agent="claude-code", transcript_path="/tmp/t.jsonl",
                       pinned_at=datetime(2026, 8, 4, tzinfo=timezone.utc)),
    )
    handler = lambda r: httpx.Response(200, json={"version": 2, "new_since": 1,
                                                  "by_type": {"learning": 1}, "conflicts": 0})
    cli.main(["--state-dir", str(tmp_path)], transport=httpx.MockTransport(handler))

    assert len(register_tools_calls) == 2
    assert register_tools_calls[1]["resolve_binding"]().shared_id == "sh-wired"
    assert "sh-wired" in create_mcp_calls[-1]
    assert create_mcp_calls[-1] != create_mcp_calls[0]      # not the same default string

    # LIVE, not a closure over a value already resolved when main() ran:
    # a join written to disk AFTER this main() call still shows up through
    # the SAME captured resolve_binding — exactly what lets query/contribute
    # (server.py) observe a join that happens after registration, no restart.
    write_binding(
        tmp_path / "bindings" / "claude-code.json",
        SessionBinding(agent_session_id="as-1", shared_id="sh-even-later", contributor="aditya",
                       agent="claude-code", transcript_path="/tmp/t.jsonl",
                       pinned_at=datetime(2026, 8, 5, tzinfo=timezone.utc)),
    )
    assert register_tools_calls[1]["resolve_binding"]().shared_id == "sh-even-later"


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
    # by an earlier producer POST and never acknowledged. Envelope format per
    # relay.py's post-review partition amendment: {"shared_id": ..., "finding": ...}.
    import json as _json

    from synapse_contracts import Attribution, Finding
    relay_dir = tmp_path / "relay"
    relay_dir.mkdir(parents=True)
    finding = Finding(id="f-stuck", type="learning", text="insight",
                      attributions=[Attribution(contributor="aditya", agent_session="as-1",
                                                agent="claude-code")],
                      ts=datetime(2026, 8, 4, tzinfo=timezone.utc))
    envelope = {"shared_id": "sh-joined", "finding": _json.loads(finding.model_dump_json())}
    (relay_dir / "findings.jsonl").write_text(_json.dumps(envelope) + "\n")

    def down(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("down")
    exit_code = cli.main(["--state-dir", str(tmp_path), "resync"],
                         transport=httpx.MockTransport(down))

    assert exit_code == 1
    out = capsys.readouterr().out
    assert "FAILED" in out
    assert "re-pushed 0 finding(s)" not in out


def test_resync_pushes_a_previously_recorded_session_even_when_now_unbound(
    tmp_path, capsys
) -> None:
    """Post-review amendment (2026-08-04): resync() pushes each retained
    Finding to the Shared Session it was RECORDED under (relay.py), never to
    whatever happens to be currently bound (or unbound). No bindings/ dir
    exists in this test at all -- yet the Finding tagged with a real,
    previously-recorded session ("sh-old") still goes out; a Finding tagged
    None (recorded while genuinely unbound) never can and is excluded from
    both the push and the 'total retained' count."""
    import json as _json

    from synapse_contracts import Attribution, Finding
    relay_dir = tmp_path / "relay"
    relay_dir.mkdir(parents=True)

    def _envelope(shared_id, fid):
        finding = Finding(id=fid, type="learning", text="insight",
                          attributions=[Attribution(contributor="aditya", agent_session="as-1",
                                                    agent="claude-code")],
                          ts=datetime(2026, 8, 4, tzinfo=timezone.utc))
        return _json.dumps({"shared_id": shared_id, "finding": _json.loads(finding.model_dump_json())})

    lines = [_envelope("sh-old", "f-old"), _envelope(None, "f-never-bound")]
    (relay_dir / "findings.jsonl").write_text("\n".join(lines) + "\n")

    hit = []
    def up(request: httpx.Request) -> httpx.Response:
        hit.append(str(request.url))
        return httpx.Response(200, json={"accepted": 1, "memory_version": 1})

    exit_code = cli.main(["--state-dir", str(tmp_path), "resync"],
                         transport=httpx.MockTransport(up))

    assert exit_code == 0
    assert hit == ["http://127.0.0.1:8899/v1/sessions",
                   "http://127.0.0.1:8899/v1/sessions/sh-old/findings",
                   "http://127.0.0.1:8899/v1/sessions/sh-old/synthesize"]
    out = capsys.readouterr().out
    assert "re-pushed 1 finding(s)" in out
    assert "current session: 'unbound'" in out


def test_resync_recreates_and_synthesizes_each_session_the_log_names(tmp_path, capsys) -> None:
    """push_findings gates the model on accepted > 0, so a full resync into a
    store that already holds those findings never re-synthesizes. And after a
    real restart the session does not exist at all, so the push 404s before it
    can even fail usefully. Both halves of the documented recovery path, per
    session in the BACKLOG -- not per whatever binding happens to exist.

    Iterates `relay.recorded_session_ids()`, not `resync_sessions()`: this test
    and the loop it pins are in the demo cut, and `resync_sessions()` is the
    droppable half. Step 3 narrows the synthesize pass from "every recorded
    session" to "every session that converged"; until then a /synthesize
    against a session whose push failed returns 200 and changes nothing, which
    is a wasted call and not a defect."""
    write_binding(
        tmp_path / "bindings" / "claude-code.json",
        SessionBinding(agent_session_id="as-1", shared_id="sh-joined",
                       contributor="aditya", agent="claude-code",
                       transcript_path="/tmp/t.jsonl",
                       pinned_at=datetime(2026, 8, 4, tzinfo=timezone.utc)),
    )
    import json as _json
    from synapse_contracts import Attribution, Finding
    relay_dir = tmp_path / "relay"
    relay_dir.mkdir(parents=True)
    finding = Finding(id="f-1", type="learning", text="insight",
                      attributions=[Attribution(contributor="aditya", agent_session="as-1",
                                                agent="claude-code")],
                      ts=datetime(2026, 8, 4, tzinfo=timezone.utc))
    (relay_dir / "findings.jsonl").write_text(_json.dumps(
        {"shared_id": "sh-joined", "finding": _json.loads(finding.model_dump_json())}) + "\n")

    hit = []
    def up(request: httpx.Request) -> httpx.Response:
        hit.append(request.url.path)
        if request.url.path == "/v1/sessions":
            return httpx.Response(201, json={"shared_id": "sh-joined", "purpose": "",
                                             "members": [], "created_by": "resync"})
        if request.url.path.endswith("/synthesize"):
            return httpx.Response(200, json={"memory_version": 1, "synthesized": True})
        return httpx.Response(200, json={"accepted": 1, "memory_version": 0,
                                         "synthesized": False})

    exit_code = cli.main(["--state-dir", str(tmp_path), "resync"],
                         transport=httpx.MockTransport(up))

    assert exit_code == 0
    assert hit == ["/v1/sessions",
                   "/v1/sessions/sh-joined/findings",
                   "/v1/sessions/sh-joined/synthesize"]
    assert "synthesized" in capsys.readouterr().out


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
