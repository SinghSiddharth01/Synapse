# E4 — Orchestrator Content Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Fill the orchestrator's transport shell with its actual content — the producer endpoint (D.1), the durable relay to the service (D.4), `query`/`contribute` plus the `instructions` briefing (D.3) — closing the loop so a finding travels worker → orchestrator → service → teammate's query, all verified without a single real socket.

**Architecture:** The existing `FastMCP` server becomes a factory (`create_mcp(instructions)`); its `streamable_http_app()` Starlette app gains a plain HTTP `/producer/findings` route, so MCP and the producer endpoint share one process and one port (ADR 0001's single egress). A self-contained `Relay` mirrors the worker's write-ahead pattern (append → send → mark sent → retain for resync). Every HTTP hop accepts an injected `httpx` transport, which is what makes the full three-package chain testable in-process.

**Tech Stack:** Python 3.12, `mcp==1.9.4` (pinned — 1.9.5+ pulls `cryptography`, no ARM64 Windows wheel), Starlette (ships with mcp), uvicorn, httpx, pytest.

## Global Constraints

- On the Windows/NPU box: `uv sync --python "$env:LOCALAPPDATA\Programs\Python\Python312-arm64\python.exe"`. `mcp==1.9.4`, exactly.
- **EGRESS RULE:** only `Finding` objects reach the relay. The producer endpoint rejects anything else with 422. Transcript-derived raw content must never enter this package — `contribute()`'s agent-authored prose is the one permitted transient exception, and it exits only as distilled Findings.
- **Fail open, always:** an unreachable service must never break the agent's MCP surface or lose a finding (the relay's log holds it).
- Caller identity per amendment F Q4: binding file per Agent product (`bindings/claude-code.json`); one active Agent Session per product per machine is the documented limit.
- Service routes are E3's, verbatim (`/v1/sessions/{sid}/findings`, `/watermark`, `/query`). E3 Tasks 1–4 must land first; Task 1 here is independent and can start immediately.

---

### Task 1: prove the `instructions` mechanism (nearly free, currently unproven)

Amendment F Q11 put the arrival briefing in the agent-agnostic floor **on the assumption** that real MCP clients surface the initialize response's `instructions` field. Cheapest possible check: a sentinel, asserted through a real client against the live shell — before anything is built on top.

**Files:**
- Modify: `packages/orchestrator/src/synapse_orchestrator/server.py`
- Modify: `packages/orchestrator/tests/test_server.py` (append)
- Create: `scripts/verify_instructions.py`

**Interfaces:**
- Produces: `create_mcp(instructions: str | None = None) -> FastMCP` — module-level `mcp = create_mcp()` stays for the CLI. Tasks 3–4 pass computed instructions into this factory.

- [x] **Step 1: Write the failing unit test**

Append to `packages/orchestrator/tests/test_server.py`:

```python
from synapse_orchestrator.server import SENTINEL, create_mcp


def test_factory_carries_custom_instructions():
    server = create_mcp("Shared Session: fec decode. 3 findings.")
    assert server.instructions == "Shared Session: fec decode. 3 findings."


def test_default_instructions_contain_the_sentinel():
    assert SENTINEL in create_mcp().instructions
```

- [x] **Step 2: Run to verify it fails**

Run: `uv run pytest packages/orchestrator/tests/test_server.py -v`
Expected: FAIL — `create_mcp` and `SENTINEL` don't exist.

- [x] **Step 3: Refactor server.py into a factory**

Keep the module docstring; replace the construction:

```python
# Stable marker asserted by scripts/verify_instructions.py through a REAL MCP
# client. If a client ever fails to surface it, amendment F Q11's tier
# assignment (briefing = agent-agnostic floor) is wrong and must be revisited
# BEFORE more briefing work is built.
SENTINEL = "[synapse-briefing]"

_DEFAULT_INSTRUCTIONS = (
    f"{SENTINEL} Synapse passively distils this coding session into shared "
    "team memory. No session is bound yet — run `synapse-worker join "
    "<shared_id>` in a terminal to connect one."
)


def create_mcp(instructions: str | None = None) -> FastMCP:
    return FastMCP(name="synapse", instructions=instructions or _DEFAULT_INSTRUCTIONS)


mcp = create_mcp()
```

- [x] **Step 4: Write the live probe**

```python
# scripts/verify_instructions.py
"""Does a real MCP client see the server's `instructions`? One question, live.

    uv run synapse-orchestrator          # terminal 1
    uv run python scripts/verify_instructions.py   # terminal 2 -> PROVEN / DISPROVEN
"""
from __future__ import annotations

import asyncio
import sys

from mcp import ClientSession
from mcp.client.streamable_http import streamablehttp_client

