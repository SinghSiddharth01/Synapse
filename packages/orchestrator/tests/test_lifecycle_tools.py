"""The four lifecycle MCP tools, and what `query`/`contribute` do once a
Shared Session closes underneath them.

Spec: docs/superpowers/specs/2026-08-06-session-lifecycle-design.md.

Same discipline as the rest of the suite: in-process, injected `httpx`
transports, zero real sockets. Two things here are deliberately NOT mocked,
because they are what the spec is about:

- the BINDING. `resolve_binding` is the production `cli._resolve_binding`
  pointed at a temp state dir, and the tools write through
  `synapse_worker.discovery.join_session` — the same writer `synapse-worker
  join` uses. So "leave_session removes the binding; the next query reports
  not-joined" is observed the way a user observes it, through real files, not
  through a stubbed resolver that could agree with a broken implementation.
- TRANSCRIPT DISCOVERY, which runs against a fixture `projects_root` tree laid
  out exactly like `~/.claude/projects/<slug>/<session-id>.jsonl`. Binding the
  wrong transcript is the defect this spec exists to fix; a test that stubbed
  the finder could not see it.
"""

from __future__ import annotations

import os
import time
from datetime import datetime, timezone
from functools import partial
from pathlib import Path
from types import SimpleNamespace

import httpx
from synapse_contracts.binding import SessionBinding, read_binding, write_binding
from synapse_providers import FakeProvider
from synapse_worker.discovery import project_slug

import synapse_orchestrator.cli as cli
from synapse_orchestrator.ended import ended_session_ids, record_ended
from synapse_orchestrator.relay import Relay
from synapse_orchestrator.server import _NOT_JOINED, _SESSION_ENDED, create_mcp, register_tools
from synapse_orchestrator.session_meta import SessionMeta, retained_sessions

TS = datetime(2026, 8, 6, tzinfo=timezone.utc)


def _transcript(projects_root: Path, cwd: Path, session_id: str, *,
                age_seconds: float = 0.0) -> Path:
    """One Claude Code transcript in the layout `find_claude_code_transcripts`
    reads: `<projects_root>/<project_slug(cwd)>/<session-id>.jsonl`, with an
    explicit mtime so the live window and the ambiguity window are testable
    without sleeping."""
    slug_dir = projects_root / project_slug(cwd)
    slug_dir.mkdir(parents=True, exist_ok=True)
    path = slug_dir / f"{session_id}.jsonl"
    path.write_text('{"type":"user"}\n', encoding="utf-8")
    when = time.time() - age_seconds
    os.utime(path, (when, when))
    return path


def _wire(tmp_path: Path, handler, *, contributor: str = "sid",
          distiller_factory=lambda binding: None):
    """A real FastMCP server with the real, file-backed binding resolver."""
    state_dir = tmp_path / "state"
    repo = tmp_path / "repo"
    repo.mkdir(exist_ok=True)
    projects = tmp_path / "projects"
    projects.mkdir(exist_ok=True)
    transport = httpx.MockTransport(handler)
    server = create_mcp()
    relay = Relay(state_dir / "relay", "http://svc", None, transport=transport,
                  ended_sessions=ended_session_ids(state_dir),
                  on_session_ended=partial(record_ended, state_dir))
    register_tools(server, resolve_binding=lambda: cli._resolve_binding(state_dir),
                   service_url="http://svc", relay=relay,
                   distiller_factory=distiller_factory, transport=transport,
                   state_dir=state_dir, cwd=repo, contributor=contributor,
                   projects_root=projects)
    return SimpleNamespace(server=server, state_dir=state_dir, repo=repo,
                           projects=projects, relay=relay,
                           binding_file=state_dir / "bindings" / "claude-code.json")


def _prejoin(wiring, shared_id: str = "sh-1", *, contributor: str = "sid",
             transcript: str = "/tmp/conv.jsonl") -> None:
    """Whatever a previous `join` (tool or `synapse-worker join`) left behind."""
    write_binding(wiring.binding_file,
                  SessionBinding(agent_session_id="conv-1", shared_id=shared_id,
                                 contributor=contributor, agent="claude-code",
                                 transcript_path=transcript, pinned_at=TS))


