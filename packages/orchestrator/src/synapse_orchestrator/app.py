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