from synapse_orchestrator.server import SENTINEL

URL = "http://127.0.0.1:8787/mcp"


async def main() -> int:
    async with streamablehttp_client(URL) as (read, write, _):
        async with ClientSession(read, write) as session:
            init = await session.initialize()
            got = init.instructions or ""
            if SENTINEL in got:
                print(f"PROVEN: client received instructions ({len(got)} chars).")
                return 0
            print("DISPROVEN: instructions missing or empty over the wire.")
            print(f"  received: {got!r}")
            print("  -> amendment F Q11's floor-tier briefing does NOT work; "
                  "fall back to a per-agent pack and update the working notes.")
            return 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
```

- [x] **Step 5: Run both, then commit**

Run: `uv run pytest packages/orchestrator -q` → PASS.
Run the live probe against a booted shell → expect `PROVEN` (record the outcome either way in `docs/STATE.md`).

```bash
git add packages/orchestrator scripts/verify_instructions.py
git commit -m "feat(orchestrator): instructions factory + live sentinel probe (amendment F Q11 verified)"
```

---

### Task 2: the Relay — durable log + service client (D.4)

**Files:**
- Create: `packages/orchestrator/src/synapse_orchestrator/relay.py`
- Create: `packages/orchestrator/tests/test_relay.py`
- Modify: `packages/orchestrator/pyproject.toml` (add `synapse-contracts`, `httpx` if absent)

**Interfaces:**
- Produces: `Relay(state_dir: Path, service_url: str, shared_id: str, *, transport=None)` with
  `record(findings: list[Finding]) -> None` (write-ahead, before any send) ·
  `async flush() -> tuple[int, int]` (sent, still-pending) ·
  `async resync() -> int` (re-push EVERYTHING retained; safe because ingest upserts by id) ·
  `pending_count() -> int`.
  Task 3's endpoint and Task 4's contribute call `record` + `flush`; the CLI exposes `resync`.

- [ ] **Step 1: Write the failing tests**

```python
# packages/orchestrator/tests/test_relay.py
import json
from datetime import datetime, timezone
from pathlib import Path

import httpx
from synapse_contracts import Attribution, Finding

from synapse_orchestrator.relay import Relay

TS = datetime(2026, 8, 4, tzinfo=timezone.utc)


def _finding(fid: str) -> Finding:
    return Finding(id=fid, type="learning", text=f"insight {fid}",
                   attributions=[Attribution(contributor="aditya",
                                             agent_session="as-1", agent="claude-code")],
                   ts=TS)


def _relay(tmp_path: Path, handler) -> Relay:
    return Relay(tmp_path, "http://svc", "sh-1",
                 transport=httpx.MockTransport(handler))


async def test_write_ahead_then_flush(tmp_path):
    received = []
    def handler(request: httpx.Request) -> httpx.Response:
        received.append(json.loads(request.content))
        return httpx.Response(200, json={"accepted": 1, "memory_version": 1})
    relay = _relay(tmp_path, handler)
    relay.record([_finding("f-1")])
    assert (tmp_path / "findings.jsonl").exists()          # durable BEFORE any send
    assert relay.pending_count() == 1
    sent, pending = await relay.flush()
    assert (sent, pending) == (1, 0)
    assert received[0]["findings"][0]["id"] == "f-1"


async def test_service_down_keeps_findings_queued_and_survives_restart(tmp_path):
    def down(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("service down")
    relay = _relay(tmp_path, down)
    relay.record([_finding("f-1")])
    sent, pending = await relay.flush()
    assert (sent, pending) == (0, 1)

    def up(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"accepted": 1, "memory_version": 1})
    reborn = _relay(tmp_path, up)                           # fresh instance = restart
    sent, pending = await reborn.flush()
    assert (sent, pending) == (1, 0)


async def test_resync_repushes_everything_even_after_ack(tmp_path):
    """The service is in-memory; its restart is answered by our retained log."""
    calls = []
    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(json.loads(request.content))
        return httpx.Response(200, json={"accepted": 0, "memory_version": 1})
    relay = _relay(tmp_path, handler)
    relay.record([_finding("f-1"), _finding("f-2")])
    await relay.flush()
    assert relay.pending_count() == 0
    pushed = await relay.resync()
    assert pushed == 2                                      # retained, not deleted on ack
    assert {f["id"] for f in calls[-1]["findings"]} == {"f-1", "f-2"}


async def test_flush_with_nothing_pending_is_free(tmp_path):
    def handler(request: httpx.Request) -> httpx.Response:  # any call would record
        raise AssertionError("no HTTP call expected")
    relay = _relay(tmp_path, handler)
    assert await relay.flush() == (0, 0)