def _service(*, members=("sid",), end_status: int = 200, end_body=None,
             urls: list[str] | None = None, bodies: dict | None = None,
             members_body: dict | None = None):
    """A stand-in Synapse Service covering every route the four tools touch.

    `bodies`, when given, collects `{path: parsed json}` for every request that
    carried one. Path-shaped assertions alone (`"POST .../members" in urls`)
    said nothing about the IDENTITY on the wire, and identity is what this
    whole change is about: VERIFIED BY MUTATION 2026-08-06 -- replacing
    `created_by=who` and `{"contributor": who}` in server.py with the literal
    "NOBODY" left all 103 orchestrator tests passing, while in production it
    attributes the new Shared Session to a stranger (so the real creator's
    `end_session` draws "only NOBODY can end this session" forever) and
    registers a phantom member (so the layer-3 gate refuses forever). Mirrors
    the `captured_body ==` assertion test_tools.py already uses for /query.

    `POST .../members` answers with `created_by` and `purpose` alongside
    `members` (2026-08-06 contract change): they are how a JOINING orchestrator
    learns who owns the session it just attached to, which is what lets its
    `resync` recreate that session under the real creator after a service
    restart instead of under an invented one. `members_body` overrides the
    whole object, so a test can stand in a service that has not been upgraded
    and still only answers `{"members": [...]}`.
    """
    def handler(request: httpx.Request) -> httpx.Response:
        if urls is not None:
            urls.append(f"{request.method} {request.url}")
        if bodies is not None and request.content:
            import json
            bodies[request.url.path] = json.loads(request.content)
        path = request.url.path
        if path == "/v1/sessions":
            return httpx.Response(201, json={"shared_id": "sh-new", "purpose": "p",
                                             "members": [], "created_by": "sid"})
        if path.endswith("/watermark"):
            return httpx.Response(200, json={"version": 1, "new_since": 0,
                                             "by_type": {"learning": 1}, "conflicts": 0,
                                             "purpose": "p", "members": list(members)})
        if path.endswith("/end"):
            return httpx.Response(end_status,
                                  json=end_body or {"shared_id": "sh-1",
                                                    "status": "ended", "ended_by": "sid"})
        if "/members" in path:
            return httpx.Response(200, json=members_body if members_body is not None
                                  else {"members": list(members), "created_by": "sid",
                                        "purpose": "p"})
        return httpx.Response(200, json={"findings": []})
    return handler


# ── create / join: the session id AND the transcript, every time ────────────

async def test_create_session_creates_binds_and_names_both_ids_it_chose(tmp_path):
    """Spec, "Requirement: bind the session we started from": the tool returns
    the transcript path and session id it bound, so the caller can VERIFY.
    Silent binding is the defect; visible binding is the fix — a create that
    quietly attached to the other window open on this machine is indetectable
    from inside the conversation until findings start going missing."""
    urls: list[str] = []
    bodies: dict = {}
    wiring = _wire(tmp_path, _service(urls=urls, bodies=bodies))
    mine = _transcript(wiring.projects, wiring.repo, "conv-mine")
    # A more recently modified conversation, which mtime detection would have
    # picked: the explicit id must beat it (spec Testing item 6, at this layer).
    _transcript(wiring.projects, wiring.repo, "conv-someone-elses")

    text = str(await wiring.server.call_tool(
        "create_session", {"purpose": "fec decode", "agent_session_id": "conv-mine"}))

    assert "sh-new" in text
    assert str(mine) in text
    assert "POST http://svc/v1/sessions" in urls
    # The BODY, exactly -- see `_service`'s docstring for the mutation a
    # path-only assertion let through.
    assert bodies["/v1/sessions"] == {"purpose": "fec decode", "created_by": "sid"}
    # And the creator is registered as a member of what they just created; the
    # service's create_session starts `members: []` and never adds created_by,
    # so without this the person sitting in the session is absent from it until
    # their first Finding arrives.
    assert bodies["/v1/sessions/sh-new/members"] == {"contributor": "sid"}
    bound = read_binding(wiring.binding_file)
    assert bound.shared_id == "sh-new"
    assert bound.agent_session_id == "conv-mine"       # not conv-someone-elses
    assert bound.contributor == "sid"


