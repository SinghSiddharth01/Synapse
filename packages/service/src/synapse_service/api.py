# packages/service/src/synapse_service/api.py
"""The ingest + retrieval surface (Plan C.3/C.5/C.6). One store, one provider.

Also the lifecycle surface since 2026-08-06 (`POST /v1/sessions/{sid}/end`,
`DELETE /v1/sessions/{sid}/members/{contributor}`). Whether a session is still
open is enforced in ONE place here -- `_unavailable` -- and not repeated per
route; see its docstring.
"""

from __future__ import annotations

import logging
import os
import time
from collections import Counter
from collections.abc import Mapping

from pydantic import ValidationError
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route

from synapse_contracts import Finding, SessionStatus
from synapse_providers import CallLog, ModelProvider, RecordingProvider

from synapse_service.arrival import SummaryCache
from synapse_service.debug import Feed, debug_routes
from synapse_service.lanes import DEFAULT_TOP_K
from synapse_service.log import MarkedTrivial, Merged
from synapse_service.retrieval import (RetrievalUnavailable, query_findings,
                                       visible_to)
from synapse_service.store import InMemoryStore
from synapse_service.synthesis import Synthesizer

logger = logging.getLogger(__name__)

# How many findings reach ONE retrieval prompt, regardless of log size. The
# route used to pass store.retrievable(sid) -- the entire visible log --
# growing linearly until an 8B could not read it.
TOP_K = DEFAULT_TOP_K

# The floor between two synthesis rounds for ONE session -- the LATENCY knob.
#
# 60s is the product answer: a teammate's contribution should reach the working
# memory within a minute. It is deliberately NOT the rate limit. A clock is a
# poor proxy for a token budget, and picking one fixed interval means choosing
# between "too slow when quiet" and "over budget when busy". The floor keeps
# latency honest; `_affordable` below keeps spend honest.
MERGE_MIN_INTERVAL_S = float(os.environ.get("SYNAPSE_MERGE_MIN_INTERVAL_S", 60))

# The synthesis key's measured ceilings (2026-08-06, read off the provider
# dashboard): 20 requests AND 25,000 tokens per hour, PER KEY.
#
# One merge currently costs ~4,000 tokens (input ~2,500 and rising with the
# candidate listing, output up to SynthesisBudget's cap), so ONE key affords
# ~6 rounds/hour. At the 60s floor a busy session would want 60. That gap is
# why the governor exists rather than a bigger constant: it spends the real
# budget at the real rate and defers only when the next round genuinely will
# not fit, instead of pacing to the worst case around the clock.
#
# SYNAPSE_SYNTHESIS_KEYS scales all of it. The pool is REAL but currently
# unused: `AIC100Provider` rotates INFERENCE_CLOUD_API_KEYS on 429, and
# scripts/local_model_server.py (the proxy the service actually talks to in
# --live) holds a single key with no rotation at all. Activating more keys is
# the only way to hold 60s under sustained load -- ~10 of them, on these
# numbers. Until then the governor degrades gracefully instead of 429ing,
# which is the same "landed, memory unchanged" symptom by another road.
#
# WHAT THIS GOVERNOR DOES NOT SEE, stated because the names above overstate it
# (2026-08-06 review): retrieval shares the SAME provider object, hence the same
# key and the same hourly ceiling, and `/query` is never charged to `_spend`.
# So a team that runs 20 queries and no pushes exhausts the key's request
# budget while `_affordable()` still answers True, and the next merge 429s
# inside the provider. That is a real gap and it is deliberately still open:
# metering retrieval here would let query traffic DEFER synthesis, which is a
# product decision (whose latency gives way to whose?) and not a bug fix. The
# constants keep their SYNTHESIS_ prefix to say exactly what they cover. The
# honest fix is one metered wrapper around the single provider before it is
# split in two -- with a shared ledger and per-component policy -- and it
# belongs with service-side persistence, post-demo.
SYNTHESIS_TOKENS_PER_HOUR = 25_000
SYNTHESIS_REQUESTS_PER_HOUR = 20
SYNTHESIS_KEYS = int(os.environ.get("SYNAPSE_SYNTHESIS_KEYS", 1))

