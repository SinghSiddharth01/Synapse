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
from pathlib import Path

import time

import httpx
from starlette.testclient import TestClient

import synapse_orchestrator.cli as cli
from synapse_contracts.binding import SessionBinding, write_binding
from synapse_orchestrator.session_meta import record_session

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


def test_serve_wires_the_lifecycle_tools_state_dir_cwd_and_contributor(
    monkeypatch, tmp_path
) -> None:
    """The three kwargs the lifecycle tools cannot work without, asserted where
    they are actually composed.

    The test above already captures the whole kwargs dict and asserted only on
    `resolve_binding`, so the rest was free to rot: VERIFIED BY MUTATION
    2026-08-06 — changing cli.main's call to `state_dir=None, cwd=None,
    contributor=None` left all 103 orchestrator tests passing. In the real
    process that makes every lifecycle tool answer `_NO_STATE_DIR` ("This
    orchestrator was started without a state directory") or "No contributor
    identity is configured" on every call: the entire feature is gone and
    nothing goes red. test_lifecycle_tools.py cannot see it — `_wire` there
    calls `register_tools` directly and never goes through `cli.main`.
    """
    monkeypatch.setattr(cli.uvicorn, "run", lambda app, **kw: None)
    calls: list[dict] = []
    monkeypatch.setattr(cli, "register_tools", lambda server, **kw: calls.append(kw))

    cli.main(["--state-dir", str(tmp_path), "--contributor", "sid"])

    assert calls[0]["state_dir"] == tmp_path
    # The process's cwd, which is what transcript detection is scoped to when a
    # caller does not name its own agent_session_id.
    assert calls[0]["cwd"] == Path.cwd()
    assert calls[0]["contributor"] == "sid"


def test_the_contributor_default_is_the_env_var_then_the_os_login_name(
    monkeypatch, tmp_path
) -> None:
    """`--contributor` unset falls back to SYNAPSE_CONTRIBUTOR, then to the OS
    login name — and NEITHER is resolved while argparse is building the parser.

    `getpass.getuser()` raises on a host with no passwd entry for the uid and
    no USER/LOGNAME/LNAME/USERNAME set (a plain `docker run -u 1001 ...`).
    Evaluated as an argparse default it ran for EVERY invocation, including
    `synapse-orchestrator resync`, which never reads this value — so that
    command died with an unhandled OSError before parsing finished, where
    before it ran and flushed the backlog. Third case below.
    """
    monkeypatch.setattr(cli.uvicorn, "run", lambda app, **kw: None)
    calls: list[dict] = []
    monkeypatch.setattr(cli, "register_tools", lambda server, **kw: calls.append(kw))

    monkeypatch.setenv("SYNAPSE_CONTRIBUTOR", "from-env")
    cli.main(["--state-dir", str(tmp_path)])
    assert calls[-1]["contributor"] == "from-env"

    monkeypatch.delenv("SYNAPSE_CONTRIBUTOR")
    monkeypatch.setattr(cli.getpass, "getuser", lambda: "from-login")
    cli.main(["--state-dir", str(tmp_path)])
    assert calls[-1]["contributor"] == "from-login"

    # A host with no answer at all: None, not a traceback. create_session and
    # join_session already return usable prose for an unset identity.
    def no_such_user():
        raise OSError("no login name")
    monkeypatch.setattr(cli.getpass, "getuser", no_such_user)
    cli.main(["--state-dir", str(tmp_path)])
    assert calls[-1]["contributor"] is None
    # ...and a subcommand that never wants an identity still runs there.
    assert cli.main(["--state-dir", str(tmp_path), "resync"]) == 0