async def test_create_session_reports_the_new_id_even_when_binding_is_refused(tmp_path):
    """Two live transcripts written within AMBIGUITY_WINDOW_SECONDS of each
    other: refuse and list them (spec's error table) rather than bind a coin
    toss. The Shared Session exists by then, so its id is still reported —
    telling the agent only "binding failed" would strand a live session nobody
    can name."""
    wiring = _wire(tmp_path, _service())
    _transcript(wiring.projects, wiring.repo, "conv-a", age_seconds=0)
    _transcript(wiring.projects, wiring.repo, "conv-b", age_seconds=2)

    text = str(await wiring.server.call_tool("create_session", {"purpose": "fec decode"}))

    assert "sh-new" in text                       # the session was created
    assert "Refusing to guess" in text
    assert "conv-a" in text and "conv-b" in text   # the candidates, named
    assert "agent_session_id" in text              # and how to resolve it
    assert not wiring.binding_file.exists()        # nothing bound on a guess


async def test_join_session_binds_the_named_conversation_and_names_it_back(tmp_path):
    urls: list[str] = []
    bodies: dict = {}
    wiring = _wire(tmp_path, _service(urls=urls, bodies=bodies))
    mine = _transcript(wiring.projects, wiring.repo, "conv-mine")

    text = str(await wiring.server.call_tool(
        "join_session", {"shared_id": "sh-team", "agent_session_id": "conv-mine"}))

    assert "sh-team" in text and str(mine) in text
    assert "POST http://svc/v1/sessions/sh-team/members" in urls
    assert bodies["/v1/sessions/sh-team/members"] == {"contributor": "sid"}
    assert read_binding(wiring.binding_file).shared_id == "sh-team"


# ── who owns this session, retained locally (2026-08-06) ────────────────────
# The other half of the resync fix. `cmd_resync`'s recreate pass used to invent
# `created_by="resync"`, which after a real restart made "resync" the CREATOR of
# the recreated session and left `api.end_session`'s creator-only gate refusing
# the human who started it. It can only send the truth if something wrote the
# truth down when this machine bound the session, and these two tools are the
# only places that ever happens. VERIFIED BY MUTATION: deleting the `_remember`
# call from either tool leaves every other lifecycle test green, and puts
# resync back to recreating that session ownerless.


async def test_create_session_retains_who_created_it_and_what_for(tmp_path):
    wiring = _wire(tmp_path, _service(), contributor="siddsing")
    _transcript(wiring.projects, wiring.repo, "conv-mine")

    await wiring.server.call_tool(
        "create_session", {"purpose": "fec decode", "agent_session_id": "conv-mine"})

    assert retained_sessions(wiring.state_dir) == {
        "sh-new": SessionMeta(created_by="siddsing", purpose="fec decode")}


async def test_create_session_retains_the_session_even_when_binding_is_refused(tmp_path):
    """The session EXISTS from the moment the POST returns, whatever happens to
    the binding — that is why the tool reports its id on a refusal rather than
    stranding a live session nobody can name. The retained record follows the
    same rule for the same reason: it is the SESSION's identity, and dropping it
    here would leave a live session this machine created with no record of who
    created it, which is exactly the hole being closed."""
    wiring = _wire(tmp_path, _service(), contributor="siddsing")
    _transcript(wiring.projects, wiring.repo, "conv-a", age_seconds=0)
    _transcript(wiring.projects, wiring.repo, "conv-b", age_seconds=2)

    text = str(await wiring.server.call_tool("create_session", {"purpose": "fec decode"}))

    assert "Refusing to guess" in text
    assert not wiring.binding_file.exists()
    assert retained_sessions(wiring.state_dir) == {
        "sh-new": SessionMeta(created_by="siddsing", purpose="fec decode")}