```

- [ ] **Step 2: Run to verify they fail**

Run: `uv run pytest packages/orchestrator/tests/test_relay.py -v`
Expected: FAIL with `ModuleNotFoundError`.

- [ ] **Step 3: Implement**

```python
# packages/orchestrator/src/synapse_orchestrator/relay.py
"""Write-ahead durable log + the sole egress to the Synapse Service (Plan D.4).

Deliberately mirrors synapse_worker.producer's file discipline rather than
importing it — the packages share contracts only, and the two logs guard
different hops. Same posture: findings.jsonl append-only, sent.jsonl marks
delivery, unsent = the difference, RETAINED after ack so `resync` can answer
a service restart (amendment F Q5). Replay is safe because ingest upserts by
Finding.id (first-write-wins, E3 Task 1)."""

from __future__ import annotations

import json
import logging
from pathlib import Path

import httpx

from synapse_contracts import Finding

logger = logging.getLogger(__name__)


class Relay:
    def __init__(self, state_dir: Path, service_url: str, shared_id: str, *,
                 timeout: float = 10.0,
                 transport: httpx.AsyncBaseTransport | None = None) -> None:
        self.state_dir = Path(state_dir)
        self.state_dir.mkdir(parents=True, exist_ok=True)
        self.findings_path = self.state_dir / "findings.jsonl"
        self.sent_path = self.state_dir / "sent.jsonl"
        self.service_url = service_url.rstrip("/")
        self.shared_id = shared_id
        self.timeout = timeout
        self._transport = transport

    # ── write-ahead ─────────────────────────────────────────────────────────
    def record(self, findings: list[Finding]) -> None:
        with self.findings_path.open("a", encoding="utf-8") as fh:
            for f in findings:
                fh.write(f.model_dump_json() + "\n")

    def _load(self, path: Path) -> list[str]:
        if not path.is_file():
            return []
        return [ln for ln in path.read_text(encoding="utf-8").splitlines() if ln.strip()]

    def _all_findings(self) -> list[Finding]:
        out = []
        for line in self._load(self.findings_path):
            try:
                out.append(Finding.model_validate_json(line))
            except ValueError as exc:
                logger.warning("Skipping corrupt relay line (%s)", exc)
        return out

    def _sent_ids(self) -> set[str]:
        return set(self._load(self.sent_path))

    def _pending(self) -> list[Finding]:
        sent = self._sent_ids()
        return [f for f in self._all_findings() if f.id not in sent]

    def pending_count(self) -> int:
        return len(self._pending())

    # ── egress ──────────────────────────────────────────────────────────────
    async def _post(self, findings: list[Finding]) -> bool:
        payload = {"findings": [f.model_dump(mode="json") for f in findings]}
        url = f"{self.service_url}/v1/sessions/{self.shared_id}/findings"
        try:
            async with httpx.AsyncClient(transport=self._transport,
                                         timeout=self.timeout) as client:
                resp = await client.post(url, json=payload)
                resp.raise_for_status()
            return True
        except (httpx.HTTPError, OSError) as exc:
            logger.info("Service unavailable (%s); %d findings stay queued",
                        exc.__class__.__name__, len(findings))
            return False

    async def flush(self) -> tuple[int, int]:
        pending = self._pending()
        if not pending:
            return (0, 0)
        if not await self._post(pending):
            return (0, len(pending))
        with self.sent_path.open("a", encoding="utf-8") as fh:
            for f in pending:
                fh.write(f.id + "\n")
        return (len(pending), 0)

    async def resync(self) -> int:
        """Re-push the entire retained log. The recovery path for a service restart."""
        everything = self._all_findings()
        if everything and await self._post(everything):
            return len(everything)
        return 0
```

Add to `packages/orchestrator/pyproject.toml` dependencies: `"synapse-contracts"`, `"httpx>=0.27"` (with the matching `[tool.uv.sources]` workspace entry), then `uv sync`.

- [ ] **Step 4: Run, then commit**

Run: `uv run pytest packages/orchestrator/tests/test_relay.py -v` → PASS.

```bash
git add packages/orchestrator pyproject.toml uv.lock
git commit -m "feat(orchestrator): Relay — write-ahead egress with retained-log resync (Plan D.4)"
```

---

### Task 3: producer endpoint on the shared app (D.1) + CLI rewire

**Files:**
- Create: `packages/orchestrator/src/synapse_orchestrator/app.py`
- Modify: `packages/orchestrator/src/synapse_orchestrator/cli.py`
- Create: `packages/orchestrator/tests/test_producer_endpoint.py`

**Interfaces:**
- Consumes: `create_mcp` (Task 1), `Relay` (Task 2), `read_binding` from `synapse_contracts.binding`.
- Produces: `build_app(relay: Relay, mcp_server: FastMCP | None = None) -> Starlette` exposing MCP at `/mcp` **and** `POST /producer/findings` → `{accepted, sent}` / 422 on non-Finding payloads. The worker's `HttpSink` default URL (`http://127.0.0.1:8787/producer/findings` in `config/synapse.toml`) matches this route — keep them aligned.