def test_serve_seeds_the_relay_with_the_retained_ended_set_and_writes_back(
    monkeypatch, tmp_path
) -> None:
    """The serve path's half of the resync stopgap, which nothing covered.

    `test_resync_does_not_resurrect_a_session_the_team_ended` exercises
    `cmd_resync` only. VERIFIED BY MUTATION 2026-08-06: deleting both kwargs
    from `cli.main`'s `Relay(...)` construction left all 103 orchestrator tests
    passing. What that costs: a session ended while the orchestrator was down
    comes back with an EMPTY `_ended` set, so the retained backlog is re-POSTed
    at a closed session on every flush tick forever — precisely what relay.py's
    termination note ("Leaving them queued would re-POST a dead session on
    every tick forever") says must not happen. And the `on_session_ended` half
    stops persisting, so a closure this long-running process observes itself
    never survives into the next `resync`.
    """
    monkeypatch.setattr(cli.uvicorn, "run", lambda app, **kw: None)
    (tmp_path / "ended.json").write_text('["sh-ended"]')

    kwargs: list[dict] = []
    real_relay = cli.Relay

    def capture(*args, **kw):
        kwargs.append(kw)
        return real_relay(*args, **kw)

    monkeypatch.setattr(cli, "Relay", capture)
    cli.main(["--state-dir", str(tmp_path)])

    assert kwargs[0]["ended_sessions"] == {"sh-ended"}
    # Not merely "a callable was passed": it must WRITE THROUGH to the same
    # ended.json the next resync reads, which is the whole point of the hook.
    kwargs[0]["on_session_ended"]("sh-closed-while-up")
    assert cli.ended_session_ids(tmp_path) == {"sh-ended", "sh-closed-while-up"}


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
                   "http://127.0.0.1:8899/v1/sessions/sh-old/members",
                   "http://127.0.0.1:8899/v1/sessions/sh-old/synthesize"]
    out = capsys.readouterr().out
    assert "re-pushed 1 finding(s)" in out
    assert "current session: 'unbound'" in out


def test_resync_does_not_resurrect_a_session_the_team_ended(tmp_path, capsys) -> None:
    """The lifecycle spec's "Durability caveat", pinned.

    `synapse_service.store` is in-memory: a service restart un-ends an ended
    session, and Step 1 of resync is create-or-return, so without this the
    recovery path would re-create sh-ended and push its whole retained log
    back into a session the team deliberately closed — the single most
    destructive thing in the recovery path, and completely silent.

    The stopgap is the locally retained `ended.json` (synapse_orchestrator.
    ended), and it stands in for service-side log persistence (STATE.md's
    first post-demo entry). A live session in the SAME backlog still resyncs
    normally — the skip is per session, not a global bail-out."""
    import json as _json

    from synapse_contracts import Attribution, Finding
    relay_dir = tmp_path / "relay"
    relay_dir.mkdir(parents=True)

    def _envelope(shared_id, fid):
        finding = Finding(id=fid, type="learning", text="insight",
                          attributions=[Attribution(contributor="aditya", agent_session="as-1",
                                                    agent="claude-code")],
                          ts=datetime(2026, 8, 4, tzinfo=timezone.utc))
        return _json.dumps({"shared_id": shared_id,
                            "finding": _json.loads(finding.model_dump_json())})

    (relay_dir / "findings.jsonl").write_text(
        _envelope("sh-ended", "f-dead") + "\n" + _envelope("sh-live", "f-alive") + "\n")
    (tmp_path / "ended.json").write_text('["sh-ended"]')

    hit = []
    def up(request: httpx.Request) -> httpx.Response:
        hit.append(str(request.url))
        if request.url.path.endswith("/synthesize"):
            return httpx.Response(200, json={"memory_version": 1, "synthesized": True})
        return httpx.Response(200, json={"accepted": 1, "memory_version": 1})

    exit_code = cli.main(["--state-dir", str(tmp_path), "resync"],
                         transport=httpx.MockTransport(up))

    assert exit_code == 0
    # Not one request mentions the ended session -- not the create-or-return
    # that would bring it back, not the push, not the synthesize.
    assert not any("sh-ended" in url for url in hit)
    assert hit == ["http://127.0.0.1:8899/v1/sessions",           # recreate: sh-live only
                   "http://127.0.0.1:8899/v1/sessions/sh-live/findings",
                   "http://127.0.0.1:8899/v1/sessions/sh-live/members",
                   "http://127.0.0.1:8899/v1/sessions/sh-live/synthesize"]
    out = capsys.readouterr().out
    # Exit 0, not the "FAILED — 1 of 2 re-pushed" loud branch: refusing to
    # resurrect an ended session is the correct outcome, not a shortfall, which
    # is why `retained_count()` excludes it too.
    assert "FAILED" not in out
    assert "re-pushed 1 finding(s)" in out
    assert "skipped 1 ended session(s): ['sh-ended']" in out


