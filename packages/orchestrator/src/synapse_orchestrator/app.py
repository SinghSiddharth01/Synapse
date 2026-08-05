"""One Starlette app: the MCP surface AND the producer endpoint (Plan D.1).

ADR 0001's single-egress property is structural only if everything shares one
process — so the producer route is appended onto FastMCP's own
streamable-http app rather than served separately.

EGRESS RULE enforced here: the body must parse as {"findings": [Finding…]}.
Anything else — segments, events, raw text, unparseable JSON, or a JSON value
that isn't even an object — is 422, never forwarded, never a 500.
"""

from __future__ import annotations

import json
from collections.abc import Callable

from pydantic import ValidationError
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route

from synapse_contracts import Attribution, Finding, LocalBinding

from synapse_orchestrator.relay import Relay
from synapse_orchestrator.server import create_mcp


def build_app(relay: Relay, mcp_server=None, *,
             resolve_binding: Callable[[], LocalBinding | None] | None = None) -> Starlette:
    """`resolve_binding`, when given, is called fresh on every POST.

    Two things depend on reading it live rather than once at boot: (1) a
    `synapse-worker join` run after this process started must take effect
    without a restart — `relay.rebind()` follows it; (2) `LocalBinding` is
    "owned by the orchestrator, which stamps it onto every Finding arriving
    from any local producer" (schemas.py) — a producer's own `attributions`
    are trusted shape but never trusted content, since anything on an
    unauthenticated local endpoint (`--host 0.0.0.0` is a supported flag) can
    claim to be any teammate. When no binding is resolved, findings are still
    durably recorded but never egressed — see `Relay._post`.
    """
    server = mcp_server or create_mcp()
    app = server.streamable_http_app()

    async def producer_findings(request: Request) -> JSONResponse:
        try:
            body = await request.json()
        except json.JSONDecodeError:
            return JSONResponse({"error": "body must be {'findings': [Finding, ...]}, "
                                          "not raw text"}, status_code=422)
        if not isinstance(body, dict):
            return JSONResponse({"error": "body must be {'findings': [Finding, ...]}, "
                                          "not a bare JSON array/scalar"}, status_code=422)
        raw = body.get("findings")
        if not isinstance(raw, list) or not raw:
            return JSONResponse({"error": "body must be {'findings': [Finding, ...]}"},
                                status_code=422)
        try:
            findings = [Finding.model_validate(item) for item in raw]
        except ValidationError as exc:
            return JSONResponse({"error": f"egress rule: only Findings pass. {exc}"},
                                status_code=422)

        if resolve_binding is not None:
            # Re-resolved on every call, not captured once at boot — see the
            # docstring above. When resolve_binding is absent (existing
            # single-shot callers/tests), the Relay's shared_id as constructed
            # stands and attributions pass through unstamped.
            binding = resolve_binding()
            relay.rebind(binding.shared_id if binding is not None else None)
            if binding is not None:
                stamped = Attribution(contributor=binding.contributor,
                                      agent_session=binding.agent_session_id,
                                      agent=binding.agent)
                for f in findings:
                    f.attributions = [stamped]

        relay.record(findings)                     # durable before any send
        sent, _pending = await relay.flush()       # fail-open: False just queues
        return JSONResponse({"accepted": len(findings), "sent": sent > 0})

    app.router.routes.append(
        Route("/producer/findings", producer_findings, methods=["POST"]))
    return app