- [ ] **Step 1: Write the failing tests**

```python
# packages/orchestrator/tests/test_producer_endpoint.py
import json
from datetime import datetime, timezone

import httpx
from starlette.testclient import TestClient

from synapse_orchestrator.app import build_app
from synapse_orchestrator.relay import Relay

TS = "2026-08-04T12:00:00Z"
FINDING = {"id": "f-1", "type": "learning", "text": "insight",
           "attributions": [{"contributor": "aditya", "agent_session": "as-1",
                             "agent": "claude-code"}],
           "ts": TS, "refs": [], "provenance": "distilled", "status": "kept",
           "merged_from": [], "merged_into": None}


def _app(tmp_path, handler):
    relay = Relay(tmp_path, "http://svc", "sh-1", transport=httpx.MockTransport(handler))
    return build_app(relay)


def test_findings_are_accepted_recorded_and_forwarded(tmp_path):
    forwarded = []
    def handler(request: httpx.Request) -> httpx.Response:
        forwarded.append(json.loads(request.content))
        return httpx.Response(200, json={"accepted": 1, "memory_version": 1})
    with TestClient(_app(tmp_path, handler)) as client:
        resp = client.post("/producer/findings", json={"findings": [FINDING]})
    assert resp.status_code == 200
    assert resp.json() == {"accepted": 1, "sent": True}
    assert forwarded[0]["findings"][0]["id"] == "f-1"
    assert (tmp_path / "findings.jsonl").exists()           # write-ahead happened


def test_egress_rule_rejects_non_finding_payloads(tmp_path):
    def handler(request):                                    # must never be reached
        raise AssertionError("nothing should egress")
    with TestClient(_app(tmp_path, handler)) as client:
        assert client.post("/producer/findings",
                           json={"findings": [{"raw": "transcript text"}]}).status_code == 422
        assert client.post("/producer/findings", json={"segments": []}).status_code == 422


def test_service_down_still_accepts_and_queues(tmp_path):
    def down(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("down")
    with TestClient(_app(tmp_path, down)) as client:
        resp = client.post("/producer/findings", json={"findings": [FINDING]})
    assert resp.status_code == 200
    assert resp.json() == {"accepted": 1, "sent": False}     # fail open, queued


def test_mcp_surface_is_mounted_on_the_same_app(tmp_path):
    app = _app(tmp_path, lambda r: httpx.Response(200))
    paths = {getattr(r, "path", "") for r in app.router.routes}
    assert any(p.startswith("/mcp") for p in paths)          # one process, one port
```

- [ ] **Step 2: Run to verify they fail**

Run: `uv run pytest packages/orchestrator/tests/test_producer_endpoint.py -v`
Expected: FAIL with `ModuleNotFoundError: synapse_orchestrator.app`.

- [ ] **Step 3: Implement `app.py`**

```python
# packages/orchestrator/src/synapse_orchestrator/app.py
"""One Starlette app: the MCP surface AND the producer endpoint (Plan D.1).

ADR 0001's single-egress property is structural only if everything shares one
process — so the producer route is appended onto FastMCP's own
streamable-http app rather than served separately.

EGRESS RULE enforced here: the body must parse as {"findings": [Finding…]}.
Anything else — segments, events, raw text — is 422, never forwarded.
"""

from __future__ import annotations

from pydantic import ValidationError
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route

from synapse_contracts import Finding

from synapse_orchestrator.relay import Relay
from synapse_orchestrator.server import create_mcp


def build_app(relay: Relay, mcp_server=None) -> Starlette:
    server = mcp_server or create_mcp()
    app = server.streamable_http_app()

    async def producer_findings(request: Request) -> JSONResponse:
        body = await request.json()
        raw = body.get("findings")
        if not isinstance(raw, list) or not raw:
            return JSONResponse({"error": "body must be {'findings': [Finding, ...]}"},
                                status_code=422)
        try:
            findings = [Finding.model_validate(item) for item in raw]
        except ValidationError as exc:
            return JSONResponse({"error": f"egress rule: only Findings pass. {exc}"},
                                status_code=422)
        relay.record(findings)                     # durable before any send
        sent, _pending = await relay.flush()       # fail-open: False just queues
        return JSONResponse({"accepted": len(findings), "sent": sent > 0})

    app.router.routes.append(
        Route("/producer/findings", producer_findings, methods=["POST"]))
    return app
```

