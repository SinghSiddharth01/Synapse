"""One Starlette app: the MCP surface AND the producer endpoint (Plan D.1).

ADR 0001's single-egress property is structural only if everything shares one
process — so the producer route is appended onto FastMCP's own
streamable-http app rather than served separately.

EGRESS RULE enforced here: the body must parse as {"findings": [Finding…]}.
Anything else — segments, events, raw text, unparseable JSON, or a JSON value
that isn't even an object — is 422, never forwarded, never a 500.

#### Post-review amendment (2026-08-04)

Two design corrections from round 2 review, both about the producer/binding
trust boundary:

1. **Attribution is no longer re-stamped.** A previous pass had this
   endpoint overwrite every incoming Finding's `attributions` with the
   single binding `resolve_binding()` returned. `_resolve_binding` (cli.py)
   picks ONE binding across every joined Agent product ("most recently
   joined wins" — Plan D.2 allows one binding file per product, i.e. more
   than one can be joined at once). Stamping that single binding over
   EVERY Finding silently relabelled another product's Findings with the
   wrong Contributor/Agent Session/Agent whenever two products were joined
   — the worker already stamps Attribution correctly from its own
   LocalBinding (distiller.py); re-stamping here only had a chance to make
   it wrong. Attribution is now preserved as the producer sent it, same as
   the plan's original Task 3 Step 3. Its SHAPE is still enforced
   (non-empty, `Finding.model_validate` already enforces the rest), and the
   other producer-untrusted fields below are still rejected outright.
2. **An unbound orchestrator now 503s instead of accept-and-queue.** A
   local producer POSTing to an orchestrator with no Shared Session joined
   used to get 200 `{"accepted": N, "sent": False}` and have its Findings
   durably queued here with no session to ever send them to. Returning 503
   instead means the WORKER's own `HttpSink` — which already treats any
   non-2xx as "stay queued, retry later" (producer.py) — keeps owning that
   backlog in ITS OWN write-ahead log, rather than this process silently
   taking custody of Findings it has nowhere to route. Nothing egresses
   without a real binding, and now nothing is even durably recorded here
   without one either.
"""

from __future__ import annotations

import json
from collections.abc import Callable

from pydantic import ValidationError
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route

from synapse_contracts import Finding, FindingStatus, LocalBinding, Provenance

from synapse_orchestrator.relay import Relay
from synapse_orchestrator.server import create_mcp

# What a producer is allowed to claim about provenance. SYNTHESIZED findings
# are written by synthesis, service-side, and never originate at a producer.
_PRODUCER_LEGITIMATE_PROVENANCE = {Provenance.DISTILLED, Provenance.CONTRIBUTED}


def _trust_violation(findings: list[Finding]) -> str | None:
    """None if every Finding respects the producer/service boundary
    (schemas.py: "Written by synthesis, service-side. Producers leave these
    at defaults."); otherwise the 422 message. `status`, `merged_from` and
    `merged_into` are synthesis's fields — a producer that sets them is
    manufacturing a tombstone or a merge lineage nobody made. `attributions`
    must be non-empty: an empty list would make the Finding un-attributable
    and, worse, satisfy the awareness suppression check for every asker
    (schemas.py: suppressed only when EVERY attribution matches)."""
    for f in findings:
        if not f.attributions:
            return f"finding {f.id!r}: attributions must be non-empty"
        if (f.status != FindingStatus.KEPT or f.merged_from
                or f.merged_into is not None):
            return (f"finding {f.id!r}: status/merged_from/merged_into are "
                    "written by synthesis, service-side; producers must leave "
                    "them at their defaults")
        if f.provenance not in _PRODUCER_LEGITIMATE_PROVENANCE:
            return (f"finding {f.id!r}: provenance {f.provenance.value!r} is not "
                    "producer-legitimate (must be 'distilled' or 'contributed')")
    return None


def build_app(relay: Relay, mcp_server=None, *,
             resolve_binding: Callable[[], LocalBinding | None] | None = None) -> Starlette:
    """`resolve_binding`, when given, is called fresh on every POST.

    Two things depend on reading it live rather than once at boot: (1) a
    `synapse-worker join` run after this process started must take effect
    without a restart — `relay.rebind()` follows it, tagging every Finding
    recorded from this point on with the newly joined Shared Session (see
    relay.py's `record()`/`rebind()`); (2) when no binding resolves at all,
    the endpoint 503s rather than accepting a Finding it has nowhere to
    route — see the module docstring's amendment note. Attribution itself is
    NOT rewritten from the binding; see the same note.
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

        violation = _trust_violation(findings)
        if violation is not None:
            return JSONResponse({"error": violation}, status_code=422)

        if resolve_binding is not None:
            # Re-resolved on every call, not captured once at boot — see the
            # docstring above. When resolve_binding is absent (existing
            # single-shot callers/tests), the Relay's shared_id as
            # constructed stands.
            binding = resolve_binding()
            if binding is None:
                return JSONResponse(
                    {"error": "not joined: run `synapse-worker join <shared_id>` "
                              "before producing findings; nothing egresses without "
                              "a Shared Session bound"},
                    status_code=503,
                )
            relay.rebind(binding.shared_id)

        relay.record(findings)                     # durable before any send
        sent, _pending = await relay.flush()       # fail-open: False just queues
        return JSONResponse({"accepted": len(findings), "sent": sent > 0})

    app.router.routes.append(
        Route("/producer/findings", producer_findings, methods=["POST"]))
    return app