def test_resync_learning_a_session_ended_mid_run_is_not_a_failure(tmp_path, capsys) -> None:
    """The FIRST resync that observes a closure, which is the case the sibling
    test above cannot reach: it pre-seeds `ended.json`, so it only ever
    exercises the already-known half.

    Here `ended.json` does not exist — the session was ended by a teammate, or
    by the service directly, or while this orchestrator was down. `cmd_resync`
    computed `total = relay.retained_count()` BEFORE any request, so sh-ended's
    findings were still counted; `resync_sessions()` then POSTed, got
    `409 {"error": "session_ended"}`, correctly dropped them and correctly did
    not count them as pushed — and the comparison fired

        resync: FAILED — 1 of 2 finding(s) re-pushed … is the service
        reachable, and is a Shared Session joined?

    with exit code 1, for a resync that did exactly the right thing, pointing
    the operator at two diagnoses that are both false. Running the identical
    command a second time then succeeded, because by then `ended.json` named
    it. Verified by execution. `retained_count`'s own docstring says this
    message is precisely what excluding ended sessions is meant to avoid, and
    an exit 1 here fails any script or CI step gating on resync's status.
    """
    import json as _json

    from synapse_contracts import Attribution, Finding
    relay_dir = tmp_path / "relay"
    relay_dir.mkdir(parents=True)

    def _envelope(shared_id, fid):
        finding = Finding(id=fid, type="learning", text="insight",
                          attributions=[Attribution(contributor="aditya", agent_session="as-1",
                                                    agent="claude-code")],
                          ts=datetime(2026, 8, 4, tzinfo=timezone.utc))
        return _json.dumps({"shared_id": shared_id,
                            "finding": _json.loads(finding.model_dump_json())})

    (relay_dir / "findings.jsonl").write_text(
        _envelope("sh-ended", "f-dead") + "\n" + _envelope("sh-live", "f-alive") + "\n")
    assert not (tmp_path / "ended.json").exists()      # nothing knows yet

    def service(request: httpx.Request) -> httpx.Response:
        if "sh-ended" in str(request.url) and request.url.path.endswith("/findings"):
            return httpx.Response(409, json={"error": "session_ended"})
        if request.url.path.endswith("/synthesize"):
            return httpx.Response(200, json={"memory_version": 1, "synthesized": True})
        return httpx.Response(200, json={"accepted": 1, "memory_version": 1})

    exit_code = cli.main(["--state-dir", str(tmp_path), "resync"],
                         transport=httpx.MockTransport(service))

    out = capsys.readouterr().out
    assert exit_code == 0, out
    assert "FAILED" not in out
    assert "re-pushed 1 finding(s)" in out
    # Reported through the same "skipped" clause the already-known case uses,
    # so the two runs read identically instead of one of them screaming.
    assert "skipped 1 ended session(s): ['sh-ended']" in out
    # And it is remembered, so the SECOND resync never even attempts it.
    assert cli.ended_session_ids(tmp_path) == {"sh-ended"}


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
                   "/v1/sessions/sh-joined/members",     # registration follows the push
                   "/v1/sessions/sh-joined/synthesize"]
    assert "synthesized" in capsys.readouterr().out


