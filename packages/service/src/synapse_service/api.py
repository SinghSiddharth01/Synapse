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
from synapse_providers import CallLog, ModelProvider, RecordingProvider

from synapse_service.debug import Feed, debug_routes
from synapse_service.lanes import DEFAULT_TOP_K
from synapse_service.log import MarkedTrivial, Merged
from synapse_service.retrieval import query_findings, visible_to
from synapse_service.store import InMemoryStore
from synapse_service.synthesis import Synthesizer

# How many findings reach ONE retrieval prompt, regardless of log size. The
# route used to pass store.retrievable(sid) -- the entire visible log --
# growing linearly until an 8B could not read it.
TOP_K = DEFAULT_TOP_K


def _missing(body: dict, *required: str) -> JSONResponse | None:
    """The plan's contract is `422 {error}` for a malformed payload
    (docs/plans/exec/2026-08-04-e3-service.md L719); push_findings was the
    only route that honored it. create_session, add_member, and query
    indexed body["..."] directly and raised KeyError on a missing field --
    an unhandled 500, not a reported, terminal 422. E4's Relay treats 5xx
    as retryable, so that turned a client bug into an infinite retry loop
    against a request that could never succeed."""
    missing = [k for k in required if k not in body]
    if missing:
        return JSONResponse({"error": f"missing required field(s): {', '.join(missing)}"},
                            status_code=422)
    return None


