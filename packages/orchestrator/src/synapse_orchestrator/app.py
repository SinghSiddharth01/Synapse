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

#### Post-review amendment (2026-08-04), round 3

Round 3 review found the fix above closed attribution trust but not
routing: preserving `attributions` as the producer sent them means nothing
if the endpoint still tags/routes every Finding in a POST to the SAME
single `resolve_binding()` result, picked as "most recently joined across
every Agent product" (cli.py's `_resolve_binding`). With two products
joined to two different Shared Sessions, a correctly-attributed
codex-produced Finding was still egressing to whatever session claude-code
happened to be joined to (or vice versa) — a live cross-Shared-Session leak,
reproduced end-to-end in `test_producer_endpoint.py`.

Fixed by resolving PER FINDING instead of once per request:
`resolve_binding_for_agent`, when given, is called with each Finding's
`attributions[0].agent` — not the single `resolve_binding` used elsewhere
in this module for the "no product joined at all" 503 gate. Each Finding is
recorded (`relay.record(group, shared_id=...)`, relay.py's new per-call
override) and routed to the binding that actually matches the Agent that
produced it. A Finding whose agent has no matching binding — including the
case where nothing at all is joined — 503s the same way as before, naming
the unmatched agent(s), and (same as before) nothing is durably recorded
for a request that 503s.

This does NOT close the sibling gap for a Finding queued in the WORKER's
own write-ahead log across a re-join of the SAME Agent product — see
relay.py's module docstring, "round 3" note, for why that one needs a
worker-side (Task 5) change this branch does not make.

`relay.rebind()` is no longer called from this endpoint at all: each group
is recorded with its own resolved `shared_id` passed explicitly, so this
path no longer depends on (or mutates) `relay.shared_id` — removing the
second concurrency hazard round 2/3 review flagged ("two in-flight POSTs
mutate the same `relay.shared_id` between rebind and await flush").
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
             resolve_binding_for_agent: Callable[[str], LocalBinding | None] | None = None,
             resolve_binding_for_session: (
                 Callable[[str, str | None], LocalBinding | None] | None) = None,
             ) -> Starlette:
    """`resolve_binding_for_agent`, when given, is called fresh on every POST,
    once per distinct Agent found among the batch's Findings (each Finding's
    `attributions[0].agent` — the Agent that actually produced it, never
    re-stamped; see the module docstring's amendment notes).

    Three things depend on resolving PER AGENT, live, rather than once at
    boot: (1) a `synapse-worker join` for a given Agent product run after
    this process started must take effect without a restart; (2) two Agent
    products joined to two different Shared Sessions must route each
    Finding to ITS OWN product's Shared Session, never to whichever product
    happens to be "most recently joined" overall; (3) when a Finding's Agent
    has no matching binding at all — including the case where nothing is
    joined — the endpoint 503s rather than accepting a Finding it has
    nowhere to route. Attribution itself is NOT rewritten from any binding;
    see the module docstring's round 2 amendment note.

    `resolve_binding_for_session` (W2, 2026-08-06) is the same thing one level
    finer, and WINS when both are given: it is called with
    `(attributions[0].agent, attributions[0].agent_session)`. It exists because
    W2 made "one Agent product, two Shared Sessions" reachable — two Claude
    Code windows, each joined somewhere different — and on that machine
    `attributions[0].agent` no longer identifies a destination. Reproduced
    before it existed: window A bound to sh-1, window B to sh-2, a Finding
    correctly attributed to A egressed to sh-2, because the single
    per-product binding file named whichever window joined last. It is a
    SEPARATE parameter rather than a widened `resolve_binding_for_agent`
    deliberately: that one's documented shape is `Callable[[str], ...]` and
    in-tree callers pass things like `dict.get`, which would silently accept
    a second positional argument and return it as a default.
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

        if resolve_binding_for_session is not None or resolve_binding_for_agent is not None:
            # Re-resolved on every call, per Agent (and since W2 per Agent
            # SESSION) — not captured once at boot and not a single
            # request-wide binding; see the docstring above. Grouping first
            # (rather than recording/rebinding as each Finding is matched)
            # means a request with an unmatched agent never writes anything
            # durably — same all-or-nothing guarantee the old single-binding
            # 503 path had.
            groups: dict[str, list[Finding]] = {}
            unmatched_agents: set[str] = set()
            for f in findings:
                agent = f.attributions[0].agent
                if resolve_binding_for_session is not None:
                    binding = resolve_binding_for_session(
                        agent, f.attributions[0].agent_session)
                else:
                    binding = resolve_binding_for_agent(agent)
                if binding is None:
                    unmatched_agents.add(agent)
                    continue
                groups.setdefault(binding.shared_id, []).append(f)
            if unmatched_agents:
                joined = ", ".join(sorted(unmatched_agents))
                return JSONResponse(
                    {"error": f"not joined for agent(s) {joined}: run "
                              "`synapse-worker join <shared_id>` before producing "
                              "findings; nothing egresses without a Shared Session "
                              "bound for the producing Agent"},
                    status_code=503,
                )
            for shared_id, group in groups.items():
                relay.record(group, shared_id=shared_id)   # durable before any send,
                                                             # tagged with the binding
                                                             # that matches ITS agent
        else:
            relay.record(findings)                          # legacy/no-resolver callers

        sent, _pending = await relay.flush()       # fail-open: False just queues
        return JSONResponse({"accepted": len(findings), "sent": sent > 0})

    app.router.routes.append(
        Route("/producer/findings", producer_findings, methods=["POST"]))
    return app