- [ ] **Step 4: Rewire the CLI**

In `cli.py`: replace the `mcp.run(transport="streamable-http")` block with:

```python
import uvicorn
from pathlib import Path

from synapse_contracts.binding import read_binding
from synapse_orchestrator.app import build_app
from synapse_orchestrator.relay import Relay
```

and in `main()` after parsing (new args: `--state-dir` default `.synapse`, `--service-url` default `http://127.0.0.1:8899`):

```python
    state_dir = Path(args.state_dir)
    binding = read_binding(state_dir / "bindings" / "claude-code.json")
    shared_id = binding.shared_id if binding else "unbound"
    relay = Relay(state_dir / "relay", args.service_url, shared_id)
    app = build_app(relay)
    print(f"synapse-orchestrator on http://{args.host}:{args.port} "
          f"(mcp at /mcp, producer at /producer/findings, session: {shared_id})")
    uvicorn.run(app, host=args.host, port=args.port)
```

Also add a `resync` subcommand — mirror the existing argparse pattern; it builds the same `Relay` and prints `await relay.resync()`. Update `tests/test_cli.py` following its existing invocation pattern so CLI coverage stays 100%.

- [ ] **Step 5: Run, then commit**

Run: `uv run pytest packages/orchestrator -q` → PASS.

```bash
git add packages/orchestrator
git commit -m "feat(orchestrator): producer endpoint on the shared app + resync CLI (Plan D.1)"
```

---

### Task 4: `query` + `contribute` tools + the real briefing (D.3)

**Files:**
- Modify: `packages/orchestrator/src/synapse_orchestrator/server.py`
- Create: `packages/orchestrator/src/synapse_orchestrator/briefing.py`
- Create: `packages/orchestrator/tests/test_tools.py`
- Modify: `packages/orchestrator/pyproject.toml` (add `synapse-distiller`, `synapse-providers`)

**Interfaces:**
- Consumes: E3's `/watermark` and `/query` routes; `synapse_distiller.Distiller` + `synapse_providers.NPUProvider` (contribute's round-trip — same model, same pack, same gate as the passive path: this **is** the "one distiller" of the hybrid amendment, invoked in-process); `SessionBinding.to_local_binding()`.
- Produces: `register_tools(server, *, binding, service_url, relay, distiller_factory) -> None` and `async build_briefing(binding, service_url, *, transport=None) -> str`. The CLI composes: `briefing = await build_briefing(...)`; `server = create_mcp(briefing)`; `register_tools(server, ...)`; `build_app(relay, server)`.

- [ ] **Step 1: Write the failing tests**

```python
# packages/orchestrator/tests/test_tools.py
from datetime import datetime, timezone

import httpx
import pytest
from synapse_contracts import Attribution, Finding, LocalBinding
from synapse_providers import FakeProvider

from synapse_orchestrator.briefing import build_briefing
from synapse_orchestrator.relay import Relay
from synapse_orchestrator.server import SENTINEL, create_mcp, register_tools

BINDING = LocalBinding(agent_session_id="as-1", shared_id="sh-1",
                       contributor="aditya", agent="claude-code")
TS = datetime(2026, 8, 4, tzinfo=timezone.utc)


async def test_briefing_reflects_the_watermark_and_fails_open():
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/v1/sessions/sh-1/watermark"
        return httpx.Response(200, json={"version": 3, "new_since": 2,
                                         "by_type": {"learning": 4, "dead_end": 1},
                                         "conflicts": 1})
    text = await build_briefing(BINDING, "http://svc",
                                transport=httpx.MockTransport(handler))
    assert SENTINEL in text and "sh-1" in text
    assert "5 findings" in text and "1 conflict" in text and "v3" in text

    def down(request):  # service dead -> default text, never an exception
        raise httpx.ConnectError("down")
    text = await build_briefing(BINDING, "http://svc",
                                transport=httpx.MockTransport(down))
    assert SENTINEL in text                     # fail-open default


async def test_query_tool_calls_the_service_and_formats_findings(tmp_path):
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/query"):
            f = Finding(id="f-9", type="learning", text="the 40ms window",
                        attributions=[Attribution(contributor="akhil",
                                                  agent_session="as-2", agent="codex")],
                        ts=TS)
            return httpx.Response(200, json={"findings": [f.model_dump(mode="json")]})
        raise AssertionError(request.url.path)
    server = create_mcp()
    relay = Relay(tmp_path, "http://svc", "sh-1", transport=httpx.MockTransport(handler))
    register_tools(server, binding=BINDING, service_url="http://svc", relay=relay,
                   distiller_factory=lambda: None,
                   transport=httpx.MockTransport(handler))
    result = await server.call_tool("query", {"question": "timing?"})
    text = str(result)
    assert "40ms window" in text and "akhil" in text


async def test_contribute_round_trips_through_the_distiller_and_relay(tmp_path):
    from synapse_contracts import Provenance
    from synapse_distiller import Distiller

    sent_to_service = []
    def handler(request: httpx.Request) -> httpx.Response:
        import json as _json
        sent_to_service.append(_json.loads(request.content))
        return httpx.Response(200, json={"accepted": 1, "memory_version": 1})

    fake = FakeProvider(scripts=[{"findings": [
        {"type": "learning", "text": "contributed insight about the retry backoff"}]}])
    server = create_mcp()
    relay = Relay(tmp_path, "http://svc", "sh-1", transport=httpx.MockTransport(handler))
    register_tools(server, binding=BINDING, service_url="http://svc", relay=relay,
                   distiller_factory=lambda: Distiller(fake, BINDING),
                   transport=httpx.MockTransport(handler))
    await server.call_tool("contribute", {"text": "the retry backoff matters because…"})

    [pushed] = sent_to_service
    [finding] = pushed["findings"]
    assert finding["provenance"] == Provenance.CONTRIBUTED.value
    assert finding["attributions"][0]["contributor"] == "aditya"
```