def build_app(provider: ModelProvider) -> Starlette:
    store = InMemoryStore()

    # E6: one CallLog, one Feed, for the whole service instance -- wrapping
    # per component (not per session) is deliberate. RecordingProvider is
    # transparent (same result, exceptions re-raised), so this changes
    # nothing about what synthesis or retrieval actually do; it only gives
    # /debug something to read.
    call_log = CallLog()
    feed = Feed()
    synthesis_provider = RecordingProvider(provider, "synthesis", call_log)
    retrieval_provider = RecordingProvider(provider, "retrieval", call_log)
    synthesizer = Synthesizer(synthesis_provider)

    def _session_or_404(sid: str):
        return store.get_session(sid)

    def _record_synthesis_feed(sid: str, log_before: int) -> None:
        """Diff the log around the `merge()` call just made, rather than
        having synthesis.py report counts itself -- keeps this instrumentation
        entirely in api.py, matching the plan's Files list for this task."""
        entries = store.log_entries(sid)[log_before:]
        merged = [e for e in entries if isinstance(e, Merged)]
        trivial = [e for e in entries if isinstance(e, MarkedTrivial)]
        trivial_count = sum(len(e.finding_ids) for e in trivial)
        ctx = store.get_context(sid)
        feed.event(
            "synthesis",
            f"{sid}: {len(merged)} merge(s), {trivial_count} trivial, "
            f"{len(ctx.conflicts)} conflict(s) (v{ctx.memory_version})",
            session=sid,
            new_ids=[e.result.id for e in merged],
            merges=len(merged),
            trivial=trivial_count,
            conflicts=len(ctx.conflicts),
            version=ctx.memory_version,
        )

    async def create_session(request: Request) -> JSONResponse:
        body = await request.json()
        if (err := _missing(body, "purpose", "created_by")) is not None:
            return err
        requested = body.get("shared_id")
        existed = requested is not None and store.get_session(requested) is not None
        session = store.create_session(purpose=body["purpose"],
                                       created_by=body["created_by"],
                                       shared_id=requested)
        return JSONResponse(session.model_dump(mode="json"),
                            status_code=200 if existed else 201)

    async def add_member(request: Request) -> JSONResponse:
        sid = request.path_params["sid"]
        if _session_or_404(sid) is None:
            return JSONResponse({"error": f"unknown session {sid}"}, status_code=404)
        body = await request.json()
        if (err := _missing(body, "contributor")) is not None:
            return err
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
        version_before = store.get_context(sid).memory_version
        synthesized = False
        if accepted:
            # `findings` is passed through (not []) so synthesis knows which
            # ids were just pushed and can force them into its candidate
            # window regardless of CANDIDATE_WINDOW (see synthesis.merge's
            # docstring). Replays (accepted == 0) never reach the model at all.
            #
            # ⟨CORRECTED 2026-08-05, Plan E Task E.6⟩ This comment used to say
            # "store.upsert is idempotent, so merge()'s own upsert of the same
            # list is a harmless no-op". Idempotent in the VIEW, yes. Not free
            # in the LOG: `SharedMemory.append` records a resend on purpose, so
            # merge()'s second upsert writes one more FindingAppended per
            # finding and one push of N costs 3N entries. Pinned by
            # test_a_second_upsert_of_the_same_batch_appends_without_reindexing.
            # A THIRD upsert would cost another N. Removing merge()'s is the
            # real fix and needs all 19 direct call sites in test_synthesis.py
            # to upsert first -- post-demo.
            log_before = len(store.log_entries(sid))
            await synthesizer.merge(store, sid, findings)
            _record_synthesis_feed(sid, log_before)
            # A provider outage, an exhausted retry, or a verdict that
            # fails structural validation all make merge() return with
            # memory_version untouched -- "landed, quality degraded" by
            # design (synthesis.py's own docstring), but otherwise
            # indistinguishable from a completed merge in this response.
            # `synthesized` reports whether the version actually moved
            # this round so a producer can tell the two apart rather than
            # treating every 200 as a completed merge.
            synthesized = store.get_context(sid).memory_version > version_before
        version = store.get_context(sid).memory_version
        return JSONResponse({"accepted": accepted, "memory_version": version,
                             "synthesized": synthesized})

    async def synthesize(request: Request) -> JSONResponse:
        """Self-heal path (E3 residual, Finding #11's sibling gap): a
        session whose last push failed synthesis (a malformed verdict, a
        provider outage) has no way to re-run it without a NEW finding to
        push -- `push_findings` only calls `merge()` when `accepted > 0`.
        This route runs `merge()` over what is ALREADY stored, offering
        the same bounded candidate window synthesis always would (no
        `new_findings`, so nothing is re-landed -- `store.upsert` isn't
        even reached with a nonempty list). It is also Plan C.3's replay
        primitive: pushing a machine's entire retained log into a fresh
        store as one batch, then calling this once, is how a resync
        converges to the same state as the original incremental stream."""
        sid = request.path_params["sid"]
        if _session_or_404(sid) is None:
            return JSONResponse({"error": f"unknown session {sid}"}, status_code=404)
        version_before = store.get_context(sid).memory_version
        log_before = len(store.log_entries(sid))
        await synthesizer.merge(store, sid, [])
        _record_synthesis_feed(sid, log_before)
        version = store.get_context(sid).memory_version
        return JSONResponse({"memory_version": version,
                             "synthesized": version > version_before})

    async def watermark(request: Request) -> JSONResponse:
        sid = request.path_params["sid"]
        if _session_or_404(sid) is None:
            return JSONResponse({"error": f"unknown session {sid}"}, status_code=404)
        agent_session = request.query_params.get("agent_session", "")
        ctx = store.get_context(sid)

        # Round-2 adjudication on watermark suppression, split deliberately
        # in two:
        #   - `by_type` and `conflicts` are CONTENT fields: they describe
        #     what this asker would actually see, so they run through the
        #     same all-attributions suppression rule as /query (invariant
        #     3) -- a Finding, or a Conflict touching only Findings, that
        #     are entirely the asker's own is already in that agent's
        #     context window and is not "new team knowledge".
        #   - `version` and `new_since` are CHANGE fields: they measure how
        #     much the Shared Memory has moved since this asker last
        #     looked, not whether any of that movement is visible to them,
        #     and stay global (unfiltered by suppression).
        # This means new_since can be > 0 while by_type == {} and
        # conflicts == 0 -- e.g. a version bump that was entirely the
        # asker's own findings merging with each other. That is not a
        # contradiction to paper over: it is the intended distinction
        # between "the memory changed" (new_since, cheap, coarse) and
        # "here is what changed for you" (by_type/conflicts, precise,
        # content-scoped). E4's briefing composer renders both, and is
        # expected to phrase them as separate signals, not force them to
        # agree.
        visible = visible_to(store.retrievable(sid), agent_session)
        by_type = Counter(f.type.value for f in visible)

        # A Conflict counts if at least one side is visible to this asker:
        # even when the OTHER side is something already in their own
        # context window, learning that a teammate disagrees with it is
        # new information. A Conflict entirely between two of the asker's
        # own (suppressed) Findings is not.
        visible_ids = {f.id for f in visible}
        conflicts = sum(1 for c in ctx.conflicts
                        if c.finding_a in visible_ids or c.finding_b in visible_ids)

        # `topics`, `purpose` and `members` are CONTENT fields and join
        # by_type/conflicts under the same suppression rule. `version` and
        # `new_since` are CHANGE fields and stay global -- they measure how
        # much the Shared Memory moved, not whether that movement is visible
        # to this asker. That split is deliberate (E3's round-2 adjudication)
        # and this task does not touch it.
        #
        # `version` is SessionContext.memory_version: VERDICT ROUNDS APPLIED,
        # not merges completed and not Log.version.
        topics = store.topic_summaries(sid, only=frozenset(visible_ids))

        return JSONResponse({
            "version": ctx.memory_version,
            "new_since": ctx.memory_version - store.last_seen(sid, agent_session),
            "by_type": dict(by_type),
            "conflicts": conflicts,
            "topics": [{"id": t.topic_id, "size": t.size, "label": t.label}
                       for t in topics],
            "purpose": ctx.purpose,
            "members": list(store.get_session(sid).members),
        })

    async def query(request: Request) -> JSONResponse:
        sid = request.path_params["sid"]
        if _session_or_404(sid) is None:
            return JSONResponse({"error": f"unknown session {sid}"}, status_code=404)
        body = await request.json()
        if (err := _missing(body, "query")) is not None:
            return err
        agent_session = body.get("agent_session", "")

        # Invariant 3 at the lanes seam. `visible_to` stays the ONE definition
        # of the rule (retrieval.py); here it computes what must be EXCLUDED
        # from candidate selection, and query_findings still applies it again
        # before the prompt. Applying an idempotent predicate twice is the
        # belt; the definition living in one module is the braces.
        #
        # Suppression is a pure predicate over a Finding: an O(N) Python loop
        # with no model and no prompt cost. What must be bounded is the
        # PROMPT, not the loop.
        visible = store.retrievable(sid)
        allowed = visible_to(visible, agent_session)

        if len(allowed) <= TOP_K:
            # THE BYPASS. Everything the asker may see already fits in one
            # prompt, so selecting a subset of it can only lose recall -- and
            # the selectors available here (BM25, symbol overlap, a
            # HashingEmbedder with no paraphrase signal) are weaker at this
            # scale than the 8B reading all of it. Byte-identical to what this
            # route did before lanes existed. Above TOP_K the lanes are the
            # only way the prompt stays bounded, and they run.
            candidates = allowed
        else:
            suppressed = frozenset(f.id for f in visible) - {f.id for f in allowed}
            cands = store.candidates(sid, body["query"], top_k=TOP_K, exclude=suppressed)
            candidates = [c.finding for c in cands.candidates]

        ranked = await query_findings(
            retrieval_provider,
            context=store.get_context(sid),
            candidates=candidates,
            query=body["query"],
            asking_agent_session=agent_session,
        )
        store.mark_seen(sid, agent_session)
        feed.event(
            "query",
            f"{sid}: '{body['query'][:60]}' -> {len(candidates)} candidates, "
            f"{len(ranked)} ranked",
            session=sid,
            candidates=len(candidates),
            ranked=len(ranked),
        )
        return JSONResponse({"findings": [f.model_dump(mode="json") for f in ranked]})

    app = Starlette(routes=[
        Route("/v1/sessions", create_session, methods=["POST"]),
        Route("/v1/sessions/{sid}/members", add_member, methods=["POST"]),
        Route("/v1/sessions/{sid}/findings", push_findings, methods=["POST"]),
        Route("/v1/sessions/{sid}/synthesize", synthesize, methods=["POST"]),
        Route("/v1/sessions/{sid}/watermark", watermark, methods=["GET"]),
        Route("/v1/sessions/{sid}/query", query, methods=["POST"]),
        *debug_routes(store, call_log, feed),
    ])
    app.state.store = store          # test seam: no route reads it
    return app