def test_resync_recreates_a_session_whose_findings_were_already_acked_before_a_restart(
    tmp_path, capsys
) -> None:
    """Invariant 4: findings are retained AFTER SENDING precisely so a
    service restart is recoverable by resync -- that is the whole point of
    NOT deleting a finding from the log once `sent.jsonl` records it. Every
    other resync test in this file seeds only `findings.jsonl`, never
    `sent.jsonl`, so every finding they exercise is still PENDING. None of
    them touches the actual demo scenario (Beat 7): a push that already
    succeeded, `sent.jsonl` recording it, and THEN the service dies and
    loses all state.

    Mutating `Relay.recorded_session_ids` (relay.py) to read `self._pending()`
    instead of `self._all_entries()` survives the whole suite, including
    both resync tests above: every finding THEY seed is pending, so
    pending-vs-all makes no observable difference to them. It matters
    exactly here -- an already-SENT finding is invisible to `_pending()`, so
    the mutant's `cmd_resync` Step 1 recreate loop iterates over ZERO
    sessions, never recreating `sh-joined` on the (restarted, stateless)
    service, and the push below 404s against a session the mock was never
    told to create. `resync_sessions()` itself is unaffected by this mutant
    (it reads `_all_entries()` directly) and still attempts the push either
    way -- the mutant's damage is entirely in skipping the recreate."""
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
    finding = Finding(id="f-acked", type="learning", text="insight",
                      attributions=[Attribution(contributor="aditya", agent_session="as-1",
                                                agent="claude-code")],
                      ts=datetime(2026, 8, 4, tzinfo=timezone.utc))
    (relay_dir / "findings.jsonl").write_text(_json.dumps(
        {"shared_id": "sh-joined", "finding": _json.loads(finding.model_dump_json())}) + "\n")
    # ACKED before the restart: the push already succeeded once, and
    # flush()/contribute() would have written exactly this line.
    (relay_dir / "sent.jsonl").write_text("f-acked\n")

    # A restarted service: it knows NOTHING until told to create a session
    # (the recreate pass), then behaves normally for that session only --
    # unlike the sibling test's mock above, which accepts any push
    # unconditionally and so cannot tell "recreated first" apart from "never
    # recreated, pushed anyway."
    known_sessions: set[str] = set()
    def restarted_service(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/v1/sessions":
            body = _json.loads(request.content)
            known_sessions.add(body["shared_id"])
            return httpx.Response(201, json={"shared_id": body["shared_id"], "purpose": "",
                                             "members": [], "created_by": "resync"})
        sid = request.url.path.split("/")[3]        # /v1/sessions/{sid}/...
        if sid not in known_sessions:
            return httpx.Response(404, json={"error": f"unknown session {sid}"})
        if request.url.path.endswith("/synthesize"):
            return httpx.Response(200, json={"memory_version": 1, "synthesized": True})
        return httpx.Response(200, json={"accepted": 1, "memory_version": 0,
                                         "synthesized": False})

    exit_code = cli.main(["--state-dir", str(tmp_path), "resync"],
                         transport=httpx.MockTransport(restarted_service))

    assert exit_code == 0
    out = capsys.readouterr().out
    assert "FAILED" not in out
    assert "re-pushed 1 finding(s)" in out


def _seed_retained_finding(tmp_path, shared_id: str, fid: str = "f-1") -> None:
    """One finding in the relay's retained log under `shared_id` — enough to put
    that session into `recorded_session_ids()` and therefore into resync's
    recreate pass, which is the only thing these two tests are about."""
    import json as _json

    from synapse_contracts import Attribution, Finding
    relay_dir = tmp_path / "relay"
    relay_dir.mkdir(parents=True, exist_ok=True)
    finding = Finding(id=fid, type="learning", text="insight",
                      attributions=[Attribution(contributor="aditya", agent_session="as-1",
                                                agent="claude-code")],
                      ts=datetime(2026, 8, 4, tzinfo=timezone.utc))
    with (relay_dir / "findings.jsonl").open("a", encoding="utf-8") as fh:
        fh.write(_json.dumps({"shared_id": shared_id,
                              "finding": _json.loads(finding.model_dump_json())}) + "\n")


def _recreate_body_capture():
    """A restarted, empty service that records the recreate POST's BODY.

    Path-shaped assertions are what let the defect this pair pins live for a
    week: `POST /v1/sessions` was hit with exactly the right url and exactly
    the wrong contents. The body is the assertion."""
    import json as _json

    sent: list[dict] = []

    def service(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/v1/sessions":
            body = _json.loads(request.content)
            sent.append(body)
            return httpx.Response(201, json={"shared_id": body["shared_id"],
                                             "purpose": body["purpose"], "members": [],
                                             "created_by": body["created_by"]})
        if request.url.path.endswith("/synthesize"):
            return httpx.Response(200, json={"memory_version": 1, "synthesized": True})
        return httpx.Response(200, json={"accepted": 1, "memory_version": 0,
                                         "synthesized": False})

    return sent, service


def test_resync_recreates_a_known_session_with_its_REAL_creator_and_purpose(
    tmp_path, capsys
) -> None:
    """The defect this whole change exists for (2026-08-06, verified by reading
    the wire).

    resync's step 1 used to POST `{"purpose": "(recovered by resync)",
    "created_by": "resync"}` because the retained log carries findings and
    nothing about the session. Against a LIVE service that is inert —
    `store.create_session` returns an existing session unchanged — so the only
    run in which it did anything was the one it was written for: after a
    genuine restart the store is empty, the POST is a real CREATE, and the
    session's creator became the literal string "resync". `api.end_session` is
    creator-only, so from that moment the human who created the session was
    refused ("only resync can end this session") while anyone who had read
    cli.py could close it by sending `{"ended_by": "resync"}` — a gate that
    refuses the owner and admits everyone else.

    `sessions.json` is what this machine recorded when `create_session` (or
    `join_session`) bound it, so the recreate can send the truth instead. The
    purpose rides along on the same record, which is why STATE.md's "the
    session's purpose is lost on a genuine recreate" stops being true for a
    session this machine knows.
    """
    record_session(tmp_path, "sh-joined", created_by="siddsing", purpose="fec decode")
    _seed_retained_finding(tmp_path, "sh-joined")
    sent, service = _recreate_body_capture()

    exit_code = cli.main(["--state-dir", str(tmp_path), "resync"],
                         transport=httpx.MockTransport(service))

    assert exit_code == 0, capsys.readouterr().out
    assert sent == [{"purpose": "fec decode", "created_by": "siddsing",
                     "shared_id": "sh-joined"}]


def test_resync_recreates_an_unknown_session_ownerless_never_as_a_sentinel(
    tmp_path, capsys
) -> None:
    """No record on this machine — a session joined by `synapse-worker join`
    before the lifecycle tools existed, or one whose findings this orchestrator
    holds without ever having created or joined it.

    `created_by: null`, not "resync" and not a guess. The key still travels
    (the service requires it, so a typo cannot silently create an ownerless
    session) but its value is the honest unknown, which the service answers by
    letting any CURRENT MEMBER end the session instead of naming a creator who
    never existed. `purpose` keeps a placeholder because the contract still
    requires a string there — an unknown purpose costs a label, an unknown
    creator would cost a permission.
    """
    _seed_retained_finding(tmp_path, "sh-orphan")
    assert not (tmp_path / "sessions.json").exists()      # nothing knows who owns it
    sent, service = _recreate_body_capture()

    exit_code = cli.main(["--state-dir", str(tmp_path), "resync"],
                         transport=httpx.MockTransport(service))

    assert exit_code == 0, capsys.readouterr().out
    assert sent == [{"purpose": "(recovered by resync)", "created_by": None,
                     "shared_id": "sh-orphan"}]


def test_resync_sends_each_session_its_own_identity(tmp_path, capsys) -> None:
    """Two sessions in one backlog, one known and one not. The lookup is per
    session — a single `metadata` read applied to whichever session happens to
    be first would attribute one team's session to the other's creator, which
    is the same class of error as the sentinel and just as silent."""
    record_session(tmp_path, "sh-known", created_by="siddsing", purpose="fec decode")
    _seed_retained_finding(tmp_path, "sh-known", fid="f-known")
    _seed_retained_finding(tmp_path, "sh-orphan", fid="f-orphan")
    sent, service = _recreate_body_capture()

    exit_code = cli.main(["--state-dir", str(tmp_path), "resync"],
                         transport=httpx.MockTransport(service))

    assert exit_code == 0, capsys.readouterr().out
    assert {b["shared_id"]: (b["created_by"], b["purpose"]) for b in sent} == {
        "sh-known": ("siddsing", "fec decode"),
        "sh-orphan": (None, "(recovered by resync)"),
    }


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
        # What the real SynapseConfig resolves: provider.max_tokens capped at
        # the capability record's response_reserve. Set BELOW max_tokens here so
        # the assertion below proves the clamped value is the one passed, not
        # the raw one -- identical values would let a regression pass.
        effective_max_tokens = 90

    class FakePack:
        """A sentinel, not a PromptPack — this test is about identity and wiring.

        It carries `example_finding_texts` because Distiller reads the corpus for
        its example-echo guard off whichever pack it is handed (W7 F1), and a
        bare object() would make this test fail for a reason that has nothing to
        do with what it checks.
        """

        example_finding_texts: tuple[str, ...] = ()

    fake_pack = FakePack()
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
        "max_tokens": 90, "temperature": 0.5, "timeout": 7.0,
    }, "contribute() must ask for the SAME clamped max_tokens as the passive path"


def test_main_attaches_the_briefing_refresher_so_it_does_not_stay_a_boot_snapshot(
    monkeypatch, tmp_path
) -> None:
    """Kills the mutant that drops `attach_briefing_refresher` from main().

    Without it, `instructions` is whatever the session looked like the
    instant `synapse-orchestrator` started, for the life of the process —
    observed live 2026-08-05 as an orchestrator telling every agent that
    connected "0 findings" while `query` returned six on the same
    connection. Drives the REAL app handed to `uvicorn.run`, and runs its
    lifespan, so it pins the composition rather than the helper.
    """
    captured: dict = {}
    monkeypatch.setattr(cli.uvicorn, "run", lambda app, **kw: captured.setdefault("app", app))

    findings = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/watermark"):
            return httpx.Response(200, json={
                "version": findings["n"], "new_since": findings["n"],
                "by_type": {"learning": findings["n"]} if findings["n"] else {},
                "conflicts": 0, "working_memory": "", "purpose": "p", "members": [],
            })
        return httpx.Response(200, json={"accepted": 1, "memory_version": 1})

    write_binding(tmp_path / "bindings" / "claude-code.json", SessionBinding(
        agent_session_id="as-1", shared_id="sh-1", contributor="me",
        agent="claude-code", transcript_path=str(tmp_path / "t.jsonl"),
        pinned_at=datetime.now(timezone.utc)))

    cli.main(["--state-dir", str(tmp_path), "--briefing-refresh", "0.01"],
             transport=httpx.MockTransport(handler))
    app = captured["app"]

    findings["n"] = 6                       # a teammate pushes AFTER boot
    with TestClient(app):
        deadline = time.time() + 3.0
        while time.time() < deadline:
            if "6 findings" in (cli_server_instructions(app) or ""):
                break
            time.sleep(0.02)
    assert "6 findings" in (cli_server_instructions(app) or ""), (
        "the briefing never refreshed — it is still the boot snapshot"
    )


def cli_server_instructions(app) -> str | None:
    """The instructions string the next MCP client would receive."""
    return app.state.synapse_mcp._mcp_server.instructions