`server.call_tool(name, args)` is FastMCP's programmatic invocation in `mcp==1.9.4`; if its return shape differs from plain text, unwrap per its type (`list[TextContent]` → `result[0].text`) — adjust the two assertions, not the architecture. If the distiller's output-parsing rejects the fake's script shape, copy a working script literal from `packages/distiller/tests/` (the distiller tests already script `FakeProvider` for exactly this call).

- [ ] **Step 2: Run to verify they fail**

Run: `uv run pytest packages/orchestrator/tests/test_tools.py -v`
Expected: FAIL — `briefing` module and `register_tools` don't exist.

- [ ] **Step 3: Implement `briefing.py`**

```python
# packages/orchestrator/src/synapse_orchestrator/briefing.py
"""The arrival briefing — composed from the watermark, carried by `instructions`.

Hard-capped and headline-only by design: counts and types, never finding
bodies (context economy — bodies grow with session length, headlines do not).
FAIL OPEN: any error yields the default unbound text. A briefing that can
break an agent's session start is worse than no briefing."""

from __future__ import annotations

import logging

import httpx

from synapse_contracts import LocalBinding

from synapse_orchestrator.server import _DEFAULT_INSTRUCTIONS, SENTINEL

logger = logging.getLogger(__name__)


async def build_briefing(binding: LocalBinding | None, service_url: str, *,
                         timeout: float = 2.0,
                         transport: httpx.AsyncBaseTransport | None = None) -> str:
    if binding is None:
        return _DEFAULT_INSTRUCTIONS
    url = (f"{service_url.rstrip('/')}/v1/sessions/{binding.shared_id}/watermark")
    try:
        async with httpx.AsyncClient(transport=transport, timeout=timeout) as client:
            resp = await client.get(url, params={"agent_session": binding.agent_session_id})
            resp.raise_for_status()
            w = resp.json()
    except (httpx.HTTPError, OSError, ValueError) as exc:
        logger.info("Briefing fail-open (%s)", exc.__class__.__name__)
        return _DEFAULT_INSTRUCTIONS

    total = sum(w.get("by_type", {}).values())
    types = ", ".join(f"{k}: {v}" for k, v in sorted(w.get("by_type", {}).items()))
    return (
        f"{SENTINEL} You are in Synapse Shared Session {binding.shared_id} as "
        f"{binding.contributor}. Team memory holds {total} findings ({types}), "
        f"{w.get('conflicts', 0)} conflict(s), at version v{w.get('version', 0)} — "
        f"{w.get('new_since', 0)} new since you last looked. Call the `query` tool "
        "before exploring an unfamiliar subsystem, when debugging something a "
        "teammate may also be working on, or before concluding something is a "
        "dead end. Call `contribute` when you learn something non-obvious a "
        "teammate would benefit from."
    )
```

Note the test asserts `"5 findings"` (4+1) and `"1 conflict"` — the f-string above produces both.

- [ ] **Step 4: Implement `register_tools` in server.py**

Append:

```python
def register_tools(server: FastMCP, *, binding, service_url: str, relay,
                   distiller_factory, transport=None) -> None:
    """Tools speak trigger-voice; bodies stay small. `transport` is test-only."""
    import httpx as _httpx

    from synapse_contracts import Provenance, Segment

    @server.tool(description=(
        "Search the team's shared memory. Call BEFORE exploring an unfamiliar "
        "subsystem, when debugging something a teammate may also be working on, "
        "or before concluding something is a dead end."))
    async def query(question: str) -> str:
        url = f"{service_url.rstrip('/')}/v1/sessions/{binding.shared_id}/query"
        try:
            async with _httpx.AsyncClient(transport=transport, timeout=15.0) as client:
                resp = await client.post(url, json={
                    "query": question, "agent_session": binding.agent_session_id})
                resp.raise_for_status()
                findings = resp.json().get("findings", [])
        except (_httpx.HTTPError, OSError) as exc:
            return f"Shared memory is unreachable right now ({exc.__class__.__name__})."
        if not findings:
            return "Team memory has nothing relevant to that. (Checked — not skipped.)"
        lines = [f"- [{f['type']}] {f['text']} — {f['attributions'][0]['contributor']}"
                 for f in findings]
        return "Relevant team findings, best first:\n" + "\n".join(lines)

    @server.tool(description=(
        "Push an insight to the team's shared memory. Call when you have learned "
        "something non-obvious a teammate would benefit from — a root cause, a "
        "dead end, a decision and its why. A few sentences of plain prose."))
    async def contribute(text: str) -> str:
        from datetime import datetime, timezone
        ts = datetime.now(timezone.utc)
        event = {"role": "assistant", "kind": "text", "content": text,
                 "ts": ts.isoformat(), "agent_session_id": binding.agent_session_id}
        segment = Segment(id=f"contrib-{ts.strftime('%H%M%S')}",
                          agent_session_id=binding.agent_session_id,
                          events=[event], started_at=ts, ended_at=ts)
        distiller = distiller_factory()
        findings, stats = await distiller.distil(segment)
        for f in findings:
            f.provenance = Provenance.CONTRIBUTED
        if not findings:
            return "Nothing durable extracted from that — try stating the insight directly."
        relay.record(findings)
        sent, pending = await relay.flush()
        state = "shared with the team" if sent else f"queued ({pending} pending)"
        return f"{len(findings)} finding(s) {state}."
```

(`Segment.model_validate` will coerce the event dict; if construction complains, build a real `AgentEvent(...)` instead — same fields.)