async def test_join_session_retains_the_creator_the_service_reported(tmp_path):
    """Not `who` — the person JOINING is not the person who created it. The
    `/members` response is the one place a joiner is told, and a resync issued
    from this machine after a restart is issued by whoever holds the findings,
    not by the creator, so recording the wrong name here reinstates the whole
    defect one seat over."""
    wiring = _wire(tmp_path, _service(
        members_body={"members": ["aditya", "sid"], "created_by": "aditya",
                      "purpose": "fec decode"}), contributor="sid")
    _transcript(wiring.projects, wiring.repo, "conv-mine")

    await wiring.server.call_tool(
        "join_session", {"shared_id": "sh-team", "agent_session_id": "conv-mine"})

    assert retained_sessions(wiring.state_dir) == {
        "sh-team": SessionMeta(created_by="aditya", purpose="fec decode")}


async def test_join_session_against_a_service_that_reports_no_creator_still_joins(tmp_path):
    """An orchestrator and a service are separate processes on separate laptops
    and deploy in either order, so `POST .../members` may still answer the old
    `{"members": [...]}`. The join must SUCCEED and bind — a missing field is
    not an error — and what gets recorded is the honest unknown, which resync
    then sends as `created_by: null` rather than as a name it made up."""
    wiring = _wire(tmp_path, _service(members_body={"members": ["sid"]}))
    mine = _transcript(wiring.projects, wiring.repo, "conv-mine")

    text = str(await wiring.server.call_tool(
        "join_session", {"shared_id": "sh-team", "agent_session_id": "conv-mine"}))

    assert "Joined Shared Session sh-team" in text and str(mine) in text
    assert read_binding(wiring.binding_file).shared_id == "sh-team"
    assert retained_sessions(wiring.state_dir) == {
        "sh-team": SessionMeta(created_by=None, purpose=None)}


async def test_the_live_binding_supplies_the_identity_not_the_configured_default(tmp_path):
    """`_identity()`'s documented precedence — "the live binding wins" — with
    the two identities actually DIFFERENT, which no fixture here made them.

    Every other test in this file uses "sid" for both the configured
    `--contributor` and the pre-existing binding, so the rule was untestable by
    construction: VERIFIED BY MUTATION 2026-08-06, replacing `_identity()`'s
    body with `return contributor` (ignoring the resolved binding entirely)
    left all 291 orchestrator + service tests passing.

    It has to be the binding, because the binding's contributor is the string
    the worker already stamps on `Attribution.contributor` and the service has
    already seen. Take the configured one instead and one human splits into two
    identities: the contributor-keyed watermark and self-suppression this whole
    re-key exists for both key on the name the findings carry, and
    `end_session` sees the other name in `members` and refuses, naming the user
    to themselves."""
    urls: list[str] = []
    bodies: dict = {}
    wiring = _wire(tmp_path, _service(urls=urls, bodies=bodies), contributor="sid")
    # What a `synapse-worker join --contributor aditya` left behind.
    _prejoin(wiring, "sh-1", contributor="aditya")
    _transcript(wiring.projects, wiring.repo, "conv-mine")

    await wiring.server.call_tool(
        "join_session", {"shared_id": "sh-1", "agent_session_id": "conv-mine"})

    assert bodies["/v1/sessions/sh-1/members"] == {"contributor": "aditya"}
    # ...and the watermark probe asked as aditya too, so the place it reads is
    # the one the findings advanced.
    assert any("watermark?contributor=aditya" in url for url in urls), urls
    assert read_binding(wiring.binding_file).contributor == "aditya"


async def test_join_session_refuses_a_session_that_has_already_ended(tmp_path):
    """`POST /members` is deliberately NOT gated service-side (a member of a
    closed session must still be able to leave it), so joining a dead session
    would otherwise succeed, write a binding, and only surface on the next
    `query` — after the worker had begun distilling towards it."""
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/watermark"):
            return httpx.Response(409, json={"error": "session_ended"})
        raise AssertionError(f"nothing else should be called: {request.url}")

    wiring = _wire(tmp_path, handler)
    _transcript(wiring.projects, wiring.repo, "conv-mine")

    text = str(await wiring.server.call_tool(
        "join_session", {"shared_id": "sh-dead", "agent_session_id": "conv-mine"}))

    assert "has ended" in text
    assert not wiring.binding_file.exists()


