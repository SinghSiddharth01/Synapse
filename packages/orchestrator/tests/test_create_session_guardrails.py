"""`create_session` guardrails (2026-08-06 review, Sidd):

1. One conversation, one Shared Session — a bound conversation's
   `create_session` REFUSES with context, BEFORE anything reaches the
   service, so no session is ever created and stranded.
2. A service that answers-and-refuses (full, closed, 5xx) is reported as
   exactly that — "not accepting new sessions" — never as "unreachable".
3. The success text carries the dashboard URL to share with the team.

Same in-process discipline as test_lifecycle_tools.py: real FastMCP server,
real file-backed bindings, injected httpx transport, zero sockets.
"""

from __future__ import annotations

import json
import os
import time
from datetime import datetime, timezone
from functools import partial
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest
from synapse_contracts.binding import SessionBinding, write_binding
from synapse_worker.discovery import project_slug

import synapse_orchestrator.cli as cli
from synapse_orchestrator.ended import ended_session_ids, record_ended
from synapse_orchestrator.relay import Relay
from synapse_orchestrator.server import create_mcp, register_tools

TS = datetime(2026, 8, 6, tzinfo=timezone.utc)


def _wire(tmp_path: Path, handler):
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
                   distiller_factory=lambda binding: None, transport=transport,
                   state_dir=state_dir, cwd=repo, contributor="sid",
                   projects_root=projects)
    return SimpleNamespace(server=server, state_dir=state_dir, repo=repo,
                           projects=projects)


def _transcript(projects_root: Path, cwd: Path, session_id: str) -> Path:
    slug_dir = projects_root / project_slug(cwd)
    slug_dir.mkdir(parents=True, exist_ok=True)
    path = slug_dir / f"{session_id}.jsonl"
    path.write_text('{"type":"user"}\n', encoding="utf-8")
    now = time.time()
    os.utime(path, (now, now))
    return path


def _bind_conversation(wiring, session_id: str, shared_id: str = "sh-1") -> None:
    """What a previous create/join left behind: the W2 per-conversation file."""
    write_binding(
        wiring.state_dir / "bindings" / "claude-code" / f"{session_id}.json",
        SessionBinding(agent_session_id=session_id, shared_id=shared_id,
                       contributor="sid", agent="claude-code",
                       transcript_path="/tmp/conv.jsonl", pinned_at=TS))


def _counting_service(created: list[str]):
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/v1/sessions" and request.method == "POST":
            created.append(json.loads(request.content).get("purpose", ""))
            return httpx.Response(201, json={"shared_id": "sh-new", "purpose": "p",
                                             "members": [], "created_by": "sid"})
        if "/members" in request.url.path:
            return httpx.Response(200, json={"members": ["sid"],
                                             "created_by": "sid", "purpose": "p"})
        if request.url.path.endswith("/arrival"):
            return httpx.Response(404)
        return httpx.Response(200, json={})
    return handler


async def test_bound_conversation_is_refused_with_context_and_nothing_is_created(tmp_path):
    created: list[str] = []
    wiring = _wire(tmp_path, _counting_service(created))
    _bind_conversation(wiring, "conv-1", shared_id="sh-1")

    text = str(await wiring.server.call_tool(
        "create_session", {"purpose": "another one",
                           "agent_session_id": "conv-1"}))
    assert "already in Shared Session sh-1" in text
    assert "leave_session" in text          # the way out is named
    assert "sh-new" not in text
    assert created == []                    # the service was never asked


async def test_the_refusal_fires_without_an_explicit_id_via_the_live_transcript(tmp_path):
    created: list[str] = []
    wiring = _wire(tmp_path, _counting_service(created))
    _transcript(wiring.projects, wiring.repo, "conv-live")
    _bind_conversation(wiring, "conv-live", shared_id="sh-7")

    text = str(await wiring.server.call_tool("create_session", {"purpose": "p2"}))
    assert "already in Shared Session sh-7" in text
    assert created == []


async def test_an_unbound_conversation_still_creates_and_gets_the_dashboard_url(tmp_path):
    created: list[str] = []
    wiring = _wire(tmp_path, _counting_service(created))
    _transcript(wiring.projects, wiring.repo, "conv-free")

    text = str(await wiring.server.call_tool(
        "create_session", {"purpose": "fresh", "agent_session_id": "conv-free"}))
    assert "Created Shared Session sh-new" in text
    assert "http://svc/debug" in text       # the dashboard, to share with the team
    assert created == ["fresh"]


@pytest.mark.parametrize("status,body", [
    (503, {"error": "session limit reached — not accepting new sessions"}),
    (500, "not json at all"),
])
async def test_a_refusing_server_is_reported_as_refusing_not_unreachable(
        tmp_path, status, body):
    def refusing(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/v1/sessions":
            return (httpx.Response(status, json=body) if isinstance(body, dict)
                    else httpx.Response(status, text=body))
        return httpx.Response(200, json={})
    wiring = _wire(tmp_path, refusing)
    _transcript(wiring.projects, wiring.repo, "conv-free")

    text = str(await wiring.server.call_tool(
        "create_session", {"purpose": "p", "agent_session_id": "conv-free"}))
    assert f"HTTP {status}" in text
    assert "not accepting new sessions" in text
    assert "nothing was created" in text
    assert "unreachable" not in text        # reachable-and-refusing ≠ down
    if isinstance(body, dict):
        # the service's own prose reaches the user
        assert "session limit reached" in text


async def test_a_dead_server_is_still_reported_as_unreachable(tmp_path):
    def down(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("refused")
    wiring = _wire(tmp_path, down)
    _transcript(wiring.projects, wiring.repo, "conv-free")

    text = str(await wiring.server.call_tool(
        "create_session", {"purpose": "p", "agent_session_id": "conv-free"}))
    assert "unreachable" in text
    assert "no session was created" in text