CLI composition in `main()` (replacing Task 3's `build_app(relay)` line):

```python
    briefing = asyncio.run(build_briefing(
        binding.to_local_binding() if binding else None, args.service_url))
    server = create_mcp(briefing)
    if binding is not None:
        register_tools(server, binding=binding.to_local_binding(),
                       service_url=args.service_url, relay=relay,
                       distiller_factory=build_npu_distiller)   # NPUProvider + configured pack
    app = build_app(relay, server)
```

where `build_npu_distiller()` constructs `Distiller(NPUProvider(...), binding.to_local_binding())` from `synapse_distiller.load_config()` exactly the way `synapse_worker.cli`'s run command does — same config, same pack, same model: the one-distiller property.

- [ ] **Step 5: Run everything, then commit**

Run: `uv run pytest packages/orchestrator -q` → PASS.

```bash
git add packages/orchestrator
git commit -m "feat(orchestrator): query/contribute tools + watermark-driven briefing (Plan D.3)"
```

---

### Task 5: the closed loop, in-process

The star test: worker sink → orchestrator endpoint → relay → **real** service app → synthesis → a teammate's query gets the finding back. Three packages, zero sockets.

**Files:**
- Modify: `packages/worker/src/synapse_worker/producer.py` (HttpSink gains `transport` kwarg)
- Create: `packages/orchestrator/tests/test_end_to_end.py`

**Interfaces:**
- Consumes: everything above + E3's `build_app` + `FakeProvider`.

- [ ] **Step 1: Give HttpSink an injectable transport**

In `producer.py`, `HttpSink.__init__` gains `transport: httpx.AsyncBaseTransport | None = None`, stored as `self._transport`; its `send()` passes `transport=self._transport` to `httpx.AsyncClient(...)`. Default `None` — production behaviour is byte-identical.

- [ ] **Step 2: Write the failing end-to-end test**

```python
# packages/orchestrator/tests/test_end_to_end.py
"""worker sink -> orchestrator -> relay -> REAL service -> teammate's query.

Every hop is the production code path; only the HTTP transports are swapped
for in-process ASGI. This is the walking skeleton, grown up (Plan 0 Task 0.6)."""
from datetime import datetime, timezone

import httpx
from synapse_contracts import Attribution, Finding
from synapse_providers import FakeProvider

from synapse_orchestrator.app import build_app as build_orchestrator_app
from synapse_orchestrator.relay import Relay
from synapse_service.api import build_app as build_service_app
from synapse_worker.producer import HttpSink, Producer

TS = datetime(2026, 8, 4, tzinfo=timezone.utc)
MERGE_NOOP = {"working_memory": "wm", "merges": [], "trivial_ids": [], "conflicts": []}


async def test_a_finding_crosses_all_three_packages(tmp_path):
    # real service, FakeProvider scripted for one merge + one query
    service_app = build_service_app(FakeProvider(scripts=[MERGE_NOOP, {"ranked": [0]}]))
    service_transport = httpx.ASGITransport(app=service_app)

    async with httpx.AsyncClient(transport=service_transport,
                                 base_url="http://svc") as bootstrap:
        sid = (await bootstrap.post("/v1/sessions", json={
            "purpose": "e2e", "created_by": "siddsing"})).json()["shared_id"]

    relay = Relay(tmp_path / "relay", "http://svc", sid, transport=service_transport)
    orchestrator_app = build_orchestrator_app(relay)
    orch_transport = httpx.ASGITransport(app=orchestrator_app)

    # the worker's own producer, pointed at the orchestrator
    producer = Producer(tmp_path / "wal",
                        HttpSink("http://orch/producer/findings", transport=orch_transport))
    finding = Finding(id="f-e2e-1", type="learning", text="the 40ms window matters",
                      attributions=[Attribution(contributor="aditya",
                                                agent_session="as-1", agent="claude-code")],
                      ts=TS)
    producer.record([finding])
    sent, pending = await producer.flush()
    assert (sent, pending) == (1, 0)

    # a teammate's agent queries the service and gets it back
    async with httpx.AsyncClient(transport=service_transport,
                                 base_url="http://svc") as teammate:
        resp = await teammate.post(f"/v1/sessions/{sid}/query", json={
            "query": "what do we know about timing", "agent_session": "as-OTHER"})
    assert [f["id"] for f in resp.json()["findings"]] == ["f-e2e-1"]


async def test_suppression_holds_across_the_full_chain(tmp_path):
    """The producing agent itself asks — and is told nothing (it already knows)."""
    service_app = build_service_app(FakeProvider(scripts=[MERGE_NOOP]))
    service_transport = httpx.ASGITransport(app=service_app)
    async with httpx.AsyncClient(transport=service_transport,
                                 base_url="http://svc") as bootstrap:
        sid = (await bootstrap.post("/v1/sessions", json={
            "purpose": "e2e", "created_by": "s"})).json()["shared_id"]
    relay = Relay(tmp_path / "relay", "http://svc", sid, transport=service_transport)
    producer = Producer(tmp_path / "wal",
                        HttpSink("http://orch/producer/findings",
                                 transport=httpx.ASGITransport(app=build_orchestrator_app(relay))))
    producer.record([Finding(id="f-mine", type="learning", text="x",
                             attributions=[Attribution(contributor="a",
                                                       agent_session="as-me",
                                                       agent="claude-code")], ts=TS)])
    await producer.flush()
    async with httpx.AsyncClient(transport=service_transport,
                                 base_url="http://svc") as me:
        resp = await me.post(f"/v1/sessions/{sid}/query",
                             json={"query": "anything", "agent_session": "as-me"})
    assert resp.json()["findings"] == []     # suppressed pre-model: no rank script needed
```

- [ ] **Step 3: Run to verify, fix, pass**

Run: `uv run pytest packages/orchestrator/tests/test_end_to_end.py -v`
Expected first run: FAIL on the HttpSink kwarg until Step 1 lands, then PASS.
Then the full repo: `uv run pytest -q` — everything green.

- [ ] **Step 4: Commit + spec sync**

Update `docs/plans/2026-08-03-plan-d-orchestrator.md` status banner (shell → built: D.1/D.3/D.4, with what remains: freshness pointer, relevance skill, Codex pack) and `docs/STATE.md` blockers.

```bash
git add packages/worker/src/synapse_worker/producer.py packages/orchestrator/tests/test_end_to_end.py docs/plans/2026-08-03-plan-d-orchestrator.md docs/STATE.md
git commit -m "feat: the loop closes — worker to service to teammate query, three packages, zero sockets"
```

---

## Done when

1. `uv run pytest -q` green across the whole workspace, offline.
2. `verify_instructions.py` prints PROVEN against the live shell (outcome recorded either way).
3. A finding produced by the worker's own `Producer` is retrievable by a teammate's query through the real service app.
4. The producing Agent Session's own query returns nothing — suppression across the chain.
5. Service down: producer endpoint still 200s, findings queue, `resync` recovers.
6. `contribute()` findings carry `provenance: contributed` and the binding's Attribution.