async def test_every_lifecycle_tool_names_the_shared_session_in_its_result(tmp_path):
    """The spec's whole reason for existing, as one assertion per tool.

    "Every lifecycle tool result names the bound session id explicitly. Silent
    binding is the defect; visible binding is the fix." An agent cannot read
    back to its user what it was never told, and none of these four is
    observable from inside the conversation any other way."""
    wiring = _wire(tmp_path, _service())
    _transcript(wiring.projects, wiring.repo, "conv-mine")
    args = {"agent_session_id": "conv-mine"}

    created = str(await wiring.server.call_tool(
        "create_session", {"purpose": "fec decode", **args}))
    joined = str(await wiring.server.call_tool(
        "join_session", {"shared_id": "sh-team", **args}))
    left = str(await wiring.server.call_tool("leave_session", {}))
    await wiring.server.call_tool("join_session", {"shared_id": "sh-final", **args})
    ended = str(await wiring.server.call_tool("end_session", {}))

    assert "sh-new" in created
    assert "sh-team" in joined and "sh-team" in left
    assert "sh-final" in ended
    # And the transcript, on every one of them -- an id alone does not tell the
    # user WHICH conversation was attached to it.
    assert all("conv-mine" in text for text in (created, joined, left, ended))


# ── leave ──────────────────────────────────────────────────────────────────

async def test_leave_session_removes_the_binding_and_the_next_query_reports_not_joined(
        tmp_path):
    """Spec Testing item 3, end to end through real files.

    The binding file IS the "am I in a session" signal (synapse_contracts.
    binding: "File presence is the only 'is a session active' signal"), so a
    leave that only told the service would leave this conversation still
    feeding a session the user believes they left."""
    urls: list[str] = []
    wiring = _wire(tmp_path, _service(urls=urls))
    _prejoin(wiring, "sh-1", transcript="/tmp/conv-1.jsonl")

    text = str(await wiring.server.call_tool("leave_session", {}))

    assert "sh-1" in text and "/tmp/conv-1.jsonl" in text
    assert "DELETE http://svc/v1/sessions/sh-1/members/sid" in urls
    assert not wiring.binding_file.exists()
    assert _NOT_JOINED in str(await wiring.server.call_tool("query", {"question": "x?"}))
    assert _NOT_JOINED in str(await wiring.server.call_tool("contribute", {"text": "x"}))


async def test_leave_session_detaches_every_product_bound_to_that_session(tmp_path):
    """Spec Testing item 3 on the configuration it actually fails in: TWO Agent
    products bound to one Shared Session.

    That is the documented path, not an exotic one. `create_session` with no
    `agent_session_id` goes through `_worker_join_session`, which loops
    `AGENT_REGISTRY` and writes a binding for EVERY live product
    (discovery.py) — the result text names both. `leave_session` then cleared
    only `binding_path_for_agent(state_dir, binding.agent)`, i.e. whichever
    file `_resolve_binding` picked by `pinned_at`, and returned "nothing more
    from here will reach sh-1. `query` and `contribute` will say you are not
    joined." All three claims were false: the surviving binding still named
    sh-1, the very next `query` answered from it, and the worker following that
    product's transcript kept distilling into the session the user was told
    they had left — with `Relay._register_members` silently re-adding the
    contributor the DELETE had just removed.

    Both contributors are removed at the service too: one human with a
    `synapse-worker join` under one name and a tool join under another is still
    one human leaving."""
    urls: list[str] = []
    wiring = _wire(tmp_path, _service(urls=urls))
    _prejoin(wiring, "sh-1", contributor="sid", transcript="/tmp/cc.jsonl")
    write_binding(wiring.state_dir / "bindings" / "codex.json",
                  SessionBinding(agent_session_id="conv-codex", shared_id="sh-1",
                                 contributor="aditya", agent="codex",
                                 transcript_path="/tmp/codex.jsonl",
                                 # LATER than the claude-code pin, so
                                 # `_resolve_binding` picks this one and the
                                 # old single-file clear would have deleted it
                                 # and left claude-code.json behind.
                                 pinned_at=datetime(2026, 8, 7, tzinfo=timezone.utc)))

    text = str(await wiring.server.call_tool("leave_session", {}))

    assert not wiring.binding_file.exists()
    assert not (wiring.state_dir / "bindings" / "codex.json").exists()
    assert "/tmp/cc.jsonl" in text and "/tmp/codex.jsonl" in text
    assert "DELETE http://svc/v1/sessions/sh-1/members/sid" in urls
    assert "DELETE http://svc/v1/sessions/sh-1/members/aditya" in urls
    # The claim the tool makes about itself is now true.
    assert _NOT_JOINED in str(await wiring.server.call_tool("query", {"question": "x?"}))