# What one round is assumed to cost before we have measured one. Replaced by
# real usage as soon as a merge reports it -- see `_record_spend`.
ASSUMED_MERGE_TOKENS = 4_000


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


def _asking_contributor(source: Mapping[str, str]) -> str:
    """WHO is asking, as a person. `contributor` if it is there,
    `agent_session` if it is not -- one reader for the query body and the
    watermark query string.

    This is the WATERMARK's key (`store.last_seen`/`mark_seen`), and only
    that, since the 2026-08-06 split (decisions/001): "how much have I not
    seen" is a fact about a person, and ending one conversation to start
    another must not reset it. Suppression reads `_asking_agent_session`
    below instead.

    The `agent_session` fallback is what keeps the wire additive for a client
    that sends no `contributor` at all: `briefing.py` and an un-upgraded
    `orchestrator/server.py` run as separate processes on other people's
    laptops, and reading nothing would make them anonymous -- which does not
    error, it silently resets their watermark. The `or` chain treats an empty
    string as absent, so a client that sends `contributor: ""` for an agent it
    could not identify falls back to its Agent Session id rather than being
    lumped in with every other anonymous asker.
    """
    return source.get("contributor") or source.get("agent_session") or ""


def _asking_agent_session(source: Mapping[str, str]) -> str | None:
    """WHICH CONVERSATION is asking, or None if the request did not say.

    SUPPRESSION's key since the 2026-08-06 split (decisions/001,
    retrieval.visible_to): "is this finding already in the context window
    asking?" is a fact about one conversation, and one machine can now hold
    several. A request that names none falls back to the Contributor
    comparison inside `visible_to` -- which is the pre-W2 behaviour for a
    contributor-only client, and no suppression at all for a wholly anonymous
    one, since an anonymous asker owns nothing.

    Read unconditionally, unlike the `_legacy_agent_session` hatch this
    REPLACES: that one returned None the moment a `contributor` was present,
    because the contributor was the key. Now `agent_session` is the key, so an
    upgraded client sending both gets the conversation comparison, which IS
    the W2 behaviour -- two windows of one human see each other's findings and
    still never their own.
    """
    return source.get("agent_session") or None


