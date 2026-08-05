# packages/service/src/synapse_service/api.py
"""The ingest + retrieval surface (Plan C.3/C.5/C.6). One store, one provider."""

from __future__ import annotations

from collections import Counter

from pydantic import ValidationError
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route

from synapse_contracts import Finding
from synapse_providers import ModelProvider

from synapse_service.retrieval import query_findings, visible_to
from synapse_service.store import InMemoryStore
from synapse_service.synthesis import Synthesizer


def build_app(provider: ModelProvider) -> Starlette:
    store = InMemoryStore()
    synthesizer = Synthesizer(provider)

    def _session_or_404(sid: str):
        return store.get_session(sid)

    async def create_session(request: Request) -> JSONResponse:
        body = await request.json()
        session = store.create_session(purpose=body["purpose"],
                                       created_by=body["created_by"])
        return JSONResponse(session.model_dump(mode="json"), status_code=201)

    async def add_member(request: Request) -> JSONResponse:
        sid = request.path_params["sid"]
        if _session_or_404(sid) is None:
            return JSONResponse({"error": f"unknown session {sid}"}, status_code=404)
        body = await request.json()
        store.add_member(sid, body["contributor"])
        return JSONResponse({"members": store.get_session(sid).members})

    async def push_findings(request: Request) -> JSONResponse:
        sid = request.path_params["sid"]
        if _session_or_404(sid) is None:
            return JSONResponse({"error": f"unknown session {sid}"}, status_code=404)
        body = await request.json()
        try:
            findings = [Finding.model_validate(f) for f in body.get("findings", [])]
        except ValidationError as exc:
            return JSONResponse({"error": str(exc)}, status_code=422)
        accepted = store.upsert(sid, findings)
        if accepted:
            # Findings are already upserted; the empty list just avoids double
            # insertion. Replays (accepted == 0) never reach the model at all.
            await synthesizer.merge(store, sid, [])
        version = store.get_context(sid).memory_version
        return JSONResponse({"accepted": accepted, "memory_version": version})

    async def watermark(request: Request) -> JSONResponse:
        sid = request.path_params["sid"]
        if _session_or_404(sid) is None:
            return JSONResponse({"error": f"unknown session {sid}"}, status_code=404)
        agent_session = request.query_params.get("agent_session", "")
        ctx = store.get_context(sid)
        # Invariant 3 governs the whole awareness layer, not just /query:
        # suppress a Finding here too, or watermark advertises "new team
        # knowledge" built entirely from the asker's own findings that
        # /query then correctly refuses to show.
        visible = visible_to(store.retrievable(sid), agent_session)
        by_type = Counter(f.type.value for f in visible)
        # new_since is a raw memory_version delta, coarser than by_type (a
        # per-visible-finding count) -- the two only ever go fully out of
        # sync at the boundary where NOTHING is visible to this asker: a
        # version bump can be entirely the asker's own findings merging with
        # each other, in which case by_type == {} but a naive delta would
        # still be > 0. E4's briefing composer renders both together
        # ("{new_since} new ... {sum(by_type.values())} findings"), so an
        # empty visible set must report new_since == 0 too, or the briefing
        # claims new team knowledge that /query then correctly refuses to
        # show. When something IS visible, the coarser delta stands.
        new_since = ctx.memory_version - store.last_seen(sid, agent_session)
        if not visible:
            new_since = 0
        return JSONResponse({
            "version": ctx.memory_version,
            "new_since": new_since,
            "by_type": dict(by_type),
            "conflicts": len(ctx.conflicts),
        })

    async def query(request: Request) -> JSONResponse:
        sid = request.path_params["sid"]
        if _session_or_404(sid) is None:
            return JSONResponse({"error": f"unknown session {sid}"}, status_code=404)
        body = await request.json()
        ranked = await query_findings(
            provider,
            context=store.get_context(sid),
            candidates=store.retrievable(sid),          # the Log, never the prose
            query=body["query"],
            asking_agent_session=body.get("agent_session", ""),
        )
        store.mark_seen(sid, body.get("agent_session", ""))
        return JSONResponse({"findings": [f.model_dump(mode="json") for f in ranked]})

    return Starlette(routes=[
        Route("/v1/sessions", create_session, methods=["POST"]),
        Route("/v1/sessions/{sid}/members", add_member, methods=["POST"]),
        Route("/v1/sessions/{sid}/findings", push_findings, methods=["POST"]),
        Route("/v1/sessions/{sid}/watermark", watermark, methods=["GET"]),
        Route("/v1/sessions/{sid}/query", query, methods=["POST"]),
    ])