async def test_an_ended_session_clears_every_product_bound_to_it(tmp_path):
    """The same one-file bug on the 409 path. `_SESSION_ENDED` states "The
    local binding has been cleared, so the next call will say you are not
    joined rather than retrying a dead session" — with two products bound that
    was wrong on the first 409, and the next `query` retried the dead session
    through the surviving binding, which is exactly the forever-loop the
    clearing exists to stop."""
    wiring = _wire(tmp_path, lambda r: httpx.Response(409, json={"error": "session_ended"}))
    _prejoin(wiring, "sh-1")
    write_binding(wiring.state_dir / "bindings" / "codex.json",
                  SessionBinding(agent_session_id="conv-codex", shared_id="sh-1",
                                 contributor="sid", agent="codex",
                                 transcript_path="/tmp/codex.jsonl",
                                 pinned_at=datetime(2026, 8, 7, tzinfo=timezone.utc)))

    assert "This Shared Session has ended." in str(
        await wiring.server.call_tool("query", {"question": "why the 401?"}))

    assert not wiring.binding_file.exists()
    assert not (wiring.state_dir / "bindings" / "codex.json").exists()
    assert _NOT_JOINED in str(await wiring.server.call_tool("query", {"question": "again?"}))
    # And the LIVE relay agrees, not just `ended.json`: its `_ended` set is
    # otherwise seeded at boot and grown only by 409s it observes itself, so
    # this closure — seen on query's request — would have left the running
    # process re-POSTing sh-1's queued findings on every tick forever.
    assert wiring.relay.ended_session_ids() == frozenset({"sh-1"})