def build_app(provider: ModelProvider, *, debug: bool = True,
              merge_min_interval_s: float | None = None) -> Starlette:
    """`debug=True` (the default, preserving every existing call site's
    behavior) mounts `/debug` on this same listener and wraps the provider in
    `RecordingProvider` so the dashboard has something to read.

    `debug=False` is the off switch the plan's "localhost-only" constraint
    needs and never had: `synapse-service` shares one Starlette app between
    the product API and `/debug`, and unlike the worker (which binds `/debug`
    on its own `127.0.0.1`-only socket, gated by `if debug_port:` in cli.py)
    there was no way to run this service with `--host 0.0.0.0` (the
    deployment CONTEXT.md describes) without the debug surface riding along
    on the public listener with no auth. With `debug=False`: no
    RecordingProvider wrapping (the provider passed in is used directly, so
    no 200-entry ring of prompt/output previews accumulates for a service
    instance nobody will ever point a browser at), no `/debug` routes
    mounted, no `Feed` -- matching the worker's `if debug_port:` gating of
    the identical machinery.
    """
    store = InMemoryStore()

    # E6: one CallLog, one Feed, for the whole service instance -- wrapping
    # per component (not per session) is deliberate. RecordingProvider is
    # transparent (same result, exceptions re-raised), so this changes
    # nothing about what synthesis or retrieval actually do; it only gives
    # /debug something to read. Both are None when debug is disabled so
    # nothing retains a call/prompt history no one can ever look at.
    call_log = CallLog() if debug else None
    feed = Feed() if debug else None
    synthesis_provider = (
        RecordingProvider(provider, "synthesis", call_log) if debug else provider
    )
    retrieval_provider = (
        RecordingProvider(provider, "retrieval", call_log) if debug else provider
    )
    synthesizer = Synthesizer(synthesis_provider)

    # Debounce state, per session and per app instance -- see
    # MERGE_MIN_INTERVAL_S. `_pending` carries the findings of every deferred
    # push so the next round still receives them as `new_findings` and they
    # still force their way into the candidate window. Closure scope, not
    # module scope, so two apps in one test process cannot debounce each other.
    _last_merge: dict[str, float] = {}
    _pending: dict[str, list[Finding]] = {}
    # `merge_min_interval_s=0` is the seam that keeps merge-per-push available
    # to tests asserting on incremental synthesis (replay convergence, the
    # push response's `synthesized` flag). Same shape as `debug=` above: the
    # production default lives in the module constant, the parameter overrides
    # it, and nothing has to monkeypatch a global.
    interval_s = (MERGE_MIN_INTERVAL_S if merge_min_interval_s is None
                  else merge_min_interval_s)

    # (monotonic ts, tokens) per completed synthesis round, last hour. App
    # scope rather than per session: the ceiling belongs to the KEY, and two
    # busy sessions share one.
    _spend: list[tuple[float, int]] = []

    # The arrival summary's memo (W5, decisions/004). Closure scope like the
    # debounce state above and for the same reason: two apps in one test
    # process must not serve each other's cached summaries. Invalidation is
    # structural -- the key holds the session's content fingerprint -- so no
    # route has to remember to clear it; see `SummaryCache`.
    _summaries = SummaryCache()

    def _record_spend() -> None:
        """Charge the round that just ran, at its real cost when the provider
        reported one. `last_usage` is set even for a schema-invalid verdict --
        a truncated response is billed like any other.

        ⟨CORRECTED 2026-08-06⟩ Charge only rounds that actually reached the
        provider. `Synthesizer.merge` returns at `if not candidates` -- a
        session with nothing retrievable -- without making a request, and both
        call sites charged it anyway: with `last_usage` still None that is a
        full ASSUMED_MERGE_TOKENS for zero spend. `POST /synthesize` is public
        and ungated, so six calls against an EMPTY session pushed `_spend` to
        24,000 of the 25,000/hour ceiling with no model call made, and the next
        genuine push -- on a DIFFERENT session, because `_spend` is app-scoped
        -- came back `deferred: true`. A governor that defers real synthesis on
        the strength of rounds that spent nothing produces the exact "findings
        landed, memory unchanged" symptom it was added to make visible.
        `cmd_resync` POSTs /synthesize once per session in the backlog, so one
        resync could book several phantom rounds in a single command.

        The three states are now distinct, and each is charged for what it
        really cost: no call (nothing), a call whose usage came back (its real
        cost), a call that raised or reported nothing (the assumed cost -- the
        tokens went out regardless of what came back).
        """
        if not getattr(synthesizer, "last_call_made", True):
            return
        usage = getattr(synthesizer, "last_usage", None)
        tokens = ASSUMED_MERGE_TOKENS
        if usage is not None:
            tokens = max(1, usage.input_tokens + usage.output_tokens)
        _spend.append((time.monotonic(), tokens))

    def _affordable() -> tuple[bool, str]:
        """Whether one more synthesis round fits the hour's remaining budget.

        Charges the NEXT round at the most expensive recent one rather than an
        average: merge cost grows with the candidate listing, so an average
        lags the trend and the governor would keep authorising rounds the key
        can no longer pay for. Requests are checked at 2 per round because
        AIC100Provider retries once internally, and a failing round -- the
        expensive kind -- always takes that retry.
        """
        cutoff = time.monotonic() - 3600
        while _spend and _spend[0][0] < cutoff:
            _spend.pop(0)
        tokens_spent = sum(t for _, t in _spend)
        next_cost = max([t for _, t in _spend[-5:]] or [ASSUMED_MERGE_TOKENS])
        token_budget = SYNTHESIS_TOKENS_PER_HOUR * SYNTHESIS_KEYS
        request_budget = SYNTHESIS_REQUESTS_PER_HOUR * SYNTHESIS_KEYS
        if tokens_spent + next_cost > token_budget:
            return False, (f"token budget: {tokens_spent}+{next_cost} would exceed "
                           f"{token_budget}/hour across {SYNTHESIS_KEYS} key(s)")
        if (len(_spend) + 1) * 2 > request_budget:
            return False, (f"request budget: {len(_spend)} round(s) this hour against "
                           f"{request_budget}/hour, 2 requests each worst case")
        return True, ""

    def _session_or_404(sid: str):
        return store.get_session(sid)

    def _unavailable(sid: str) -> JSONResponse | None:
        """THE liveness gate. 404 if this session never existed, 409
        `{"error": "session_ended"}` if it has been closed, None if it is
        live -- every route that serves the memory or extends it goes through
        this one function (2026-08-06, session lifecycle spec).

        One helper rather than an `if store.get_context(sid).status is ...`
        repeated in four handlers, because the failure mode of the inline
        version is a FIFTH route added later that quietly omits it: writes to
        a session the team has closed would be accepted with a 200 and
        disappear into a log nothing reads. The uniform shape is also what
        lets the orchestrator recognise "ended" once (status + error body)
        instead of per route.

        404 is checked first: an unknown session is not an ended one, and
        reporting `session_ended` for a typo'd id would send a client down the
        "clear the binding, it's over" path for a session that is fine.

        Deliberately NOT applied to `add_member`, `POST /end` or
        `DELETE /members/{c}`. Ending twice must stay idempotent rather than
        409, and a member of a session that closed underneath them must still
        be able to leave it -- that DELETE is exactly how a client cleans up
        after seeing the 409 here.
        """
        if store.get_session(sid) is None:
            return JSONResponse({"error": f"unknown session {sid}"}, status_code=404)
        if store.get_context(sid).status is SessionStatus.ENDED:
            return JSONResponse({"error": "session_ended"}, status_code=409)
        return None

    def _record_synthesis_feed(sid: str, log_before: int) -> None:
        """Diff the log around the `merge()` call just made, rather than
        having synthesis.py report counts itself -- keeps this instrumentation
        entirely in api.py, matching the plan's Files list for this task.
        A no-op when debug is disabled (`feed is None`)."""
        if feed is None:
            return
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
        """`created_by` is a REQUIRED KEY whose VALUE may be null (2026-08-06).

        `_missing` tests key PRESENCE, not truthiness, which is exactly the
        split this needs: `{"created_by": null}` means "recreated after a
        restart and the caller genuinely does not know who owns this" and is
        accepted, while omitting the key is still a 422 so a typo or a
        half-written client cannot silently mint an ownerless session. Not
        knowing has to be asserted; it must never be the default.

        Nothing is done to STOP a null here landing on a session that already
        has a real creator, because nothing needs to be: `store.create_session`
        is create-or-return and hands the existing session back untouched. See
        its docstring.
        """
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
        """Join. The response also carries `created_by` and `purpose`
        (2026-08-06) so the joining orchestrator can RECORD who owns the
        session it just joined.

        Those two fields are the whole of a Shared Session's identity and, on
        a machine that joined rather than created, this response was the only
        place they were ever visible -- and it dropped them. So after a service
        restart the joiner's `resync` had nothing to restore from and invented
        a creator instead (`cli.py:261`, `created_by="resync"`), which is the
        defect `SynapseSession.created_by`'s comment describes. Returning them
        here is what lets a joiner keep the same local record a creator keeps,
        from the one call it is guaranteed to make.

        Additive: `members` is unchanged in name, shape and position, so an
        un-upgraded client reads exactly what it read before.
        """
        sid = request.path_params["sid"]
        if _session_or_404(sid) is None:
            return JSONResponse({"error": f"unknown session {sid}"}, status_code=404)
        body = await request.json()
        if (err := _missing(body, "contributor")) is not None:
            return err
        store.add_member(sid, body["contributor"])
        session = store.get_session(sid)
        return JSONResponse({"members": session.members,
                             "created_by": session.created_by,
                             "purpose": session.purpose})

    async def end_session(request: Request) -> JSONResponse:
        """Close a Shared Session for everyone. CREATOR ONLY -- or, when the
        creator is unknown, MEMBER ONLY.

        Layer 2 of the three the spec puts around this call (the harness
        permission prompt is layer 1, and "refuse while other members are
        present" is layer 3, in the orchestrator where the member list is
        already rendered to a human). This layer is in the SERVICE and not in
        the client on purpose: it is the only one an agent cannot talk its way
        past, and closing a session is the one operation that can destroy the
        team's memory for everyone at once.

        ⟨AMENDED 2026-08-06⟩ `created_by` became `str | None`, so this gate has
        two arms:

          * a real creator  -> creator only, 403 otherwise. UNCHANGED.
          * `created_by is None` ("recreated after a restart, nobody here knows
            who owned it") -> any CURRENT MEMBER may end it; a non-member gets
            a 403 that says membership is required and that the creator was
            lost to a restart.

        WHY membership is a proportionate fallback: there is no auth anywhere
        in Synapse. `ended_by` is self-asserted, exactly like `contributor` on
        every other route, and `POST .../members` is ungated -- anyone who can
        reach this route can make themselves a member first. So this gate has
        never been a control against an attacker and cannot become one here;
        it is a guardrail against ACCIDENT, and the accidents it catches
        (ending the wrong session id, an agent closing a session it merely
        joined) are caught just as well by membership. The two alternatives
        are both worse: leaving a restart-recovered session permanently
        UNENDABLE punishes the team for the service's lack of durability, and
        inventing a creator is the defect this whole change removes -- it
        refuses the real owner and admits everyone who reads the source.

        Ending an already-ended session is a 200, not a 409. The fold keeps
        the first `SessionEnded`, so a retried POST -- the ordinary outcome of
        a dropped response on a call nobody wants to leave ambiguous -- is
        inert and reports the ORIGINAL closer.
        """
        sid = request.path_params["sid"]
        session = _session_or_404(sid)
        if session is None:
            return JSONResponse({"error": f"unknown session {sid}"}, status_code=404)
        try:
            body = await request.json()
        except ValueError:
            # An absent or unparseable body reaches `_missing` as {} and comes
            # back a 422. Without this it is an unhandled 500, and _missing's
            # own docstring says why that is the wrong answer: E4's Relay
            # retries 5xx forever against a request that can never succeed.
            body = {}
        if (err := _missing(body, "ended_by")) is not None:
            return err
        ended_by = body["ended_by"]
        if session.created_by is not None:
            if ended_by != session.created_by:
                # Names the creator rather than saying "forbidden": the caller
                # is an agent relaying this to a human who now knows who to ask.
                return JSONResponse(
                    {"error": f"only {session.created_by} can end this session"},
                    status_code=403)
        elif ended_by not in session.members:
            # The None arm. `is not None` above rather than truthiness, so a
            # creator legitimately named "" is still compared as a creator and
            # never falls through to here.
            #
            # The message says WHY, in both halves: an agent that only read
            # "membership is required" would report a permissions problem to
            # its human, when what actually happened is that a service restart
            # lost the session's owner and joining is the remedy.
            return JSONResponse(
                {"error": "this session's creator was lost to a service "
                          "restart, so ending it requires being a current "
                          f"member of it; {ended_by!r} is not one"},
                status_code=403)
        attributed_to = store.end_session(sid, ended_by)
        return JSONResponse({"shared_id": sid, "status": SessionStatus.ENDED.value,
                             "ended_by": attributed_to})

    async def leave_session(request: Request) -> JSONResponse:
        """Detach ONE member. The session stays open for everyone else and
        their findings stay in the log, attributed to them (store.remove_member).

        Not gated on status: leaving a session that closed underneath you is
        exactly how a client cleans up after a 409, so a guard here would trap
        members inside a dead session. Idempotent for a contributor who is not
        a member, because a retried DELETE must not be an error.
        """
        sid = request.path_params["sid"]
        if _session_or_404(sid) is None:
            return JSONResponse({"error": f"unknown session {sid}"}, status_code=404)
        store.remove_member(sid, request.path_params["contributor"])
        return JSONResponse({"members": store.get_session(sid).members})

    async def push_findings(request: Request) -> JSONResponse:
        sid = request.path_params["sid"]
        if (gate := _unavailable(sid)) is not None:
            return gate
        body = await request.json()
        try:
            findings = [Finding.model_validate(f) for f in body.get("findings", [])]
        except ValidationError as exc:
            return JSONResponse({"error": str(exc)}, status_code=422)
        accepted = store.upsert(sid, findings)
        version_before = store.get_context(sid).memory_version
        synthesized = False
        deferred = False
        if accepted:
            # DEBOUNCE. Synthesis is the only thing here that costs a model
            # call, and the hosted synthesis key allows 20 requests and 25,000
            # tokens per hour. One merge runs ~3,250 tokens, so the TOKEN
            # ceiling binds first at roughly 7 rounds/hour -- and a merge whose
            # verdict fails costs TWO requests, because AIC100Provider retries
            # internally. Merging on every push was fine when pushes were
            # occasional; a live `synapse-worker run` pushes every couple of
            # minutes, which is ~30 rounds/hour and blows through both limits.
            #
            # Nothing is lost by waiting. The findings are already upserted
            # above, so /query and the watermark see them IMMEDIATELY -- only
            # the synthesized working memory lags, and it catches up on the
            # next round with the whole accumulated batch. The pending list is
            # what makes that true: a deferred push's findings are carried
            # forward so they still arrive as `new_findings` and still force
            # their way into the candidate window (synthesis.merge's
            # docstring), rather than aging out unsynthesized.
            pending = _pending.setdefault(sid, [])
            pending.extend(findings)
            now = time.monotonic()
            last = _last_merge.get(sid)
            if last is not None and (now - last) < interval_s:
                deferred = True
                logger.info(
                    "Synthesis for %s deferred: %.0fs since last round (minimum "
                    "%.0fs), %d finding(s) pending. They are queryable now; the "
                    "working memory folds them in next round.",
                    sid, now - last, interval_s, len(pending))
            elif not (afford := _affordable())[0]:
                # The floor has passed and we still will not spend. Distinct
                # from the interval case in the log because the fix is
                # different: this one is answered by more keys
                # (SYNAPSE_SYNTHESIS_KEYS + INFERENCE_CLOUD_API_KEYS), never
                # by lowering SYNAPSE_MERGE_MIN_INTERVAL_S.
                deferred = True
                logger.warning(
                    "Synthesis for %s deferred on BUDGET, not latency (%s). %d "
                    "finding(s) pending and queryable; the working memory is "
                    "behind until the hour rolls or more keys are activated.",
                    sid, afford[1], len(pending))
        if accepted and not deferred:
            findings = _pending.pop(sid, findings)
            _last_merge[sid] = time.monotonic()
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
            _record_spend()
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
        # `deferred` distinguishes "we ran a merge and it did not move the
        # version" (a failed verdict -- the truncation bug) from "we chose not
        # to run one yet". Both leave `synthesized` false, and conflating them
        # is what made the 2026-08-06 failure so hard to see from the outside.
        return JSONResponse({"accepted": accepted, "memory_version": version,
                             "synthesized": synthesized, "deferred": deferred,
                             "pending": len(_pending.get(sid, []))})

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
        converges to the same state as the original incremental stream.

        Since the debounce landed (2026-08-06) this is also the FORCE-NOW
        override: it ignores MERGE_MIN_INTERVAL_S and flushes whatever
        `push_findings` has been holding back. An operator who does not want to
        wait out the interval -- mid-demo, most obviously -- has one call that
        settles it, and the rate limit stays an automatic default rather than
        a thing anyone has to fight. Draining `_pending` here (rather than
        merging `[]` and leaving it queued) is what stops the next push from
        re-offering findings this round already synthesized."""
        sid = request.path_params["sid"]
        if (gate := _unavailable(sid)) is not None:
            return gate
        version_before = store.get_context(sid).memory_version
        log_before = len(store.log_entries(sid))
        pending = _pending.pop(sid, [])
        _last_merge[sid] = time.monotonic()
        await synthesizer.merge(store, sid, pending)
        # Charged like any other round: this route is the operator override
        # for the CLOCK, not a way to spend budget that is not there.
        _record_spend()
        _record_synthesis_feed(sid, log_before)
        version = store.get_context(sid).memory_version
        return JSONResponse({"memory_version": version,
                             "synthesized": version > version_before,
                             "flushed": len(pending)})

    async def watermark(request: Request) -> JSONResponse:
        sid = request.path_params["sid"]
        if (gate := _unavailable(sid)) is not None:
            return gate
        contributor = _asking_contributor(request.query_params)
        asking_session = _asking_agent_session(request.query_params)
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
        #
        # ⟨SPLIT 2026-08-06, decisions/001⟩ The CONTENT fields suppress by the
        # asking CONVERSATION and the CHANGE fields still count by the asking
        # PERSON: `new_since` below reads `store.last_seen(sid, contributor)`,
        # deliberately unchanged, so ending one conversation and starting
        # another does not replay the whole memory at you.
        visible = visible_to(store.retrievable(sid),
                             asking_agent_session=asking_session,
                             asking_contributor=contributor)
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
            "new_since": ctx.memory_version - store.last_seen(sid, contributor),
            "by_type": dict(by_type),
            "conflicts": conflicts,
            "topics": [{"id": t.topic_id, "size": t.size, "label": t.label}
                       for t in topics],
            "purpose": ctx.purpose,
            "members": list(store.get_session(sid).members),
        })

    async def arrival(request: Request) -> JSONResponse:
        """What a JOINER is told (W5). Two parts: what has accumulated, and
        what is new since this person last read the memory.

        Sits alongside `/watermark` rather than inside it. The watermark is a
        HEADLINE — counts and a version delta, composed into the MCP server's
        `instructions` on every connection and refreshed on a timer, so it has
        to stay small and cheap. This is a body, fetched once, at join, and
        rendered into a tool result the joining agent reads out loud. Merging
        them would make every ten-second briefing refresh pay for a summary
        nobody asked for, and would push finding prose into `instructions`,
        which `briefing.py` caps at 1200 characters precisely to keep it out.

        DOES NOT `mark_seen`. Reading the summary is not reading the memory:
        this route is idempotent, an orchestrator may call it on a join that
        then fails to bind, and moving somebody's watermark here would mean the
        first call silently erased the "new since" the second one is for. The
        watermark still moves where it always did -- on `/query`.

        Identity is read exactly as `/watermark` reads it: `contributor` for
        the watermark half, `agent_session` for suppression (decisions/001),
        with the same `_asking_*` fallbacks, so an un-upgraded client that
        sends only one of them is not anonymous here either.
        """
        sid = request.path_params["sid"]
        if (gate := _unavailable(sid)) is not None:
            return gate
        summary = _summaries.get(
            store, sid,
            contributor=_asking_contributor(request.query_params),
            agent_session=_asking_agent_session(request.query_params))
        return JSONResponse(summary.to_json())

    async def query(request: Request) -> JSONResponse:
        sid = request.path_params["sid"]
        if (gate := _unavailable(sid)) is not None:
            return gate
        body = await request.json()
        if (err := _missing(body, "query")) is not None:
            return err
        contributor = _asking_contributor(body)
        asking_session = _asking_agent_session(body)

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
        allowed = visible_to(visible, asking_agent_session=asking_session,
                             asking_contributor=contributor)

        suppressed = frozenset(f.id for f in visible) - {f.id for f in allowed}
        if len(allowed) <= TOP_K:
            # THE BYPASS, on MEMBERSHIP only. Everything the asker may see
            # already fits in one prompt, so selecting a subset of it can only
            # lose recall -- and the selectors available here (BM25, symbol
            # overlap, a HashingEmbedder with no paraphrase signal) are weaker
            # at this scale than the 8B reading all of it.
            #
            # ⟨AMENDED 2026-08-05⟩ It used to bypass ORDER as well, and those
            # are separable. `candidates = allowed` handed the model the
            # arrival order, so below TOP_K the route had no ordering, no
            # relevance signal and no drops -- none of the three -- which is
            # the small-session case, i.e. every demo and every new team. The
            # system was weakest exactly where it is most watched.
            #
            # Measured on the six findings then in shared memory, asked
            # "Cirrascale API key rejected / 401 unauthorized": the lanes put
            # the one relevant finding at rank 1 for 7 of 10 phrasings and in
            # the top 3 for 10 of 10, with NO model involved. The shipped path
            # returned it sixth of six every time. That ordering was being
            # computed-for-free and thrown away.
            #
            # So: run the lanes to ORDER, then append anything they did not
            # surface, in arrival order. Recall below TOP_K is provably
            # unchanged -- every allowed finding still reaches the prompt --
            # while the model now reads them best-first instead of
            # oldest-first. Above TOP_K nothing here changes at all.
            ordered = store.candidates(sid, body["query"],
                                       top_k=max(TOP_K, len(allowed)),
                                       exclude=suppressed).candidates
            by_id = {c.finding.id: c.finding for c in ordered}
            candidates = list(by_id.values()) + [
                f for f in allowed if f.id not in by_id
            ]
        else:
            cands = store.candidates(sid, body["query"], top_k=TOP_K, exclude=suppressed)
            candidates = [c.finding for c in cands.candidates]

        try:
            ranked = await query_findings(
                retrieval_provider,
                context=store.get_context(sid),
                candidates=candidates,
                query=body["query"],
                asking_agent_session=asking_session,
                asking_contributor=contributor,
            )
        except RetrievalUnavailable as exc:
            # 503, not 502: this service is healthy and its DEPENDENCY is not.
            # Relay's 5xx-retryable classification never sees this — Relay only
            # posts /findings, never /query.
            #
            # Two side effects here are load-bearing and both are achieved by
            # control flow rather than by a flag: `store.mark_seen` below is
            # NOT reached, so a query that returned nothing does not advance
            # the asker's watermark (they did not see anything, so the next
            # briefing must still call it new); and the debug feed gets a
            # DISTINCT event, so /debug shows a dead brain as dead instead of
            # as a quiet query that ranked zero.
            if feed is not None:
                feed.event(
                    "query_failed",
                    f"{sid}: retrieval provider {exc.provider_id} DOWN — "
                    f"{exc.cause_name}",
                    session=sid,
                    asked_by=contributor or "anonymous",
                    provider=exc.provider_id,
                    cause=exc.cause_name,
                )
            return JSONResponse(
                {"error": "retrieval_unavailable", "provider": exc.provider_id,
                 "detail": str(exc)},
                status_code=503)
        # By CONTRIBUTOR, unchanged by the split: the watermark is a fact
        # about a person, so a query from either of one human's windows moves
        # the place they resume from.
        store.mark_seen(sid, contributor)
        if feed is not None:
            # Counts alone could not answer "what did the asker actually get
            # back?", which is the only thing an operator watching a query
            # wants to know — and with suppression in play the interesting
            # case is precisely WHICH findings were withheld from whom.
            withheld = len(visible) - len(allowed)
            feed.event(
                "query",
                f"{sid}: '{body['query'][:60]}' -> {len(candidates)} candidates, "
                f"{len(ranked)} ranked"
                + (f", {withheld} suppressed for {contributor or 'anonymous'}"
                   if withheld else ""),
                session=sid,
                # The feed key stays `asked_by` -- the debug page's JS reads
                # `d.asked_by` (debug.py:549) and this is a Contributor now,
                # which is what an operator wanted to read there anyway.
                asked_by=contributor or "anonymous",
                candidates=len(candidates),
                ranked=len(ranked),
                suppressed=withheld,
                returned=[
                    {"id": f.id, "type": f.type.value, "text": f.text[:160],
                     "from": [a.contributor for a in f.attributions]}
                    for f in ranked
                ],
            )
        return JSONResponse({"findings": [f.model_dump(mode="json") for f in ranked]})

    routes = [
        Route("/v1/sessions", create_session, methods=["POST"]),
        Route("/v1/sessions/{sid}/members", add_member, methods=["POST"]),
        Route("/v1/sessions/{sid}/members/{contributor}", leave_session,
              methods=["DELETE"]),
        Route("/v1/sessions/{sid}/end", end_session, methods=["POST"]),
        Route("/v1/sessions/{sid}/findings", push_findings, methods=["POST"]),
        Route("/v1/sessions/{sid}/synthesize", synthesize, methods=["POST"]),
        Route("/v1/sessions/{sid}/watermark", watermark, methods=["GET"]),
        Route("/v1/sessions/{sid}/arrival", arrival, methods=["GET"]),
        Route("/v1/sessions/{sid}/query", query, methods=["POST"]),
    ]
    if debug:
        routes.extend(debug_routes(store, call_log, feed))

    app = Starlette(routes=routes)
    app.state.store = store          # test seam: no route reads it
    # test seam: lets a test confirm no CallLog exists at all when debug is
    # disabled (not merely that it's unreachable) -- no route reads this.
    app.state.call_log = call_log
    return app