async def test_leave_session_unbinds_even_when_the_service_is_unreachable(tmp_path):
    """Rejected alternative, recorded in the tool: hold the binding until the
    service confirms the departure. That trades a metadata inconsistency the
    next push repairs by itself (`Relay._register_members` is idempotent) for a
    conversation that keeps being distilled into a session the user was told
    they had left."""
    def down(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("service down")

    wiring = _wire(tmp_path, down)
    _prejoin(wiring, "sh-1")

    text = str(await wiring.server.call_tool("leave_session", {}))

    assert "sh-1" in text
    assert "may still list you as a member" in text     # honest about what it could not do
    assert not wiring.binding_file.exists()


# ── end ────────────────────────────────────────────────────────────────────

async def test_end_session_refuses_while_other_contributors_are_members(tmp_path):
    """Layer 3 of the spec's three gates: "refuse when others are still
    members — and name them". Ending is the one call that destroys the whole
    team's memory at once, and the member list is the only local evidence that
    somebody else would lose it."""
    urls: list[str] = []
    wiring = _wire(tmp_path, _service(members=("sid", "akhil", "aditya"), urls=urls))
    _prejoin(wiring, "sh-1")

    text = str(await wiring.server.call_tool("end_session", {}))

    assert "akhil" in text and "aditya" in text        # named, not just counted
    assert "leave_session" in text                     # and what unblocks it
    assert not any("/end" in url for url in urls)      # never even attempted
    assert wiring.binding_file.exists()                # nothing was closed


async def test_end_session_surfaces_the_403_as_prose_naming_the_creator(tmp_path):
    """Creator-only is enforced in the SERVICE (layer 2) — this asserts the
    orchestrator relays that verdict as something an agent can act on. The
    service writes "only <creator> can end this session" for exactly this
    reason, so it is passed through rather than re-worded to "forbidden": the
    agent's next useful act is telling the user who to ask."""
    wiring = _wire(tmp_path, _service(
        end_status=403, end_body={"error": "only aditya can end this session"}))
    _prejoin(wiring, "sh-1")

    text = str(await wiring.server.call_tool("end_session", {}))

    assert "only aditya can end this session" in text
    assert wiring.binding_file.exists()      # a refusal changes nothing, locally either


async def test_end_session_closes_unbinds_and_is_remembered_across_a_service_restart(
        tmp_path):
    """The successful path, plus the durability stopgap it has to write.

    `synapse_service.store` is in-memory, so a restart un-ends this session and
    `resync`'s create-or-return would re-create it. Recording the id locally
    (`ended.json`) is what stops that until service-side log persistence lands
    — see the spec's "Durability caveat"."""
    urls: list[str] = []
    wiring = _wire(tmp_path, _service(urls=urls))
    _prejoin(wiring, "sh-1", transcript="/tmp/conv-1.jsonl")

    text = str(await wiring.server.call_tool("end_session", {}))

    assert "sh-1" in text and "/tmp/conv-1.jsonl" in text
    assert 'POST http://svc/v1/sessions/sh-1/end' in urls
    assert not wiring.binding_file.exists()
    assert ended_session_ids(wiring.state_dir) == {"sh-1"}


# ── a session that closes underneath a member ──────────────────────────────

async def test_an_ended_session_makes_query_return_prose_and_clears_the_binding(tmp_path):
    """Spec: "409 → prose, binding cleared, no raise".

    Clearing matters as much as the prose. Without it every later call retries
    a session that can never answer, and the agent is told "unreachable"
    forever — indistinguishable from a service outage, and it would keep
    waiting for one to end."""
    wiring = _wire(tmp_path, lambda r: httpx.Response(409, json={"error": "session_ended"}))
    _prejoin(wiring, "sh-1")

    text = str(await wiring.server.call_tool("query", {"question": "why the 401?"}))

    assert "This Shared Session has ended." in text
    assert "unreachable" not in text            # an ended session is not an outage
    assert not wiring.binding_file.exists()
    assert ended_session_ids(wiring.state_dir) == {"sh-1"}
    # The next call reports not-joined rather than retrying a dead session.
    assert _NOT_JOINED in str(await wiring.server.call_tool("query", {"question": "again?"}))


async def test_an_unrelated_409_never_clears_the_binding(tmp_path):
    """`is_session_ended` reads the body as well as the status precisely so
    this cannot happen: 409 is a generic conflict status and anything between
    this process and the service can emit one, while the reaction to the
    session_ended 409 is destructive and irreversible from here."""
    wiring = _wire(tmp_path, lambda r: httpx.Response(409, json={"error": "lock held"}))
    _prejoin(wiring, "sh-1")

    text = str(await wiring.server.call_tool("query", {"question": "why the 401?"}))

    assert _SESSION_ENDED not in text
    assert wiring.binding_file.exists()
    assert ended_session_ids(wiring.state_dir) == set()


async def test_contribute_to_an_ended_session_returns_prose_and_never_raises(tmp_path):
    """The Relay swallows every HTTP outcome by design, so contribute() has to
    ASK whether the session it just wrote to turned out to be closed. Reporting
    "queued (1 pending)" for a note that has already been dropped is the most
    misleading answer available: it reads as "it will land later"."""
    from synapse_distiller import Distiller

    fake = FakeProvider(scripts=[{"findings": [
        {"type": "learning", "text": "the retry backoff matters"}]}])
    wiring = _wire(tmp_path, lambda r: httpx.Response(409, json={"error": "session_ended"}),
                   distiller_factory=lambda binding: Distiller(fake, binding))
    _prejoin(wiring, "sh-1")

    text = str(await wiring.server.call_tool("contribute", {"text": "the retry backoff…"}))

    assert "This Shared Session has ended." in text
    assert "NOT recorded" in text               # never "queued (1 pending)"
    assert not wiring.binding_file.exists()
    assert ended_session_ids(wiring.state_dir) == {"sh-1"}


# ── nothing raises out of an MCP tool ──────────────────────────────────────

async def test_no_lifecycle_tool_raises_when_the_service_is_unreachable(tmp_path):
    """"Nothing may raise out of an MCP tool. FastMCP would surface a raw
    exception string to the agent" — the spec's own error table, and the same
    fail-open discipline the long comment inside `contribute()` records. An
    agent handed a raw traceback mid-conversation has no idea whether its
    session is now joined, half-joined or ended."""
    def down(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("service down")

    wiring = _wire(tmp_path, down)
    _transcript(wiring.projects, wiring.repo, "conv-mine")
    _prejoin(wiring, "sh-1")

    results = [
        str(await wiring.server.call_tool("create_session", {"purpose": "p"})),
        str(await wiring.server.call_tool("join_session", {"shared_id": "sh-x"})),
        str(await wiring.server.call_tool("end_session", {})),
    ]

    assert all("unreachable" in text for text in results)
    assert all("Traceback" not in text and "ConnectError(" not in text for text in results)
    # An end that could not be attempted must leave the session bound: the
    # local binding is the only record that this conversation belongs to sh-1.
    assert wiring.binding_file.exists()


async def test_a_404_from_the_service_reads_as_a_missing_session_not_an_outage(tmp_path):
    """The three 404 branches, none of which any fixture reached — the
    stand-in service never returned one. VERIFIED BY MUTATION 2026-08-06:
    deleting `join_session`'s `if probe.status_code == 404`, `end_session`'s
    `if wm.status_code == 404`, and `leave_session`'s `if resp.status_code !=
    404:` tolerance left all 103 orchestrator tests passing.

    What that costs in production: a user who pastes a typo'd id into
    `join_session` is told "Shared memory is unreachable right now
    (HTTPStatusError) — Is `synapse-service` running?" and goes off to debug a
    service that is fine; and `leave_session` after a service restart (the
    store is in-memory, so the session is simply gone) emits the spurious "may
    still list you as a member" warning about a session that no longer exists
    to list anyone."""
    def not_found(request: httpx.Request) -> httpx.Response:
        return httpx.Response(404, json={"error": "unknown session sh-nope"})

    wiring = _wire(tmp_path, not_found)
    _transcript(wiring.projects, wiring.repo, "conv-mine")

    joined = str(await wiring.server.call_tool(
        "join_session", {"shared_id": "sh-nope", "agent_session_id": "conv-mine"}))
    assert "No Shared Session 'sh-nope' exists" in joined
    assert "unreachable" not in joined            # a 404 is not an outage
    assert not wiring.binding_file.exists()       # and nothing was bound

    _prejoin(wiring, "sh-gone", transcript="/tmp/conv-1.jsonl")
    ended = str(await wiring.server.call_tool("end_session", {}))
    assert "probably lost in a service restart" in ended
    assert wiring.binding_file.exists()           # nothing ended, nothing unbound

    left = str(await wiring.server.call_tool("leave_session", {}))
    assert "Left Shared Session sh-gone" in left
    assert "may still list you as a member" not in left   # there is no list
    assert not wiring.binding_file.exists()


async def test_the_lifecycle_tools_exist_before_anything_is_joined(tmp_path):
    """Registered unconditionally, exactly like query/contribute (round 3's
    tools-frozen-at-boot fix). `create_session` in particular MUST exist while
    nothing is joined — it is the tool whose job is to make something joined —
    and `leave_session`/`end_session` degrade to the plain not-joined result
    rather than not existing."""
    wiring = _wire(tmp_path, _service())

    assert {"query", "contribute", "create_session", "join_session",
            "leave_session", "end_session"} <= set(wiring.server._tool_manager._tools)
    assert _NOT_JOINED in str(await wiring.server.call_tool("leave_session", {}))
    assert _NOT_JOINED in str(await wiring.server.call_tool("end_session", {}))
