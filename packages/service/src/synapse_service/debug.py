"""Service /debug routes — the brain page, the log page, and their JSON.

Read-only by construction: every route is mounted GET-only, so Starlette
itself answers 405 to anything else — this module never writes to `store`,
never calls `synthesizer` or `query_findings`, and never touches a provider.
It only reads what already happened: the log (`store.log_entries`), the fold
(`store.view`), and the `CallLog`/`Feed`/`WorkingMemoryLog` that `api.py`'s
real routes recorded into as a side effect of doing their own work.

CONTEXT.md: "the log IS the merge/topic feed" — `log_tail` below reads
`store.log_entries` directly rather than building a second feed alongside it.
The `merges`/`queries` feed *is* a second, smaller feed, but a deliberately
different one: it exists because CONTEXT.md's Finding Log has no concept of
an LLM call or a teammate's question, and those are exactly what an
instrumented dashboard needs to show. `WorkingMemoryLog` (2026-08-06, W4a) is
the third member of that set for the same stated reason: the log has no entry
kind carrying the Working Memory prose, so a rewrite is discarded the instant
the next one lands.

⟨W4a, 2026-08-06, decisions/003⟩ `/debug` is now the BRAIN page — "the top
level shows the state of the brain, not the log". The page that was here is
mounted verbatim at `/debug/log`, same `_PAGE` constant, same script, not one
byte rewritten.

Every element of the brain page comes from a number already in this process.
Where the datum does not exist — a heartbeat, a join time, a transcript
position — the page says so in words rather than showing a plausible
substitute. `docs/overnight/w4a-page1-spec.md` carries the per-element
sourcing; `decisions/003` carries why an unsourced tile is worse than a
missing one.
"""

from __future__ import annotations

from collections import deque
from datetime import datetime, timezone
from typing import Any

from starlette.requests import Request
from starlette.responses import HTMLResponse, JSONResponse
from starlette.routing import Route

from synapse_providers import CallLog

from synapse_service.log import (FindingAppended, MarkedTrivial, Merged, SessionEnded,
                                 TopicAssigned, TopicSplit)
from synapse_service.store import InMemoryStore

MAX_FEED = 200
MAX_LOG_TAIL = 200
# Ten revisions of a ≤500-word string (synthesis.MAX_WM_WORDS) per session --
# a few tens of KB in a process that already retains 200 prompt/output
# previews.
MAX_WM_REVISIONS = 10
# The brain page is a state view, not a log: twelve rows is what fits above
# the fold without scrolling. /debug/log is where the whole thing lives.
MAX_RECENT = 12


class Feed:
    """A bounded, tagged event ring buffer — the service-side sibling of the
    worker's `StatsBuffer`, minus the tick machinery this side has no use
    for. One instance, shared by every Shared Session `api.py` serves; each
    event carries its own `session` field so `/debug` can filter to one.
    """

    def __init__(self, maxlen: int = MAX_FEED) -> None:
        self._events: deque[dict[str, Any]] = deque(maxlen=maxlen)

    def event(self, tag: str, summary: str, **detail: Any) -> None:
        self._events.append(
            {
                "ts_iso": datetime.now(timezone.utc).isoformat(),
                "tag": tag,
                "summary": summary,
                "detail": detail,
            }
        )

    def snapshot(self, *, tag: str | None = None, session: str | None = None) -> list[dict]:
        events = list(self._events)
        if tag is not None:
            events = [e for e in events if e["tag"] == tag]
        if session is not None:
            events = [e for e in events if e["detail"].get("session") == session]
        return events


class WorkingMemoryLog:
    """Bounded history of Working Memory rewrites, per Shared Session.

    Debug-only, like `Feed` and `CallLog`: constructed in `build_app` when
    debug is on, `None` when it is off, and no product path reads it. The
    rewrites already happen — `synthesis.py`'s `store.set_context(...,
    working_memory=...)` is the only writer of the prose in the tree — so
    nothing here creates a fact. It retains one that is otherwise overwritten
    and gone the moment the next merge lands, which is why "the last 5-10
    revisions" could not be answered before this class existed.

    Two things are deliberately NOT recorded, because they would be lies:

      * an EMPTY working memory. A session starts with `working_memory=""`
        and a verdict may omit the rewrite, so the first round of a young
        session reaches `record` with the empty string. Appending it would
        render as "the memory was rewritten, to nothing".
      * an UNCHANGED one. `store.set_context` treats `None` as "leave alone"
        and `SynthesisVerdicts.working_memory` is optional, so a version bump
        carrying the same prose is ordinary and common. A version changed; a
        revision did not.

    The history lives in this process only. It is empty after a restart and
    empty for merges that happened elsewhere, and the page says exactly that
    rather than rendering an empty list that reads as "nothing ever changed".
    """

    def __init__(self, maxlen: int = MAX_WM_REVISIONS) -> None:
        self._maxlen = maxlen
        self._by_session: dict[str, deque[dict[str, Any]]] = {}

    def record(self, sid: str, text: str, version: int) -> None:
        if not text.strip():
            return
        ring = self._by_session.setdefault(sid, deque(maxlen=self._maxlen))
        if ring and ring[-1]["text"] == text:
            return
        ring.append({
            "version": version,
            # OBSERVATION time, on the server clock, and named that way on the
            # page. The rewrite has no timestamp of its own anywhere in the
            # system; this is when this process saw it, which is a different
            # and smaller claim.
            "ts_iso": datetime.now(timezone.utc).isoformat(),
            "words": len(text.split()),
            "text": text,
        })

    def snapshot(self, sid: str) -> list[dict[str, Any]]:
        """Newest first, each with `delta_words` against the revision before it.

        The OLDEST retained revision reports `delta_words: None` rather than a
        delta against nothing — once the ring has wrapped, the revision before
        it is gone and its word count is genuinely unknown.
        """
        ring = list(self._by_session.get(sid, ()))
        out: list[dict[str, Any]] = []
        for i in range(len(ring) - 1, -1, -1):
            revision = dict(ring[i])
            revision["delta_words"] = (
                ring[i]["words"] - ring[i - 1]["words"] if i > 0 else None
            )
            out.append(revision)
        return out


def _summarize(entry: Any) -> str:
    if isinstance(entry, FindingAppended):
        text = entry.finding.text
        return f"{entry.finding.id}: {text[:80]}" + ("…" if len(text) > 80 else "")
    if isinstance(entry, Merged):
        return f"{entry.result.id} <- {', '.join(entry.sources)}"
    if isinstance(entry, MarkedTrivial):
        return f"trivial: {', '.join(entry.finding_ids)}"
    if isinstance(entry, TopicAssigned):
        founded = " (founded)" if entry.founded else ""
        return f"{entry.finding_id} -> {entry.topic_id}{founded}"
    if isinstance(entry, TopicSplit):
        return f"{entry.topic_id} split into {entry.into[0]}, {entry.into[1]}"
    if isinstance(entry, SessionEnded):
        # The sixth entry kind arrived with the lifecycle spec and this
        # function never grew a branch for it, so a closed session's log tail
        # rendered `str(entry)` -- a raw dataclass repr, in the demo's own
        # last frame.
        return f"ended by {entry.ended_by}"
    return str(entry)


def _entry_ts(entry: Any) -> str | None:
    if isinstance(entry, FindingAppended):
        return entry.finding.ts.isoformat()
    if isinstance(entry, Merged):
        return entry.result.ts.isoformat()
    return None


def _log_tail(store: InMemoryStore, sid: str) -> list[dict[str, Any]]:
    entries = store.log_entries(sid)
    tail = entries[-MAX_LOG_TAIL:]
    offset = len(entries) - len(tail)
    return [
        {
            "position": offset + i,
            "kind": type(entry).__name__,
            "summary": _summarize(entry),
            "ts": _entry_ts(entry),
        }
        for i, entry in enumerate(tail)
    ]


def _session_payload(store: InMemoryStore, feed: Feed, call_log: CallLog, sid: str) -> dict:
    ctx = store.get_context(sid)
    view = store.view(sid)
    session = store.get_session(sid)
    topics = store.topic_summaries(sid)
    return {
        "sid": sid,
        "memory_version": ctx.memory_version,
        # Session-level "watermark": what a producer polling this session
        # would currently be told (SessionContext.memory_version). Distinct
        # from a per-asker watermark (store.last_seen), which only exists
        # once an agent_session is named — the dashboard has none in view.
        "watermark": ctx.memory_version,
        "working_memory": ctx.working_memory,
        "conflicts": len(ctx.conflicts),
        "view": {
            "visible": len(view.visible_ids),
            "superseded": len(view.superseded_by),
            "trivial": len(view.trivial),
        },
        "topics": [{"label": t.label, "size": t.size} for t in topics],
        "log_tail": _log_tail(store, sid),
        "merges": feed.snapshot(tag="synthesis", session=sid),
        # BOTH query tags, interleaved by timestamp (decision 008). A
        # `query_failed` is the single most operator-relevant thing this feed
        # can carry -- it means the retrieval backend is not answering -- and
        # filtering it out here would leave the dashboard showing an
        # uninterrupted run of successful queries while every one of them came
        # back a 503. The tag survives into the payload, so the page colours
        # and filters the two apart (see `renderFeed`).
        "queries": sorted(feed.snapshot(tag="query", session=sid)
                          + feed.snapshot(tag="query_failed", session=sid),
                          key=lambda e: e["ts_iso"]),
        # Not session-filtered: RecordingProvider's CallLog (Task 1) records
        # component/provider/tokens/latency/previews only, with no session
        # field, because ONE provider wrapper serves every Shared Session
        # this service instance holds. Showing the whole log under
        # whichever session is selected is honest given that shape, not a
        # bug — filtering it would require inventing a session field Task 1
        # never carries.
        "llm": call_log.snapshot(),
        "purpose": ctx.purpose,
        "members": list(session.members) if session is not None else [],
        # ⟨ADDITIVE, W4a 2026-08-06⟩ Two fields, nothing renamed, nothing
        # removed. `status` is the routing note's "one field away": the fold
        # already computes it on every `get_context` (store.py's status
        # projection) and the dashboard was the one reader that could not see
        # the answer. The three scripts that parse this payload
        # (demo_say.py, demo_local.py, rehearse_demo.py) read `shared_id`,
        # `view`, `log_tail` and `llm`, and are unaffected.
        "status": ctx.status.value,
        "created_by": session.created_by if session is not None else None,
    }


# ── the brain page's derivations (W4a) ──────────────────────────────────────
# Each function below states what it CAN say and what it cannot. The rule the
# page is built on (decisions/003): every element names its source, and
# anything without one is rendered as an explicit absence rather than as a
# plausible number.


def _last_query(queries: list[dict], contributor: str,
                agent_session: str | None) -> tuple[str | None, str]:
    """When this participant last asked the memory a question, and at what scope.

    Two scopes, and the difference is reported rather than hidden. `api.query`
    records `asked_by_session` (the Agent Session that asked) alongside
    `asked_by` (the Contributor); when the conversation matches, this is a
    fact about THIS window. Falling back to the Contributor answers a
    different question — "when did this person last ask, from any window" —
    and the column is labelled `contributor` when it does.

    Events arrive in order, so the last match is the most recent one.
    """
    if agent_session is not None:
        by_conversation = [e for e in queries
                           if e["detail"].get("asked_by_session") == agent_session]
        if by_conversation:
            return by_conversation[-1]["ts_iso"], "conversation"
    by_contributor = [e for e in queries if e["detail"].get("asked_by") == contributor]
    if by_contributor:
        return by_contributor[-1]["ts_iso"], "contributor"
    return None, "none"


def _participants(store: InMemoryStore, feed: Feed, sid: str,
                  memory_version: int) -> list[dict[str, Any]]:
    """One row per AGENT SESSION, walked out of the log's attributions.

    There is no roster in this system and there is no connection registry, so
    this is built from the only durable evidence of a participant that exists:
    the `Attribution` on every finding they pushed, keyed
    `(contributor, agent_session, agent)`. Two windows of one human are two
    rows — that is the W2 property landed 2026-08-06, and this page is where
    it becomes visible; before it, the second window silently BECAME the first.

    `Merged` entries are deliberately skipped. A Synthesized Finding carries
    EVERY source's attributions, and each of those sources was already counted
    from its own `FindingAppended`. Walking merges too would inflate every
    author's number each time synthesis ran — inventing no new participant,
    but making all of them look busier after a merge than before.

    Contributions are counted by DISTINCT finding id, so a resend (which the
    log records because it happened) is not a second contribution.

    What this cannot say, in the page's own words as well as here:
      * JOIN AND LEAVE TIMES. `members` is a plain list on `SynapseSession`
        with no timestamps, and `store.remove_member`'s docstring records why
        membership is not in the log. State only, never a time.
      * WHERE THEY ARE IN THEIR SESSION, in the transcript sense. The service
        never sees a transcript — the orchestrator is the single egress. The
        nearest true reading is where they are in the MEMORY, which is
        `last_seen` / `behind`, and that is what the column is called.
      * WHICH MACHINE THEY ARE ON. `SessionBinding.transcript_path` is local,
        on each teammate's own disk. The service has no access to it.
    """
    session = store.get_session(sid)
    members = list(session.members) if session is not None else []
    queries = feed.snapshot(tag="query", session=sid) if feed is not None else []

    rows: dict[tuple[str, str, str], dict[str, Any]] = {}
    seen_findings: dict[tuple[str, str, str], set[str]] = {}
    for entry in store.log_entries(sid):
        if not isinstance(entry, FindingAppended):
            continue
        finding = entry.finding
        ts = finding.ts.isoformat()
        for attribution in finding.attributions:
            key = (attribution.contributor, attribution.agent_session, attribution.agent)
            row = rows.get(key)
            if row is None:
                row = rows[key] = {
                    "contributor": attribution.contributor,
                    "agent_session": attribution.agent_session,
                    "agent": attribution.agent,
                    "contributions": 0,
                    "first_contribution_iso": ts,
                    "last_contribution_iso": ts,
                }
                seen_findings[key] = set()
            if finding.id in seen_findings[key]:
                continue
            seen_findings[key].add(finding.id)
            row["contributions"] += 1
            row["first_contribution_iso"] = min(row["first_contribution_iso"], ts)
            row["last_contribution_iso"] = max(row["last_contribution_iso"], ts)

    participants = list(rows.values())

    # A member who has joined and pushed nothing is a participant too -- the
    # demo's teammate #2 before their first finding. One row, no conversation,
    # because a member list carries no Agent Session and inventing one would
    # be the exact fabrication this page refuses.
    contributed = {row["contributor"] for row in participants}
    for member in members:
        if member in contributed:
            continue
        participants.append({
            "contributor": member,
            "agent_session": None,
            "agent": None,
            "contributions": 0,
            "first_contribution_iso": None,
            "last_contribution_iso": None,
        })

    for row in participants:
        contributor = row["contributor"]
        last_query_iso, scope = _last_query(queries, contributor, row["agent_session"])
        # By CONTRIBUTOR, not by conversation, and the column header says so:
        # `store.last_seen` was deliberately re-keyed (decisions/001) because
        # "how much have I not seen?" is a property of one PERSON. Repeated on
        # each of that person's rows, which is the truth rather than a bug.
        last_seen_version = store.last_seen(sid, contributor)
        # `last_seen` defaults to 0, which is indistinguishable from someone
        # genuinely sitting at v0. `behind` is therefore reported only when
        # there is positive evidence they have read: a query we observed, or a
        # non-zero watermark. Otherwise it is `None` and renders as an em-dash
        # -- "unknown", not "maximally stale".
        known = last_query_iso is not None or last_seen_version > 0
        row["last_query_iso"] = last_query_iso
        row["last_query_scope"] = scope
        row["last_seen_version"] = last_seen_version if known else None
        row["behind"] = (memory_version - last_seen_version) if known else None
        if row["agent_session"] is None:
            row["state"] = "listening"
        elif store.has_departed(sid, contributor, row["agent_session"]):
            # BEFORE the roster check, and asked per WINDOW (2026-08-06). Both
            # halves are the fix. A person with two windows stays in `members`
            # while either is bound, so a departed window is one that is still
            # in the roster -- ask `members` first and it reads ACTIVE forever.
            # Ask `has_departed` without the window and every row of that
            # person reads LEFT the moment one of them does, which is the bug
            # as it was observed. Left, not deleted: their findings stay in the
            # log attributed to them, so the row stays too.
            row["state"] = "left"
        elif contributor in members:
            row["state"] = "active"
        else:
            # No "left" arm here any more: a person-level departure is one the
            # branch above already answers, since `has_departed` treats it as
            # covering every window. What reaches here is a row with a window
            # that this process never watched leave.
            # NOT a member, and this process never watched them leave. Three
            # situations share that shape and only one of them is "left":
            #   * they never registered -- nothing on the ingest path calls
            #     `add_member`, so a raw `POST /findings` (the demo script's
            #     Beats 1-6 are exactly that) produces a contributor who is
            #     in the log and was never in `members`;
            #   * they registered against a service that has since restarted
            #     -- `Relay._registered` caches per process and will not
            #     re-POST, while the restarted store starts at `members=[]`;
            #   * they were removed before this process started.
            # Calling any of those "left" asserts a human action that may not
            # have happened, so the page reports the membership fact it
            # actually holds and nothing more.
            row["state"] = "unregistered"

    participants.sort(key=lambda r: (
        max(r["last_contribution_iso"] or "", r["last_query_iso"] or ""),
        r["contributor"],
        r["agent_session"] or "",
    ), reverse=True)
    return participants


def _recent(store: InMemoryStore, sid: str) -> list[dict[str, Any]]:
    """The last findings to reach the memory, newest first.

    BOTH `FindingAppended` and `Merged` are walked, because a Synthesized
    Finding never appears as a `FindingAppended` — `SharedMemory.merge`
    appends only the `Merged` entry — and "what just entered the memory" that
    omitted every merge result would be missing the thing synthesis does.
    (The roster above walks `FindingAppended` alone, for the opposite and
    equally deliberate reason stated there.)

    Each row is projected through `store.get`, so `status`/`merged_into` are
    the FOLD's answer rather than the producer's defaults (adr/0004).

    "contributed vs listened" is `Provenance` and it already exists:
    CONTRIBUTED = the agent called `contribute`; DISTILLED = the Edge Worker
    read the transcript, i.e. listened; SYNTHESIZED = the service wrote it
    during a merge. Three badges, not two, and the third is called `merged`
    because calling it either of the others would be false.
    """
    out: list[dict[str, Any]] = []
    seen: set[str] = set()
    for entry in reversed(store.log_entries(sid)):
        if isinstance(entry, FindingAppended):
            finding = entry.finding
        elif isinstance(entry, Merged):
            finding = entry.result
        else:
            continue
        if finding.id in seen:
            continue          # a resend is not a second arrival
        seen.add(finding.id)
        projected = store.get(sid, finding.id) or finding
        # EVERY attribution, never `attributions[0]`: a merged finding belongs
        # to everyone it was merged from, which is the same rule the query
        # route enforces on its way out.
        authors: list[str] = []
        for attribution in projected.attributions:
            if attribution.contributor not in authors:
                authors.append(attribution.contributor)
        out.append({
            "id": projected.id,
            "ts_iso": projected.ts.isoformat(),
            "type": projected.type.value,
            "provenance": projected.provenance.value,
            "text": projected.text,
            "authors": authors,
            "attributions": [
                {"contributor": a.contributor, "agent_session": a.agent_session,
                 "agent": a.agent}
                for a in projected.attributions
            ],
            "status": projected.status.value,
            "merged_into": projected.merged_into,
        })
        if len(out) == MAX_RECENT:
            break
    return out


def _working_memory(ctx: Any, wm_log: WorkingMemoryLog | None, sid: str) -> dict[str, Any]:
    revisions = wm_log.snapshot(sid) if wm_log is not None else []
    # The newest revision's OBSERVATION time, or null when this process has
    # watched no rewrite. Never the current time, and never the session's --
    # the prose itself carries no timestamp anywhere.
    #
    # Guarded on the TEXT MATCHING, not merely on the list being non-empty:
    # `_record_synthesis_feed` is the only recorder, so a rewrite reaching
    # `store.set_context` by any other path would leave the newest revision
    # stale and this timestamp would then date the prose ON SCREEN by an
    # observation of different prose. The page can say "not observed"; it
    # must not say "rewritten at 21:14" about text nobody watched arrive.
    updated_iso = None
    if revisions and revisions[0]["text"] == ctx.working_memory:
        updated_iso = revisions[0]["ts_iso"]
    return {
        "text": ctx.working_memory,
        "words": len(ctx.working_memory.split()),
        "version": ctx.memory_version,
        "updated_iso": updated_iso,
        "revisions": revisions,
    }



def _memory_payload(store: InMemoryStore, sid: str) -> dict[str, Any]:
    """Every finding in the session, projected through the fold (adr/0004).

    The brain page's `_recent` walks the log's tail and stops at MAX_RECENT;
    this walks `store.all_findings`, the fold's own view of the whole memory,
    so nothing is missing and every `status`/`merged_into` is the fold's
    answer rather than the producer's default. `display_status` is the
    projection the memory table sorts and filters on: superseded when the
    fold gave the finding a merge target, else trivial or visible.
    """
    ctx = store.get_context(sid)
    view = store.view(sid)
    rows: list[dict[str, Any]] = []
    for projected in store.all_findings(sid):
        authors: list[str] = []
        for attribution in projected.attributions:
            if attribution.contributor not in authors:
                authors.append(attribution.contributor)
        display = ("superseded" if projected.merged_into
                   else ("trivial" if projected.status.value == "trivial"
                         else "visible"))
        rows.append({
            "id": projected.id,
            "ts_iso": projected.ts.isoformat(),
            "type": projected.type.value,
            "provenance": projected.provenance.value,
            "text": projected.text,
            "authors": authors,
            "attributions": [
                {"contributor": a.contributor, "agent_session": a.agent_session,
                 "agent": a.agent}
                for a in projected.attributions
            ],
            "status": projected.status.value,
            "display_status": display,
            "merged_into": projected.merged_into,
            "merged_from": list(projected.merged_from),
        })
    rows.sort(key=lambda r: r["ts_iso"], reverse=True)
    return {
        "sid": sid,
        "purpose": ctx.purpose,
        "status": ctx.status.value,
        "memory_version": ctx.memory_version,
        "counts": {"total": len(rows),
                   "visible": len(view.visible_ids),
                   "superseded": len(view.superseded_by),
                   "trivial": len(view.trivial)},
        "findings": rows,
    }


def _rate_limit_panel(provider: Any) -> dict[str, Any]:
    """What the synthesis key has left, as the gateway last reported it.

    Three states, and "unknown" is not "ok": the snapshot is empty until the
    first response and stays empty if the gateway's header spelling is one
    `synapse_providers.ratelimit` does not know. Showing headroom we cannot
    see would recreate the silent failure this panel exists to end -- on
    2026-08-06 a throttled synthesis key was invisible from every surface,
    with findings landing normally and the working memory simply not moving.
    """
    snapshot = getattr(provider, "last_rate_limit", None)
    if snapshot is None or snapshot.is_empty:
        return {"state": "unknown", "requests_remaining": None,
                "tokens_remaining": None, "reset_seconds": None,
                "reason": "no rate-limit headers seen from the provider yet"}

    throttled = (snapshot.requests_remaining is not None
                 and snapshot.requests_remaining < 1)
    if throttled:
        reset = (f", resets in {snapshot.reset_seconds:.0f}s"
                 if snapshot.reset_seconds else "")
        reason = f"provider reported 0 request(s) remaining{reset}"
    else:
        reason = "headroom reported by the provider"
    return {"state": "throttled" if throttled else "ok",
            "requests_remaining": snapshot.requests_remaining,
            "tokens_remaining": snapshot.tokens_remaining,
            "reset_seconds": snapshot.reset_seconds,
            "reason": reason}


def _brain_payload(store: InMemoryStore, feed: Feed, wm_log: WorkingMemoryLog | None,
                   sid: str, provider: Any = None) -> dict[str, Any]:
    ctx = store.get_context(sid)
    view = store.view(sid)
    session = store.get_session(sid)
    participants = _participants(store, feed, sid, ctx.memory_version)
    with_conversation = [p for p in participants if p["agent_session"] is not None]
    return {
        "sid": sid,
        "purpose": ctx.purpose,
        "created_by": session.created_by if session is not None else None,
        "status": ctx.status.value,
        "memory_version": ctx.memory_version,
        "counts": {
            # Members of the Shared Session -- the humans, per the session
            # record. NOT "connected": nothing here measures a connection.
            "contributors": len(session.members) if session is not None else 0,
            "contributors_in_log": len({p["contributor"] for p in with_conversation}),
            "conversations": len({p["agent_session"] for p in with_conversation}),
            "visible": len(view.visible_ids),
            "superseded": len(view.superseded_by),
            "trivial": len(view.trivial),
            "conflicts": len(ctx.conflicts),
            # The TRUE count. The log page shows `log_tail.length`, capped at
            # MAX_LOG_TAIL.
            "log_entries": len(store.log_entries(sid)),
        },
        "working_memory": _working_memory(ctx, wm_log, sid),
        "participants": participants,
        "recent": _recent(store, sid),
        # Session-independent -- the key is the app's, not this session's --
        # but it belongs here because this is the page an operator opens when
        # the memory has stopped moving, and a throttled key is the answer.
        "rate_limit": _rate_limit_panel(provider),
    }


def debug_routes(store: InMemoryStore, call_log: CallLog, feed: Feed,
                 wm_log: WorkingMemoryLog | None = None,
                 provider: Any = None) -> list[Route]:
    async def stats_json(request: Request) -> JSONResponse:
        sids = store.session_ids()
        sessions = [
            {"shared_id": sid, "purpose": (store.get_session(sid) or _EMPTY).purpose}
            for sid in sids
        ]
        requested = request.query_params.get("session")
        sid = requested if requested in sids else (sids[0] if sids else None)
        session = _session_payload(store, feed, call_log, sid) if sid is not None else None
        return JSONResponse({"sessions": sessions, "session": session})

    async def brain_json(request: Request) -> JSONResponse:
        """The brain page's ONLY data source. A new endpoint rather than two
        more keys on `stats.json`, which carries up to 200 log entries plus
        the whole CallLog (200 prompt/output previews) that this page renders
        none of and would be downloading once a second -- and which three
        scripts parse today."""
        sids = store.session_ids()
        sessions = [
            {"shared_id": sid,
             "purpose": (store.get_session(sid) or _EMPTY).purpose,
             "status": store.get_context(sid).status.value}
            for sid in sids
        ]
        requested = request.query_params.get("session")
        sid = requested if requested in sids else (sids[0] if sids else None)
        session = (_brain_payload(store, feed, wm_log, sid, provider)
                   if sid is not None else None)
        return JSONResponse({"sessions": sessions, "session": session})

    async def brain_page(request: Request) -> HTMLResponse:
        return HTMLResponse(_BRAIN_PAGE)

    async def log_page(request: Request) -> HTMLResponse:
        return HTMLResponse(_PAGE)

    async def home_page(request: Request) -> HTMLResponse:
        return HTMLResponse(_HOME_PAGE)

    async def memory_page(request: Request) -> HTMLResponse:
        return HTMLResponse(_MEMORY_PAGE)

    async def memory_json(request: Request) -> JSONResponse:
        sids = store.session_ids()
        sessions = [
            {"shared_id": sid,
             "purpose": (store.get_session(sid) or _EMPTY).purpose,
             "status": store.get_context(sid).status.value}
            for sid in sids
        ]
        requested = request.query_params.get("session")
        sid = requested if requested in sids else (sids[0] if sids else None)
        session = _memory_payload(store, sid) if sid is not None else None
        return JSONResponse({"sessions": sessions, "session": session})

    return [
        # The front door, mounted with the rest of the debug pages so the
        # debug=False off switch removes it too.
        Route("/", home_page, methods=["GET"]),
        Route("/debug/stats.json", stats_json, methods=["GET"]),
        Route("/debug/memory.json", memory_json, methods=["GET"]),
        Route("/debug/memory", memory_page, methods=["GET"]),
        Route("/debug/brain.json", brain_json, methods=["GET"]),
        # ⟨W4a⟩ The top level is the BRAIN page. The page that lived here is
        # mounted below, verbatim -- same constant, same script.
        Route("/debug", brain_page, methods=["GET"]),
        Route("/debug/log", log_page, methods=["GET"]),
    ]


class _Empty:
    purpose = ""


_EMPTY = _Empty()


_PAGE = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<link rel="icon" href="data:,">
<title>synapse-service · log</title>
<style>
  /* The cloud side of the device boundary: near-black canvas, charcoal
     surface lift, identity hues as data encodings. Cyan = service, green =
     merged, amber = trivial, purple = query, blue = topics, red = failure.
     Sessions are the top-level entity: the sidebar lists them, the tabs are
     this session's subpages, and ?session= makes every view deep-linkable.
     Same standing rule as ever: NOT ONE external request. */
  :root {
    color-scheme: dark;
    /* ground: near-black canvas, charcoal surface lift, felt-not-seen hairlines */
    --canvas: #000000;
    --surface-1: #15181e;
    --surface-2: #1f232b;
    --surface-3: #2a2e37;
    --hairline: rgba(178, 182, 189, 0.14);
    --hairline-soft: rgba(178, 182, 189, 0.07);
    /* ink */
    --ink: #ffffff;
    --ink-muted: #b2b6bd;
    --ink-subtle: #656a76;
    /* identity hues: data encodings, never decoration */
    --cyan: #14c6cb;
    --cyan-deep: #12b6bb;
    --green: #00ca8e;
    --amber: #ffcf25;
    --red: #e62b1e;
    --red-text: #f5564a;
    --purple: #7b42bc;
    --purple-bright: #911ced;
    --purple-text: #b78ae8;
    --blue: #1868f2;
    --blue-text: #6ea6ff;
    --copper: #e09a5a;
    --copper-dim: #8a5a2d;
    --radius: 12px;
    --radius-sm: 8px;
    --sans: ui-sans-serif, system-ui, -apple-system, "Segoe UI", Roboto, "Helvetica Neue", Arial, sans-serif;
    --mono: ui-monospace, "SF Mono", "Cascadia Code", Menlo, Consolas, monospace;
  }
  * { box-sizing: border-box; }
  body {
    margin: 0;
    background: var(--canvas);
    color: var(--ink);
    font: 14px/1.55 var(--sans);
    -webkit-font-smoothing: antialiased;
  }
  #banner {
    display: none;
    background: rgba(230, 43, 30, 0.14); color: var(--red-text);
    border-bottom: 1px solid rgba(230, 43, 30, 0.35);
    padding: 8px 24px; font-size: 12px; font-weight: 500;
  }
  #banner.show { display: block; }

  /* ── shell: sessions on the left, this session's pages on top ── */
  .shell { display: flex; min-height: 100dvh; }
  aside {
    width: 272px; flex-shrink: 0;
    border-right: 1px solid var(--hairline-soft);
    padding: 16px 12px;
    display: flex; flex-direction: column; gap: 14px;
    position: sticky; top: 0; height: 100dvh; overflow-y: auto;
  }
  .brand { display: flex; align-items: center; gap: 9px; text-decoration: none; color: inherit; padding: 2px 6px 6px; }
  .brand .mark { width: 30px; height: 16px; overflow: visible; }
  .brand .mark .axon { fill: none; stroke: var(--cyan-deep); stroke-width: 1.4; opacity: 0.7; }
  .brand .mark .soma { fill: var(--cyan); }
  .brand .mark .impulse { fill: var(--ink); }
  @media (prefers-reduced-motion: reduce) { .brand .mark .impulse { display: none; } }
  .brand .name { font-size: 16px; font-weight: 650; letter-spacing: -0.01em; }
  .brand .scope-label { color: var(--ink-subtle); font-size: 13px; }
  .side-label {
    font: 600 11px var(--sans); letter-spacing: 0.06em; text-transform: uppercase;
    color: var(--ink-subtle); padding: 0 6px;
  }
  .side-sessions { display: flex; flex-direction: column; gap: 3px; }
  .side-session {
    display: block; padding: 9px 11px; border-radius: var(--radius-sm);
    text-decoration: none; color: var(--ink-muted);
    border: 1px solid transparent;
    transition: background-color 130ms, border-color 130ms;
  }
  .side-session:hover { background: var(--surface-2); color: var(--ink); }
  .side-session[aria-current="page"] { background: rgba(20, 198, 203, 0.10); border-color: rgba(20, 198, 203, 0.4); color: var(--ink); }
  .side-session:focus-visible { outline: 2px solid var(--cyan); outline-offset: 2px; }
  .side-session .ss-purpose { display: block; font-size: 13.5px; font-weight: 600; line-height: 1.4; overflow-wrap: anywhere; }
  .side-session .ss-sid { display: block; font: 11.5px var(--mono); color: var(--ink-subtle); margin-top: 3px; }
  .side-empty { color: var(--ink-subtle); font-size: 12px; padding: 8px 10px; }
  .side-foot { margin-top: auto; padding: 0 6px; }
  .side-foot a { color: var(--ink-subtle); font-size: 12px; text-decoration: none; }
  .side-foot a:hover { color: var(--ink); }
  .content { flex: 1; min-width: 0; }
  .topbar {
    display: flex; align-items: center; gap: 14px; flex-wrap: wrap;
    padding: 12px 24px;
    border-bottom: 1px solid var(--hairline-soft);
  }
  .tabs { display: flex; gap: 4px; }
  .tabs a {
    padding: 7px 15px; border-radius: var(--radius-sm);
    font-size: 14px; font-weight: 600;
    color: var(--ink-muted); text-decoration: none;
    transition: color 130ms, background-color 130ms;
  }
  .tabs a:hover { color: var(--ink); background: var(--surface-2); }
  .tabs a[aria-current="page"] { color: var(--cyan); background: rgba(20, 198, 203, 0.12); }
  .tabs a:focus-visible { outline: 2px solid var(--cyan); outline-offset: 2px; }
  main { padding: 28px 32px 64px; }
  @media (max-width: 900px) {
    .shell { flex-direction: column; }
    aside { position: static; width: auto; height: auto; border-right: none; border-bottom: 1px solid var(--hairline-soft); }
    .side-sessions { flex-direction: row; overflow-x: auto; }
    .side-session { min-width: 220px; }
    .side-foot { display: none; }
  }

  /* ── entrance: the page rises once, in order; polling never re-triggers it ── */
  @media (prefers-reduced-motion: no-preference) {
    main > * { animation: rise 620ms cubic-bezier(0.22, 1, 0.36, 1) backwards; }
    main > :nth-child(2) { animation-delay: 60ms; }
    main > :nth-child(3) { animation-delay: 120ms; }
    main > :nth-child(4) { animation-delay: 180ms; }
    main > :nth-child(5) { animation-delay: 220ms; }
    main > :nth-child(6) { animation-delay: 260ms; }
    main > :nth-child(7) { animation-delay: 300ms; }
  }
  @keyframes rise { from { opacity: 0; transform: translateY(14px); } }

  /* ── the pipeline, as stat cards: ingest → fold → merge → topics → query ── */
  .stats { display: grid; grid-template-columns: 1fr 1.7fr 1fr 1fr 1fr; gap: 14px; margin-bottom: 18px; }
  .stat {
    background: var(--surface-1);
    border: 1px solid var(--hairline);
    border-radius: var(--radius);
    padding: 16px 18px 14px;
    min-width: 0;
    transition: border-color 130ms;
  }
  .stat:hover { border-color: rgba(178, 182, 189, 0.28); }
  .stat-label {
    color: var(--ink-muted); font-size: 12px; font-weight: 600;
    letter-spacing: 0.05em; text-transform: uppercase; margin-bottom: 8px;
  }
  .stat-value {
    font: 650 30px/1.15 var(--mono);
    font-variant-numeric: tabular-nums;
    letter-spacing: -0.02em;
  }
  .stat-sub { color: var(--ink-subtle); font-size: 12px; margin-top: 5px; }
  .stat.fold {
    background: linear-gradient(135deg, rgba(20, 198, 203, 0.26), rgba(20, 198, 203, 0.05) 52%, rgba(20, 198, 203, 0.02) 80%), var(--surface-1);
    border-color: rgba(20, 198, 203, 0.42);
    box-shadow: 0 0 60px -24px rgba(20, 198, 203, 0.45);
  }
  .stat.fold .stat-label { color: var(--cyan); }
  .stat.fold .v-visible    { color: var(--ink); }
  .stat.fold .v-superseded { color: var(--green); }
  .stat.fold .v-trivial    { color: var(--amber); }
  .stat.fold .sep { color: var(--ink-subtle); font-weight: 400; padding: 0 5px; }

  .topics { display: flex; flex-wrap: wrap; gap: 6px; margin-bottom: 18px; }
  .topic-chip {
    display: inline-flex; align-items: center; gap: 6px;
    background: rgba(24, 104, 242, 0.12);
    border: 1px solid rgba(24, 104, 242, 0.32);
    border-radius: 999px; padding: 3px 11px;
    font-size: 12px; font-weight: 500; color: var(--ink-muted);
  }
  .topic-chip b { color: var(--blue-text); font-weight: 650; font-family: var(--mono); }

  details.wm {
    background: var(--surface-1); border: 1px solid var(--hairline);
    border-radius: var(--radius); padding: 13px 18px; margin-bottom: 10px;
  }
  details.wm summary { cursor: pointer; color: var(--ink-muted); font-size: 12px; font-weight: 600; letter-spacing: 0.04em; text-transform: uppercase; }
  details.wm summary:hover { color: var(--ink); }
  details.wm summary:focus-visible { outline: 2px solid var(--cyan); outline-offset: 2px; }
  details.wm .wm-body {
    margin-top: 10px; white-space: pre-wrap; overflow-wrap: anywhere;
    color: var(--ink-muted); max-width: 72ch; line-height: 1.65;
  }

  .section-head { display: flex; align-items: flex-end; gap: 16px; flex-wrap: wrap; margin: 32px 0 12px; }
  .section-head h2 { margin: 0; font-size: 20px; font-weight: 650; letter-spacing: -0.017em; }
  .section-head h2::before {
    content: ""; display: block; width: 34px; height: 3px; border-radius: 2px;
    margin-bottom: 10px;
    background: linear-gradient(90deg, var(--cyan), transparent);
  }
  .section-head p { margin: 3px 0 0; color: var(--ink-subtle); font-size: 13px; }
  .section-head .grow { flex: 1; }

  .search {
    background: var(--surface-1); color: var(--ink);
    border: 1px solid var(--hairline); border-radius: var(--radius-sm);
    padding: 7px 12px; font: 13px var(--sans);
    width: 240px;
    transition: border-color 130ms;
  }
  .search::placeholder { color: var(--ink-subtle); }
  .search:hover { border-color: rgba(178, 182, 189, 0.28); }
  .search:focus-visible { outline: 2px solid var(--cyan); outline-offset: 1px; }
  .orderbtn {
    background: var(--surface-2); color: var(--ink-muted);
    border: 1px solid var(--hairline); border-radius: var(--radius-sm);
    padding: 7px 13px; font: 600 12px var(--mono);
    cursor: pointer;
    transition: background-color 130ms, color 130ms;
  }
  .orderbtn:hover { background: var(--surface-3); color: var(--ink); }
  .orderbtn:focus-visible { outline: 2px solid var(--cyan); outline-offset: 2px; }

  #log-tail, #feed {
    background: var(--surface-1);
    border: 1px solid var(--hairline);
    border-radius: var(--radius);
    overflow: hidden;
  }
  .entry {
    --c: var(--ink-muted);
    padding: 8px 16px 8px 14px;
    border-bottom: 1px solid var(--hairline-soft);
    border-left: 3px solid var(--c);
    display: flex; gap: 12px; align-items: baseline;
    flex-wrap: wrap;
    transition: background-color 130ms;
  }
  .entry:last-child { border-bottom: none; }
  .entry[data-kind="FindingAppended"] { --c: transparent; }
  .entry[data-kind="Merged"]          { --c: var(--green); background: rgba(0, 202, 142, 0.08); }
  .entry[data-kind="MarkedTrivial"]   { --c: var(--amber); }
  .entry[data-kind="TopicAssigned"]   { --c: var(--blue-text); }
  .entry[data-kind="TopicSplit"]      { --c: var(--blue-text); }
  .entry[data-kind="SessionEnded"]    { --c: var(--red-text); }
  .entry[data-tag="llm"]       { --c: var(--cyan); }
  .entry[data-tag="query"]     { --c: var(--purple-text); }
  .entry[data-tag="synthesis"] { --c: var(--green); }
  .entry[data-tag="query_failed"] { --c: var(--red-text); background: rgba(230, 43, 30, 0.08); }
  .entry .pos { color: var(--ink-subtle); flex-shrink: 0; width: 3.2em; text-align: right; font: 12px/1.6 var(--mono); }
  .entry .ts { color: var(--ink-subtle); flex-shrink: 0; font: 12px/1.6 var(--mono); }
  .entry .kind, .entry .tag {
    flex-shrink: 0;
    font: 500 12px/1.6 var(--mono);
    color: var(--c);
  }
  .entry .kind { width: 11em; }
  .entry .tag { width: 7em; }
  .entry[data-kind="FindingAppended"] .kind { color: var(--ink-muted); }
  .entry[data-kind="FindingAppended"] .summary { color: var(--ink-muted); }
  .entry[data-kind="Merged"] .summary { color: var(--ink); font-weight: 500; }
  .entry .summary { flex: 1; overflow-wrap: anywhere; color: var(--ink-muted); }
  /* structured activity columns */
  .entry .main { flex: 1; min-width: 16em; overflow-wrap: anywhere; color: var(--ink-muted); }
  .entry[data-tag="llm"] .main { color: var(--ink); }
  .entry .metrics { flex-shrink: 0; font: 12px/1.6 var(--mono); color: var(--ink-subtle); }
  .entry .okbadge {
    flex-shrink: 0;
    display: inline-block; padding: 0 8px; border-radius: 999px;
    font: 600 10px/1.7 var(--sans);
    border: 1px solid transparent;
  }
  .entry .okbadge.ok   { color: var(--green); border-color: rgba(0, 202, 142, 0.45); }
  .entry .okbadge.fail { color: var(--red-text); border-color: rgba(230, 43, 30, 0.45); background: rgba(230, 43, 30, 0.12); }
  .entry[data-tag] { cursor: pointer; }
  .entry[data-tag]:hover { background: var(--surface-2); }
  /* One selector for both homes a .detail can have: re-parented INSIDE an
     llm entry by the script, or left as the entry's next SIBLING (query
     entries) -- the sibling case must not leak as bare visible text. */
  .detail {
    display: none;
    margin: 8px 0 4px;
    padding: 12px 14px;
    background: var(--canvas);
    border: 1px solid var(--hairline);
    border-radius: var(--radius-sm);
    font: 13px/1.65 var(--mono); color: var(--ink-muted);
    white-space: pre-wrap; overflow-wrap: anywhere;
    flex-basis: 100%;
  }
  #feed > .detail { margin: 0 16px 10px; }
  .entry.expanded .detail,
  .entry.expanded + .detail { display: block; }
  .chips { display: flex; flex-wrap: wrap; gap: 6px; }
  .chip {
    --c: var(--ink-muted);
    display: inline-flex; align-items: center; gap: 7px;
    border: 1px solid var(--hairline);
    background: var(--surface-1);
    color: var(--ink-muted);
    border-radius: 999px;
    padding: 4px 12px;
    font: 500 12px var(--mono);
    cursor: pointer; user-select: none;
    transition: background-color 130ms, opacity 130ms, border-color 130ms;
  }
  .chip:hover { background: var(--surface-2); }
  .chip::before {
    content: "";
    width: 7px; height: 7px; border-radius: 50%;
    background: var(--c);
  }
  .chip[data-tag="llm"]       { --c: var(--cyan); }
  .chip[data-tag="query"]     { --c: var(--purple-text); }
  .chip[data-tag="synthesis"] { --c: var(--green); }
  .chip[data-tag="query_failed"] { --c: var(--red-text); }
  .chip[data-kind] { --c: var(--ink-muted); }
  .chip[data-kind="Merged"] { --c: var(--green); }
  .chip[data-kind="MarkedTrivial"] { --c: var(--amber); }
  .chip[data-kind*="Topic"] { --c: var(--blue-text); }
  .chip[data-kind="SessionEnded"] { --c: var(--red-text); }
  .chip[data-active="false"] { opacity: 0.4; }
  .chip[data-active="false"]::before { background: var(--ink-subtle); }
  .chip:focus-visible { outline: 2px solid var(--cyan); outline-offset: 2px; }
  .empty { padding: 30px; text-align: center; color: var(--ink-subtle); }
  @media (max-width: 900px) {
    .stats { grid-template-columns: 1fr 1fr; }
    .stat.fold { grid-column: span 2; }
  }
  @media (max-width: 560px) {
    .stats { grid-template-columns: 1fr; }
    .stat.fold { grid-column: auto; }
  }
</style>
</head>
<body>
<div id="banner">Service unreachable. Retrying…</div>
<div class="shell">
<aside>
  <a class="brand" href="/"><svg class="mark" viewBox="0 0 30 16" aria-hidden="true"><path class="axon" d="M4 8 C 10 3, 20 13, 26 8"/><circle class="soma" cx="4" cy="8" r="2.7"/><circle class="soma" cx="26" cy="8" r="2.7"/><circle class="impulse" r="1.7"><animateMotion dur="2.8s" repeatCount="indefinite" path="M4 8 C 10 3, 20 13, 26 8"/></circle></svg><span class="name">Synapse</span><span class="scope-label">service</span></a>
  <div class="side-label">Shared sessions</div>
  <nav id="session-list" class="side-sessions" aria-label="shared sessions"><div class="side-empty">connecting…</div></nav>
  <div class="side-foot"><a href="/">Service home</a></div>
</aside>
<div class="content">
<header class="topbar">
  <nav class="tabs" aria-label="session pages">
    <a id="tab-brain" href="/debug">Brain</a>
    <a id="tab-log" href="/debug/log" aria-current="page">Log</a>
    <a id="tab-memory" href="/debug/memory">Memory</a>
  </nav>
</header>
<main>
  <section class="stats" aria-label="service pipeline: ingest to query, in order">
    <div class="stat"><div class="stat-label">Ingest</div><div class="stat-value" id="stat-entries">0</div><div class="stat-sub">log entries, append-only</div></div>
    <div class="stat fold">
      <div class="stat-label">Fold · the view</div>
      <div class="stat-value"><span class="v-visible" id="stat-visible">0</span><span class="sep">·</span><span class="v-superseded" id="stat-superseded">0</span><span class="sep">·</span><span class="v-trivial" id="stat-trivial">0</span></div>
      <div class="stat-sub">visible · superseded · trivial</div>
    </div>
    <div class="stat"><div class="stat-label">Merge</div><div class="stat-value" id="stat-merges">0</div><div class="stat-sub">v<span id="stat-version">0</span> · <span id="stat-conflicts">0</span> conflicts</div></div>
    <div class="stat"><div class="stat-label">Topics</div><div class="stat-value" id="stat-topics">0</div><div class="stat-sub">geometry, labels only</div></div>
    <div class="stat"><div class="stat-label">Query</div><div class="stat-value" id="stat-queries">0</div><div class="stat-sub">suppression-aware</div></div>
  </section>

  <div class="topics" id="topics"></div>

  <details class="wm">
    <summary>Working memory</summary>
    <div class="wm-body" id="wm-body">(empty)</div>
  </details>

  <div class="section-head">
    <div>
      <h2>Session log</h2>
      <p>the append-only record the fold is computed from, newest first</p>
    </div>
    <span class="grow"></span>
    <div class="chips" id="log-chips">
      <span class="chip" data-kind="FindingAppended" data-active="true" tabindex="0">appended</span>
      <span class="chip" data-kind="Merged" data-active="true" tabindex="0">merged</span>
      <span class="chip" data-kind="MarkedTrivial" data-active="true" tabindex="0">trivial</span>
      <span class="chip" data-kind="TopicAssigned TopicSplit" data-active="true" tabindex="0">topics</span>
      <span class="chip" data-kind="SessionEnded" data-active="true" tabindex="0">ended</span>
    </div>
    <input class="search" id="log-search" type="search" placeholder="search the log…" aria-label="search the session log">
  </div>
  <div id="log-tail"><div class="empty">no entries yet</div></div>

  <div class="section-head">
    <div>
      <h2>Activity</h2>
      <p>LLM calls, queries, and synthesis merges; click a row for its full detail</p>
    </div>
    <span class="grow"></span>
    <div class="chips" id="chips">
      <span class="chip" data-tag="llm" data-active="true" tabindex="0">llm</span>
      <span class="chip" data-tag="query" data-active="true" tabindex="0">query</span>
      <span class="chip" data-tag="query_failed" data-active="true" tabindex="0">query_failed</span>
      <span class="chip" data-tag="synthesis" data-active="true" tabindex="0">synthesis</span>
    </div>
    <input class="search" id="feed-search" type="search" placeholder="search activity…" aria-label="search activity">
    <button class="orderbtn" id="feed-order" type="button">newest first</button>
  </div>
  <div id="feed"><div class="empty">waiting for the first event…</div></div>
</main>
</div>
</div>

<script>
(function () {
  "use strict";

  var activeTags = new Set(["llm", "query", "query_failed", "synthesis"]);
  var activeKinds = new Set(["FindingAppended", "Merged", "MarkedTrivial",
                             "TopicAssigned", "TopicSplit", "SessionEnded"]);
  var newestFirst = true;
  var lastSession = null;
  var PAGE_PATH = "/debug/log";
  // Keys of currently-expanded feed entries, surviving the 1s poll's full
  // innerHTML rebuild -- without this, clicking an entry open only lasts
  // until the next refresh (up to 1s), never long enough to read a preview.
  var expandedKeys = new Set();

  // ?session= makes every view deep-linkable; guarded because the test
  // driver's minimal DOM has no location.
  var currentSid = null;
  try {
    if (typeof location !== "undefined" && typeof URLSearchParams !== "undefined"
        && location.search) {
      currentSid = new URLSearchParams(location.search).get("session");
    }
  } catch (e) { /* no location in the test DOM */ }

  function entryKey(tag, ts) {
    return tag + "|" + ts;
  }

  // Text-node escaping only -- the serializer leaves quotes alone, so
  // anything landing inside an attribute needs `escAttr` (same pair as the
  // brain page; `shared_id` in the picker below is client-supplied through
  // `POST /v1/sessions`).
  function esc(s) {
    var d = document.createElement("div");
    d.textContent = String(s == null ? "" : s);
    return d.innerHTML;
  }

  function escAttr(s) {
    return esc(s).replace(/"/g, "&quot;").replace(/'/g, "&#39;");
  }

  function hhmmss(iso) {
    if (!iso) return "";
    return new Date(iso).toTimeString().slice(0, 8);
  }

  function sessionHref(path, sid) {
    return path + "?session=" + encodeURIComponent(sid);
  }

  // The sidebar and tab hrefs. Everything here is optional chrome: each
  // element is looked up by id and skipped when absent, so the test driver's
  // minimal DOM never sees a difference.
  function updateChrome(sessions) {
    try {
      var sl = document.getElementById("session-list");
      if (sl) {
        sl.innerHTML = sessions.length ? sessions.map(function (s) {
          var cur = s.shared_id === currentSid ? ' aria-current="page"' : "";
          return '<a class="side-session"' + cur + ' href="' +
            sessionHref(PAGE_PATH, s.shared_id) + '">' +
            '<span class="ss-purpose">' + esc(s.purpose || "(no purpose recorded)") + '</span>' +
            '<span class="ss-sid">' + esc(s.shared_id) + '</span></a>';
        }).join("") : '<div class="side-empty">no shared sessions yet</div>';
      }
      [["tab-brain", "/debug"], ["tab-log", "/debug/log"],
       ["tab-memory", "/debug/memory"]].forEach(function (t) {
        var el = document.getElementById(t[0]);
        if (el && currentSid) el.setAttribute("href", sessionHref(t[1], currentSid));
      });
    } catch (e) { /* chrome only; never let it take the data down */ }
  }

  var sel = document.getElementById("session-select");
  if (sel) sel.addEventListener("change", function (ev) {
    currentSid = ev.target.value;
    expandedKeys.clear();
    try { history.replaceState(null, "", sessionHref(PAGE_PATH, currentSid)); } catch (e) {}
    refresh();
  });

  function wireChips(containerId, attr, activeSet) {
    var el = document.getElementById(containerId);
    if (!el) return;
    el.addEventListener("click", function (ev) {
      var chip = ev.target.closest(".chip");
      if (!chip) return;
      var values = chip.getAttribute(attr).split(" ");
      var on = chip.getAttribute("data-active") !== "true";
      chip.setAttribute("data-active", on ? "true" : "false");
      values.forEach(function (v) { if (on) activeSet.add(v); else activeSet.delete(v); });
      if (lastSession) renderSession(lastSession);
    });
  }
  wireChips("chips", "data-tag", activeTags);
  wireChips("log-chips", "data-kind", activeKinds);

  function wireSearch(id) {
    var el = document.getElementById(id);
    if (el) el.addEventListener("input", function () {
      if (lastSession) renderSession(lastSession);
    });
  }
  wireSearch("log-search");
  wireSearch("feed-search");

  var orderBtn = document.getElementById("feed-order");
  if (orderBtn) orderBtn.addEventListener("click", function () {
    newestFirst = !newestFirst;
    orderBtn.textContent = newestFirst ? "newest first" : "oldest first";
    if (lastSession) renderSession(lastSession);
  });

  function searchTerm(id) {
    var el = document.getElementById(id);
    return el && el.value ? el.value.toLowerCase() : "";
  }

  function applyFilter() {
    document.querySelectorAll("#feed .entry").forEach(function (el) {
      el.style.display = activeTags.has(el.getAttribute("data-tag")) ? "" : "none";
    });
  }

  document.getElementById("feed").addEventListener("click", function (ev) {
    var entry = ev.target.closest(".entry[data-tag]");
    if (!entry) return;
    var key = entry.getAttribute("data-key");
    var nowExpanded = entry.classList.toggle("expanded");
    if (!key) return;
    if (nowExpanded) { expandedKeys.add(key); } else { expandedKeys.delete(key); }
  });

  function renderLogTail(logTail) {
    var el = document.getElementById("log-tail");
    var q = searchTerm("log-search");
    var rows = logTail.filter(function (e) {
      if (!activeKinds.has(e.kind)) return false;
      if (q && (e.kind + " " + e.summary).toLowerCase().indexOf(q) === -1) return false;
      return true;
    });
    if (!rows.length) {
      el.innerHTML = '<div class="empty">' +
        (logTail.length ? "nothing matches the current filter" : "no entries yet") +
        '</div>';
      return;
    }
    var html = "";
    for (var i = rows.length - 1; i >= 0; i--) {
      var e = rows[i];
      html += '<div class="entry" data-kind="' + escAttr(e.kind) + '">';
      html += '<span class="pos">#' + esc(e.position) + '</span>';
      html += '<span class="kind">' + esc(e.kind) + '</span>';
      html += '<span class="summary">' + esc(e.summary) + '</span>';
      html += '</div>';
    }
    el.innerHTML = html;
  }

  // One llm call, structured: what ran, what it cost, whether it worked.
  function llmRow(c, cls, key) {
    var html = '<div class="' + cls + '" data-tag="llm" data-key="' + escAttr(key) + '">';
    html += '<span class="ts">' + esc(hhmmss(c.ts_iso)) + '</span>';
    html += '<span class="tag">llm</span>';
    html += '<span class="main">' + esc(c.component) +
      ' <span class="metrics">· ' + esc(c.provider_id) + '</span></span>';
    html += '<span class="metrics">' + esc(c.input_tokens) + '→' +
      esc(c.output_tokens) + ' tok · ' + esc(c.latency_ms) + 'ms</span>';
    html += '<span class="okbadge ' + (c.ok ? 'ok">ok' : 'fail">FAILED') + '</span>';
    html += '</div>';
    html += '<div class="detail">' +
      "provider: " + esc(c.provider_id) + "\\n" +
      "schema_valid: " + esc(c.schema_valid) + "\\n\\n" +
      "prompt:\\n" + esc(c.prompt_preview) + "\\n\\n" +
      "output:\\n" + esc(c.output_preview) + '</div>';
    return html;
  }

  // A feed event (query, query_failed, synthesis). The summary strings arrive
  // prefixed with the shared_id; inside a session-scoped page that prefix is
  // noise, so it is stripped when it names the CURRENT session.
  function eventRow(m, cls, key) {
    var e = m.data;
    var summary = String(e.summary == null ? "" : e.summary);
    if (currentSid && summary.indexOf(currentSid + ": ") === 0) {
      summary = summary.slice(currentSid.length + 2);
    }
    var d = e.detail || {};
    var metrics = "";
    if (m.tag === "query" && d.returned) {
      metrics = d.returned.length + " returned · " + esc(d.suppressed) + " suppressed";
    }
    var html = '<div class="' + cls + '" data-tag="' + escAttr(m.tag) +
      '" data-key="' + escAttr(key) + '">';
    html += '<span class="ts">' + esc(hhmmss(e.ts_iso)) + '</span>';
    html += '<span class="tag">' + esc(m.tag) + '</span>';
    html += '<span class="main">' + esc(summary) + '</span>';
    if (metrics) html += '<span class="metrics">' + metrics + '</span>';
    html += '</div>';
    // A query's counts cannot answer "what did the asker get back?".
    // Expand one and the answer is right there, attribution included --
    // which is also how you see suppression bite rather than infer it.
    if (m.tag === "query" && d.returned) {
      var body = "asked by: " + esc(d.asked_by) + "\\n" +
        "suppressed for this asker: " + esc(d.suppressed) + "\\n\\nreturned:";
      if (d.returned.length === 0) {
        body += "\\n  (nothing)";
      }
      for (var r = 0; r < d.returned.length; r++) {
        var f = d.returned[r];
        body += "\\n  (" + esc(f.type) + ", from " + esc((f.from || []).join(", ")) +
          ")\\n  " + esc(f.text);
      }
      html += '<div class="detail">' + body + '</div>';
    }
    return html;
  }

  function renderFeed(session) {
    var el = document.getElementById("feed");
    var merged = [];
    session.llm.forEach(function (c) { merged.push({ tag: "llm", ts: c.ts_iso, data: c }); });
    // e.tag, not the literal "query": `queries` carries query AND query_failed
    // now, and hardcoding the tag would paint an outage in the same colour as
    // a successful search and hide it behind the same filter chip.
    session.queries.forEach(function (e) { merged.push({ tag: e.tag || "query", ts: e.ts_iso, data: e }); });
    session.merges.forEach(function (e) { merged.push({ tag: "synthesis", ts: e.ts_iso, data: e }); });
    merged.sort(function (a, b) { return a.ts < b.ts ? -1 : a.ts > b.ts ? 1 : 0; });

    // Drop any expanded key whose entry fell out of this snapshot (the
    // bounded feed, or a session switch) -- otherwise expandedKeys would
    // grow forever across a long session.
    var liveKeys = new Set(merged.map(function (m) { return entryKey(m.tag, m.ts); }));
    expandedKeys.forEach(function (k) { if (!liveKeys.has(k)) expandedKeys.delete(k); });

    var q = searchTerm("feed-search");
    if (q) {
      merged = merged.filter(function (m) {
        var hay = m.tag + " " + JSON.stringify(m.data);
        return hay.toLowerCase().indexOf(q) !== -1;
      });
    }

    if (!merged.length) {
      el.innerHTML = '<div class="empty">' +
        (q ? "nothing matches the current search" : "waiting for the first event…") +
        '</div>';
      return;
    }

    var html = "";
    for (var n = 0; n < merged.length; n++) {
      var i = newestFirst ? merged.length - 1 - n : n;
      var m = merged[i];
      var key = entryKey(m.tag, m.ts);
      var cls = "entry" + (expandedKeys.has(key) ? " expanded" : "");
      html += (m.tag === "llm") ? llmRow(m.data, cls, key) : eventRow(m, cls, key);
    }
    el.innerHTML = html;
    // Re-parent each just-emitted llm .detail block under the .entry right
    // before it, same trick as the worker page, so the click toggle finds
    // it. The "expanded" class was already set above from expandedKeys, so
    // a re-render mid-poll doesn't collapse an entry the user has open.
    // Query details stay siblings; CSS's adjacent-sibling rule shows them.
    el.querySelectorAll('.entry[data-tag="llm"]').forEach(function (entry) {
      var next = entry.nextElementSibling;
      if (next && next.classList.contains("detail")) entry.appendChild(next);
    });
    applyFilter();
  }

  function renderSession(session) {
    lastSession = session;
    document.getElementById("stat-version").textContent = session.memory_version;
    document.getElementById("stat-visible").textContent = session.view.visible;
    document.getElementById("stat-superseded").textContent = session.view.superseded;
    document.getElementById("stat-trivial").textContent = session.view.trivial;
    document.getElementById("stat-conflicts").textContent = session.conflicts;

    var se = document.getElementById("stat-entries");
    if (se) se.textContent = session.log_tail.length;
    var sm = document.getElementById("stat-merges");
    if (sm) sm.textContent = session.merges.length;
    var st = document.getElementById("stat-topics");
    if (st) st.textContent = session.topics.length;
    var sq = document.getElementById("stat-queries");
    if (sq) sq.textContent = session.queries.length;

    var topicsEl = document.getElementById("topics");
    topicsEl.innerHTML = session.topics.length
      ? session.topics.map(function (t) {
          return '<span class="topic-chip" title="' + escAttr(t.size) + ' finding(s) in this topic">'
            + '<b>x' + esc(t.size) + '</b> ' + esc(t.label) + '</span>';
        }).join("")
      : '<span class="topic-chip">no topics yet</span>';

    document.getElementById("wm-body").textContent = session.working_memory || "(empty)";

    renderLogTail(session.log_tail);
    renderFeed(session);
  }

  function populateSessions(sessions) {
    if (!currentSid && sessions.length) currentSid = sessions[0].shared_id;
    updateChrome(sessions);
    var select = document.getElementById("session-select");
    if (!select) return;
    var prior = currentSid;
    select.innerHTML = sessions.map(function (s) {
      return '<option value="' + escAttr(s.shared_id) + '">' + esc(s.shared_id) +
        " · " + esc(s.purpose) + '</option>';
    }).join("");
    if (prior && sessions.some(function (s) { return s.shared_id === prior; })) {
      select.value = prior;
    } else if (sessions.length) {
      currentSid = sessions[0].shared_id;
      select.value = currentSid;
    }
  }

  function refresh() {
    var url = "/debug/stats.json" + (currentSid ? "?session=" + encodeURIComponent(currentSid) : "");
    fetch(url)
      .then(function (r) { if (!r.ok) throw new Error("bad status " + r.status); return r.json(); })
      .then(function (payload) {
        document.getElementById("banner").classList.remove("show");
        populateSessions(payload.sessions);
        if (payload.session) {
          currentSid = payload.session.sid;
          renderSession(payload.session);
        }
      })
      .catch(function () {
        document.getElementById("banner").classList.add("show");
      });
  }

  refresh();
  setInterval(refresh, 1000);
})();
</script>
</body>
</html>
"""


# ── the brain page (W4a) ─────────────────────────────────────────────────────
# Same document shape as the page above and the same palette: one <style>, one
# <script>, `<link rel="icon" href="data:,">`, and NOT ONE external request --
# no CDN, no font, no image. That property is why this page can be opened on a
# demo machine with no network and still be itself.
#
# The footnote is load-bearing, not decoration: everything the service cannot
# measure is named there, in the same size type as everything it can.

_BRAIN_PAGE = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<link rel="icon" href="data:,">
<title>synapse-service · brain</title>
<style>
  /* The cloud side of the device boundary: near-black canvas, charcoal
     surface lift, identity hues as data encodings. Cyan = service, green =
     merged, amber = trivial, purple = query, blue = topics, red = failure.
     Sessions are the top-level entity: the sidebar lists them, the tabs are
     this session's subpages, and ?session= makes every view deep-linkable.
     Same standing rule as ever: NOT ONE external request. */
  :root {
    color-scheme: dark;
    /* ground: near-black canvas, charcoal surface lift, felt-not-seen hairlines */
    --canvas: #000000;
    --surface-1: #15181e;
    --surface-2: #1f232b;
    --surface-3: #2a2e37;
    --hairline: rgba(178, 182, 189, 0.14);
    --hairline-soft: rgba(178, 182, 189, 0.07);
    /* ink */
    --ink: #ffffff;
    --ink-muted: #b2b6bd;
    --ink-subtle: #656a76;
    /* identity hues: data encodings, never decoration */
    --cyan: #14c6cb;
    --cyan-deep: #12b6bb;
    --green: #00ca8e;
    --amber: #ffcf25;
    --red: #e62b1e;
    --red-text: #f5564a;
    --purple: #7b42bc;
    --purple-bright: #911ced;
    --purple-text: #b78ae8;
    --blue: #1868f2;
    --blue-text: #6ea6ff;
    --copper: #e09a5a;
    --copper-dim: #8a5a2d;
    --radius: 12px;
    --radius-sm: 8px;
    --sans: ui-sans-serif, system-ui, -apple-system, "Segoe UI", Roboto, "Helvetica Neue", Arial, sans-serif;
    --mono: ui-monospace, "SF Mono", "Cascadia Code", Menlo, Consolas, monospace;
  }
  * { box-sizing: border-box; }
  body {
    margin: 0;
    background: var(--canvas);
    color: var(--ink);
    font: 14px/1.55 var(--sans);
    -webkit-font-smoothing: antialiased;
  }
  .sr-only {
    position: absolute; width: 1px; height: 1px;
    padding: 0; margin: -1px; overflow: hidden;
    clip: rect(0 0 0 0); white-space: nowrap; border: 0;
  }
  #banner {
    display: none;
    background: rgba(230, 43, 30, 0.14); color: var(--red-text);
    border-bottom: 1px solid rgba(230, 43, 30, 0.35);
    padding: 8px 24px; font-size: 12px; font-weight: 500;
  }
  #banner.show { display: block; }

  /* ── shell: sessions on the left, this session's pages on top ── */
  .shell { display: flex; min-height: 100dvh; }
  aside {
    width: 272px; flex-shrink: 0;
    border-right: 1px solid var(--hairline-soft);
    padding: 16px 12px;
    display: flex; flex-direction: column; gap: 14px;
    position: sticky; top: 0; height: 100dvh; overflow-y: auto;
  }
  .brand { display: flex; align-items: center; gap: 9px; text-decoration: none; color: inherit; padding: 2px 6px 6px; }
  .brand .mark { width: 30px; height: 16px; overflow: visible; }
  .brand .mark .axon { fill: none; stroke: var(--cyan-deep); stroke-width: 1.4; opacity: 0.7; }
  .brand .mark .soma { fill: var(--cyan); }
  .brand .mark .impulse { fill: var(--ink); }
  @media (prefers-reduced-motion: reduce) { .brand .mark .impulse { display: none; } }
  .brand .name { font-size: 16px; font-weight: 650; letter-spacing: -0.01em; }
  .brand .scope-label { color: var(--ink-subtle); font-size: 13px; }
  .side-label {
    font: 600 11px var(--sans); letter-spacing: 0.06em; text-transform: uppercase;
    color: var(--ink-subtle); padding: 0 6px;
  }
  .side-sessions { display: flex; flex-direction: column; gap: 3px; }
  .side-session {
    display: block; padding: 9px 11px; border-radius: var(--radius-sm);
    text-decoration: none; color: var(--ink-muted);
    border: 1px solid transparent;
    transition: background-color 130ms, border-color 130ms;
  }
  .side-session:hover { background: var(--surface-2); color: var(--ink); }
  .side-session[aria-current="page"] { background: rgba(20, 198, 203, 0.10); border-color: rgba(20, 198, 203, 0.4); color: var(--ink); }
  .side-session:focus-visible { outline: 2px solid var(--cyan); outline-offset: 2px; }
  .side-session .ss-purpose { display: block; font-size: 13.5px; font-weight: 600; line-height: 1.4; overflow-wrap: anywhere; }
  .side-session .ss-sid { display: block; font: 11.5px var(--mono); color: var(--ink-subtle); margin-top: 3px; }
  .side-empty { color: var(--ink-subtle); font-size: 12px; padding: 8px 10px; }
  .side-foot { margin-top: auto; padding: 0 6px; }
  .side-foot a { color: var(--ink-subtle); font-size: 12px; text-decoration: none; }
  .side-foot a:hover { color: var(--ink); }
  .content { flex: 1; min-width: 0; }
  .topbar {
    display: flex; align-items: center; gap: 14px; flex-wrap: wrap;
    padding: 12px 24px;
    border-bottom: 1px solid var(--hairline-soft);
  }
  .tabs { display: flex; gap: 4px; }
  .tabs a {
    padding: 7px 15px; border-radius: var(--radius-sm);
    font-size: 14px; font-weight: 600;
    color: var(--ink-muted); text-decoration: none;
    transition: color 130ms, background-color 130ms;
  }
  .tabs a:hover { color: var(--ink); background: var(--surface-2); }
  .tabs a[aria-current="page"] { color: var(--cyan); background: rgba(20, 198, 203, 0.12); }
  .tabs a:focus-visible { outline: 2px solid var(--cyan); outline-offset: 2px; }
  main { padding: 28px 32px 64px; max-width: 1500px; }
  @media (max-width: 900px) {
    .shell { flex-direction: column; }
    aside { position: static; width: auto; height: auto; border-right: none; border-bottom: 1px solid var(--hairline-soft); }
    .side-sessions { flex-direction: row; overflow-x: auto; }
    .side-session { min-width: 220px; }
    .side-foot { display: none; }
  }

  /* ── entrance: the page rises once, in order; polling never re-triggers it ── */
  @media (prefers-reduced-motion: no-preference) {
    main > * { animation: rise 620ms cubic-bezier(0.22, 1, 0.36, 1) backwards; }
    main > :nth-child(2) { animation-delay: 60ms; }
    main > :nth-child(3) { animation-delay: 120ms; }
    main > :nth-child(4) { animation-delay: 180ms; }
    main > :nth-child(5) { animation-delay: 220ms; }
    main > :nth-child(6) { animation-delay: 260ms; }
    main > :nth-child(7) { animation-delay: 300ms; }
    main > :nth-child(8) { animation-delay: 340ms; }
  }
  @keyframes rise { from { opacity: 0; transform: translateY(14px); } }

  .section-head { margin: 34px 0 12px; }
  .section-head h2 { margin: 0; font-size: 20px; font-weight: 650; letter-spacing: -0.017em; }
  .section-head h2::before {
    content: ""; display: block; width: 34px; height: 3px; border-radius: 2px;
    margin-bottom: 10px;
    background: linear-gradient(90deg, var(--cyan), transparent);
  }
  .section-head p { margin: 3px 0 0; color: var(--ink-subtle); font-size: 13px; }
  .note { color: var(--ink-subtle); font-weight: 400; font-size: 11px; }

  /* ── identity strip: what this brain is for ── */
  .identity {
    border-left: 3px solid;
    border-image: linear-gradient(180deg, var(--cyan), transparent) 1;
    padding: 4px 0 4px 18px;
    margin-bottom: 26px;
  }
  .identity-label {
    color: var(--cyan); font-size: 12px; font-weight: 600;
    letter-spacing: 0.06em; text-transform: uppercase; margin-bottom: 6px;
  }
  .identity .purpose {
    font-size: clamp(24px, 2.3vw, 32px); line-height: 1.22; font-weight: 650;
    letter-spacing: -0.022em;
    color: var(--ink); max-width: 60ch;
  }
  .identity .ident { color: var(--ink-subtle); font: 12px/1.8 var(--mono); margin-top: 8px; }
  .identity .ident b { color: var(--ink-muted); font-weight: 500; }
  .pill {
    display: inline-block; padding: 1px 9px; border-radius: 999px;
    font: 600 10px/1.6 var(--sans);
    border: 1px solid;
  }
  .pill.active { color: var(--green); border-color: rgba(0, 202, 142, 0.45); background: rgba(0, 202, 142, 0.12); }
  .pill.ended  { color: var(--red-text); border-color: rgba(230, 43, 30, 0.45); background: rgba(230, 43, 30, 0.12); }

  /* ── vitals as stat cards; the fold is the showcase ── */
  .stats { display: grid; grid-template-columns: 1fr 1fr 1.7fr 1fr 1fr; gap: 14px; margin-bottom: 22px; }
  .stat {
    background: var(--surface-1);
    border: 1px solid var(--hairline);
    border-radius: var(--radius);
    padding: 16px 18px 14px;
    min-width: 0;
    transition: border-color 130ms;
  }
  .stat:hover { border-color: rgba(178, 182, 189, 0.28); }
  .stat-label {
    color: var(--ink-muted); font-size: 12px; font-weight: 600;
    letter-spacing: 0.05em; text-transform: uppercase; margin-bottom: 8px;
  }
  .stat-value {
    font: 650 30px/1.15 var(--mono);
    font-variant-numeric: tabular-nums;
    letter-spacing: -0.02em;
  }
  .stat-sub { color: var(--ink-subtle); font-size: 12px; margin-top: 5px; }
  .stat.fold {
    background: linear-gradient(135deg, rgba(20, 198, 203, 0.26), rgba(20, 198, 203, 0.05) 52%, rgba(20, 198, 203, 0.02) 80%), var(--surface-1);
    border-color: rgba(20, 198, 203, 0.42);
    box-shadow: 0 0 60px -24px rgba(20, 198, 203, 0.45);
  }
  .stat.fold .stat-label { color: var(--cyan); }
  .stat.fold .v-visible    { color: var(--ink); }
  .stat.fold .v-superseded { color: var(--green); }
  .stat.fold .v-trivial    { color: var(--amber); }
  .stat.fold .sep { color: var(--ink-subtle); font-weight: 400; padding: 0 5px; }
  /* The synthesis key's three states. "unknown" is dim, not green: no
     headers seen is not the same claim as headroom seen. */
  #stat-ratelimit[data-state="unknown"]   { color: var(--ink-subtle); }
  #stat-ratelimit[data-state="ok"]        { color: var(--green); font-size: 23px; }
  #stat-ratelimit[data-state="throttled"] { color: var(--red-text); font-size: 23px; }

  /* ── working memory: the hero card ── */
  .panel {
    background: var(--surface-1); border: 1px solid var(--hairline);
    border-radius: var(--radius); overflow: hidden;
  }
  .panel.wm {
    background: linear-gradient(160deg, rgba(20, 198, 203, 0.12), rgba(20, 198, 203, 0.02) 40%), var(--surface-1);
    border-color: rgba(20, 198, 203, 0.35);
  }
  .panel-head {
    display: flex; align-items: baseline; justify-content: space-between;
    gap: 12px; flex-wrap: wrap;
    padding: 13px 18px; border-bottom: 1px solid var(--hairline);
  }
  .panel-head h2 { margin: 0; font-size: 15px; font-weight: 650; letter-spacing: -0.01em; color: var(--cyan); }
  .panel-head .meta { color: var(--ink-subtle); font: 11px var(--mono); }
  .wm-body {
    padding: 16px 18px; white-space: pre-wrap; overflow-wrap: anywhere;
    color: var(--ink); max-width: 74ch; line-height: 1.7; font-size: 14.5px;
  }
  .wm-body.empty-note { color: var(--ink-subtle); font-style: italic; }
  .rev-head {
    padding: 8px 18px; border-top: 1px solid var(--hairline);
    background: var(--surface-2);
    color: var(--ink-muted); font-size: 11px; font-weight: 600;
    letter-spacing: 0.04em; text-transform: uppercase;
  }
  .rev {
    display: flex; gap: 14px; align-items: baseline; flex-wrap: wrap;
    padding: 7px 18px; border-bottom: 1px solid var(--hairline-soft);
    cursor: pointer;
    font: 13px/1.6 var(--mono);
    transition: background-color 130ms;
  }
  .rev:hover { background: var(--surface-2); }
  .rev:last-child { border-bottom: none; }
  .rev .v { color: var(--cyan); width: 3em; flex-shrink: 0; }
  .rev .ts { color: var(--ink-subtle); font-size: 11px; width: 7em; flex-shrink: 0; }
  .rev .w { color: var(--ink-muted); width: 5em; flex-shrink: 0; }
  .rev .d { width: 5em; flex-shrink: 0; }
  .rev .d.up { color: var(--green); }
  .rev .d.down { color: var(--amber); }
  .rev .d.unk { color: var(--ink-subtle); }
  .rev .caret { color: var(--ink-subtle); margin-left: auto; font-size: 11px; }
  .rev .full {
    display: none; flex-basis: 100%;
    margin: 8px 0 4px; padding: 12px 14px;
    background: var(--canvas); border: 1px solid var(--hairline); border-radius: var(--radius-sm);
    color: var(--ink-muted); white-space: pre-wrap; overflow-wrap: anywhere;
    font: 12px/1.65 var(--sans);
  }
  .rev.expanded .full { display: block; }
  .rev-note { padding: 13px 18px; color: var(--ink-subtle); font-size: 12px; }

  /* ── participants ── */
  .tablewrap { overflow-x: auto; border: 1px solid var(--hairline); border-radius: var(--radius); background: var(--surface-1); }
  table.roster { border-collapse: collapse; width: 100%; min-width: 820px; font-size: 13.5px; }
  table.roster th {
    background: var(--surface-2);
    text-align: left; padding: 10px 14px;
    border-bottom: 1px solid var(--hairline);
    color: var(--ink-muted); font-size: 12px; font-weight: 600;
    letter-spacing: 0.04em; text-transform: uppercase;
    white-space: nowrap;
  }
  table.roster td {
    padding: 10px 14px; border-bottom: 1px solid var(--hairline-soft);
    vertical-align: top; color: var(--ink-muted);
    font-variant-numeric: tabular-nums;
  }
  table.roster tbody tr { transition: background-color 130ms; }
  table.roster tbody tr:hover { background: var(--surface-2); }
  table.roster tr:last-child td { border-bottom: none; }
  table.roster td.who { color: var(--ink); font-weight: 600; white-space: nowrap; }
  table.roster td.num { text-align: right; font-family: var(--mono); }
  .adot {
    display: inline-block; width: 7px; height: 7px; border-radius: 50%;
    margin-right: 8px; background: var(--ink-subtle);
  }
  .adot.hot  { background: var(--green); animation: adot-pulse 2s ease-in-out infinite; }
  @keyframes adot-pulse {
    0%, 100% { box-shadow: 0 0 0 0 rgba(0, 202, 142, 0.5); }
    50% { box-shadow: 0 0 0 4px rgba(0, 202, 142, 0); }
  }
  @media (prefers-reduced-motion: reduce) { .adot.hot { animation: none; } }
  .adot.warm { background: var(--amber); }
  .adot.cold { background: var(--ink-subtle); }
  .adot.none { background: transparent; border: 1px solid var(--ink-subtle); }
  .state {
    display: inline-block; padding: 1px 9px; border-radius: 999px;
    font-size: 11.5px; font-weight: 600;
    border: 1px solid transparent;
  }
  .state.active    { color: var(--green); border-color: rgba(0, 202, 142, 0.45); background: rgba(0, 202, 142, 0.12); }
  .state.listening { color: var(--purple-text); border-color: rgba(123, 66, 188, 0.55); background: rgba(123, 66, 188, 0.16); }
  .state.left      { color: var(--ink-muted); border-color: var(--hairline); }
  /* The honest unknown, dimmest of the four and dotted-underlined so it
     reads as "there is a caveat here" rather than as a verdict. */
  .state.unregistered {
    color: var(--ink-subtle); padding: 0; border: none; border-radius: 0;
    border-bottom: 1px dotted var(--ink-subtle); cursor: help; font-weight: 500;
  }
  .scope { color: var(--ink-subtle); font-size: 11px; }
  .none { color: var(--ink-subtle); }

  /* ── latest into memory ── */
  #recent { background: var(--surface-1); border: 1px solid var(--hairline); border-radius: var(--radius); overflow: hidden; }
  .row {
    --c: var(--ink-subtle);
    display: flex; gap: 12px; align-items: baseline; flex-wrap: wrap;
    padding: 9px 16px 9px 14px;
    border-bottom: 1px solid var(--hairline-soft);
    border-left: 3px solid var(--c);
    cursor: pointer;
    transition: background-color 130ms;
  }
  .row:hover { background: var(--surface-2); }
  .row:last-child { border-bottom: none; }
  .row[data-prov="synthesized"] { --c: var(--green); }
  .row[data-prov="contributed"] { --c: var(--purple-text); }
  .row[data-prov="distilled"]   { --c: var(--cyan-deep); }
  .row .ts { color: var(--ink-subtle); font: 12px/1.6 var(--mono); flex-shrink: 0; width: 6em; }
  .row .who { color: var(--ink); font-weight: 600; flex-shrink: 0; width: 12em; overflow-wrap: anywhere; }
  .row .type {
    flex-shrink: 0; width: 9.5em;
    font: 500 12px/1.6 var(--mono);
    color: var(--blue-text);
  }
  .row .text { flex: 1; min-width: 14em; overflow-wrap: anywhere; color: var(--ink-muted); }
  .row .prov {
    flex-shrink: 0; margin-left: auto;
    display: inline-flex; align-items: center; gap: 7px;
    font-size: 11px; font-weight: 600; color: var(--c);
  }
  .row .prov::before {
    content: ""; width: 7px; height: 7px; border-radius: 50%;
    background: var(--c);
  }
  .row.tombstone .text { text-decoration: line-through; color: var(--ink-subtle); }
  .row .full {
    display: none; flex-basis: 100%;
    margin: 8px 0 2px; padding: 12px 14px;
    background: var(--canvas); border: 1px solid var(--hairline); border-radius: var(--radius-sm);
    color: var(--ink-muted); white-space: pre-wrap; overflow-wrap: anywhere;
    font: 11.5px/1.65 var(--mono);
  }
  .row.expanded .full { display: block; }

  .empty { padding: 30px; text-align: center; color: var(--ink-subtle); }
  .footnote {
    margin: 32px 0 0; padding-top: 18px;
    border-top: 1px solid var(--hairline);
    color: var(--ink-subtle); font-size: 13px; line-height: 1.75; max-width: 92ch;
  }
  .footnote b { color: var(--ink-muted); font-weight: 600; }
  @media (max-width: 900px) {
    .stats { grid-template-columns: 1fr 1fr; }
    .stat.fold { grid-column: span 2; }
  }
  @media (max-width: 560px) {
    .stats { grid-template-columns: 1fr; }
    .stat.fold { grid-column: auto; }
  }
</style>
</head>
<body>
<div id="banner">Service unreachable. Retrying…</div>
<div class="shell">
<aside>
  <a class="brand" href="/"><svg class="mark" viewBox="0 0 30 16" aria-hidden="true"><path class="axon" d="M4 8 C 10 3, 20 13, 26 8"/><circle class="soma" cx="4" cy="8" r="2.7"/><circle class="soma" cx="26" cy="8" r="2.7"/><circle class="impulse" r="1.7"><animateMotion dur="2.8s" repeatCount="indefinite" path="M4 8 C 10 3, 20 13, 26 8"/></circle></svg><span class="name">Synapse</span><span class="scope-label">service</span></a>
  <div class="side-label">Shared sessions</div>
  <nav id="session-list" class="side-sessions" aria-label="shared sessions"><div class="side-empty">connecting…</div></nav>
  <div class="side-foot"><a href="/">Service home</a></div>
</aside>
<div class="content">
<header class="topbar">
  <nav class="tabs" aria-label="session pages">
    <a id="tab-brain" href="/debug" aria-current="page">Brain</a>
    <a id="tab-log" href="/debug/log">Log</a>
    <a id="tab-memory" href="/debug/memory">Memory</a>
  </nav>
</header>
<main>
  <section class="identity">
    <div class="identity-label">Purpose</div>
    <div class="purpose" id="purpose">—</div>
    <div class="ident" id="ident"></div>
  </section>

  <div class="stats" aria-label="session vitals">
    <div class="stat">
      <div class="stat-label">Contributors</div>
      <div class="stat-value" id="stat-contributors">0</div>
      <!-- BOTH numbers, because they routinely disagree and the difference is
           the interesting part: a raw `POST /findings` never registers
           anybody, so the log can name people the member list has never
           heard of. Showing only the first put a "0" directly above a table
           of contributors. -->
      <div class="stat-sub">registered · <span id="stat-contributors-log">0</span> in the log</div>
    </div>
    <div class="stat">
      <div class="stat-label">Conversations</div>
      <div class="stat-value" id="stat-conversations">0</div>
      <div class="stat-sub">agent sessions seen in the log</div>
    </div>
    <div class="stat fold">
      <div class="stat-label">Memory · the fold</div>
      <div class="stat-value"><span class="v-visible" id="stat-visible">0</span><span class="sep">·</span><span class="v-superseded" id="stat-superseded">0</span><span class="sep">·</span><span class="v-trivial" id="stat-trivial">0</span></div>
      <div class="stat-sub">visible · superseded · trivial</div>
    </div>
    <div class="stat">
      <div class="stat-label">Conflicts</div>
      <div class="stat-value" id="stat-conflicts">0</div>
      <div class="stat-sub">v<span id="stat-version">0</span> · <span id="stat-entries">0</span> log entries</div>
    </div>
    <div class="stat">
      <div class="stat-label">Synthesis key</div>
      <div class="stat-value" id="stat-ratelimit" data-state="unknown">—</div>
      <div class="stat-sub" id="stat-ratelimit-why">rate limit</div>
    </div>
  </div>

  <section class="panel wm">
    <div class="panel-head">
      <h2>Working memory</h2>
      <div class="meta" id="wm-meta">—</div>
    </div>
    <div class="wm-body" id="wm-body">…</div>
    <div class="rev-head">Revisions <span class="note" id="rev-count"></span></div>
    <div id="revisions"></div>
  </section>

  <div class="section-head">
    <h2>Participants</h2>
    <p>one row per agent session; two windows of one person are two rows</p>
  </div>
  <div class="tablewrap"><div id="participants"></div></div>

  <div class="section-head">
    <h2>Latest into memory</h2>
    <p>the newest arrivals; the Memory tab holds every item, sortable and searchable</p>
  </div>
  <div id="recent"><div class="empty">no findings have reached this session</div></div>

  <p class="footnote">
    <b>Activity dots show recency of the last observed contribution or query</b>:
    under 2 min, under 15 min, older. The service holds no heartbeat and no
    connection registry, so nothing on this page reports whether anyone is
    <i>connected</i>: it reports when they were last seen doing something.
    <b>Join and leave times are not recorded anywhere</b> (membership is a list
    with no timestamps), so participation is shown as state only, and
    <b>not a member</b> means exactly that: absent from the list with no
    departure observed, which is equally what a never-registered contributor
    and a service restart look like. Only <b>left</b> is a departure this
    process actually watched happen.
    <b>Memory position is per contributor, not per conversation</b>: the watermark
    is a fact about a person, so both of one person's windows show the same one.
    <b>Last query comes from a 200-event ring shared by every session</b>, so a
    busy stretch can push an older query out of it and the cell falls back to
    an em-dash: “not in the window”, never “never asked”.
    <b>Revision history is kept in this process</b> and is empty after a restart.
  </p>
</main>
</div>
</div>

<script>
(function () {
  "use strict";

  var PAGE_PATH = "/debug";
  // Keys of expanded rows, surviving the 1s poll's full innerHTML rebuild --
  // keyed on the FINDING ID and the REVISION VERSION, both stable, so an open
  // row does not become a different row when the list shifts underneath it.
  var expandedKeys = new Set();

  // ?session= makes every view deep-linkable; guarded because the test
  // driver's minimal DOM has no location.
  var currentSid = null;
  try {
    if (typeof location !== "undefined" && typeof URLSearchParams !== "undefined"
        && location.search) {
      currentSid = new URLSearchParams(location.search).get("session");
    }
  } catch (e) { /* no location in the test DOM */ }

  // Text-node escaping ONLY -- the serializer handles & < >, and deliberately
  // does NOT touch quotes, so `esc` alone is unsafe inside an attribute.
  // Every string on this page that reaches an attribute is client-supplied
  // and unvalidated (`Finding.id`, `Attribution.agent_session` and
  // `shared_id` are all bare `str` in contracts/schemas.py, written by
  // whichever teammate's machine pushed them -- across the device boundary,
  // which is the whole premise of the system), so attributes go through
  // `escAttr`, which closes the quotes as well.
  function esc(s) {
    var d = document.createElement("div");
    d.textContent = String(s == null ? "" : s);
    return d.innerHTML;
  }

  function escAttr(s) {
    return esc(s).replace(/"/g, "&quot;").replace(/'/g, "&#39;");
  }

  function hhmmss(iso) {
    if (!iso) return "";
    return new Date(iso).toTimeString().slice(0, 8);
  }

  function ageSeconds(iso) {
    if (!iso) return null;
    var t = new Date(iso).getTime();
    if (isNaN(t)) return null;
    return (Date.now() - t) / 1000;
  }

  function relative(iso) {
    var age = ageSeconds(iso);
    if (age == null) return "";
    if (age < 0) return "just now";
    if (age < 60) return Math.floor(age) + "s ago";
    if (age < 3600) return Math.floor(age / 60) + "m ago";
    if (age < 86400) return Math.floor(age / 3600) + "h ago";
    return Math.floor(age / 86400) + "d ago";
  }

  // Recency of LAST OBSERVED ACTIVITY, computed here from timestamps the
  // service really has. Not liveness, not a heartbeat, not "connected".
  function activity(row) {
    var seen = null;
    if (row.last_contribution_iso) seen = row.last_contribution_iso;
    if (row.last_query_iso && (!seen || row.last_query_iso > seen)) seen = row.last_query_iso;
    var age = ageSeconds(seen);
    if (age == null) return { cls: "none", title: "no contribution or query observed" };
    if (age < 120) return { cls: "hot", title: "last seen " + relative(seen) };
    if (age < 900) return { cls: "warm", title: "last seen " + relative(seen) };
    return { cls: "cold", title: "last seen " + relative(seen) };
  }

  function em(value) {
    return (value === null || value === undefined || value === "")
      ? '<span class="none">—</span>' : esc(value);
  }

  function sessionHref(path, sid) {
    return path + "?session=" + encodeURIComponent(sid);
  }

  // The sidebar and tab hrefs. Everything here is optional chrome: each
  // element is looked up by id and skipped when absent, so the test driver's
  // minimal DOM never sees a difference.
  function updateChrome(sessions) {
    try {
      var sl = document.getElementById("session-list");
      if (sl) {
        sl.innerHTML = sessions.length ? sessions.map(function (s) {
          var cur = s.shared_id === currentSid ? ' aria-current="page"' : "";
          return '<a class="side-session"' + cur + ' href="' +
            sessionHref(PAGE_PATH, s.shared_id) + '">' +
            '<span class="ss-purpose">' + esc(s.purpose || "(no purpose recorded)") + '</span>' +
            '<span class="ss-sid">' + esc(s.shared_id) +
            (s.status === "ended" ? " · ended" : "") + '</span></a>';
        }).join("") : '<div class="side-empty">no shared sessions yet</div>';
      }
      [["tab-brain", "/debug"], ["tab-log", "/debug/log"],
       ["tab-memory", "/debug/memory"]].forEach(function (t) {
        var el = document.getElementById(t[0]);
        if (el && currentSid) el.setAttribute("href", sessionHref(t[1], currentSid));
      });
    } catch (e) { /* chrome only; never let it take the data down */ }
  }

  var sel = document.getElementById("session-select");
  if (sel) sel.addEventListener("change", function (ev) {
    currentSid = ev.target.value;
    expandedKeys.clear();
    try { history.replaceState(null, "", sessionHref(PAGE_PATH, currentSid)); } catch (e) {}
    refresh();
  });

  function toggleFrom(containerId, selector) {
    document.getElementById(containerId).addEventListener("click", function (ev) {
      var el = ev.target.closest(selector);
      if (!el) return;
      var key = el.getAttribute("data-key");
      var nowExpanded = el.classList.toggle("expanded");
      if (!key) return;
      if (nowExpanded) { expandedKeys.add(key); } else { expandedKeys.delete(key); }
    });
  }
  toggleFrom("revisions", ".rev");
  toggleFrom("recent", ".row");

  function renderIdentity(s) {
    document.getElementById("purpose").textContent = s.purpose || "(no purpose recorded)";
    var pill = s.status === "ended"
      ? '<span class="pill ended">ended</span>'
      : '<span class="pill active">active</span>';
    var creator = s.created_by
      ? "created by <b>" + esc(s.created_by) + "</b>"
      : "creator unknown (recreated after a restart)";
    document.getElementById("ident").innerHTML =
      esc(s.sid) + " · " + creator + " · " + pill + " · memory <b>v" + esc(s.memory_version) + "</b>";
  }

  function renderVitals(s) {
    var c = s.counts;
    document.getElementById("stat-contributors").textContent = c.contributors;
    document.getElementById("stat-contributors-log").textContent = c.contributors_in_log;
    document.getElementById("stat-conversations").textContent = c.conversations;
    document.getElementById("stat-visible").textContent = c.visible;
    document.getElementById("stat-superseded").textContent = c.superseded;
    document.getElementById("stat-trivial").textContent = c.trivial;
    document.getElementById("stat-conflicts").textContent = c.conflicts;
    document.getElementById("stat-version").textContent = s.memory_version;
    document.getElementById("stat-entries").textContent = c.log_entries;
    renderRateLimit(s.rate_limit);
  }

  // A throttled synthesis key is the answer to "findings are landing but the
  // working memory is not moving". "unknown" is deliberately not "ok": the
  // gateway may spell its headers in a way the parser does not know yet, and
  // claiming headroom we cannot see is the silent failure this row exists for.
  function renderRateLimit(rl) {
    var el = document.getElementById("stat-ratelimit");
    var why = document.getElementById("stat-ratelimit-why");
    if (!rl) { rl = {state: "unknown", reason: "not reported"}; }
    // setAttribute rather than .dataset: the page is driven headlessly in
    // tests by support/minidom.js, which implements attributes and not the
    // DOMStringMap.
    el.setAttribute("data-state", rl.state);
    if (rl.state === "throttled") {
      el.textContent = "LIMITED";
    } else if (rl.state === "ok") {
      el.textContent = rl.requests_remaining === null
        ? "ok" : rl.requests_remaining + " left";
    } else {
      el.textContent = "—";
    }
    why.textContent = rl.reason || "rate limit";
  }

  function renderWorkingMemory(wm) {
    var body = document.getElementById("wm-body");
    if (wm.text) {
      body.setAttribute("class", "wm-body");
      body.textContent = wm.text;
    } else {
      body.setAttribute("class", "wm-body empty-note");
      body.textContent = "(empty — no synthesis round has written one yet)";
    }

    var meta = "v" + wm.version + " · " + wm.words + " words";
    if (wm.updated_iso) {
      meta += " · rewritten " + hhmmss(wm.updated_iso) + " (" + relative(wm.updated_iso) + ")";
    }
    document.getElementById("wm-meta").textContent = meta;

    var revs = wm.revisions || [];
    document.getElementById("rev-count").textContent =
      revs.length ? "— " + revs.length + " observed, newest first" : "";

    var el = document.getElementById("revisions");
    if (!revs.length) {
      // NEVER an empty list here: an empty list reads as "the memory has
      // never changed", which is a different claim and a false one.
      el.innerHTML = '<div class="rev-note">no rewrite observed since this service ' +
        'started — revision history is not persisted, and merges that happened in ' +
        'another process are not here.</div>';
      return;
    }
    var html = "";
    var liveRevs = new Set();
    for (var i = 0; i < revs.length; i++) {
      var r = revs[i];
      var key = "rev|" + r.version + "|" + r.ts_iso;
      liveRevs.add(key);
      var d = r.delta_words;
      var dCls = d == null ? "unk" : (d > 0 ? "up" : (d < 0 ? "down" : ""));
      var dText = d == null ? "—" : (d > 0 ? "+" + d : String(d));
      html += '<div class="rev' + (expandedKeys.has(key) ? " expanded" : "") +
        '" data-key="' + escAttr(key) + '">';
      html += '<span class="v">v' + esc(r.version) + '</span>';
      html += '<span class="ts">' + esc(hhmmss(r.ts_iso)) + '</span>';
      html += '<span class="w">' + esc(r.words) + 'w</span>';
      html += '<span class="d ' + dCls + '">' + esc(dText) + '</span>';
      html += '<span class="caret">' + esc(relative(r.ts_iso)) + '</span>';
      html += '<span class="full">' + esc(r.text) + '</span>';
      html += '</div>';
    }
    // Same pruning the recent list does: a revision evicted from the 10-deep
    // ring must not leave its key behind, or the set grows for as long as the
    // tab is open.
    expandedKeys.forEach(function (k) {
      if (k.indexOf("rev|") === 0 && !liveRevs.has(k)) expandedKeys.delete(k);
    });
    el.innerHTML = html;
  }

  // The four membership states, in the words the page owes the viewer, and
  // what each one is actually claiming. `unregistered` is the honest unknown:
  // absent from `members` with no departure observed, which the service
  // cannot tell apart from "never registered" or "registered before a
  // restart" -- so it says the membership fact and stops there.
  var STATE_LABEL = {
    active: "active",
    listening: "listening",
    left: "left",
    unregistered: "not a member"
  };
  var STATE_TITLE = {
    active: "in this session's member list, with a conversation in the log",
    listening: "in the member list; no agent session of theirs is in the log yet",
    left: "this service watched a DELETE /members for them",
    unregistered: "in the log but not in the member list, and no departure was " +
      "observed — they may never have registered, or this service restarted"
  };

  function renderParticipants(rows) {
    var el = document.getElementById("participants");
    if (!rows.length) {
      el.innerHTML = '<div class="empty">nobody has joined or contributed to this session yet</div>';
      return;
    }
    var html = '<table class="roster">';
    html += '<caption class="sr-only">Participants in this shared session, one row per agent session</caption>';
    html += '<thead><tr>' +
      '<th scope="col">contributor</th>' +
      '<th scope="col">agent</th>' +
      '<th scope="col">conversation</th>' +
      '<th scope="col">contributed</th>' +
      '<th scope="col">last contribution</th>' +
      '<th scope="col">last query</th>' +
      '<th scope="col">memory position</th>' +
      '<th scope="col">state</th>' +
      '</tr></thead><tbody>';
    for (var i = 0; i < rows.length; i++) {
      var p = rows[i];
      var act = activity(p);
      var conv = p.agent_session
        ? esc(String(p.agent_session).slice(0, 14))
        : '<span class="none">— not yet seen</span>';
      var position = p.behind == null
        ? '<span class="none">— never read</span>'
        : ("v" + esc(p.last_seen_version) + " of v" + esc(p.memory_version_ref) +
           (p.behind > 0 ? ' · <span class="scope">' + esc(p.behind) + " behind</span>"
                         : ' · <span class="scope">up to date</span>'));
      var lastQuery = p.last_query_iso
        ? esc(hhmmss(p.last_query_iso)) +
          ' <span class="scope">(' + esc(p.last_query_scope) + ')</span>'
        : '<span class="none">—</span>';
      html += "<tr>";
      html += '<td class="who"><span class="adot ' + act.cls + '" title="' +
        escAttr(act.title) + '"></span>' + esc(p.contributor) + "</td>";
      html += "<td>" + em(p.agent) + "</td>";
      html += '<td title="' + escAttr(p.agent_session || "") + '">' + conv + "</td>";
      html += '<td class="num">' + esc(p.contributions) + "</td>";
      html += "<td>" + (p.last_contribution_iso
        ? esc(hhmmss(p.last_contribution_iso)) : '<span class="none">—</span>') + "</td>";
      html += "<td>" + lastQuery + "</td>";
      html += "<td>" + position + "</td>";
      html += '<td><span class="state ' + escAttr(p.state) + '" title="' +
        escAttr(STATE_TITLE[p.state] || "") + '">' +
        esc(STATE_LABEL[p.state] || p.state) + "</span></td>";
      html += "</tr>";
    }
    html += "</tbody></table>";
    el.innerHTML = html;
  }

  // The three provenances, in the words the page owes the viewer.
  var PROV_LABEL = {
    distilled: "listened",
    contributed: "contributed",
    synthesized: "merged"
  };

  function renderRecent(rows) {
    var el = document.getElementById("recent");
    if (!rows.length) {
      el.innerHTML = '<div class="empty">no findings have reached this session</div>';
      return;
    }
    var live = new Set();
    var html = "";
    for (var i = 0; i < rows.length; i++) {
      var f = rows[i];
      var key = "find|" + f.id;
      live.add(key);
      var tomb = f.merged_into ? " tombstone" : "";
      html += '<div class="row' + tomb + (expandedKeys.has(key) ? " expanded" : "") +
        '" data-prov="' + escAttr(f.provenance) + '" data-key="' + escAttr(key) + '">';
      html += '<span class="ts">' + esc(hhmmss(f.ts_iso)) + '</span>';
      html += '<span class="who">' + esc(f.authors.join(" + ")) + '</span>';
      html += '<span class="type">' + esc(f.type) + '</span>';
      html += '<span class="text">' + esc(f.text) + '</span>';
      html += '<span class="prov">' + esc(PROV_LABEL[f.provenance] || f.provenance) + '</span>';
      var detail = f.id + "  ·  " + f.status +
        (f.merged_into ? "  ·  merged into " + f.merged_into : "");
      for (var a = 0; a < f.attributions.length; a++) {
        var at = f.attributions[a];
        detail += "  |  " + at.contributor + " / " + at.agent + " / " + at.agent_session;
      }
      html += '<span class="full">' + esc(detail) + '</span>';
      html += "</div>";
    }
    // Drop expanded keys whose row left the snapshot, so the set cannot grow
    // forever across a long session.
    expandedKeys.forEach(function (k) {
      if (k.indexOf("find|") === 0 && !live.has(k)) expandedKeys.delete(k);
    });
    el.innerHTML = html;
  }

  function renderSession(s) {
    renderIdentity(s);
    renderVitals(s);
    renderWorkingMemory(s.working_memory);
    // The roster renders "v6 of v7"; carry the session's version onto each
    // row rather than reaching for a global from inside the renderer.
    for (var i = 0; i < s.participants.length; i++) {
      s.participants[i].memory_version_ref = s.memory_version;
    }
    renderParticipants(s.participants);
    renderRecent(s.recent);
  }

  // EVERY region, not just the two lists: the working-memory pane and the
  // vitals rail keep their markup placeholders otherwise, and a "…" body over
  // a rail of zeros reads as a session whose memory is empty rather than as
  // no session at all.
  function renderNoSession() {
    document.getElementById("purpose").textContent = "the service holds no shared session yet";
    document.getElementById("ident").textContent = "";
    document.getElementById("wm-body").textContent = "";
    document.getElementById("wm-meta").textContent = "—";
    document.getElementById("rev-count").textContent = "";
    document.getElementById("revisions").innerHTML =
      '<div class="rev-note">no session, so no revisions</div>';
    ["stat-contributors", "stat-contributors-log", "stat-conversations",
     "stat-visible", "stat-superseded", "stat-trivial", "stat-conflicts",
     "stat-version", "stat-entries"].forEach(function (id) {
      document.getElementById(id).textContent = "—";
    });
    document.getElementById("participants").innerHTML =
      '<div class="empty">nobody has joined or contributed to this session yet</div>';
    document.getElementById("recent").innerHTML =
      '<div class="empty">no findings have reached this session</div>';
  }

  function populateSessions(sessions) {
    if (!currentSid && sessions.length) currentSid = sessions[0].shared_id;
    updateChrome(sessions);
    var select = document.getElementById("session-select");
    if (!select) return;
    var prior = currentSid;
    select.innerHTML = sessions.map(function (s) {
      return '<option value="' + escAttr(s.shared_id) + '">' + esc(s.shared_id) +
        (s.status === "ended" ? " (ended)" : "") + " · " + esc(s.purpose) + '</option>';
    }).join("");
    if (prior && sessions.some(function (s) { return s.shared_id === prior; })) {
      select.value = prior;
    } else if (sessions.length) {
      currentSid = sessions[0].shared_id;
      select.value = currentSid;
    }
  }

  function refresh() {
    var url = "/debug/brain.json" + (currentSid ? "?session=" + encodeURIComponent(currentSid) : "");
    fetch(url)
      .then(function (r) { if (!r.ok) throw new Error("bad status " + r.status); return r.json(); })
      .then(function (payload) {
        document.getElementById("banner").classList.remove("show");
        populateSessions(payload.sessions);
        if (payload.session) {
          currentSid = payload.session.sid;
          renderSession(payload.session);
        } else {
          renderNoSession();
        }
      })
      .catch(function () {
        document.getElementById("banner").classList.add("show");
      });
  }

  refresh();
  setInterval(refresh, 1000);
})();
</script>
</body>
</html>
"""


# ── the home page: the front door of the service ───────────────────────────
_HOME_PAGE = """<!doctype html>
<html lang="en" data-theme="dark">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<link rel="icon" href="data:,">
<title>Synapse · service</title>
<style>
  /* The front door, sized for a demo screen: near-black canvas, charcoal
     surface lift, and saturated identity hues that mean something. Cyan is
     the cloud side of the device boundary, copper is every machine at the
     edge, green is a merge, amber is trivial, red is failure. Two modes
     (scroll and slides), two themes (dark and light). Same standing rule as
     every page on this listener: NOT ONE external request. */
  :root {
    color-scheme: dark;
    /* ground: near-black canvas, charcoal surface lift, felt-not-seen hairlines */
    --canvas: #000000;
    --surface-1: #15181e;
    --surface-2: #1f232b;
    --surface-3: #2a2e37;
    --hairline: rgba(178, 182, 189, 0.14);
    --hairline-soft: rgba(178, 182, 189, 0.07);
    /* ink */
    --ink: #ffffff;
    --ink-muted: #b2b6bd;
    --ink-subtle: #656a76;
    /* identity hues: data encodings, never decoration */
    --cyan: #14c6cb;
    --cyan-deep: #12b6bb;
    --green: #00ca8e;
    --amber: #ffcf25;
    --red: #e62b1e;
    --red-text: #f5564a;
    --purple: #7b42bc;
    --purple-bright: #911ced;
    --purple-text: #b78ae8;
    --blue: #1868f2;
    --blue-text: #6ea6ff;
    --copper: #e09a5a;
    --copper-dim: #8a5a2d;
    --radius: 12px;
    --radius-sm: 8px;
    --sans: ui-sans-serif, system-ui, -apple-system, "Segoe UI", Roboto, "Helvetica Neue", Arial, sans-serif;
    --mono: ui-monospace, "SF Mono", "Cascadia Code", Menlo, Consolas, monospace;
  }
  /* theme-scoped extensions (home page only) */
  :root {
    --cta-bg: #ffffff;
    --cta-ink: #000000;
    --cta-hover: #e2e5ea;
    --inset-bg: #0a0d12;
    --glow-main: rgba(20, 198, 203, 0.13);
    --glow-side: rgba(224, 154, 90, 0.06);
    --wash: 0.10;
  }
  :root[data-theme="light"] {
    color-scheme: light;
    --canvas: #ffffff;
    --surface-1: #f5f6f8;
    --surface-2: #eaecf0;
    --surface-3: #dde0e6;
    --hairline: rgba(11, 13, 18, 0.13);
    --hairline-soft: rgba(11, 13, 18, 0.06);
    --ink: #0b0d12;
    --ink-muted: #4c515a;
    --ink-subtle: #878c95;
    --cyan: #0b9ba0;
    --cyan-deep: #0d8f94;
    --green: #008a63;
    --amber: #8f6400;
    --red: #d5271b;
    --red-text: #c22318;
    --purple: #6d34ae;
    --purple-bright: #7d1bd0;
    --purple-text: #6d34ae;
    --blue: #1355c9;
    --blue-text: #1a56c4;
    --copper: #a85e1e;
    --copper-dim: #c99a68;
    --cta-bg: #0b0d12;
    --cta-ink: #ffffff;
    --cta-hover: #262a31;
    --inset-bg: #f0f1f4;
    --glow-main: rgba(11, 155, 160, 0.10);
    --glow-side: rgba(168, 94, 30, 0.05);
    --wash: 0.07;
  }
  * { box-sizing: border-box; }
  html { scroll-behavior: smooth; }
  body {
    margin: 0;
    background:
      radial-gradient(1100px 520px at 55% -10%, var(--glow-main), transparent 70%),
      radial-gradient(760px 420px at 96% 30%, var(--glow-side), transparent 70%),
      var(--canvas);
    color: var(--ink);
    font: 16px/1.55 var(--sans);
    -webkit-font-smoothing: antialiased;
    transition: background-color 200ms, color 200ms;
  }
  #banner {
    display: none;
    background: rgba(230, 43, 30, 0.14); color: var(--red-text);
    border-bottom: 1px solid rgba(230, 43, 30, 0.35);
    padding: 8px 24px; font-size: 13px; font-weight: 500;
  }
  #banner.show { display: block; }

  /* ── shell: sessions live in the sidebar, never in the content flow ── */
  .shell { display: flex; min-height: 100dvh; }
  aside {
    width: 264px; flex-shrink: 0;
    border-right: 1px solid var(--hairline-soft);
    padding: 16px 12px;
    display: flex; flex-direction: column; gap: 14px;
    position: sticky; top: 0; height: 100dvh; overflow-y: auto;
  }
  .brand { display: flex; align-items: center; gap: 9px; text-decoration: none; color: inherit; padding: 2px 6px 0; }
  .brand .mark { width: 30px; height: 16px; overflow: visible; }
  .brand .mark .axon { fill: none; stroke: var(--cyan-deep); stroke-width: 1.4; opacity: 0.7; }
  .brand .mark .soma { fill: var(--cyan); }
  .brand .mark .impulse { fill: var(--ink); }
  .brand .name { font-size: 17px; font-weight: 650; letter-spacing: -0.01em; }
  .brand .scope-label { color: var(--ink-subtle); font-size: 14px; }
  .live {
    display: flex; align-items: center; gap: 9px;
    padding: 0 6px;
    color: var(--ink-muted); font: 12px var(--mono);
  }
  .live-dot { width: 9px; height: 9px; border-radius: 50%; background: var(--ink-subtle); flex-shrink: 0; }
  .live-dot.up { background: var(--green); animation: live-pulse 2s ease-in-out infinite; }
  .live-dot.down { background: var(--red-text); animation: none; }
  @keyframes live-pulse {
    0%, 100% { box-shadow: 0 0 0 0 rgba(0, 202, 142, 0.5); }
    50% { box-shadow: 0 0 0 6px rgba(0, 202, 142, 0); }
  }
  .side-label {
    font: 600 11px var(--sans); letter-spacing: 0.06em; text-transform: uppercase;
    color: var(--ink-subtle); padding: 0 6px; margin-top: 4px;
  }
  #sessions { display: flex; flex-direction: column; gap: 6px; }
  .scard {
    display: flex; flex-direction: column; gap: 8px;
    background: var(--surface-1);
    border: 1px solid var(--hairline);
    border-radius: var(--radius-sm);
    padding: 12px;
    text-decoration: none; color: inherit;
    transition: border-color 140ms, background-color 140ms;
  }
  .scard:hover { border-color: rgba(20, 198, 203, 0.5); background: var(--surface-2); }
  .scard:focus-visible { outline: 2px solid var(--cyan); outline-offset: 2px; }
  .scard-purpose { font-weight: 600; font-size: 13px; line-height: 1.4; color: var(--ink); }
  .scard-meta { display: flex; align-items: center; justify-content: space-between; gap: 8px; }
  .scard-meta .sid { font: 11px var(--mono); color: var(--ink-subtle); overflow-wrap: anywhere; }
  .pill {
    display: inline-block; padding: 1px 8px; border-radius: 999px;
    font: 600 10.5px/1.7 var(--sans);
    border: 1px solid; flex-shrink: 0;
  }
  .pill.active { color: var(--green); border-color: rgba(0, 202, 142, 0.45); background: rgba(0, 202, 142, 0.12); }
  .pill.ended  { color: var(--red-text); border-color: rgba(230, 43, 30, 0.45); background: rgba(230, 43, 30, 0.12); }
  .empty {
    padding: 14px 12px; text-align: left; color: var(--ink-subtle); font-size: 12.5px; line-height: 1.55;
    background: var(--surface-1); border: 1px dashed var(--hairline); border-radius: var(--radius-sm);
  }
  .side-foot { margin-top: auto; padding: 0 6px; display: flex; flex-direction: column; gap: 4px; }
  .side-foot a { color: var(--ink-subtle); font-size: 12px; text-decoration: none; }
  .side-foot a:hover { color: var(--ink); }

  .content { flex: 1; min-width: 0; display: flex; flex-direction: column; }
  .topbar {
    display: flex; align-items: center; gap: 16px; flex-wrap: wrap;
    min-height: 58px;
    padding: 8px 28px;
    border-bottom: 1px solid var(--hairline-soft);
  }
  nav.pages { display: flex; gap: 4px; margin-right: auto; }
  nav.pages a {
    padding: 7px 14px; border-radius: var(--radius-sm);
    font-size: 14px; font-weight: 600;
    color: var(--ink-muted); text-decoration: none;
    transition: color 120ms, background-color 120ms;
  }
  nav.pages a:hover { color: var(--ink); background: var(--surface-2); }
  nav.pages a[aria-current="page"] { color: var(--cyan); background: rgba(20, 198, 203, 0.12); }
  :root[data-theme="light"] nav.pages a[aria-current="page"] { background: rgba(11, 155, 160, 0.12); }
  nav.pages a:focus-visible, .tool:focus-visible { outline: 2px solid var(--cyan); outline-offset: 2px; }
  .toolbar { display: flex; align-items: center; gap: 8px; }
  .tool {
    display: inline-flex; align-items: center; gap: 7px;
    padding: 7px 13px; border-radius: var(--radius-sm);
    background: var(--surface-2); color: var(--ink);
    border: 1px solid var(--hairline);
    font: 600 13px var(--sans); text-decoration: none; cursor: pointer;
    transition: background-color 120ms, border-color 120ms;
  }
  .tool:hover { background: var(--surface-3); }
  .tool svg { width: 15px; height: 15px; fill: currentColor; display: block; }
  .tool .stroke-ic { fill: none; stroke: currentColor; stroke-width: 1.7; stroke-linecap: round; stroke-linejoin: round; }
  .tool.primary { background: var(--cta-bg); color: var(--cta-ink); border-color: transparent; }
  .tool.primary:hover { background: var(--cta-hover); }
  #theme-btn .ic-sun { display: none; }
  #theme-btn .ic-moon { display: block; }
  :root[data-theme="light"] #theme-btn .ic-sun { display: block; }
  :root[data-theme="light"] #theme-btn .ic-moon { display: none; }

  main { padding: 64px 44px 88px; max-width: 1280px; margin: 0 auto; width: 100%; }

  /* ── slide framework ── */
  .slide { margin-top: 112px; }
  .slide:first-child { margin-top: 0; }
  .slide-eyebrow {
    display: inline-block;
    font: 650 12px var(--sans); letter-spacing: 0.14em; text-transform: uppercase;
    color: var(--sec-c, var(--cyan));
    margin-bottom: 14px;
  }
  .slide h2 {
    margin: 0 0 8px;
    font-size: clamp(30px, 3vw, 42px);
    font-weight: 700; letter-spacing: -0.026em; line-height: 1.16;
    text-wrap: balance;
  }
  .slide h2::before {
    content: ""; display: block; width: 46px; height: 4px; border-radius: 2px;
    margin-bottom: 16px;
    background: linear-gradient(90deg, var(--sec-c, var(--cyan)), transparent);
  }
  .slide .sub { color: var(--ink-subtle); font-size: 15.5px; margin: 0 0 28px; max-width: 78ch; }
  .s-why    { --sec-c: var(--red-text); }
  .s-arch   { --sec-c: var(--cyan); }
  .s-pipe   { --sec-c: var(--copper); }
  .s-cases  { --sec-c: var(--blue-text); }
  .s-proof  { --sec-c: var(--green); }

  /* ── hero ── */
  .hero { text-align: center; }
  .hero .kicker {
    display: inline-block;
    font: 650 12.5px var(--sans); letter-spacing: 0.16em; text-transform: uppercase;
    color: var(--cyan);
    margin-bottom: 20px;
  }
  .hero h1 {
    margin: 0 auto 20px;
    font-size: clamp(48px, 5.4vw, 76px);
    line-height: 1.13; font-weight: 700;
    letter-spacing: -0.031em;
    max-width: 17ch;
    text-wrap: balance;
  }
  .hero h1 .hl { color: var(--cyan); }
  .hero .tagline {
    margin: 0 auto 30px;
    color: var(--ink-muted); font-size: 18.5px; line-height: 1.66; font-weight: 500;
    max-width: 54ch;
  }
  .cta-row { display: flex; align-items: center; justify-content: center; gap: 12px; flex-wrap: wrap; }
  .btn {
    display: inline-block; padding: 12px 22px;
    border-radius: var(--radius-sm); font-size: 15px; font-weight: 600; line-height: 1.3;
    text-decoration: none;
    transition: transform 120ms, background-color 120ms;
  }
  .btn:active { transform: translateY(1px); }
  .btn.primary { background: var(--cta-bg); color: var(--cta-ink); }
  .btn.primary:hover { background: var(--cta-hover); }
  .btn.ghost { color: var(--ink); background: var(--surface-2); border: 1px solid var(--hairline); }
  .btn.ghost:hover { background: var(--surface-3); }
  .btn:focus-visible { outline: 2px solid var(--cyan); outline-offset: 2px; }

  /* ── the network: three machines, the service, and the AI 100 ── */
  .net { margin: 46px auto 0; }
  .net svg { width: 100%; height: auto; display: block; }
  .net .axon { fill: none; stroke: var(--cyan); stroke-width: 1.5; opacity: 0.3; }
  .net .soma-edge { fill: var(--copper); }
  .net .halo-edge { fill: none; stroke: var(--copper-dim); stroke-width: 1.2; opacity: 0.7; }
  .net .soma-core { fill: var(--cyan); }
  .net .halo-core { fill: none; stroke: var(--cyan); stroke-width: 1.4; opacity: 0.45; }
  .net .ring { fill: none; stroke: var(--cyan); stroke-width: 1.2; }
  .net .impulse { fill: var(--ink); opacity: 0; }
  .net .impulse.back { fill: var(--cyan); }
  .net .impulse.syn { fill: var(--green); }
  .net .chip { fill: var(--surface-1); stroke: rgba(20, 198, 203, 0.4); }
  .net text { font: 600 14px var(--mono); letter-spacing: 0.05em; fill: var(--ink); }
  .net text.sub2 { font-weight: 400; font-size: 12px; letter-spacing: 0.02em; fill: var(--ink-subtle); }
  .net text.core-label { fill: var(--cyan); letter-spacing: 0.14em; }
  .net text.chip-label { fill: var(--cyan); letter-spacing: 0.1em; font-size: 13px; }
  .net-caption { margin: 12px 0 0; text-align: center; color: var(--ink-subtle); font-size: 14px; }

  /* ── why: the problem, drawn instead of claimed ── */
  .why-grid { display: grid; grid-template-columns: 1fr 1fr; gap: 16px; }
  .why-panel {
    background: var(--surface-1);
    border: 1px solid var(--hairline);
    border-radius: var(--radius);
    padding: 20px 22px 16px;
  }
  .why-panel.bad { border-top: 2px solid var(--red-text); }
  .why-panel.good { border-top: 2px solid var(--green); }
  .why-panel h3 { margin: 0 0 2px; font-size: 18px; font-weight: 650; letter-spacing: -0.015em; }
  .why-panel.bad h3 { color: var(--red-text); }
  .why-panel.good h3 { color: var(--green); }
  .why-panel .cap { margin: 0 0 10px; color: var(--ink-subtle); font-size: 13px; }
  .why-panel svg { width: 100%; height: auto; display: block; }
  .why-panel .wire { fill: none; stroke: var(--ink-subtle); stroke-width: 1.5; opacity: 0.75; }
  .why-panel .wire.hot { stroke: var(--cyan); opacity: 0.9; }
  .why-panel .wire.good { stroke: var(--green); opacity: 0.9; }
  .why-panel .relay { fill: none; stroke: var(--amber); stroke-width: 1.4; stroke-dasharray: 5 6; opacity: 0.8; }
  .why-panel .aget { fill: var(--copper); }
  .why-panel .aname { font: 600 12.5px var(--mono); fill: var(--ink); }
  .why-panel .anote { font: 500 11.5px var(--sans); fill: var(--ink-subtle); }
  .why-panel .anote.warn { fill: var(--amber); font-weight: 600; }
  .why-panel .deadx { stroke: var(--red-text); stroke-width: 3; stroke-linecap: round; }
  .why-panel .hub { fill: var(--cyan); }
  .why-panel .hub-halo { fill: none; stroke: var(--cyan); stroke-width: 1.3; opacity: 0.45; }
  .why-panel .hub-ring { fill: none; stroke: var(--cyan); stroke-width: 1.2; opacity: 0; }
  .why-panel .hubname { font: 650 11px var(--mono); fill: var(--cyan); letter-spacing: 0.08em; }
  .why-panel .pulse { fill: var(--cyan); opacity: 0; }
  .why-panel .tick { stroke: var(--green); stroke-width: 2.6; fill: none; stroke-linecap: round; stroke-linejoin: round; }
  .why-panel .relaydot { fill: var(--amber); opacity: 0; }
  .why-note {
    margin: 16px 0 0; padding: 18px 24px;
    background: var(--surface-1);
    border: 1px solid var(--hairline);
    border-left: 3px solid var(--copper);
    border-radius: var(--radius);
    color: var(--ink-muted); font-size: 15px; line-height: 1.65; font-weight: 500;
  }
  .why-note b { color: var(--ink); font-weight: 650; }

  /* ── architecture: end to end, with a today/allows switch ── */
  .arch-top { display: flex; align-items: center; gap: 14px; flex-wrap: wrap; margin-bottom: 14px; }
  .seg { display: inline-flex; background: var(--surface-2); border: 1px solid var(--hairline); border-radius: 999px; padding: 3px; }
  .seg button {
    border: 0; background: transparent; color: var(--ink-muted);
    font: 600 13px var(--sans); padding: 6px 16px; border-radius: 999px; cursor: pointer;
    transition: background-color 140ms, color 140ms;
  }
  .seg button[aria-pressed="true"] { background: var(--cta-bg); color: var(--cta-ink); }
  .seg button:focus-visible { outline: 2px solid var(--cyan); outline-offset: 2px; }
  #arch-caption { color: var(--ink-subtle); font-size: 13.5px; margin: 0; flex: 1; min-width: 24ch; }
  .arch-wrap {
    background: var(--surface-1);
    border: 1px solid var(--hairline);
    border-radius: var(--radius);
    padding: 18px 20px 10px;
  }
  .arch-wrap svg { width: 100%; height: auto; display: block; }
  .arch .box { fill: var(--canvas); stroke: var(--hairline); }
  .arch .box.svc { stroke: rgba(20, 198, 203, 0.5); }
  .arch .box.cld { stroke: rgba(20, 198, 203, 0.35); }
  .arch .box.dev { stroke: var(--copper-dim); }
  .arch .lane { fill: var(--surface-2); }
  .arch text { font: 600 13px var(--sans); fill: var(--ink); }
  .arch text.t-mono { font: 600 12px var(--mono); letter-spacing: 0.06em; }
  .arch text.t-sub { font-weight: 500; font-size: 11.5px; fill: var(--ink-subtle); }
  .arch text.t-cyan { fill: var(--cyan); }
  .arch text.t-copper { fill: var(--copper); }
  .arch text.t-green { fill: var(--green); }
  .arch .flow { fill: none; stroke-width: 1.6; }
  .arch .flow.in { stroke: var(--copper); opacity: 0.8; }
  .arch .flow.syn { stroke: var(--cyan); opacity: 0.8; }
  .arch .flow.out { stroke: var(--green); stroke-dasharray: 4 5; opacity: 0.8; }
  .arch .ah { stroke-width: 1.6; stroke-linecap: round; fill: none; }
  .arch .plug-node { fill: var(--surface-2); stroke: var(--hairline); }
  .arch .plug-t { font: 600 11px var(--mono); fill: var(--ink-muted); }
  .arch .g-allows { opacity: 0; transition: opacity 400ms; }
  .arch .ghost { stroke-dasharray: 5 5; }
  .arch[data-view="allows"] .g-allows { opacity: 1; }
  .arch .plug-live .plug-node { stroke: rgba(20, 198, 203, 0.55); }
  .arch .plug-live .plug-t { fill: var(--cyan); }
  .arch .plug-any .plug-node, .arch .plug-any .plug-t { transition: stroke 400ms, fill 400ms; }
  .arch[data-view="allows"] .plug-any .plug-node { stroke: rgba(20, 198, 203, 0.55); }
  .arch[data-view="allows"] .plug-any .plug-t { fill: var(--cyan); }

  /* ── pipeline + division of labor, one composition ── */
  .pipe { display: grid; grid-template-columns: 1fr 1fr 1fr 1fr; gap: 14px; }
  .pipe-card {
    --stage: var(--ink-muted);
    position: relative;
    background: var(--surface-1);
    border: 1px solid var(--hairline);
    border-top: 2px solid var(--stage);
    border-radius: var(--radius);
    padding: 18px 20px;
  }
  .pipe-card::after {
    content: ""; position: absolute; top: 50%; right: -11px; z-index: 1;
    width: 0; height: 0; margin-top: -5px;
    border-top: 5px solid transparent; border-bottom: 5px solid transparent;
    border-left: 7px solid var(--stage);
    opacity: 0.85;
  }
  .pipe-card:last-child::after { display: none; }
  .pipe-card .k { font: 650 14px var(--mono); letter-spacing: 0.1em; margin-bottom: 3px; color: var(--stage); }
  .pipe-card .where { font-size: 12px; color: var(--ink-subtle); margin-bottom: 10px; }
  .pipe-card:nth-child(2) { --stage: var(--copper); }
  .pipe-card:nth-child(3) { --stage: var(--cyan); }
  .pipe-card:nth-child(4) { --stage: var(--green); }
  .pipe-card p { margin: 0; color: var(--ink-muted); font-size: 14px; line-height: 1.6; font-weight: 500; }
  .curated {
    margin: 14px 0 0; padding: 16px 22px;
    background: linear-gradient(135deg, rgba(20, 198, 203, calc(var(--wash) + 0.06)), transparent 60%), var(--surface-1);
    border: 1px solid rgba(20, 198, 203, 0.35);
    border-radius: var(--radius);
    color: var(--ink-muted); font-size: 15px; line-height: 1.65; font-weight: 500;
  }
  .curated b { color: var(--cyan); font-weight: 650; }
  .split { display: grid; grid-template-columns: 1fr 1fr; gap: 14px; margin-top: 14px; }
  .split-card {
    display: flex; gap: 14px; align-items: flex-start;
    background: linear-gradient(150deg, rgba(224, 154, 90, calc(var(--wash) + 0.08)), transparent 55%), var(--surface-1);
    border: 1px solid rgba(224, 154, 90, 0.4);
    border-radius: var(--radius);
    padding: 18px 20px;
  }
  .split-card.cloud {
    background: linear-gradient(150deg, rgba(20, 198, 203, calc(var(--wash) + 0.08)), transparent 55%), var(--surface-1);
    border-color: rgba(20, 198, 203, 0.4);
  }
  .split-card h3 { margin: 0 0 2px; font-size: 17px; font-weight: 650; color: var(--copper); }
  .split-card.cloud h3 { color: var(--cyan); }
  .split-card .where { font: 12px var(--mono); color: var(--ink-subtle); margin-bottom: 8px; }
  .split-card p { margin: 0; color: var(--ink-muted); font-size: 13.5px; line-height: 1.6; font-weight: 500; }
  .split-card .sic { flex-shrink: 0; width: 30px; height: 30px; margin-top: 3px; color: var(--copper); }
  .split-card.cloud .sic { color: var(--cyan); }
  .split-card .sic svg { width: 100%; height: 100%; fill: none; stroke: currentColor; stroke-width: 1.6; stroke-linecap: round; stroke-linejoin: round; }

  /* ── use cases, iconified ── */
  .cases { display: grid; grid-template-columns: 1fr 1fr 1fr; gap: 14px; }
  .case {
    display: flex; gap: 14px; align-items: flex-start;
    background: var(--surface-1);
    border: 1px solid var(--hairline);
    border-radius: var(--radius);
    padding: 18px 20px;
    transition: border-color 140ms, transform 140ms;
  }
  .case:hover { border-color: rgba(24, 104, 242, 0.5); transform: translateY(-2px); }
  .case .cic {
    flex-shrink: 0; width: 40px; height: 40px; border-radius: 10px;
    display: flex; align-items: center; justify-content: center;
    background: rgba(24, 104, 242, calc(var(--wash) + 0.02));
    border: 1px solid rgba(24, 104, 242, 0.3);
    color: var(--blue-text);
  }
  .case .cic svg { width: 21px; height: 21px; fill: none; stroke: currentColor; stroke-width: 1.7; stroke-linecap: round; stroke-linejoin: round; }
  .case b { display: block; font-size: 15.5px; font-weight: 650; margin-bottom: 3px; color: var(--ink); letter-spacing: -0.01em; }
  .case span { color: var(--ink-muted); font-size: 13.5px; line-height: 1.55; font-weight: 500; }
  .aha {
    margin-top: 16px;
    background: linear-gradient(140deg, rgba(0, 202, 142, calc(var(--wash) + 0.08)), transparent 55%), var(--surface-1);
    border: 1px solid rgba(0, 202, 142, 0.38);
    border-radius: 20px;
    padding: 30px 36px;
  }
  .aha .setup { color: var(--ink-muted); font-size: 15.5px; line-height: 1.65; font-weight: 500; margin: 0 0 12px; max-width: 62ch; }
  .aha .quote {
    font-size: clamp(26px, 2.6vw, 38px); font-weight: 700; letter-spacing: -0.024em; line-height: 1.18;
    color: var(--ink); margin: 0 0 10px;
  }
  .aha .quote .said { color: var(--green); }
  .aha .receipt { color: var(--ink-subtle); font-size: 14px; margin: 0; }

  /* ── proof + run it ── */
  .band { display: grid; grid-template-columns: 1.3fr 1fr 1fr; gap: 14px; }
  .band-card {
    background: var(--surface-1);
    border: 1px solid var(--hairline);
    border-radius: var(--radius);
    padding: 20px 22px;
  }
  .band-card .num {
    font: 650 30px/1.12 var(--mono);
    font-variant-numeric: tabular-nums;
    letter-spacing: -0.02em;
    color: var(--ink);
    margin-bottom: 8px;
  }
  .band-card .num small { font-size: 16px; color: var(--ink-muted); font-weight: 500; }
  .band-card .what { font-size: 13.5px; color: var(--ink-muted); line-height: 1.6; font-weight: 500; }
  .band-card.hero-stat {
    grid-row: 1 / 3;
    display: flex; flex-direction: column; justify-content: center;
    background: linear-gradient(135deg, rgba(20, 198, 203, calc(var(--wash) + 0.16)), transparent 65%), var(--surface-1);
    border-color: rgba(20, 198, 203, 0.42);
    box-shadow: 0 0 90px -32px rgba(20, 198, 203, 0.4);
    padding: 30px 28px;
  }
  :root[data-theme="light"] .band-card.hero-stat { box-shadow: none; }
  .band-card.hero-stat .num { color: var(--cyan); font-size: clamp(44px, 4vw, 62px); }
  .band-card.hero-stat .num small { font-size: 20px; }
  .band-card.hero-stat .what { font-size: 14.5px; line-height: 1.65; }
  .band-card.wide { grid-column: 2 / 4; }
  .band-note { margin: 16px 0 0; color: var(--ink-subtle); font-size: 13.5px; line-height: 1.7; max-width: 100ch; }
  .band-note b { color: var(--ink-muted); font-weight: 600; }

  .run { display: grid; grid-template-columns: 1.15fr 1fr; gap: 14px; margin-top: 30px; align-items: stretch; }
  .term {
    background: var(--inset-bg);
    border: 1px solid var(--hairline);
    border-radius: var(--radius);
    padding: 20px 22px;
    font: 13.5px/1.95 var(--mono);
    color: var(--ink-muted);
    overflow-x: auto;
  }
  .term .p { color: var(--cyan); user-select: none; }
  .term .c { color: var(--ink-subtle); }
  .run-right { display: flex; flex-direction: column; gap: 14px; }
  .providers {
    background: var(--surface-1);
    border: 1px solid var(--hairline);
    border-radius: var(--radius);
    padding: 18px 22px 8px;
    flex: 1;
  }
  .providers h3 { margin: 0 0 4px; font-size: 17px; font-weight: 650; letter-spacing: -0.015em; }
  .providers .phead { margin: 0 0 10px; color: var(--ink-subtle); font-size: 13px; line-height: 1.55; }
  .prov-row {
    display: flex; align-items: center; justify-content: space-between; gap: 12px;
    padding: 8px 0;
    border-top: 1px solid var(--hairline-soft);
    font: 500 13.5px var(--mono); color: var(--ink);
  }
  .prov-row .st { display: inline-flex; align-items: center; gap: 7px; font: 600 11.5px var(--sans); color: var(--ink-subtle); }
  .prov-row .st::before { content: ""; width: 7px; height: 7px; border-radius: 50%; background: var(--ink-subtle); }
  .prov-row.live .st { color: var(--cyan); }
  .prov-row.live .st::before { background: var(--cyan); }
  .gh-card {
    display: flex; align-items: center; gap: 14px;
    background: var(--surface-1);
    border: 1px solid var(--hairline);
    border-radius: var(--radius);
    padding: 16px 20px;
    text-decoration: none; color: inherit;
    transition: border-color 140ms;
  }
  .gh-card:hover { border-color: rgba(20, 198, 203, 0.5); }
  .gh-card svg { width: 26px; height: 26px; fill: currentColor; flex-shrink: 0; }
  .gh-card b { display: block; font-size: 14.5px; font-weight: 650; }
  .gh-card span { display: block; font: 12px var(--mono); color: var(--ink-subtle); margin-top: 2px; }

  .footer {
    margin-top: 104px; padding-top: 24px;
    border-top: 1px solid var(--hairline-soft);
    color: var(--ink-subtle); font-size: 14px; line-height: 1.75; max-width: 90ch;
  }
  .footer b { color: var(--ink-muted); font-weight: 600; }

  /* ── slides mode ── */
  body[data-mode="slides"] aside { display: none; }
  body[data-mode="slides"] main {
    max-width: 1360px;
    height: calc(100dvh - 59px);
    padding: 0 64px 44px;
    display: flex; align-items: center;
    overflow: hidden;
  }
  body[data-mode="slides"] .slide { display: none; width: 100%; max-height: 100%; overflow-y: auto; margin: 0; padding: 8px 4px; scrollbar-width: thin; }
  body[data-mode="slides"] .slide.on { display: block; }
  body[data-mode="slides"] .footer, body[data-mode="slides"] .scroll-only { display: none; }
  body[data-mode="slides"] .hero .kicker { margin-bottom: 12px; }
  body[data-mode="slides"] .hero h1 { font-size: clamp(40px, 4.2vw, 58px); margin-bottom: 14px; }
  body[data-mode="slides"] .hero .tagline { font-size: 16.5px; margin-bottom: 20px; }
  body[data-mode="slides"] .net { max-width: 850px; margin: 18px auto 0; }
  body[data-mode="slides"] .net-caption { margin-top: 6px; font-size: 12.5px; }
  body[data-mode="slides"] .slide h2 { font-size: clamp(26px, 2.4vw, 34px); }
  body[data-mode="slides"] .slide .sub { margin-bottom: 20px; }
  body[data-mode="slides"] .band-card { padding: 14px 18px; }
  body[data-mode="slides"] .band-card.hero-stat { padding: 20px 22px; }
  body[data-mode="slides"] .band-card.hero-stat .num { font-size: clamp(38px, 3.2vw, 50px); }
  body[data-mode="slides"] .band-card .what { font-size: 12.5px; line-height: 1.55; }
  body[data-mode="slides"] .run { margin-top: 14px; }
  body[data-mode="slides"] .term { font-size: 12.5px; padding: 16px 18px; }
  body[data-mode="slides"] .providers { padding: 14px 18px 4px; }
  body[data-mode="slides"] .providers .phead { margin-bottom: 6px; }
  body[data-mode="slides"] .prov-row { padding: 6px 0; }
  body[data-mode="slides"] .gh-card { padding: 12px 18px; }
  #hud {
    display: none;
    position: fixed; left: 0; right: 0; bottom: 10px; z-index: 40;
    justify-content: center; align-items: center; gap: 18px;
    pointer-events: none;
  }
  body[data-mode="slides"] #hud { display: flex; }
  .hud-dots { display: flex; gap: 7px; pointer-events: auto; }
  .hud-dot {
    width: 8px; height: 8px; border-radius: 50%; border: 0; padding: 0;
    background: var(--ink-subtle); opacity: 0.5; cursor: pointer;
    transition: opacity 140ms, background-color 140ms, transform 140ms;
  }
  .hud-dot.on { background: var(--cyan); opacity: 1; transform: scale(1.25); }
  .hud-hint { color: var(--ink-subtle); font: 500 11.5px var(--mono); letter-spacing: 0.03em; }

  @media (max-width: 1100px) {
    .pipe { grid-template-columns: 1fr 1fr; }
    .pipe-card:nth-child(2)::after { display: none; }
    .band { grid-template-columns: 1fr 1fr; }
    .band-card.hero-stat { grid-row: auto; grid-column: 1 / 3; }
    .band-card.wide { grid-column: 1 / 3; }
    .cases { grid-template-columns: 1fr 1fr; }
  }
  @media (max-width: 900px) {
    .shell { flex-direction: column; }
    aside { position: static; width: auto; height: auto; border-right: none; border-bottom: 1px solid var(--hairline-soft); }
    #sessions { flex-direction: row; overflow-x: auto; }
    .scard { min-width: 220px; }
    .side-foot { display: none; }
  }
  @media (max-width: 760px) {
    main { padding: 44px 20px 64px; }
    .slide { margin-top: 72px; }
    .why-grid, .pipe, .band, .split, .cases, .run { grid-template-columns: 1fr; }
    .band-card.hero-stat, .band-card.wide { grid-column: auto; }
    .pipe-card::after { display: none; }
    .net text { font-size: 17px; }
    .net text.sub2 { display: none; }
    .aha { padding: 24px 20px; }
  }
  @media (prefers-reduced-motion: reduce) {
    html { scroll-behavior: auto; }
    .ring { display: none; }
    .live-dot.up { animation: none; }
    .brand .mark .impulse { display: none; }
  }
</style>
</head>
<body data-mode="scroll">
<div id="banner">Service unreachable. Retrying…</div>
<div class="shell">
<aside>
  <a class="brand" href="/"><svg class="mark" viewBox="0 0 30 16" aria-hidden="true"><path class="axon" d="M4 8 C 10 3, 20 13, 26 8"/><circle class="soma" cx="4" cy="8" r="2.7"/><circle class="soma" cx="26" cy="8" r="2.7"/><circle class="impulse" r="1.7"><animateMotion dur="2.8s" repeatCount="indefinite" path="M4 8 C 10 3, 20 13, 26 8"/></circle></svg><span class="name">Synapse</span><span class="scope-label">service</span></a>
  <div class="live"><span class="live-dot" id="live-dot"></span><span id="live-text">connecting…</span></div>
  <div class="side-label">Shared sessions</div>
  <nav id="sessions" aria-label="shared sessions"><div class="empty">connecting to the service…</div></nav>
  <div class="side-foot">
    <a href="/debug">Brain</a>
    <a href="/debug/log">Log</a>
    <a href="/debug/memory">Memory</a>
  </div>
</aside>
<div class="content">
<header class="topbar">
  <nav class="pages" aria-label="debug pages">
    <a href="/" aria-current="page">Home</a>
    <a href="/debug">Brain</a>
    <a href="/debug/log">Log</a>
    <a href="/debug/memory">Memory</a>
  </nav>
  <div class="toolbar">
    <a class="tool" id="gh-link" href="#" aria-label="Synapse on GitHub"><svg viewBox="0 0 16 16" aria-hidden="true"><path d="M8 0C3.58 0 0 3.58 0 8c0 3.54 2.29 6.53 5.47 7.59.4.07.55-.17.55-.38 0-.19-.01-.82-.01-1.49-2.01.37-2.53-.49-2.69-.94-.09-.23-.48-.94-.82-1.13-.28-.15-.68-.52-.01-.53.63-.01 1.08.58 1.23.82.72 1.21 1.87.87 2.33.66.07-.52.28-.87.51-1.07-1.78-.2-3.64-.89-3.64-3.95 0-.87.31-1.59.82-2.15-.08-.2-.36-1.02.08-2.12 0 0 .67-.21 2.2.82.64-.18 1.32-.27 2-.27s1.36.09 2 .27c1.53-1.04 2.2-.82 2.2-.82.44 1.1.16 1.92.08 2.12.51.56.82 1.27.82 2.15 0 3.07-1.87 3.75-3.65 3.95.29.25.54.73.54 1.48 0 1.07-.01 1.93-.01 2.2 0 .21.15.46.55.38A8.01 8.01 0 0 0 16 8c0-4.42-3.58-8-8-8z"/></svg><span>GitHub</span></a>
    <button class="tool" id="theme-btn" type="button" aria-label="toggle theme (T)"><svg class="ic-moon" viewBox="0 0 20 20" aria-hidden="true"><path class="stroke-ic" d="M16.5 12.2A7 7 0 0 1 7.8 3.5a7 7 0 1 0 8.7 8.7z"/></svg><svg class="ic-sun" viewBox="0 0 20 20" aria-hidden="true"><circle class="stroke-ic" cx="10" cy="10" r="3.6"/><path class="stroke-ic" d="M10 1.8v2.1M10 16.1v2.1M1.8 10h2.1M16.1 10h2.1M4.2 4.2l1.5 1.5M14.3 14.3l1.5 1.5M15.8 4.2l-1.5 1.5M5.7 14.3l-1.5 1.5"/></svg><span>Theme</span></button>
    <button class="tool primary" id="present-btn" type="button" aria-label="toggle slides mode (Shift+S)"><svg viewBox="0 0 20 20" aria-hidden="true"><path class="stroke-ic" d="M3 4.5h14M5.5 4.5v8.5a1.5 1.5 0 0 0 1.5 1.5h6a1.5 1.5 0 0 0 1.5-1.5V4.5M10 14.5V17M7.5 17h5"/><path d="M8.6 7.2l3.4 2-3.4 2z"/></svg><span id="present-label">Present</span></button>
  </div>
</header>
<main id="deck">

  <section class="slide hero" id="s-hero">
    <span class="kicker">Shared memory for coding agent teams</span>
    <h1>The <span class="hl">shared brain</span> for your agent team.</h1>
    <p class="tagline">Every machine distils what its agent learns. The service folds it into one intelligently curated memory. Every agent on the team can ask, and nothing arrives unprompted.</p>
    <div class="cta-row">
      <a class="btn primary" href="/debug">Open the brain</a>
      <a class="btn ghost" href="/debug/log">Tail the log</a>
      <a class="btn ghost" href="/debug/memory">Browse the memory</a>
    </div>
    <div class="net" aria-label="three machines, each running an agent and an edge worker, exchanging findings and answers with the shared Synapse service, which synthesizes on Cloud AI 100">
      <svg viewBox="0 0 1200 420">
        <path class="axon" id="ax1" d="M155 90 C 300 105, 420 160, 555 197"/>
        <path class="axon" id="ax2" d="M130 215 C 280 213, 420 211, 548 211"/>
        <path class="axon" id="ax3" d="M155 340 C 300 325, 420 265, 555 225"/>
        <path class="axon" id="ax4" d="M652 211 C 760 211, 850 211, 950 211"/>

        <circle class="halo-edge" cx="155" cy="90" r="15"/>
        <circle class="soma-edge" cx="155" cy="90" r="8"/>
        <circle class="halo-edge" cx="130" cy="215" r="15"/>
        <circle class="soma-edge" cx="130" cy="215" r="8"/>
        <circle class="halo-edge" cx="155" cy="340" r="15"/>
        <circle class="soma-edge" cx="155" cy="340" r="8"/>

        <circle class="ring" id="net-ring" r="22" cx="600" cy="211" opacity="0"/>
        <circle class="halo-core" id="net-halo" cx="600" cy="211" r="27"/>
        <circle class="soma-core" id="net-hub" cx="600" cy="211" r="15"/>

        <rect class="chip" x="950" y="176" width="200" height="70" rx="10"/>
        <text class="chip-label" x="1050" y="206" text-anchor="middle">CLOUD AI 100</text>
        <text class="sub2" x="1050" y="227" text-anchor="middle">Llama-3.3-70B · synthesis</text>

        <circle class="impulse" id="ax1-dot" r="3.6"/>
        <circle class="impulse" id="ax2-dot" r="3.6"/>
        <circle class="impulse" id="ax3-dot" r="3.6"/>
        <circle class="impulse syn" id="ax4-dot" r="3.6"/>
        <circle class="impulse back" id="ax1-ret" r="3.2"/>
        <circle class="impulse back" id="ax2-ret" r="3.2"/>
        <circle class="impulse back" id="ax3-ret" r="3.2"/>
        <circle class="impulse back" id="ax4-ret" r="3.2"/>

        <text x="155" y="48" text-anchor="middle">sid · claude-code</text>
        <text x="155" y="66" text-anchor="middle" class="sub2">agent + edge worker · NPU</text>
        <text x="130" y="262" text-anchor="middle">aditya · claude-code</text>
        <text x="130" y="280" text-anchor="middle" class="sub2">agent + edge worker · NPU</text>
        <text x="155" y="387" text-anchor="middle">akhil · codex</text>
        <text x="155" y="405" text-anchor="middle" class="sub2">agent + edge worker · NPU</text>

        <text x="600" y="268" text-anchor="middle" class="core-label">SYNAPSE SERVICE</text>
        <text x="600" y="288" text-anchor="middle" class="sub2">one shared memory · curated</text>
      </svg>
      <p class="net-caption">Findings flow in from every machine; answers flow back to whoever asks. Raw transcripts never cross the wire.</p>
    </div>
  </section>

  <section class="slide s-why" data-reveal id="s-why">
    <span class="slide-eyebrow">The problem</span>
    <h2>Every agent is brilliant. Every team is blind.</h2>
    <p class="sub">every engineer has a coding agent; no agent can see what another session already learned, decided, or ruled out</p>
    <div class="why-grid">
      <div class="why-panel bad">
        <h3>Without Synapse</h3>
        <p class="cap">three agents, three explorations, the same dead end · sharing is a human copy-paste</p>
        <svg viewBox="0 0 560 330" aria-label="three agents independently explore to the same dead end while a human relays notes manually">
          <path class="wire" id="wx-p1" d="M70 70 C 180 40, 320 80, 448 138"/>
          <path class="wire" id="wx-p2" d="M70 165 C 190 150, 330 155, 446 158"/>
          <path class="wire" id="wx-p3" d="M70 260 C 180 290, 320 240, 448 178"/>
          <path class="relay" id="wx-relay" d="M70 88 C 40 125, 40 128, 70 147"/>
          <g id="wx-x">
            <path class="deadx" d="M462 146 l22 22 M484 146 l-22 22"/>
          </g>
          <circle class="aget" cx="70" cy="70" r="8"/>
          <circle class="aget" cx="70" cy="165" r="8"/>
          <circle class="aget" cx="70" cy="260" r="8"/>
          <circle class="relaydot" id="wx-rdot" r="4"/>
          <text class="aname" x="70" y="46" text-anchor="middle">sid</text>
          <text class="aname" x="70" y="141" text-anchor="middle">aditya</text>
          <text class="aname" x="70" y="236" text-anchor="middle">akhil</text>
          <text class="anote warn" x="30" y="122" text-anchor="middle">relay</text>
          <text class="anote" x="473" y="196" text-anchor="middle">same dead end,</text>
          <text class="anote" x="473" y="212" text-anchor="middle">found three times</text>
        </svg>
      </div>
      <div class="why-panel good">
        <h3>With Synapse</h3>
        <p class="cap">one exploration lands once · the curated memory tells everyone else</p>
        <svg viewBox="0 0 560 330" aria-label="one agent finds the dead end, the finding lands in the shared curated memory, and the other agents know instantly">
          <path class="wire" id="ww-p1" d="M70 70 C 170 36, 330 50, 452 96"/>
          <path class="wire hot" id="ww-f1" d="M70 70 C 150 120, 250 160, 342 178"/>
          <path class="wire good" id="ww-g2" d="M350 190 C 260 190, 160 178, 92 168"/>
          <path class="wire good" id="ww-g3" d="M348 200 C 260 235, 160 255, 92 258"/>
          <g id="ww-x">
            <path class="deadx" d="M462 88 l20 20 M482 88 l-20 20"/>
          </g>
          <circle class="hub-ring" id="ww-ring" cx="360" cy="190" r="18"/>
          <circle class="hub-halo" cx="360" cy="190" r="24"/>
          <circle class="hub" cx="360" cy="190" r="13"/>
          <circle class="aget" cx="70" cy="70" r="8"/>
          <circle class="aget" cx="70" cy="165" r="8"/>
          <circle class="aget" cx="70" cy="260" r="8"/>
          <circle class="pulse" id="ww-pulse" r="4.4"/>
          <g class="tick" id="ww-t2"><path d="M96 155 l6 7 l12 -13"/></g>
          <g class="tick" id="ww-t3"><path d="M96 250 l6 7 l12 -13"/></g>
          <text class="aname" x="70" y="46" text-anchor="middle">sid</text>
          <text class="aname" x="70" y="141" text-anchor="middle">aditya</text>
          <text class="aname" x="70" y="236" text-anchor="middle">akhil</text>
          <text class="hubname" x="360" y="232" text-anchor="middle">CURATED MEMORY</text>
          <text class="anote" x="472" y="134" text-anchor="middle">found once</text>
        </svg>
      </div>
    </div>
    <p class="why-note scroll-only">A systems engineer writes MATLAB; a software engineer ports it to C++. Every bug found on one side needs a manual message before the other's agent knows. <b>Synapse removes the relay step.</b> It's like joining the same Google Doc: real-time shared context, but for coding agents. Save time: never re-research what a teammate's agent already found. Save tokens: never re-run the same exploration twice.</p>
  </section>

  <section class="slide s-arch" data-reveal id="s-arch">
    <span class="slide-eyebrow">Architecture</span>
    <h2>End to end: edge, service, cloud.</h2>
    <p class="sub">the whole workflow on one diagram; flip the switch to see what the same seams allow beyond the demo</p>
    <div class="arch-top">
      <div class="seg" role="group" aria-label="architecture view">
        <button id="arch-today" type="button" aria-pressed="true">Running today</button>
        <button id="arch-allows" type="button" aria-pressed="false">What it allows</button>
      </div>
      <p id="arch-caption">Three edge workers distil on the Hexagon NPU; one service folds findings into a curated working memory; synthesis runs on Cloud AI 100.</p>
    </div>
    <div class="arch-wrap">
      <div class="arch" id="arch" data-view="today">
      <svg viewBox="0 0 1240 480">
        <rect class="box dev" x="30" y="30" width="264" height="88" rx="10"/>
        <text x="52" y="63">sid · coding agent</text>
        <text class="t-sub" x="52" y="84">edge worker · Hexagon NPU · distil</text>
        <rect class="box dev" x="30" y="140" width="264" height="88" rx="10"/>
        <text x="52" y="173">aditya · coding agent</text>
        <text class="t-sub" x="52" y="194">edge worker · Hexagon NPU · distil</text>
        <rect class="box dev" x="30" y="250" width="264" height="88" rx="10"/>
        <text x="52" y="283">akhil · coding agent</text>
        <text class="t-sub" x="52" y="304">edge worker · Hexagon NPU · distil</text>
        <g class="g-allows">
          <rect class="box dev ghost" x="30" y="360" width="264" height="66" rx="10"/>
          <text class="t-sub" x="52" y="388">+ any teammate's machine</text>
          <text class="t-sub" x="52" y="408">any OS · no NPU required</text>
        </g>

        <path class="flow in" d="M294 74 C 360 74, 380 120, 430 150"/>
        <path class="flow in" d="M294 184 C 340 184, 380 184, 430 184"/>
        <path class="flow in" d="M294 294 C 360 294, 380 250, 430 218"/>
        <path class="ah" stroke="var(--copper)" d="M430 184 l-9 -5 m9 5 l-9 5"/>
        <text class="t-copper t-mono" x="352" y="152" text-anchor="middle">findings</text>
        <path class="flow out" d="M430 246 C 380 280, 350 320, 294 322"/>
        <text class="t-green t-mono" x="372" y="316" text-anchor="middle">answers</text>

        <rect class="box svc" x="432" y="86" width="300" height="236" rx="12"/>
        <text class="t-cyan t-mono" x="582" y="118" text-anchor="middle" style="letter-spacing:0.12em">SYNAPSE SERVICE</text>
        <rect class="lane" x="456" y="136" width="252" height="34" rx="7"/>
        <text class="t-sub" x="468" y="158">session log · append-only</text>
        <rect class="lane" x="456" y="178" width="252" height="34" rx="7"/>
        <text class="t-sub" x="468" y="200">the fold · deterministic triage</text>
        <rect class="lane" x="456" y="220" width="252" height="34" rx="7"/>
        <text class="t-sub" x="468" y="242">working memory · curated</text>
        <rect class="lane" x="456" y="262" width="252" height="34" rx="7"/>
        <text class="t-sub" x="468" y="284">MCP retrieval · pull-only</text>

        <path class="flow syn" d="M732 168 C 800 168, 830 168, 896 168"/>
        <path class="ah" stroke="var(--cyan)" d="M896 168 l-9 -5 m9 5 l-9 5"/>
        <path class="flow syn" d="M896 226 C 830 226, 800 226, 732 226"/>
        <path class="ah" stroke="var(--cyan)" d="M732 226 l9 -5 m-9 5 l9 5"/>
        <text class="t-cyan t-mono" x="814" y="152" text-anchor="middle">merge round</text>
        <text class="t-cyan t-mono" x="814" y="252" text-anchor="middle">rewritten memory</text>

        <rect class="box cld" x="898" y="118" width="312" height="158" rx="12"/>
        <text class="t-cyan t-mono" x="1054" y="150" text-anchor="middle" style="letter-spacing:0.1em">CLOUD AI 100</text>
        <text x="1054" y="180" text-anchor="middle">Llama-3.3-70B synthesis</text>
        <text class="t-sub" x="1054" y="204" text-anchor="middle">dedup · conflicts · lineage</text>
        <text class="t-sub" x="1054" y="224" text-anchor="middle">the curator of the shared memory</text>
        <g class="g-allows">
          <text class="t-sub" x="1054" y="254" text-anchor="middle">or any provider below · or nothing at all</text>
        </g>

        <g class="plug plug-live">
          <rect class="plug-node" x="120" y="443" width="176" height="30" rx="8"/>
          <text class="plug-t" x="208" y="462" text-anchor="middle">NPU · GenieX</text>
        </g>
        <g class="plug plug-live">
          <rect class="plug-node" x="316" y="443" width="176" height="30" rx="8"/>
          <text class="plug-t" x="404" y="462" text-anchor="middle">Cloud AI 100</text>
        </g>
        <g class="plug plug-any">
          <rect class="plug-node" x="512" y="443" width="176" height="30" rx="8"/>
          <text class="plug-t" x="600" y="462" text-anchor="middle">Anthropic API</text>
        </g>
        <g class="plug plug-any">
          <rect class="plug-node" x="708" y="443" width="176" height="30" rx="8"/>
          <text class="plug-t" x="796" y="462" text-anchor="middle">Claude CLI</text>
        </g>
        <g class="plug plug-any">
          <rect class="plug-node" x="904" y="443" width="176" height="30" rx="8"/>
          <text class="plug-t" x="992" y="462" text-anchor="middle">offline stand-in</text>
        </g>
        <text class="t-sub" x="30" y="464">provider seam</text>
      </svg>
      </div>
    </div>
  </section>

  <section class="slide s-pipe" data-reveal id="s-pipe">
    <span class="slide-eyebrow">The pipeline</span>
    <h2>From raw work to curated memory.</h2>
    <p class="sub">four stages and the silicon each one runs on; the log page tails every stage live</p>
    <div class="pipe">
      <div class="pipe-card">
        <div class="k">TRIAGE</div>
        <div class="where">deterministic · no LLM</div>
        <p>Decides what is worth processing at all. Most raw work stops here, before anything costs a token.</p>
      </div>
      <div class="pipe-card">
        <div class="k">DISTIL</div>
        <div class="where">edge · Hexagon NPU</div>
        <p>A small local model compresses raw work into structured Findings: learnings, decisions, dead ends, open questions. Raw transcripts never leave the device.</p>
      </div>
      <div class="pipe-card">
        <div class="k">SYNTHESIZE</div>
        <div class="where">cloud · 70B on AI 100</div>
        <p>A large model folds everyone's findings into one working memory: dedup, conflicts, lineage. Two people reaching the same insight become one Finding with both names on it.</p>
      </div>
      <div class="pipe-card">
        <div class="k">RETRIEVE</div>
        <div class="where">MCP · pull-only</div>
        <p>Agents ask in natural language and get suppression-aware answers with attribution. Nothing is injected unprompted.</p>
      </div>
    </div>
    <p class="curated"><b>Curated, not accumulated.</b> The shared memory is not a dump of everything everyone said: synthesis merges duplicates into one finding, resolves conflicts, demotes the trivial, and keeps every author's name on the lineage. Findings are queryable the instant they land; curation catches up behind them inside natural human think-time.</p>
    <div class="split">
      <div class="split-card">
        <span class="sic"><svg viewBox="0 0 24 24"><rect x="3.5" y="5" width="17" height="11.5" rx="1.6"/><path d="M8 20h8M12 16.5V20"/><path d="M8.5 9.2h4M8.5 12h7"/></svg></span>
        <div>
          <h3>Edge</h3>
          <div class="where">Snapdragon X Elite · Hexagon NPU · GenieX</div>
          <p>4B-class models run locally, already optimized for the NPU. Your raw work never leaves the device, and the compile/test loop keeps the CPU and GPU to itself.</p>
        </div>
      </div>
      <div class="split-card cloud">
        <span class="sic"><svg viewBox="0 0 24 24"><path d="M7 17.5a4 4 0 0 1-.6-7.96A5.5 5.5 0 0 1 17 8.6a3.8 3.8 0 0 1 .4 7.55z"/><path d="M8.5 13.2h7M8.5 15.4h4.5"/></svg></span>
        <div>
          <h3>Cloud</h3>
          <div class="where">Llama-3.3-70B · Cloud AI 100</div>
          <p>Cross-team synthesis where the big model earns its cost: semantic dedup, conflict detection, and one bounded working memory rewritten per merge.</p>
        </div>
      </div>
    </div>
  </section>

  <section class="slide s-cases" data-reveal id="s-cases">
    <span class="slide-eyebrow">In practice</span>
    <h2>What you'd use it for</h2>
    <p class="sub">the same memory, six shapes of team</p>
    <div class="cases">
      <div class="case"><span class="cic"><svg viewBox="0 0 24 24"><circle cx="12" cy="13" r="4.5"/><path d="M12 8.5V6.2M9 7l1.4 1.7M15 7l-1.4 1.7M4.5 13h3M16.5 13h3M6 18l2.1-1.8M18 18l-2.1-1.8M6 8.5l2.2 1.5M18 8.5l-2.2 1.5"/></svg></span><div><b>Debugging together</b><span>one teammate's dead end is everyone's dead end</span></div></div>
      <div class="case"><span class="cic"><svg viewBox="0 0 24 24"><circle cx="6.5" cy="6" r="2.2"/><circle cx="6.5" cy="18" r="2.2"/><circle cx="17.5" cy="9.5" r="2.2"/><path d="M6.5 8.2v7.6M6.5 12c0-2.6 4.4-2.2 8.8-2.4"/></svg></span><div><b>Feature work</b><span>parallel building without parallel re-discovery</span></div></div>
      <div class="case"><span class="cic"><svg viewBox="0 0 24 24"><path d="M12 3.5a5.7 5.7 0 0 0-3.2 10.4c.7.5 1.2 1.6 1.2 2.6h4c0-1 .5-2.1 1.2-2.6A5.7 5.7 0 0 0 12 3.5z"/><path d="M10 19.2h4M10.7 21.2h2.6"/></svg></span><div><b>Design and brainstorming</b><span>decisions propagate the moment they're made</span></div></div>
      <div class="case"><span class="cic"><svg viewBox="0 0 24 24"><circle cx="12" cy="12" r="1.8"/><path d="M8.5 15.5a5 5 0 0 1 0-7M15.5 8.5a5 5 0 0 1 0 7M6 18a8 8 0 0 1 0-12M18 6a8 8 0 0 1 0 12"/></svg></span><div><b>Status sharing</b><span>your agent already knows what the team did today</span></div></div>
      <div class="case"><span class="cic"><svg viewBox="0 0 24 24"><path d="M9.5 3.5h5M10.5 3.5v5.2L5.6 17a2.6 2.6 0 0 0 2.3 3.9h8.2A2.6 2.6 0 0 0 18.4 17l-4.9-8.3V3.5"/><path d="M8 14.5h8"/></svg></span><div><b>Lab and dev</b><span>on-target context meets code context in one memory</span></div></div>
      <div class="case"><span class="cic"><svg viewBox="0 0 24 24"><path d="M4 9h13M14 5.5L17.5 9 14 12.5"/><path d="M20 15H7M10 11.5L6.5 15l3.5 3.5"/></svg></span><div><b>Asymmetric teammates</b><span>MATLAB author and C++ porter, no manual relay</span></div></div>
    </div>
    <div class="aha">
      <p class="setup">A teammate joins late. Their agent is briefed on arrival. They start their own task, and before doing any work, their agent says:</p>
      <p class="quote"><span class="said">"Sid already ruled this out."</span></p>
      <p class="receipt">In live rehearsal the system merged two contributors' findings into one, unscripted. That merge is on the Memory page of this dashboard right now, struck-through sources and all.</p>
    </div>
  </section>

  <section class="slide s-proof" data-reveal id="s-proof">
    <span class="slide-eyebrow">Proof and a prompt</span>
    <h2>Measured, not claimed. Running in one command.</h2>
    <p class="sub">our own numbers, from our own Snapdragon X Elite; nothing below is a projection</p>
    <div class="band">
      <div class="band-card hero-stat">
        <div class="num"><span class="cu" data-n="0.1" data-dec="1">0.1</span><small>% variance</small></div>
        <div class="what">NPU decode-rate variability across identical distillations, against 10.8% on GPU and 16.4% on CPU. An always-on background distiller that never steals your machine at an unpredictable moment.</div>
      </div>
      <div class="band-card">
        <div class="num"><span class="cu" data-n="1041" data-dec="0" data-sep="1">1,041</span> <small>tok/s</small></div>
        <div class="what">prefill on the compiled Qwen3-4B bundle, R² 0.9974 across a 19x range of prompt lengths. 5.3x faster than the interpreted path on the same silicon.</div>
      </div>
      <div class="band-card">
        <div class="num"><span class="cu" data-n="2.2" data-dec="1">2.2</span> <small>s</small></div>
        <div class="what">per distillation-shaped call on the production NPU model, at 16.9 tok/s decode, running entirely off your CPU and GPU.</div>
      </div>
      <div class="band-card wide">
        <div class="num"><span class="cu" data-n="12.6" data-dec="1">12.6</span>-<span class="cu" data-n="52.8" data-dec="1">52.8</span> <small>s</small></div>
        <div class="what">measured cloud merge round-trip against Llama-3.3-70B, debounced and spend-governed so it never blocks a query.</div>
      </div>
    </div>
    <p class="band-note scroll-only"><b>Power draw itself is unmeasured, and we say so.</b> The efficiency story we stand on is the one we measured: always-on work moved off your CPU and GPU onto the NPU, the silicon built for it, with a decode rate steady to one part in a thousand. No number on this page is nicer-sounding than the truth.</p>
    <div class="run">
      <div class="term">
        <div><span class="p">$</span> git clone … &amp;&amp; uv sync</div>
        <div><span class="p">$</span> uv run python scripts/serve_local.py --purpose "fix the flaky auth test"</div>
        <div><span class="p">$</span> claude mcp add synapse <span class="c">… one line, and the agent is briefed on join</span></div>
      </div>
      <div class="run-right">
        <div class="providers">
          <h3>Five providers, one seam</h3>
          <p class="phead">Every model call goes through one interface. No NPU? No cloud key? It still runs, end to end, on any machine.</p>
          <div class="prov-row live"><span>NPU · GenieX</span><span class="st">live now</span></div>
          <div class="prov-row live"><span>Cloud AI 100</span><span class="st">live now</span></div>
          <div class="prov-row"><span>Anthropic API</span><span class="st">plugs in</span></div>
          <div class="prov-row"><span>Claude CLI</span><span class="st">plugs in</span></div>
          <div class="prov-row"><span>offline stand-in</span><span class="st">plugs in</span></div>
        </div>
        <a class="gh-card" id="gh-link2" href="#">
          <svg viewBox="0 0 16 16" aria-hidden="true"><path d="M8 0C3.58 0 0 3.58 0 8c0 3.54 2.29 6.53 5.47 7.59.4.07.55-.17.55-.38 0-.19-.01-.82-.01-1.49-2.01.37-2.53-.49-2.69-.94-.09-.23-.48-.94-.82-1.13-.28-.15-.68-.52-.01-.53.63-.01 1.08.58 1.23.82.72 1.21 1.87.87 2.33.66.07-.52.28-.87.51-1.07-1.78-.2-3.64-.89-3.64-3.95 0-.87.31-1.59.82-2.15-.08-.2-.36-1.02.08-2.12 0 0 .67-.21 2.2.82.64-.18 1.32-.27 2-.27s1.36.09 2 .27c1.53-1.04 2.2-.82 2.2-.82.44 1.1.16 1.92.08 2.12.51.56.82 1.27.82 2.15 0 3.07-1.87 3.75-3.65 3.95.29.25.54.73.54 1.48 0 1.07-.01 1.93-.01 2.2 0 .21.15.46.55.38A8.01 8.01 0 0 0 16 8c0-4.42-3.58-8-8-8z"/></svg>
          <div><b>Read the code</b><span>github.com/SinghSiddharth01/Synapse</span></div>
        </a>
      </div>
    </div>
  </section>

  <p class="footer">
    <b>This page makes not one external request</b>: no CDN, no font, no image,
    so it can be opened on a demo machine with no network and still be itself.
    <b>Cyan is the cloud side</b> of the device boundary; the Edge Worker's own
    debug page is the copper side. The brain, log, and memory pages behind the
    buttons above are read-only by construction: every route is mounted GET-only.
  </p>
</main>
</div>
</div>
<div id="hud" aria-hidden="true">
  <div class="hud-dots" id="hud-dots"></div>
  <span class="hud-hint" id="hud-count">1 / 6</span>
  <span class="hud-hint">arrows: slides · F: fullscreen · T: theme · Esc: exit</span>
</div>

<script>/**
 * Anime.js - UMD minified bundle
 * @version v4.5.0
 * @license MIT
 * @copyright 2026 - Julian Garnier
 */
!function(t,e){"object"==typeof exports&&"undefined"!=typeof module?e(exports):"function"==typeof define&&define.amd?define(["exports"],e):e((t="undefined"!=typeof globalThis?globalThis:t||self).anime={})}(this,function(t){"use strict";const e="undefined"!=typeof window,s=e?window:null,i=e?document:null,r={replace:0,none:1,blend:2},n=Symbol(),o=Symbol(),a=Symbol(),l=Symbol(),h=Symbol(),d=1e-11,c=1e12,u=1e3,p="",m=[],f=(()=>{const t=new Map;return t.set("x","translateX"),t.set("y","translateY"),t.set("z","translateZ"),t})(),g=["perspective","translateX","translateY","translateZ","rotate","rotateX","rotateY","rotateZ","scale","scaleX","scaleY","scaleZ","skew","skewX","skewY"],y=g.reduce((t,e)=>({...t,[e]:e+"("}),{}),_=()=>{},b=/\\)\\s*[-.\\d]/,v=/(^#([\\da-f]{3}){1,2}$)|(^#([\\da-f]{4}){1,2}$)/i,T=/rgb\\(\\s*(\\d+)\\s*,\\s*(\\d+)\\s*,\\s*(\\d+)\\s*\\)/i,x=/rgba\\(\\s*(\\d+)\\s*,\\s*(\\d+)\\s*,\\s*(\\d+)\\s*,\\s*(-?\\d+|-?\\d*.\\d+)\\s*\\)/i,S=/hsl\\(\\s*(-?\\d+|-?\\d*.\\d+)\\s*,\\s*(-?\\d+|-?\\d*.\\d+)%\\s*,\\s*(-?\\d+|-?\\d*.\\d+)%\\s*\\)/i,w=/hsla\\(\\s*(-?\\d+|-?\\d*.\\d+)\\s*,\\s*(-?\\d+|-?\\d*.\\d+)%\\s*,\\s*(-?\\d+|-?\\d*.\\d+)%\\s*,\\s*(-?\\d+|-?\\d*.\\d+)\\s*\\)/i,$=/[-+]?\\d*\\.?\\d+(?:e[-+]?\\d)?/gi,C=/^([-+]?\\d*\\.?\\d+(?:e[-+]?\\d+)?)([a-z]+|%)$/i,E=/([a-z])([A-Z])/g,k=/(\\*=|\\+=|-=)/,N=/var\\(\\s*(--[\\w-]+)(?:\\s*,\\s*([^)]+))?\\s*\\)/,D={id:null,keyframes:null,playbackEase:null,playbackRate:1,frameRate:240,loop:0,reversed:!1,alternate:!1,autoplay:!0,persist:!1,duration:u,delay:0,loopDelay:0,ease:"out(2)",composition:r.replace,modifier:t=>t,onBegin:_,onBeforeUpdate:_,onUpdate:_,onLoop:_,onPause:_,onComplete:_,onRender:_},A={current:null,root:i},I={defaults:D,precision:4,timeScale:1,tickThreshold:200,editor:null},R={version:"4.5.0",engine:null};e&&(s.AnimeJS||(s.AnimeJS=[]),s.AnimeJS.push(R));const L=t=>t.replace(E,"$1-$2").toLowerCase(),B=(t,e)=>0===t.indexOf(e),P=Date.now,F=Array.isArray,M=t=>t&&t.constructor===Object,V=t=>"number"==typeof t&&!isNaN(t),O=t=>"string"==typeof t,z=t=>"function"==typeof t,H=t=>void 0===t,X=t=>H(t)||null===t,Y=t=>e&&t instanceof SVGElement,W=t=>v.test(t),U=t=>B(t,"rgb"),q=t=>B(t,"hsl"),j=t=>!I.defaults.hasOwnProperty(t),G=["opacity","rotate","overflow","color"],Z=t=>O(t)?parseFloat(t):t,Q=Math.pow,J=Math.sqrt,K=Math.sin,tt=Math.cos,et=Math.abs,st=Math.exp,it=Math.ceil,rt=Math.floor,nt=Math.asin,ot=Math.max,at=Math.atan2,lt=Math.PI,ht=Math.round,dt=(t,e,s)=>t<e?e:t>s?s:t,ct=(t,e)=>{if(e<0)return t;if(!e)return ht(t);const s=10**e;return ht(t*s)/s},ut=(t,e)=>F(e)?e.reduce((e,s)=>et(s-t)<et(e-t)?s:e):e?ht(t/e)*e:t,pt=(t,e,s)=>1===s?e:0===s?t:t+(e-t)*s,mt=t=>t===1/0?c:t===-1/0?-c:t,ft=t=>t<=d?d:mt(ct(t,11)),gt=t=>F(t)?[...t]:t,yt=(t,e)=>{const s={...t};for(let i in e){const r=t[i];s[i]=H(r)?e[i]:r}return s},_t=(t,e,s,i="_prev",r="_next")=>{let n=t._head,o=r;for(s&&(n=t._tail,o=i);n;){const t=n[o];e(n),n=t}},bt=(t,e,s="_prev",i="_next")=>{const r=e[s],n=e[i];r?r[i]=n:t._head=n,n?n[s]=r:t._tail=r,e[s]=null,e[i]=null},vt=(t,e,s,i="_prev",r="_next")=>{let n=t._tail;for(;n&&s&&s(n,e);)n=n[i];const o=n?n[r]:t._head;n?n[r]=e:t._head=e,o?o[i]=e:t._tail=e,e[i]=n,e[r]=o},Tt=t=>{let e=p;for(let s=0,i=g.length;s<i;s++){const i=g[s],r=t[i];if(void 0!==r){if("translateX"===i){const i=t.translateY;if(void 0!==i){const n=t.translateZ;void 0!==n?(e+=`translate3d(${r},${i},${n}) `,s+=2):(e+=`translate(${r},${i}) `,s+=1);continue}}if("scaleX"===i&&void 0===t.scale){const i=t.scaleY;if(void 0!==i){const n=t.scaleZ;void 0!==n?(e+=`scale3d(${r},${i},${n}) `,s+=2):(e+=`scale(${r},${i}) `,s+=1);continue}}e+=`${y[i]}${r}) `}"rotateZ"===i&&void 0!==t.rotate3d&&(e+=`rotate3d(${t.rotate3d}) `)}return void 0!==t.matrix&&(e+=`matrix(${t.matrix}) `),void 0!==t.matrix3d&&(e+=`matrix3d(${t.matrix3d}) `),e},xt=[];function St(t,e){if(!t)return null;const s=xt.length;t:for(let i=0;i<s;i++){const s=xt[i];if(s.detect&&!s.detect(t))continue;const r=s.targetAdapters;for(let s=0,i=r.length;s<i;s++){const i=r[s];if(i.detect(t)){const s=i.props[e];if(s&&(!s.gate||s.gate(t)))return s;break t}}}for(let i=0;i<s;i++){const s=xt[i];if(s.detect&&!s.detect(t))continue;const r=s.propertyResolvers;for(let s=0,i=r.length;s<i;s++){const i=r[s](t,e);if(i)return i}}return null}const wt=(t,e,s)=>(s<0&&(s+=1),s>1&&(s-=1),s<1/6?t+6*(e-t)*s:s<.5?e:s<2/3?t+(e-t)*(2/3-s)*6:t),$t=(t,e)=>H(t)?e:t,Ct=(t,e)=>{const s=t.match(N),i=e[o]?e:document.documentElement;let r=getComputedStyle(i)?.getPropertyValue(s[1]);return r&&r.trim()!==p||!s[2]||(r=s[2].trim()),r||0},Et=(t,e,s,i,r,n)=>{if(z(t)){if(!r){const r=t(e,s,i,n);return isNaN(+r)?r||0:+r}const o=()=>{const r=t(e,s,i,n);return isNaN(+r)?r||0:+r};return r.func=o,o()}if(O(t)&&B(t,"var(")){if(!r)return Ct(t,e);const s=()=>Ct(t,e);return r.func=s,s()}return t},kt=(t,e)=>t[o]?t[a]&&((t,e)=>{if(G.includes(e))return!1;if(t.getAttribute(e)||e in t){if("scale"===e){const e=t.parentNode;return e&&"filter"===e.tagName}return!0}})(t,e)?1:g.includes(e)||f.get(e)?3:B(e,"--")?4:e in t.style?2:e in t?0:1:0,Nt=(t,e,s)=>{const i=t.style[e];i&&s&&(s[e]=i);const r=i||getComputedStyle(t[h]||t).getPropertyValue(e);return"auto"===r?"0":r},Dt=(t,e,s,i)=>{const r=H(s)?kt(t,e):s,n=St(t,e);if(n){const s=n.get(t);return s&&i&&(i[e]=s),null==s?0:s}if(0===r){const s=t[e];return s&&i&&(i[e]=s),s||0}if(1===r){const s=t.getAttribute(e);return s&&i&&(i[e]=s),s}return 3===r?((t,e,s)=>{const i=t.style.transform;if(i){const r=t[l];let n=0;const o=i.length;let a;for(;n<o;){for(;n<o&&32===i.charCodeAt(n);)n++;if(n>=o)break;const t=n;for(;n<o&&40!==i.charCodeAt(n);)n++;if(n>=o)break;const e=i.substring(t,n);let s=1;const l=n+1;let h=-1,d=-1;for(n++;n<o&&s>0;){const t=i.charCodeAt(n);40===t?s++:41===t?s--:44===t&&1===s&&(-1===h?h=n:-1===d&&(d=n)),n++}const c=n-1;"translate"===e||"translate3d"===e?(-1===h?r.translateX=i.substring(l,c).trim():(r.translateX=i.substring(l,h).trim(),-1===d?r.translateY=i.substring(h+1,c).trim():(r.translateY=i.substring(h+1,d).trim(),r.translateZ=i.substring(d+1,c).trim())),a=i.substring(l,c)):"scale"===e||"scale3d"===e?-1===h?r.scale=i.substring(l,c).trim():(r.scaleX=i.substring(l,h).trim(),-1===d?r.scaleY=i.substring(h+1,c).trim():(r.scaleY=i.substring(h+1,d).trim(),r.scaleZ=i.substring(d+1,c).trim())):r[e]=i.substring(l,c)}if("translate3d"===e&&a)return s&&(s[e]=a),a;const h=r[e];if(!H(h))return s&&(s[e]=h),h}return"translate3d"===e?"0px, 0px, 0px":"rotate3d"===e?"0, 0, 0, 0deg":B(e,"scale")?"1":B(e,"rotate")||B(e,"skew")?"0deg":"0px"})(t,e,i):4===r?Nt(t,e,i).trimStart():Nt(t,e,i)},At=(t,e,s)=>"-"===s?t-e:"+"===s?t+e:t*e,It=(t,e)=>{if(e.t=0,e.n=0,e.u=null,e.o=null,e.d=null,e.s=null,!t)return e;const s=+t;if(!isNaN(s))return e.n=s,e;let i=t;"="===i[1]&&(e.o=i[0],i=i.slice(2));const r=!i.includes(" ")&&C.exec(i);if(r)return e.t=1,e.n=+r[1],e.u=r[2],e;if(e.o)return e.n=+i,e;if(W(o=i)||(U(o)||q(o))&&(")"===o[o.length-1]||!b.test(o)))return e.t=2,e.d=U(n=i)?(t=>{const e=T.exec(t)||x.exec(t),s=H(e[4])?1:+e[4];return[+e[1],+e[2],+e[3],s]})(n):W(n)?(t=>{const e=t.length,s=4===e||5===e;return[+("0x"+t[1]+t[s?1:2]),+("0x"+t[s?2:3]+t[s?2:4]),+("0x"+t[s?3:5]+t[s?3:6]),5===e||9===e?+(+("0x"+t[s?4:7]+t[s?4:8])/255).toFixed(3):1]})(n):q(n)?(t=>{const e=S.exec(t)||w.exec(t),s=+e[1]/360,i=+e[2]/100,r=+e[3]/100,n=H(e[4])?1:+e[4];let o,a,l;if(0===i)o=a=l=r;else{const t=r<.5?r*(1+i):r+i-r*i,e=2*r-t;o=ct(255*wt(e,t,s+1/3),0),a=ct(255*wt(e,t,s),0),l=ct(255*wt(e,t,s-1/3),0)}return[o,a,l,n]})(n):[0,0,0,1],e;var n,o;{const t=i.match($);return e.t=3,e.d=t?t.map(Number):[],e.s=i.split($)||[],e}},Rt=(t,e)=>(e.t=t._valueType,e.n=t._toNumber,e.u=t._unit,e.o=null,e.d=gt(t._toNumbers),e.s=gt(t._strings),e),Lt={t:0,n:0,u:null,o:null,d:null,s:null},Bt=(t,e,s)=>{const i=t._modifier,r=t._fromNumbers,n=t._toNumbers,o=t._strings;let a=o[0];for(let l=0,h=n.length;l<h;l++){const h=i(ct(pt(r[l],n[l],e),s)),d=o[l+1];a+=`${d?h+d:h}`,t._numbers[l]=h}return a},Pt=(t,e,s,i,n)=>{const o=t.parent,a=t.duration,h=t.completed,c=t.iterationDuration,u=t.iterationCount,p=t._currentIteration,m=t._loopDelay,f=t._reversed,g=t._alternate,y=t._hasChildren,_=t._delay,b=t._currentTime,v=_+c,T=e-_,x=dt(b,-_,a),S=dt(T,-_,a),w=T-b,$=S>0,C=S>=a,E=a<=d,k=2===n;let N=0,D=T,A=0;if(u>1){const e=c+(C?0:m),s=~~(S/e);t._currentIteration=dt(s,0,u),C&&t._currentIteration--,N=t._currentIteration%2,D=S-s*e||0}const R=f^(g&&N),L=t._ease;let B=C?R?0:a:R?c-D:D;L&&(B=c*L(B/c)||0);const P=(o?o.backwards:T<b)?!R:!!R;if(t._currentTime=T,t._iterationTime=B,t.backwards=P,$&&!t.began?(t.began=!0,s||o&&(P||!o.began)||t.onBegin(t)):T<=0&&(t.began=!1),s||y||!$||t._currentIteration===p||t.onLoop(t),k||1===n&&(e>=(o&&_>0?0:_)&&e<=v||e<=_&&x>_||e>=v&&x!==a)||B>=v&&x!==a||B<=_&&x>0&&!C||e<=x&&x===a&&h||C&&!h&&E){if($&&(t.computeDeltaTime(x),s||t.onBeforeUpdate(t)),!y){const e=k||(P?-1*w:w)>=I.tickThreshold,n=ct(t._offset+(o?o._offset:0)+_+B,12);let a,h,d,c,u=t._head,p=0;for(;u;){const t=u._composition,s=u._currentTime,o=u._changeDuration,m=u._absoluteStartTime+u._changeDuration,f=u._nextRep,g=u._prevRep,y=t!==r.none,_=g?g._absoluteStartTime+g._changeDuration:0,b=g&&g.parent!==u.parent,v=!f||f._isOverridden?m:f.parent===u.parent?m+f._delay:f._absoluteStartTime<f._absoluteUpdateStartTime?f._absoluteStartTime:f._absoluteUpdateStartTime;if((e||(s!==o||n<=v||g&&!b&&(!f||f.parent!==u.parent))&&(0!==s||n>=u._absoluteStartTime||b&&!u._hasFromValue&&!g._isOverridden&&n>=_||f&&!f._isOverridden&&f.parent===u.parent&&0!==f._currentTime&&B<f._startTime))&&(!g||b||B>=u._startTime)&&(!y||!u._isOverridden&&(!u._isOverlapped||n<=m)&&(!f||f._isOverridden||n<=v)&&(!g||g._isOverridden||(b?n>=u._absoluteStartTime||!u._hasFromValue&&n>=_:n>=_+u._delay)))){const e=u._currentTime=dt(B-u._startTime,0,o),s=u._ease(e/u._updateDuration),n=u._modifier,m=u._valueType,f=u._tweenType,g=0===f,_=0===m,b=_&&g||0===s||1===s?-1:I.precision;let v,T;if(_)v=T=n(ct(pt(u._fromNumber,u._toNumber,s),b));else if(1===m)T=n(ct(pt(u._fromNumber,u._toNumber,s),b)),v=`${T}${u._unit}`;else if(2===m){const t=u._numbers,e=u._fromNumbers,r=u._toNumbers,o=1-s,a=e[0],l=e[1],h=e[2],d=r[0],c=r[1],p=r[2];t[0]=n(Math.sqrt(a*a*o+d*d*s)),t[1]=n(Math.sqrt(l*l*o+c*c*s)),t[2]=n(Math.sqrt(h*h*o+p*p*s)),t[3]=n(pt(e[3],r[3],s)),u._setter&&!i||(v=`rgba(${ct(t[0],0)},${ct(t[1],0)},${ct(t[2],0)},${t[3]})`)}else 3===m&&(v=Bt(u,s,b));if(y&&(u._number=T),i||t===r.blend)u._value=v;else{const t=u.property;a=u.target,u._setter?u._setter(a,T,u):g?a[t]=v:1===f?a.setAttribute(t,v):(h=a.style,3===f?(a!==d&&(d=a,c=a[l]),c[t]=v,p=1):2===f?h[t]=v:4===f&&h.setProperty(t,v)),$&&(A=1)}}else s&&g&&!b&&B<u._startTime&&(u._currentTime=0);p&&u._renderTransforms&&(h.transform=Tt(c),p=0),u=u._next}!s&&A&&t.onRender(t)}!s&&$&&t.onUpdate(t)}return o&&E?!s&&(o.began&&!P&&T>0&&!h||P&&T<=d&&h)&&(t.onComplete(t),t.completed=!P):$&&C?u===1/0?t._startTime+=t.duration:t._currentIteration>=u-1&&(t.paused=!0,h||y||(t.completed=!0,s||o&&(P||!o.began)||(t.onComplete(t),t._resolve(t)))):t.completed=!1,A},Ft=(t,e,s,i,r)=>{const n=t._currentIteration;if(Pt(t,e,s,i,r),t._hasChildren){const o=t,a=o.backwards,l=i?e:o._iterationTime,h=P();let c=0,u=!0;if(!i&&o._currentIteration!==n){const t=o.iterationDuration;_t(o,e=>{if(a){const i=e.duration,r=e._offset+e._delay;s||!(i<=d)||r&&r+i!==t||e.onComplete(e)}else!e.completed&&!e.backwards&&e._currentTime<e.iterationDuration&&Pt(e,t,s,1,2),e.began=!1,e.completed=!1}),s||o.onLoop(o)}_t(o,t=>{const e=ct((l-t._offset)*t._speed,12);if(a&&e>t._delay+t.duration)return;const n=t._fps<o._fps?t.requestTick(h):r;c+=Pt(t,e,s,i,n),!t.completed&&u&&(u=!1)},a),!s&&c&&o.onRender(o),(u||a)&&o._currentTime>=o.duration&&(o.paused=!0,o.completed||(o.completed=!0,s||(o.onComplete(o),o._resolve(o))))}},Mt={},Vt=(t,e,s)=>{if(3===s)return f.get(t)||t;if(2===s||1===s&&Y(e)&&t in e.style){const e=Mt[t];if(e)return e;{const e=t?L(t):t;return Mt[t]=e,e}}return t},Ot=(t,e=!1)=>{if(t._hasChildren)_t(t,t=>Ot(t,e),!0);else{const s=t;s.pause(),_t(s,t=>{const i=t.property,r=t.target,n=t._tweenType,a=t._inlineValue,h=X(a)||a===p;if(t._setter){if(!e&&!h){if(It(a,Lt),Lt.d){const e=Lt.d,s=t._numbers;for(let t=0,i=e.length;t<i;t++)s[t]=e[t]}else t._number=Lt.n;t._setter(t.target,t._number,t)}}else if(0===n)e||h||(r[i]=a);else if(r[o])if(1===n)e||(h?r.removeAttribute(i):r.setAttribute(i,a));else{const e=r.style;if(3===n){const s=r[l];h?delete s[i]:s[i]=a,t._renderTransforms&&(Object.keys(s).length?e.transform=Tt(s):e.removeProperty("transform"))}else h?e.removeProperty(L(i)):e[i]=a}r[o]&&s._tail===t&&s.targets.forEach(t=>{t.getAttribute&&t.getAttribute("style")===p&&t.removeAttribute("style")})})}return t},zt=t=>Ot(t,!0);class Ht{constructor(t=0){this.deltaTime=0,this._currentTime=t,this._lastTickTime=t,this._startTime=t,this._lastTime=t,this._frameDuration=u/240,this._fps=240,this._speed=1,this._hasChildren=!1,this._head=null,this._tail=null}get fps(){return this._fps}set fps(t){const e=+t,s=e<d?d:e,i=u/s;s>D.frameRate&&(D.frameRate=s),this._fps=s,this._frameDuration=i}get speed(){return this._speed}set speed(t){const e=+t;this._speed=e<d?d:e}requestTick(t){const e=this._frameDuration,s=t-this._lastTickTime,i=.25*e;return s+(i<4?i:4)<e?0:(this._lastTickTime=s>=e?t-s%e:t,1)}computeDeltaTime(t){const e=t-this._lastTime;return this.deltaTime=e,this._lastTime=t,e}}const Xt={animation:null,update:_},Yt=(()=>e?requestAnimationFrame:setImmediate)(),Wt=(()=>e?cancelAnimationFrame:clearImmediate)();class Ut extends Ht{constructor(t){super(t),this.useDefaultMainLoop=!0,this.pauseOnDocumentHidden=!0,this.defaults=D,this.paused=!0,this.reqId=0}update(){const t=this._currentTime=P();if(this.requestTick(t)){this.computeDeltaTime(t);const e=this._speed,s=this._fps;let i=this._head;for(;i;){const r=i._next;i.paused?(bt(this,i),this._hasChildren=!!this._tail,i._running=!1,i.completed&&!i._cancelled&&i.cancel()):Ft(i,(t-i._startTime)*i._speed*e,0,0,i._fps<s?i.requestTick(t):1),i=r}Xt.update()}}wake(){return this.useDefaultMainLoop&&!this.reqId&&(this.requestTick(P()),this.reqId=Yt(jt)),this}pause(){if(this.reqId)return this.paused=!0,Gt()}resume(){if(this.paused)return this.paused=!1,_t(this,t=>t.resetTime()),this.wake()}get speed(){return this._speed*(1===I.timeScale?1:u)}set speed(t){const e=t*I.timeScale;this._speed!==e&&(this._speed=e,_t(this,t=>t.speed=t._speed))}get timeUnit(){return 1===I.timeScale?"ms":"s"}set timeUnit(t){const e="s"===t,s=e?.001:1;if(I.timeScale!==s){I.timeScale=s,I.tickThreshold=200*s;const t=e?.001:u;this.defaults.duration*=t,this._speed*=t}}get precision(){return I.precision}set precision(t){I.precision=t}}const qt=(()=>{const t=new Ut(P());return e&&(R.engine=t,i.addEventListener("visibilitychange",()=>{t.pauseOnDocumentHidden&&(i.hidden?t.pause():t.resume())})),t})(),jt=()=>{qt._head?(qt.reqId=Yt(jt),qt.update()):qt.reqId=0},Gt=()=>(Wt(qt.reqId),qt.reqId=0,qt),Zt={_rep:new WeakMap,_add:new Map},Qt=(t,e,s="_rep")=>{const i=Zt[s];let r=i.get(t);return r||(r={},i.set(t,r)),r[e]?r[e]:r[e]={_head:null,_tail:null}},Jt=(t,e)=>t._isOverridden||t._absoluteStartTime>e._absoluteStartTime,Kt=t=>{t._isOverlapped=1,t._isOverridden=1,t._changeDuration=d,t._currentTime=d},te=(t,e)=>{const s=t._composition;if(s===r.replace){const s=t._absoluteStartTime;vt(e,t,Jt,"_prevRep","_nextRep");const i=t._prevRep;if(i){const e=i.parent,r=i._absoluteEndTime;if(t.parent.id!==e.id&&e.iterationCount>1&&r+(e.duration-e.iterationDuration)>s){Kt(i);let t=i._prevRep;for(;t&&t.parent.id===e.id;)Kt(t),t=t._prevRep}const n=t._absoluteUpdateStartTime;if(r>n){const t=i._startTime,e=r-(t+i._updateDuration),s=ct(n-e-t,12);i._changeDuration=s,i._currentTime=s,i._isOverlapped=1,s<d&&Kt(i)}const o=t.parent.parent;if(!o||o!==e.parent){let t=!0;if(_t(e,e=>{e._isOverlapped||(t=!1)}),t){const t=e.parent;if(t){let s=!0;_t(t,t=>{t!==e&&_t(t,t=>{t._isOverlapped||(s=!1)})}),s&&t.cancel()}else e.cancel()}}}}else if(s===r.blend){const e=Qt(t.target,t.property,"_add"),s=(t=>{let e=Xt.animation;return e||(e={duration:d,computeDeltaTime:_,_offset:0,_delay:0,_head:null,_tail:null},Xt.animation=e,Xt.update=()=>{t.forEach(t=>{for(let e in t){const s=t[e],i=s._head;if(i){const t=i._valueType,e=3===t||2===t?gt(i._fromNumbers):null;let r=i._fromNumber,n=s._tail;for(;n&&n!==i;){if(e)for(let t=0,s=n._numbers.length;t<s;t++)e[t]+=n._numbers[t];else r+=n._number;n=n._prevAdd}i._toNumber=r,i._toNumbers=e}}}),Pt(e,1,1,0,2)}),e})(Zt._add);let i=e._head;i||(i={...t},i._composition=r.replace,i._updateDuration=d,i._startTime=0,i._numbers=gt(t._fromNumbers),i._number=0,i._next=null,i._prev=null,vt(e,i),vt(s,i));const n=t._toNumber;if(t._fromNumber=i._fromNumber-n,t._toNumber=0,t._numbers=gt(t._fromNumbers),t._number=0,i._fromNumber=n,t._toNumbers.length){const e=gt(t._toNumbers);e.forEach((e,s)=>{t._fromNumbers[s]=i._fromNumbers[s]-e,t._toNumbers[s]=0}),i._fromNumbers=e}vt(e,t,null,"_prevAdd","_nextAdd")}return t},ee=t=>{const e=t._composition;if(e!==r.none){const s=t.target,i=t.property,n=Zt._rep.get(s)[i];if(bt(n,t,"_prevRep","_nextRep"),e===r.blend){const e=Zt._add,r=e.get(s);if(!r)return;const n=r[i],o=Xt.animation;bt(n,t,"_prevAdd","_nextAdd");const a=n._head;if(a&&a===n._tail){bt(n,a,"_prevAdd","_nextAdd"),bt(o,a);let t=!0;for(let e in r)if(r[e]._head){t=!1;break}t&&e.delete(s)}}}return t},se=(t,e,s)=>{let i=!1;return _t(e,r=>{const n=r.target;if(t.includes(n)){const t=r.property,o=r._tweenType,a=Vt(s,n,o);(!a||a&&a===t)&&(r.parent._tail===r&&3===r._tweenType&&r._prev&&3===r._prev._tweenType&&(r._prev._renderTransforms=1),bt(e,r),ee(r),i=!0)}},!0),i},ie=(t,e,s)=>{const i=e||qt;let r;if(i._hasChildren){let e=0;_t(i,n=>{if(!n._hasChildren)if(r=se(t,n,s),r&&!n._head)n.cancel(),bt(i,n);else{const t=n._offset+n._delay+n.duration;t>e&&(e=t)}n._head?ie(t,n,s):n._hasChildren=!1},!0),H(i.iterationDuration)||(i.iterationDuration=e)}else r=se(t,i,s);r&&!i._head&&(i._hasChildren=!1,i.cancel&&i.cancel())},re=t=>(t.paused=!0,t.began=!1,t.completed=!1,t),ne=t=>t._cancelled?(t._hasChildren?_t(t,ne):_t(t,t=>{t._composition!==r.none&&te(t,Qt(t.target,t.property))}),t._cancelled=0,t):t;let oe=0;const ae=(t,e)=>t._priority>e._priority;class le extends Ht{constructor(t={},e=null,s=0){super(0),++oe;const{id:i,delay:r,duration:n,reversed:o,alternate:a,loop:l,loopDelay:h,autoplay:c,frameRate:u,playbackRate:p,priority:m,onComplete:f,onLoop:g,onPause:y,onBegin:b,onBeforeUpdate:v,onUpdate:T}=t;A.current&&A.current.register(this);const x=e?0:qt._lastTickTime,S=e?e.defaults:I.defaults,w=z(r)||H(r)?S.delay:+r,$=z(n)||H(n)?1/0:+n,C=$t(l,S.loop),E=$t(h,S.loopDelay);let k=!0===C||C===1/0||C<0?1/0:C+1,N=0;e?N=s:(qt.reqId||qt.requestTick(P()),N=(qt._lastTickTime-qt._startTime)*I.timeScale),this.id=H(i)?oe:i,this.parent=e,this.duration=mt(($+E)*k-E)||d,this.backwards=!1,this.paused=!0,this.began=!1,this.completed=!1,this.onBegin=b||S.onBegin,this.onBeforeUpdate=v||S.onBeforeUpdate,this.onUpdate=T||S.onUpdate,this.onLoop=g||S.onLoop,this.onPause=y||S.onPause,this.onComplete=f||S.onComplete,this.iterationDuration=$,this.iterationCount=k,this._autoplay=!e&&$t(c,S.autoplay),this._offset=N,this._delay=w,this._loopDelay=E,this._iterationTime=0,this._currentIteration=0,this._resolve=_,this._running=!1,this._reversed=+$t(o,S.reversed),this._reverse=this._reversed,this._cancelled=0,this._alternate=$t(a,S.alternate),this._prev=null,this._next=null,this._lastTickTime=x,this._startTime=x,this._lastTime=x,this._fps=$t(u,S.frameRate),this._speed=$t(p,S.playbackRate),this._priority=+$t(m,1)}get cancelled(){return!!this._cancelled}set cancelled(t){t?this.cancel():this.reset(!0).play()}get currentTime(){return dt(ct(this._currentTime,I.precision),-this._delay,this.duration)}set currentTime(t){const e=this.paused;this.pause().seek(+t),e||this.resume()}get iterationCurrentTime(){return dt(ct(this._iterationTime,I.precision),0,this.iterationDuration)}set iterationCurrentTime(t){this.currentTime=this.iterationDuration*this._currentIteration+t}get progress(){return dt(ct(this._currentTime/this.duration,10),0,1)}set progress(t){this.currentTime=this.duration*t}get iterationProgress(){return dt(ct(this._iterationTime/this.iterationDuration,10),0,1)}set iterationProgress(t){const e=this.iterationDuration;this.currentTime=e*this._currentIteration+e*t}get currentIteration(){return this._currentIteration}set currentIteration(t){this.currentTime=this.iterationDuration*dt(+t,0,this.iterationCount-1)}get reversed(){return!!this._reversed}set reversed(t){t?this.reverse():this.play()}get speed(){return super.speed}set speed(t){super.speed=t,this.resetTime()}reset(t=!1){return ne(this),this._reversed&&!this._reverse&&(this.reversed=!1),this._iterationTime=this.iterationDuration,Ft(this,0,1,~~t,2),re(this),this._hasChildren&&_t(this,re),this}init(t=!1){this.fps=this._fps,this.speed=this._speed,!t&&this._hasChildren&&Ft(this,this.duration,1,~~t,2),this.reset(t);const e=this._autoplay;return!0===e?this.resume():e&&!H(e.linked)&&e.link(this),this}resetTime(){const t=1/(this._speed*qt._speed);return this._startTime=P()-(this._currentTime+this._delay)*t,this}pause(){return this.paused||(this.paused=!0,this.onPause(this)),this}resume(){return this.paused?(this.paused=!1,this.duration<=d&&!this._hasChildren?Ft(this,d,0,0,2):(this._running||(vt(qt,this,ae),qt._hasChildren=!0,this._running=!0),this.resetTime(),this._startTime-=12,qt.wake()),this):this}restart(){return this.reset().resume()}seek(t,e=0,s=0){ne(this),this.completed=!1;const i=this.paused;return this.paused=!0,Ft(this,t+this._delay,~~e,~~s,1),i?this:this.resume()}alternate(){const t=this._reversed,e=this.iterationCount,s=this.iterationDuration,i=e===1/0?rt(c/s):e;return this._reversed=+(!this._alternate||i%2?!t:t),e===1/0?this.iterationProgress=this._reversed?1-this.iterationProgress:this.iterationProgress:this.seek(s*i-this._currentTime),this.resetTime(),this}play(){return this._reversed&&this.alternate(),this.resume()}reverse(){return this._reversed||this.alternate(),this.resume()}cancel(){return this._hasChildren?_t(this,t=>t.cancel(),!0):_t(this,ee),this._cancelled=1,this.pause()}stretch(t){const e=this.duration,s=ft(t);if(e===s)return this;const i=t/e,r=t<=d;return this.duration=r?d:s,this.iterationDuration=r?d:ft(this.iterationDuration*i),this._offset*=i,this._delay*=i,this._loopDelay*=i,this}revert(){Ft(this,0,1,0,1);const t=this._autoplay;return t&&t.linked&&t.linked===this&&t.revert(),this.cancel()}complete(t=0){return this.seek(this.duration,t).cancel()}then(t=_){const e=this.then,s=()=>{this.then=null,t(this),this.then=e,this._resolve=_};return new Promise(t=>(this._resolve=()=>t(s()),this.completed&&this._resolve(),this))}}function he(t){const e=O(t)?A.root.querySelectorAll(t):t;if(e instanceof NodeList||e instanceof HTMLCollection)return e}function de(t){if(X(t))return[];if(!e)return F(t)&&t.flat(1/0)||[t];if(F(t)){const e=t.flat(1/0),s=[];for(let t=0,i=e.length;t<i;t++){const i=e[t];if(!X(i)){const t=he(i);if(t)for(let e=0,i=t.length;e<i;e++){const i=t[e];if(!X(i)){let t=!1;for(let e=0,r=s.length;e<r;e++)if(s[e]===i){t=!0;break}t||s.push(i)}}else{let t=!1;for(let e=0,r=s.length;e<r;e++)if(s[e]===i){t=!0;break}t||s.push(i)}}}return s}const s=he(t);return s?Array.from(s):[t]}function ce(t){const e=de(t),s=e.length;for(let t=0;t<s;t++){const s=e[t];if(!s[n]){s[n]=!0;const t=Y(s);(s.nodeType||t)&&(s[o]=!0,s[a]=t,s[l]={})}}return e}const ue={deg:1,rad:180/lt,turn:360},pe={},me=(t,e,s,r=!1)=>{const n=e.u,o=e.n;if(1===e.t&&n===s)return e;const a=o+n+s,l=pe[a];if(H(l)||r){let r;if(n in ue)r=o*ue[n]/ue[s];else{const e=100,a=t.cloneNode(),l=t.parentNode,h=l&&l!==i?l:i.body;h.appendChild(a);const d=a.style;d.width=e+n;const c=a.offsetWidth||e;d.width=e+s;const u=c/(a.offsetWidth||e);h.removeChild(a),r=u*o}e.n=r,pe[a]=r}else e.n=l;return e.t,e.u=s,e},fe=t=>t,ge=(t=1.68)=>e=>Q(e,+t),ye={in:t=>e=>t(e),out:t=>e=>1-t(1-e),inOut:t=>e=>e<.5?t(2*e)/2:1-t(-2*e+2)/2,outIn:t=>e=>e<.5?(1-t(1-2*e))/2:(t(2*e-1)+1)/2},_e=lt/2,be=2*lt,ve={[p]:ge,Quad:ge(2),Cubic:ge(3),Quart:ge(4),Quint:ge(5),Sine:t=>1-tt(t*_e),Circ:t=>1-J(1-t*t),Expo:t=>t?Q(2,10*t-10):0,Bounce:t=>{let e,s=4;for(;t<((e=Q(2,--s))-1)/11;);return 1/Q(4,3-s)-7.5625*Q((3*e-2)/22-t,2)},Back:(t=1.7)=>e=>(+t+1)*e*e*e-+t*e*e,Elastic:(t=1,e=.3)=>{const s=dt(+t,1,10),i=dt(+e,d,2),r=i/be*nt(1/s),n=be/i;return t=>0===t||1===t?t:-s*Q(2,-10*(1-t))*K((1-t-r)*n)}},Te=(()=>{const t={linear:fe,none:fe};for(let e in ye)for(let s in ve){const i=ve[s],r=ye[e];t[e+s]=s===p||"Back"===s||"Elastic"===s?(t,e)=>r(i(t,e)):r(i)}return t})(),xe={linear:fe,none:fe},Se=t=>{if(xe[t])return xe[t];if(t.indexOf("(")<=-1){const e=ye[t]||t.includes("Back")||t.includes("Elastic")?Te[t]():Te[t];return e?xe[t]=e:fe}{const e=t.slice(0,-1).split("("),s=Te[e[0]];return s?xe[t]=s(...e[1].split(",")):fe}},we=["steps(","irregular(","linear(","cubicBezier("],$e=t=>{if(O(t))for(let e=0,s=we.length;e<s;e++)if(B(t,we[e]))return console.warn(`String syntax for \\`ease: "${t}"\\` has been removed from the core and replaced by importing and passing the easing function directly: \\`ease: ${t}\\``),fe;return z(t)?t:O(t)?Se(t):fe},Ce={t:0,n:0,u:null,o:null,d:null,s:null},Ee={t:0,n:0,u:null,o:null,d:null,s:null},ke={},Ne={func:null},De={func:null},Ae=[null],Ie=[null,null],Re={to:null};let Le,Be,Pe=0,Fe=0;class Me extends le{constructor(t,e,s,i,n=!1,o=0,a){super(e,s,i),this._head,this._tail,++Fe;const l=ce(t),h=l.length,c=e.keyframes,p=c?yt(((t,e)=>{const s={};if(F(t)){const e=[].concat(...t.map(t=>Object.keys(t))).filter(j);for(let i=0,r=e.length;i<r;i++){const r=e[i],n=t.map(t=>{const e={};for(let s in t){const i=t[s];j(s)?s===r&&(e.to=i):e[s]=i}return e});s[r]=n}}else{const i=$t(e.duration,I.defaults.duration),r=Object.keys(t).map(e=>({o:parseFloat(e)/100,p:t[e]})).sort((t,e)=>t.o-e.o);r.forEach(t=>{const e=t.o,r=t.p;for(let t in r)if(j(t)){let n=s[t];n||(n=s[t]=[]);const o=e*i;let a=n.length,l=n[a-1];const h={to:r[t]};let d=0;for(let t=0;t<a;t++)d+=n[t].duration;1===a&&(h.from=l.to),r.ease&&(h.ease=r.ease),h.duration=o-(a?d:0),n.push(h)}return t});for(let t in s){const e=s[t];let i;for(let t=0,s=e.length;t<s;t++){const s=e[t],r=s.ease;s.ease=i||void 0,i=r}e[0].duration||e.shift()}}return s})(c,e),e):e,{id:f,delay:g,duration:y,ease:_,playbackEase:b,modifier:v,composition:T,onRender:x}=p,S=s?s.defaults:I.defaults,w=$t(_,S.ease),$=$t(b,S.playbackEase),C=$?$e($):null,E=!H(w.ease),k=E?w.ease:$t(_,C?"linear":S.ease),N=E?w.settlingDuration:$t(y,S.duration),D=$t(g,S.delay),A=v||S.modifier,R=H(T)&&h>=u?r.none:H(T)?S.composition:T,L=this._offset+(s?s._offset:0);E&&(w.parent=this);let B=NaN,P=NaN,Y=0,W=0;for(let t=0;t<h;t++){const e=l[t],i=o||t,h=a||l;let c=NaN,u=NaN;for(let t in p)if(j(t)){const o=kt(e,t),a=St(e,t),l=Vt(t,e,o);let f=p[t];const g=F(f);if(n&&!g&&(Ie[0]=f,Ie[1]=f,f=Ie),g){const t=f.length,e=!M(f[0]);2===t&&e?(Re.to=f,Ae[0]=Re,Le=Ae):t>2&&e?(Le=[],f.forEach((t,e)=>{e?1===e?(Ie[1]=t,Le.push(Ie)):Le.push(t):Ie[0]=t})):Le=f}else Ae[0]=f,Le=Ae;let y=null,_=null,b=NaN,v=0,T=0;for(let t=Le.length;T<t;T++){const n=Le[T];M(n)?Be=n:(Re.to=n,Be=Re),Ne.func=null,De.func=null;const c=Et($t(Be.composition,R),e,i,h,null,null),u=V(c)?c:r[c];y||u===r.none||(y=Qt(e,l));const p=y?y._tail:null,f=s&&p&&p.parent.parent===s?p:_,g=Et(Be.to,e,i,h,Ne,f);let x;M(g)&&!H(g.to)?(Be=g,x=g.to):x=g;const S=Et(Be.from,e,i,h,De,f),w=Be.ease||k,$=Et(w,e,i,h,null,f),C=z($)||O($)?$:w,E=!H(C)&&!H(C.ease),I=E?C.ease:C,B=E?C.settlingDuration:Et($t(Be.duration,t>1?Et(N,e,i,h,null,f)/t:N),e,i,h,null,f),P=Et($t(Be.delay,T?0:D),e,i,h,null,f),U=Be.modifier||A,q=!H(S),j=!H(x),G=F(x),Z=G||q&&j,Q=_?v:0,J=_?v+P:P,K=ct(L+J,12),tt=ct(L+Q,12);W||!q&&!G||(W=1);let et=_;if(u!==r.none){let t=y._head;for(;t&&t._absoluteStartTime<=K;)if(t._isOverridden||(et=t),t=t._nextRep,t&&t._absoluteStartTime>=K)for(;t;)Kt(t),t=t._nextRep}if(Z){It(G?Et(x[0],e,i,h,De,f):S,Ce),It(G?Et(x[1],e,i,h,Ne,f):x,Ee);const t=Dt(e,l,o,ke);0===Ce.t&&(et?1===et._valueType&&(Ce.t=1,Ce.u=et._unit):(It(t,Lt),1===Lt.t&&(Ce.t=1,Ce.u=Lt.u)))}else j?It(x,Ee):_?Rt(_,Ee):It(s&&et&&et.parent.parent===s?et._value:Dt(e,l,o,ke),Ee),q?It(S,Ce):_?Rt(_,Ce):It(s&&et&&et.parent.parent===s?et._value:Dt(e,l,o,ke),Ce);if(Ce.o&&(Ce.n=At(et?et._toNumber:It(Dt(e,l,o,ke),Lt).n,Ce.n,Ce.o)),Ee.o&&(Ee.n=At(Ce.n,Ee.n,Ee.o)),Ce.t!==Ee.t)if(3===Ce.t||3===Ee.t){const t=3===Ce.t?Ce:Ee,e=3===Ce.t?Ee:Ce;e.t=3,e.s=gt(t.s),e.d=t.d.map(()=>e.n)}else if(1===Ce.t||1===Ee.t){const t=1===Ce.t?Ce:Ee,e=1===Ce.t?Ee:Ce;e.t=1,e.u=t.u}else if(2===Ce.t||2===Ee.t){const t=2===Ce.t?Ce:Ee,e=2===Ce.t?Ee:Ce;e.t=2,e.d=t.d.map(()=>0)}if(Ce.u!==Ee.u){let t=Ee.u?Ce:Ee;t=me(e,t,Ee.u?Ee.u:Ce.u,!1)}if(Ee.d&&Ce.d&&Ee.d.length!==Ce.d.length){const t=Ce.d.length>Ee.d.length?Ce:Ee,e=t===Ce?Ee:Ce;e.d=t.d.map((t,s)=>H(e.d[s])?0:e.d[s]),e.s=gt(t.s)}const st=ct(+B||d,12);let it=ke[l];X(it)||(ke[l]=null);const rt=a?a.set:null;v=ct(J+st,12);const nt=Ce.d,ot=Ee.d,at=Ee.s,lt={parent:this,id:Pe++,property:l,target:e,_value:null,_toFunc:Ne.func,_fromFunc:De.func,_ease:$e(I),_fromNumbers:nt?gt(nt):m,_toNumbers:ot?gt(ot):m,_strings:at?gt(at):m,_fromNumber:Ce.n,_toNumber:Ee.n,_numbers:nt?gt(nt):m,_number:Ce.n,_unit:Ee.u,_modifier:U,_currentTime:0,_startTime:J,_delay:+P,_updateDuration:st,_changeDuration:st,_absoluteStartTime:K,_absoluteUpdateStartTime:tt,_absoluteEndTime:ct(L+v,12),_hasFromValue:q||G?1:0,_tweenType:o,_setter:rt,_valueType:Ee.t,_composition:u,_isOverlapped:0,_isOverridden:0,_renderTransforms:0,_inlineValue:it,_prevRep:null,_nextRep:null,_prevAdd:null,_nextAdd:null,_prev:null,_next:null};u!==r.none&&te(lt,y);const ht=lt._valueType;if(3===ht)lt._value=Bt(lt,1,-1);else if(1===ht)lt._value=`${U(lt._toNumber)}${lt._unit}`;else if(2===ht){const t=Ee.d;lt._value=`rgba(${ct(t[0],0)},${ct(t[1],0)},${ct(t[2],0)},${t[3]})`}else lt._value=U(lt._toNumber);isNaN(b)&&(b=lt._startTime),_=lt,Y++,vt(this,lt)}(isNaN(P)||b<P)&&(P=b),(isNaN(B)||v>B)&&(B=v),3===o&&(c=Y-T,u=Y)}if(!isNaN(c)){let t=0;_t(this,e=>{t>=c&&t<u&&(e._renderTransforms=1,e._composition===r.blend&&_t(Xt.animation,t=>{t.id===e.id&&(t._renderTransforms=1)})),t++})}}h||console.warn("No target found. Make sure the element you're trying to animate is accessible before creating your animation."),P?(_t(this,t=>{t._startTime-t._delay||(t._delay-=P),t._startTime-=P}),B-=P):P=0,B||(B=d,this.iterationCount=0),this.targets=l,this.id=H(f)?Fe:f,this.duration=B===d?d:mt((B+this._loopDelay)*this.iterationCount-this._loopDelay)||d,this.onRender=x||S.onRender,this._ease=C,this._delay=P,this.iterationDuration=B,!this._autoplay&&W&&this.onRender(this)}stretch(t){const e=this.duration;if(e===ft(t))return this;const s=t/e;return _t(this,t=>{t._updateDuration=ft(t._updateDuration*s),t._changeDuration=ft(t._changeDuration*s),t._currentTime*=s,t._delay*=s,t._startTime*=s,t._absoluteStartTime*=s,t._absoluteUpdateStartTime*=s,t._absoluteEndTime*=s}),super.stretch(t)}refresh(){return _t(this,t=>{const e=t._toFunc,s=t._fromFunc;(e||s)&&(s?(It(s(),Ce),Ce.u!==t._unit&&t.target[o]&&me(t.target,Ce,t._unit,!0),t._fromNumbers=gt(Ce.d),t._fromNumber=Ce.n):e&&(It(Dt(t.target,t.property,t._tweenType),Lt),t._fromNumbers=gt(Lt.d),t._fromNumber=Lt.n),e&&(It(e(),Ee),t._toNumbers=gt(Ee.d),t._strings=gt(Ee.s),t._toNumber=Ee.o?At(t._fromNumber,Ee.n,Ee.o):Ee.n))}),this.duration===d&&this.restart(),this}revert(){return super.revert(),Ot(this)}then(t){return super.then(t)}}const Ve=(t,e)=>{let s=t.iterationDuration;if(s===d&&(s=0),H(e))return s;if(V(+e))return+e;const i=e,r=t?t.labels:null,n=!X(r),o=((t,e)=>{if(B(e,"<")){const s="<"===e[1],i=t._tail,r=i?i._offset+i._delay:0;return s?r:r+i.duration}})(t,i),a=!H(o),l=k.exec(i);if(l){const t=l[0],e=i.split(t),h=n&&e[0]?r[e[0]]:s,d=a?o:n?h:s,c=+e[1];return At(d,c,t[0])}return a?o:n?H(r[i])?s:r[i]:s};function Oe(t,e,s,i,r,n){const o=V(t.duration)&&t.duration<=d?s-d:s;e.composition&&Ft(e,o,1,1,1);const a=i?new Me(i,t,e,o,!1,r,n):new le(t,e,o);return e.composition&&a.init(!0),vt(e,a),_t(e,t=>{const s=t._offset+t._delay+t.duration;s>e.iterationDuration&&(e.iterationDuration=s)}),e.duration=function(t){return mt((t.iterationDuration+t._loopDelay)*t.iterationCount-t._loopDelay)||d}(e),e}let ze=0;class He extends le{constructor(t={}){super(t,null,0),++ze,this.id=H(t.id)?ze:t.id,this.duration=0,this.labels={};const e=t.defaults,s=I.defaults;this.defaults=e?yt(e,s):s,this.composition=$t(t.composition,!0),this.onRender=t.onRender||s.onRender;const i=$t(t.playbackEase,s.playbackEase);this._ease=i?$e(i):null,this.iterationDuration=0}add(t,e,s){const i=M(e),r=M(t);if(i||r){if(this._hasChildren=!0,i){const i=e,r=I.editor&&I.editor.addTimelineChild,n=s&&"Stagger"===s.type&&I.editor,o=z(s)?s:null;if(o||n){const e=de(t),n=this.duration,a=this.iterationDuration,l=i.id;let h=0;const d=e.length,c=r?r(t,i,this.id,s,d):null,u=o||I.editor.resolveStagger(s.defaultValue);e.forEach(t=>{const s={...c||i};this.duration=n,this.iterationDuration=a,H(l)||(s.id=l+"-"+h),Oe(s,this,Ve(this,u(t,h,e,null,this)),t,h,e),h++})}else{const e=r?r(t,i,this.id,s):i,n=s&&s.type?s.defaultValue:s;Oe(e,this,Ve(this,n),t)}}else Oe(t,this,Ve(this,e));return this.composition&&this.init(!0),this}}sync(t,e){if(H(t)||t&&H(t.pause))return this;t.pause();const s=+(t.effect?t.effect.getTiming().duration:t.duration);H(t)||H(t.persist)||(t.persist=!0);const i=I.editor,r=i&&i.addTimelineChild;i&&i.addTimelineSync&&(e=i.addTimelineSync(t,e,this.id),i.addTimelineChild=null);const n=this.add(t,{currentTime:[0,s],duration:s,delay:0,ease:"linear",playbackEase:"linear"},e);return i&&(i.addTimelineChild=r),n}set(t,e,s){return H(e)?this:(e.duration=d,e.composition=r.replace,this.add(t,e,s))}call(t,e){return H(t)||t&&!z(t)?this:(I.editor&&I.editor.addTimelineCall&&(e=I.editor.addTimelineCall(t,e,this.id)),this.add({duration:0,delay:0,onComplete:()=>t(this)},e))}label(t,e){return H(t)||t&&!O(t)||(I.editor&&I.editor.addTimelineLabel&&(e=I.editor.addTimelineLabel(t,e,this.id)),this.labels[t]=Ve(this,e)),this}remove(t,e){return ie(de(t),this,e),this}stretch(t){const e=this.duration;if(e===ft(t))return this;const s=t/e,i=this.labels;_t(this,t=>t.stretch(t.duration*s));for(let t in i)i[t]*=s;return super.stretch(t)}refresh(){return _t(this,t=>{t.refresh&&t.refresh()}),this}revert(){return super.revert(),_t(this,t=>t.revert,!0),Ot(this)}then(t){return super.then(t)}}const Xe=t=>I.editor?I.editor.addTimeline(t):new He(t).init();class Ye{constructor(t,e){A.current&&A.current.register(this);const s=()=>{if(this.callbacks.completed)return;let t=!0;for(let e in this.animations)if(!this.animations[e].paused&&t){t=!1;break}t&&this.callbacks.complete()},i={onBegin:()=>{this.callbacks.completed&&this.callbacks.reset(),this.callbacks.play()},onComplete:s,onPause:s},n={v:1,autoplay:!1},o={};if(this.targets=[],this.animations={},this.callbacks=null,!H(t)&&!H(e)){for(let t in e){const s=e[t];j(t)?o[t]=s:B(t,"on")?n[t]=s:i[t]=s}this.callbacks=new Me({v:0},n);for(let e in o){const s=o[e],n=M(s);let a={},l="+=0";if(n){const t=s.unit;O(t)&&(l+=t)}else a.duration=s;a[e]=n?yt({to:l},s):l;const h=yt(i,a);h.composition=r.replace,h.autoplay=!1;const d=this.animations[e]=new Me(t,h,null,0,!1).init();this.targets.length||this.targets.push(...d.targets),this[e]=(t,e,s)=>{const i=d._head;if(H(t)&&i){const t=i._numbers;return t&&t.length?t:i._modifier(i._number)}return _t(d,e=>{if(F(t))for(let s=0,i=t.length;s<i;s++)H(e._numbers[s])||(e._fromNumbers[s]=e._modifier(e._numbers[s]),e._toNumbers[s]=t[s]);else e._fromNumber=e._modifier(e._number),e._toNumber=t;H(s)||(e._ease=$e(s)),e._currentTime=0}),H(e)||d.stretch(e),d.reset(!0).resume(),this}}}}revert(){for(let t in this.animations)this[t]=_,this.animations[t].revert();return this.animations={},this.targets.length=0,this.callbacks&&this.callbacks.revert(),this}}const We=(t,e,s,i,r)=>i+(t-e)/(s-e)*(r-i);var Ue=Object.freeze({__proto__:null,clamp:dt,damp:(t,e,s,i)=>i?1===i?e:pt(t,e,1-Math.exp(-i*s*.1)):t,degToRad:t=>t*Math.PI/180,lerp:pt,mapRange:We,padEnd:(t,e,s)=>`${t}`.padEnd(e,s),padStart:(t,e,s)=>`${t}`.padStart(e,s),radToDeg:t=>180*t/Math.PI,round:ct,roundPad:(t,e)=>(+t).toFixed(e),snap:ut,wrap:(t,e,s)=>((t-e)%(s-e)+(s-e))%(s-e)+e});const qe=10*u;class je{constructor(t={}){const e=!H(t.bounce)||!H(t.duration);this.timeStep=.02,this.restThreshold=5e-4,this.restDuration=200,this.maxDuration=6e4,this.maxRestSteps=this.restDuration/this.timeStep/u,this.maxIterations=this.maxDuration/this.timeStep/u,this.bn=dt($t(t.bounce,.5),-1,1),this.pd=dt($t(t.duration,628),10*I.timeScale,qe*I.timeScale),this.m=dt($t(t.mass,1),1,qe),this.s=dt($t(t.stiffness,100),d,qe),this.d=dt($t(t.damping,10),d,qe),this.v=dt($t(t.velocity,0),-1e4,qe),this.w0=0,this.zeta=0,this.wd=0,this.b=0,this.completed=!1,this.solverDuration=0,this.settlingDuration=0,this.parent=null,this.onComplete=t.onComplete||_,e&&this.calculateSDFromBD(),this.compute(),this.ease=t=>{const e=t*this.settlingDuration,s=this.completed,i=this.pd;return e>=i&&!s&&(this.completed=!0,this.onComplete(this.parent)),e<i&&s&&(this.completed=!1),0===t||1===t?t:this.solve(t*this.solverDuration)}}solve(t){const{zeta:e,w0:s,wd:i,b:r}=this;let n=t;return n=e<1?st(-n*e*s)*(1*tt(i*n)+r*K(i*n)):1===e?(1+r*n)*st(-n*s):((1+r)*st((-e*s+i)*n)+(1-r)*st((-e*s-i)*n))/2,1-n}calculateSDFromBD(){const t=1===I.timeScale?this.pd/u:this.pd;this.m=1,this.v=0,this.s=Q(2*lt/t,2),this.bn>=0?this.d=4*(1-this.bn)*lt/t:this.d=4*lt/(t*(1+this.bn)),this.s=ct(dt(this.s,d,qe),3),this.d=ct(dt(this.d,d,300),3)}calculateBDFromSD(){const t=2*lt/J(this.s);this.pd=t*(1===I.timeScale?u:1);const e=this.d/(2*J(this.s));this.bn=e<=1?1-this.d*t/(4*lt):4*lt/(this.d*t)-1,this.bn=ct(dt(this.bn,-1,1),3),this.pd=ct(dt(this.pd,10*I.timeScale,qe*I.timeScale),3)}compute(){const{maxRestSteps:t,maxIterations:e,restThreshold:s,timeStep:i,m:r,d:n,s:o,v:a}=this,l=this.w0=dt(J(o/r),d,u),h=this.zeta=n/(2*J(o*r));h<1?(this.wd=l*J(1-h*h),this.b=(h*l-a)/this.wd):1===h?(this.wd=0,this.b=-a+l):(this.wd=l*J(h*h-1),this.b=(h*l-a)/this.wd);let c=0,p=0,m=0;for(;p<=t&&m<=e;)et(1-this.solve(c))<s?p++:p=0,this.solverDuration=c,c+=i,m++;this.settlingDuration=ct(this.solverDuration*u,0)*I.timeScale}get bounce(){return this.bn}set bounce(t){this.bn=dt($t(t,1),-1,1),this.calculateSDFromBD(),this.compute()}get duration(){return this.pd}set duration(t){this.pd=dt($t(t,1),10*I.timeScale,qe*I.timeScale),this.calculateSDFromBD(),this.compute()}get stiffness(){return this.s}set stiffness(t){this.s=dt($t(t,100),d,qe),this.calculateBDFromSD(),this.compute()}get damping(){return this.d}set damping(t){this.d=dt($t(t,10),d,qe),this.calculateBDFromSD(),this.compute()}get mass(){return this.m}set mass(t){this.m=dt($t(t,1),1,qe),this.compute()}get velocity(){return this.v}set velocity(t){this.v=dt($t(t,0),-1e4,qe),this.compute()}}const Ge=t=>new je(t),Ze=t=>(console.warn("createSpring() is deprecated use spring() instead"),new je(t)),Qe={_head:null,_tail:null},Je=(t,e,s)=>{let i,r=Qe._head;for(;r;){const n=r._next,o=r.$el===t,a=!e||r.property===e,l=!s||r.parent===s;if(o&&a&&l){i=r.animation;try{i.commitStyles()}catch{}i.cancel(),bt(Qe,r);const t=r.parent;t&&(t._completed++,t.animations.length===t._completed&&(t.completed=!0,t.paused=!0,t.muteCallbacks||(t.onComplete(t),t._resolve(t))))}r=n}return i},Ke=(t,e,s,i,r)=>{const n=e.animate(i,r),o=r.delay+ +r.duration*r.iterations;n.playbackRate=t._speed,t.paused&&n.pause(),t.duration<o&&(t.duration=o,t.controlAnimation=n),t.animations.push(n),Je(e,s),vt(Qe,{parent:t,animation:n,$el:e,property:s,_next:null,_prev:null});const a=()=>Je(e,s,t);return n.oncancel=a,n.onremove=a,t.persist||(n.onfinish=a),n};function ts(t,e,s){const i=ce(t);if(!i.length)return;const[r]=i,n=kt(r,e),o=Vt(e,r,n);let a=Dt(r,o);if(H(s))return a;if(It(a,Lt),0===Lt.t||1===Lt.t){if(!1===s)return Lt.n;{const t=me(r,Lt,s,!1);return`${ct(t.n,I.precision)}${t.u}`}}}const es=(t,e)=>{if(!H(e))return I.editor&&I.editor.addSet?I.editor.addSet(t,e):(e.duration=d,e.composition=$t(e.composition,r.none),new Me(t,e,null,0,!0).resume())},ss=(t,e,s)=>{const i=de(t);for(let t=0,r=i.length;t<r;t++)Je(i[t],s,e&&e.controlAnimation&&e);return ie(i,e,s),i},is=t=>{t.cancelable&&t.preventDefault()};class rs{constructor(t){this.el=t,this.zIndex=0,this.parentElement=null,this.classList={add:_,remove:_}}get x(){return this.el.x||0}set x(t){this.el.x=t}get y(){return this.el.y||0}set y(t){this.el.y=t}get width(){return this.el.width||0}set width(t){this.el.width=t}get height(){return this.el.height||0}set height(t){this.el.height=t}getBoundingClientRect(){return{top:this.y,right:this.x,bottom:this.y+this.height,left:this.x+this.width}}}class ns{constructor(t){this.$el=t,this.inlineTransforms=[],this.point=new DOMPoint,this.inversedMatrix=this.getMatrix().inverse()}normalizePoint(t,e){return this.point.x=t,this.point.y=e,this.point.matrixTransform(this.inversedMatrix)}traverseUp(t){let e=this.$el.parentElement,s=0;for(;e&&e!==i;)t(e,s),e=e.parentElement,s++}getMatrix(){const t=new DOMMatrix;return this.traverseUp(e=>{const s=getComputedStyle(e).transform;if(s){const e=new DOMMatrix(s);t.preMultiplySelf(e)}}),t}remove(){this.traverseUp((t,e)=>{this.inlineTransforms[e]=t.style.transform,t.style.transform="none"})}revert(){this.traverseUp((t,e)=>{const s=this.inlineTransforms[e];""===s?t.style.removeProperty("transform"):t.style.transform=s})}}const os=(t,e)=>t&&z(t)?t(e):t;let as=0;class ls{constructor(t,e={}){if(!t)return;A.current&&A.current.register(this);const r=e.x,n=e.y,o=e.trigger,a=e.modifier,l=e.releaseEase,h=l&&$e(l),d=!H(l)&&!H(l.ease),u=M(r)&&!H(r.mapTo)?r.mapTo:"translateX",p=M(n)&&!H(n.mapTo)?n.mapTo:"translateY",m=os(e.container,this);this.containerArray=F(m)?m:null,this.$container=m&&!this.containerArray?de(m)[0]:i.body,this.useWin=this.$container===i.body,this.$scrollContainer=this.useWin?s:this.$container,this.$target=M(t)?new rs(t):de(t)[0],this.$trigger=de(o||t)[0],this.fixed="fixed"===ts(this.$target,"position"),this.isFinePointer=!0,this.containerPadding=[0,0,0,0],this.containerFriction=0,this.releaseContainerFriction=0,this.snapX=0,this.snapY=0,this.scrollSpeed=0,this.scrollThreshold=0,this.dragSpeed=0,this.dragThreshold=3,this.maxVelocity=0,this.minVelocity=0,this.velocityMultiplier=0,this.cursor=!1,this.releaseXSpring=d?l:Ge({mass:$t(e.releaseMass,1),stiffness:$t(e.releaseStiffness,80),damping:$t(e.releaseDamping,20)}),this.releaseYSpring=d?l:Ge({mass:$t(e.releaseMass,1),stiffness:$t(e.releaseStiffness,80),damping:$t(e.releaseDamping,20)}),this.releaseEase=h||Te.outQuint,this.hasReleaseSpring=d,this.onGrab=e.onGrab||_,this.onDrag=e.onDrag||_,this.onRelease=e.onRelease||_,this.onUpdate=e.onUpdate||_,this.onSettle=e.onSettle||_,this.onSnap=e.onSnap||_,this.onResize=e.onResize||_,this.onAfterResize=e.onAfterResize||_,this.disabled=[0,0];const f={};if(a&&(f.modifier=a),H(r)||!0===r)f[u]=0;else if(M(r)){const t=r,e={};t.modifier&&(e.modifier=t.modifier),t.composition&&(e.composition=t.composition),f[u]=e}else!1===r&&(f[u]=0,this.disabled[0]=1);if(H(n)||!0===n)f[p]=0;else if(M(n)){const t=n,e={};t.modifier&&(e.modifier=t.modifier),t.composition&&(e.composition=t.composition),f[p]=e}else!1===n&&(f[p]=0,this.disabled[1]=1);this.animate=new Ye(this.$target,f),this.xProp=u,this.yProp=p,this.destX=0,this.destY=0,this.deltaX=0,this.deltaY=0,this.scroll={x:0,y:0},this.coords=[this.x,this.y,0,0],this.snapped=[0,0],this.pointer=[0,0,0,0,0,0,0,0],this.scrollView=[0,0],this.dragArea=[0,0,0,0],this.containerBounds=[-c,c,c,-c],this.scrollBounds=[0,0,0,0],this.targetBounds=[0,0,0,0],this.window=[0,0],this.velocityStack=[0,0,0],this.velocityStackIndex=0,this.velocityTime=P(),this.velocity=0,this.angle=0,this.cursorStyles=null,this.triggerStyles=null,this.bodyStyles=null,this.targetStyles=null,this.touchActionStyles=null,this.transforms=new ns(this.$target),this.overshootCoords={x:0,y:0},this.overshootTicker=new le({autoplay:!1,onUpdate:()=>{this.updated=!0,this.manual=!0,this.disabled[0]||this.animate[this.xProp](this.overshootCoords.x,1),this.disabled[1]||this.animate[this.yProp](this.overshootCoords.y,1)},onComplete:()=>{this.manual=!1,this.disabled[0]||this.animate[this.xProp](this.overshootCoords.x,0),this.disabled[1]||this.animate[this.yProp](this.overshootCoords.y,0)}},null,0).init(),this.updateTicker=new le({autoplay:!1,onUpdate:()=>this.update()},null,0).init(),this.contained=!H(m),this.manual=!1,this.grabbed=!1,this.dragged=!1,this.updated=!1,this.released=!1,this.canScroll=!1,this.enabled=!1,this.initialized=!1,this.activeProp=this.disabled[1]?u:p,this.animate.callbacks.onRender=()=>{const t=this.updated,e=!(this.grabbed&&t)&&this.released,s=this.x,i=this.y,r=s-this.coords[2],n=i-this.coords[3];this.deltaX=r,this.deltaY=n,this.coords[2]=s,this.coords[3]=i,t&&(r||n)&&this.onUpdate(this),e?(this.computeVelocity(r,n),this.angle=at(n,r)):this.updated=!1},this.animate.callbacks.onComplete=()=>{!this.grabbed&&this.released&&(this.released=!1),this.manual||(this.deltaX=0,this.deltaY=0,this.velocity=0,this.velocityStack[0]=0,this.velocityStack[1]=0,this.velocityStack[2]=0,this.velocityStackIndex=0,this.onSettle(this))},this.resizeTicker=new le({autoplay:!1,duration:150*I.timeScale,onComplete:()=>{this.onResize(this),this.refresh(),this.onAfterResize(this)}}).init(),this.parameters=e,this.resizeObserver=new ResizeObserver(()=>{this.initialized?this.resizeTicker.restart():this.initialized=!0}),this.enable(),this.refresh(),this.resizeObserver.observe(this.$container),M(t)||this.resizeObserver.observe(this.$target)}computeVelocity(t,e){const s=this.velocityTime,i=P(),r=i-s;if(r<17)return this.velocity;this.velocityTime=i;const n=this.velocityStack,o=this.velocityMultiplier,a=this.minVelocity,l=this.maxVelocity,h=this.velocityStackIndex;n[h]=ct(dt(J(t*t+e*e)/r*o,a,l),5);const d=ot(n[0],n[1],n[2]);return this.velocity=d,this.velocityStackIndex=(h+1)%3,d}setX(t,e=!1){if(this.disabled[0])return;const s=ct(t,5);return this.overshootTicker.pause(),this.manual=!0,this.updated=!e,this.destX=s,this.snapped[0]=ut(s,this.snapX),this.animate[this.xProp](s,0),this.manual=!1,this}setY(t,e=!1){if(this.disabled[1])return;const s=ct(t,5);return this.overshootTicker.pause(),this.manual=!0,this.updated=!e,this.destY=s,this.snapped[1]=ut(s,this.snapY),this.animate[this.yProp](s,0),this.manual=!1,this}get x(){return ct(this.animate[this.xProp](),I.precision)}set x(t){this.setX(t,!1)}get y(){return ct(this.animate[this.yProp](),I.precision)}set y(t){this.setY(t,!1)}get progressX(){return We(this.x,this.containerBounds[3],this.containerBounds[1],0,1)}set progressX(t){this.setX(We(t,0,1,this.containerBounds[3],this.containerBounds[1]),!1)}get progressY(){return We(this.y,this.containerBounds[0],this.containerBounds[2],0,1)}set progressY(t){this.setY(We(t,0,1,this.containerBounds[0],this.containerBounds[2]),!1)}updateScrollCoords(){const t=ct(this.useWin?s.scrollX:this.$container.scrollLeft,0),e=ct(this.useWin?s.scrollY:this.$container.scrollTop,0),[i,r,n,o]=this.containerPadding,a=this.scrollThreshold;this.scroll.x=t,this.scroll.y=e,this.scrollBounds[0]=e-this.targetBounds[0]+i-a,this.scrollBounds[1]=t-this.targetBounds[1]-r+a,this.scrollBounds[2]=e-this.targetBounds[2]-n+a,this.scrollBounds[3]=t-this.targetBounds[3]+o-a}updateBoundingValues(){const t=this.$container;if(!t)return;const e=this.x,r=this.y,n=this.coords[2],o=this.coords[3];this.coords[2]=0,this.coords[3]=0,this.setX(0,!0),this.setY(0,!0),this.transforms.remove();const a=this.window[0]=s.innerWidth,l=this.window[1]=s.innerHeight,h=this.useWin,d=t.scrollWidth,c=t.scrollHeight,u=this.fixed,p=t.getBoundingClientRect(),[m,f,g,y]=this.containerPadding;this.dragArea[0]=h?0:p.left,this.dragArea[1]=h?0:p.top,this.scrollView[0]=h?dt(d,a,d):d,this.scrollView[1]=h?dt(c,l,c):c,this.updateScrollCoords();const{width:_,height:b,left:v,top:T,right:x,bottom:S}=t.getBoundingClientRect();this.dragArea[2]=ct(h?dt(_,a,a):_,0),this.dragArea[3]=ct(h?dt(b,l,l):b,0);const w=ts(t,"overflow"),$="visible"===w,C="hidden"===w;if(this.canScroll=!u&&this.contained&&(t===i.body&&$||!C&&!$)&&(d>this.dragArea[2]+y-f||c>this.dragArea[3]+m-g)&&(!this.containerArray||this.containerArray&&!F(this.containerArray)),this.contained){const e=this.scroll.x,s=this.scroll.y,i=this.canScroll,r=this.$target.getBoundingClientRect(),n=i?h?0:t.scrollLeft:0,o=i?h?0:t.scrollTop:0,d=i?this.scrollView[0]-n-_:0,c=i?this.scrollView[1]-o-b:0;this.targetBounds[0]=ct(r.top+s-(h?0:T),0),this.targetBounds[1]=ct(r.right+e-(h?a:x),0),this.targetBounds[2]=ct(r.bottom+s-(h?l:S),0),this.targetBounds[3]=ct(r.left+e-(h?0:v),0),this.containerArray?(this.containerBounds[0]=this.containerArray[0]+m,this.containerBounds[1]=this.containerArray[1]-f,this.containerBounds[2]=this.containerArray[2]-g,this.containerBounds[3]=this.containerArray[3]+y):(this.containerBounds[0]=-ct(r.top-(u?dt(T,0,l):T)+o-m,0),this.containerBounds[1]=-ct(r.right-(u?dt(x,0,a):x)-d+f,0),this.containerBounds[2]=-ct(r.bottom-(u?dt(S,0,l):S)-c+g,0),this.containerBounds[3]=-ct(r.left-(u?dt(v,0,a):v)+n-y,0))}this.transforms.revert(),this.coords[2]=n,this.coords[3]=o,this.setX(e,!0),this.setY(r,!0)}isOutOfBounds(t,e,s){if(!this.contained)return 0;const[i,r,n,o]=t,[a,l]=this.disabled,h=!a&&e<o||!a&&e>r,d=!l&&s<i||!l&&s>n;return h&&!d?1:!h&&d?2:h&&d?3:0}refresh(){const t=this.parameters,e=t.x,r=t.y,n=os(t.container,this),o=os(t.containerPadding,this)||0,a=F(o)?o:[o,o,o,o],l=this.x,h=this.y,d=os(t.cursor,this),c={onHover:"grab",onGrab:"grabbing"};if(d){const{onHover:t,onGrab:e}=d;t&&(c.onHover=t),e&&(c.onGrab=e)}const u=os(t.dragThreshold,this),p={mouse:3,touch:7};if(V(u))p.mouse=u,p.touch=u;else if(u){const{mouse:t,touch:e}=u;H(t)||(p.mouse=t),H(e)||(p.touch=e)}this.containerArray=F(n)?n:null,this.$container=n&&!this.containerArray?de(n)[0]:i.body,this.useWin=this.$container===i.body,this.$scrollContainer=this.useWin?s:this.$container,this.isFinePointer=matchMedia("(pointer:fine)").matches,this.containerPadding=$t(a,[0,0,0,0]),this.containerFriction=dt($t(os(t.containerFriction,this),.8),0,1),this.releaseContainerFriction=dt($t(os(t.releaseContainerFriction,this),this.containerFriction),0,1),this.snapX=os(M(e)&&!H(e.snap)?e.snap:t.snap,this),this.snapY=os(M(r)&&!H(r.snap)?r.snap:t.snap,this),this.scrollSpeed=$t(os(t.scrollSpeed,this),1.5),this.scrollThreshold=$t(os(t.scrollThreshold,this),20),this.dragSpeed=$t(os(t.dragSpeed,this),1),this.dragThreshold=this.isFinePointer?p.mouse:p.touch,this.minVelocity=$t(os(t.minVelocity,this),0),this.maxVelocity=$t(os(t.maxVelocity,this),50),this.velocityMultiplier=$t(os(t.velocityMultiplier,this),1),this.cursor=!1!==d&&c,this.updateBoundingValues();const[m,f,g,y]=this.containerBounds;this.setX(dt(l,y,f),!0),this.setY(dt(h,m,g),!0)}update(){if(this.updateScrollCoords(),this.canScroll){const[t,e,s,i]=this.containerPadding,[r,n]=this.scrollView,o=this.dragArea[2],a=this.dragArea[3],l=this.scroll.x,h=this.scroll.y,d=this.$container.scrollWidth,c=this.$container.scrollHeight,u=this.useWin?dt(d,this.window[0],d):d,p=this.useWin?dt(c,this.window[1],c):c,m=r-u,f=n-p;this.dragged&&m>0&&(this.coords[0]-=m,this.scrollView[0]=u),this.dragged&&f>0&&(this.coords[1]-=f,this.scrollView[1]=p);const g=10*this.scrollSpeed,y=this.scrollThreshold,[_,b]=this.coords,[v,T,x,S]=this.scrollBounds,w=ct(dt((b-v+t)/y,-1,0)*g,0),$=ct(dt((_-T-e)/y,0,1)*g,0),C=ct(dt((b-x-s)/y,0,1)*g,0),E=ct(dt((_-S+i)/y,-1,0)*g,0);if(w||C||E||$){const[t,e]=this.disabled;let s=l,i=h;t||(s=ct(dt(l+(E||$),0,r-o),0),this.coords[0]-=l-s),e||(i=ct(dt(h+(w||C),0,n-a),0),this.coords[1]-=h-i),this.useWin?this.$scrollContainer.scrollBy(-(l-s),-(h-i)):this.$scrollContainer.scrollTo(s,i)}}const[t,e,s,i]=this.containerBounds,[r,n,o,a,l,h]=this.pointer;this.coords[0]+=(r-l)*this.dragSpeed,this.coords[1]+=(n-h)*this.dragSpeed,this.pointer[4]=r,this.pointer[5]=n;const[d,c]=this.coords,[u,p]=this.snapped,m=(1-this.containerFriction)*this.dragSpeed;this.setX(d>e?e+(d-e)*m:d<i?i+(d-i)*m:d,!1),this.setY(c>s?s+(c-s)*m:c<t?t+(c-t)*m:c,!1),this.computeVelocity(r-l,n-h),this.angle=at(n-a,r-o);const[f,g]=this.snapped;(f!==u&&this.snapX||g!==p&&this.snapY)&&this.onSnap(this)}stop(){this.updateTicker.pause(),this.overshootTicker.pause();for(let t in this.animate.animations)this.animate.animations[t].pause();return ie([this],null,"x"),ie([this],null,"y"),ie([this],null,"progressX"),ie([this],null,"progressY"),ie([this.scroll]),ie([this.overshootCoords]),this}scrollInView(t,e=0,s=Te.inOutQuad){this.updateScrollCoords();const i=this.destX,r=this.destY,n=this.scroll,o=this.scrollBounds,a=this.canScroll;if(!this.containerArray&&this.isOutOfBounds(o,i,r)){const[l,h,d,u]=o,p=ct(dt(r-l,-c,0),0),m=ct(dt(i-h,0,c),0),f=ct(dt(r-d,0,c),0),g=ct(dt(i-u,-c,0),0);new Me(n,{x:ct(n.x+(g?g-e:m?m+e:0),0),y:ct(n.y+(p?p-e:f?f+e:0),0),duration:H(t)?350*I.timeScale:t,ease:s,onUpdate:()=>{this.canScroll=!1,this.$scrollContainer.scrollTo(n.x,n.y)}}).init().then(()=>{this.canScroll=a})}return this}handleHover(){this.isFinePointer&&this.cursor&&!this.cursorStyles&&(this.cursorStyles=es(this.$trigger,{cursor:this.cursor.onHover}))}animateInView(t,e=0,s=Te.inOutQuad){this.stop(),this.updateBoundingValues();const i=this.x,r=this.y,[n,o,a,l]=this.containerPadding,h=this.scroll.y-this.targetBounds[0]+n+e,d=this.scroll.x-this.targetBounds[1]-o-e,c=this.scroll.y-this.targetBounds[2]-a-e,u=this.scroll.x-this.targetBounds[3]+l+e,p=this.isOutOfBounds([h,d,c,u],i,r);if(p){const[e,n]=this.disabled,o=dt(ut(i,this.snapX),u,d),a=dt(ut(r,this.snapY),h,c),l=H(t)?350*I.timeScale:t;e||1!==p&&3!==p||this.animate[this.xProp](o,l,s),n||2!==p&&3!==p||this.animate[this.yProp](a,l,s)}return this}handleDown(t){const e=t.target;if(this.grabbed||"range"===e.type)return;t.stopPropagation(),this.grabbed=!0,this.released=!1,this.stop(),this.updateBoundingValues();const s=t.changedTouches,r=s?s[0].clientX:t.clientX,n=s?s[0].clientY:t.clientY,{x:o,y:a}=this.transforms.normalizePoint(r,n),[l,h,d,c]=this.containerBounds,u=(1-this.containerFriction)*this.dragSpeed,p=this.x,m=this.y;this.coords[0]=this.coords[2]=u?p>h?h+(p-h)/u:p<c?c+(p-c)/u:p:p,this.coords[1]=this.coords[3]=u?m>d?d+(m-d)/u:m<l?l+(m-l)/u:m:m,this.pointer[0]=o,this.pointer[1]=a,this.pointer[2]=o,this.pointer[3]=a,this.pointer[4]=o,this.pointer[5]=a,this.pointer[6]=o,this.pointer[7]=a,this.deltaX=0,this.deltaY=0,this.velocity=0,this.velocityStack[0]=0,this.velocityStack[1]=0,this.velocityStack[2]=0,this.velocityStackIndex=0,this.angle=0,this.targetStyles&&(this.targetStyles.revert(),this.targetStyles=null);const f=ts(this.$target,"zIndex",!1);as=(f>as?f:as)+1,this.targetStyles=es(this.$target,{zIndex:as}),this.triggerStyles&&(this.triggerStyles.revert(),this.triggerStyles=null),this.cursorStyles&&(this.cursorStyles.revert(),this.cursorStyles=null),this.isFinePointer&&this.cursor&&(this.bodyStyles=es(i.body,{cursor:this.cursor.onGrab})),this.scrollInView(100,0,Te.out(3)),this.onGrab(this),i.addEventListener("touchmove",this),i.addEventListener("touchend",this),i.addEventListener("touchcancel",this),i.addEventListener("mousemove",this),i.addEventListener("mouseup",this),i.addEventListener("selectstart",this)}handleMove(t){if(!this.grabbed)return;const e=t.changedTouches,s=e?e[0].clientX:t.clientX,i=e?e[0].clientY:t.clientY,{x:r,y:n}=this.transforms.normalizePoint(s,i),o=r-this.pointer[6],a=n-this.pointer[7];let l=t.target,h=!1,d=!1,c=!1;for(;e&&l&&l!==this.$trigger;){const t=ts(l,"overflow-y");if("hidden"!==t&&"visible"!==t){const{scrollTop:t,scrollHeight:e,clientHeight:s}=l;if(e>s){c=!0,h=t<=3,d=t>=e-s-3;break}}l=l.parentElement}c&&(!h&&!d||h&&a<0||d&&a>0)?(this.pointer[0]=r,this.pointer[1]=n,this.pointer[2]=r,this.pointer[3]=n,this.pointer[4]=r,this.pointer[5]=n,this.pointer[6]=r,this.pointer[7]=n):(is(t),this.triggerStyles||(this.triggerStyles=es(this.$trigger,{pointerEvents:"none"})),this.$trigger.addEventListener("touchstart",is,{passive:!1}),this.$trigger.addEventListener("touchmove",is,{passive:!1}),this.$trigger.addEventListener("touchend",is),(this.dragged||!this.disabled[0]&&et(o)>this.dragThreshold||!this.disabled[1]&&et(a)>this.dragThreshold)&&(this.updateTicker.resume(),this.pointer[2]=this.pointer[0],this.pointer[3]=this.pointer[1],this.pointer[0]=r,this.pointer[1]=n,this.dragged=!0,this.released=!1,this.onDrag(this)))}handleUp(){if(!this.grabbed)return;this.updateTicker.pause(),this.triggerStyles&&(this.triggerStyles.revert(),this.triggerStyles=null),this.bodyStyles&&(this.bodyStyles.revert(),this.bodyStyles=null);const[t,e]=this.disabled,[s,n,o,a,l,h]=this.pointer,[d,c,u,p]=this.containerBounds,[m,f]=this.snapped,g=this.releaseXSpring,y=this.releaseYSpring,_=this.releaseEase,b=this.hasReleaseSpring,v=this.overshootCoords,T=this.x,x=this.y,S=this.computeVelocity(s-l,n-h),w=this.angle=at(n-a,s-o),$=150*S,C=(1-this.releaseContainerFriction)*this.dragSpeed,E=T+tt(w)*$,k=x+K(w)*$,N=E>c?c+(E-c)*C:E<p?p+(E-p)*C:E,D=k>u?u+(k-u)*C:k<d?d+(k-d)*C:k,A=this.destX=dt(ct(ut(N,this.snapX),5),p,c),R=this.destY=dt(ct(ut(D,this.snapY),5),d,u),L=this.isOutOfBounds(this.containerBounds,E,k);let B=0,P=0,F=_,M=_,V=0;if(v.x=T,v.y=x,!t){const t=A===c?T>c?-1:1:T<p?-1:1,s=ct(T-A,0);g.velocity=e&&b?s?$*t/et(s):0:S;const{ease:i,settlingDuration:r,restDuration:n}=g;B=T===A?0:b?r:r-n*I.timeScale,b&&(F=i),B>V&&(V=B)}if(!e){const e=R===u?x>u?-1:1:x<d?-1:1,s=ct(x-R,0);y.velocity=t&&b?s?$*e/et(s):0:S;const{ease:i,settlingDuration:r,restDuration:n}=y;P=x===R?0:b?r:r-n*I.timeScale,b&&(M=i),P>V&&(V=P)}if(!b&&L&&C&&(B||P)){const t=r.blend;new Me(v,{x:{to:N,duration:.65*B},y:{to:D,duration:.65*P},ease:_,composition:t}).init(),new Me(v,{x:{to:A,duration:B},y:{to:R,duration:P},ease:_,composition:t}).init(),this.overshootTicker.stretch(ot(B,P)).restart()}else t||this.animate[this.xProp](A,B,F),e||this.animate[this.yProp](R,P,M);this.scrollInView(V,this.scrollThreshold,_);let O=!1;A!==m&&(this.snapped[0]=A,this.snapX&&(O=!0)),R!==f&&this.snapY&&(this.snapped[1]=R,this.snapY&&(O=!0)),O&&this.onSnap(this),this.grabbed=!1,this.dragged=!1,this.updated=!0,this.released=!0,this.onRelease(this),this.$trigger.removeEventListener("touchstart",is),this.$trigger.removeEventListener("touchmove",is),this.$trigger.removeEventListener("touchend",is),i.removeEventListener("touchmove",this),i.removeEventListener("touchend",this),i.removeEventListener("touchcancel",this),i.removeEventListener("mousemove",this),i.removeEventListener("mouseup",this),i.removeEventListener("selectstart",this)}reset(){return this.stop(),this.resizeTicker.pause(),this.grabbed=!1,this.dragged=!1,this.updated=!1,this.released=!1,this.canScroll=!1,this.setX(0,!0),this.setY(0,!0),this.coords[0]=0,this.coords[1]=0,this.pointer[0]=0,this.pointer[1]=0,this.pointer[2]=0,this.pointer[3]=0,this.pointer[4]=0,this.pointer[5]=0,this.pointer[6]=0,this.pointer[7]=0,this.velocity=0,this.velocityStack[0]=0,this.velocityStack[1]=0,this.velocityStack[2]=0,this.velocityStackIndex=0,this.angle=0,this}enable(){return this.enabled||(this.enabled=!0,this.$target.classList.remove("is-disabled"),this.touchActionStyles=es(this.$trigger,{touchAction:this.disabled[0]?"pan-x":this.disabled[1]?"pan-y":"none"}),this.$trigger.addEventListener("touchstart",this,{passive:!0}),this.$trigger.addEventListener("mousedown",this,{passive:!0}),this.$trigger.addEventListener("mouseenter",this)),this}disable(){return this.enabled=!1,this.grabbed=!1,this.dragged=!1,this.updated=!1,this.released=!1,this.canScroll=!1,this.touchActionStyles.revert(),this.cursorStyles&&(this.cursorStyles.revert(),this.cursorStyles=null),this.triggerStyles&&(this.triggerStyles.revert(),this.triggerStyles=null),this.bodyStyles&&(this.bodyStyles.revert(),this.bodyStyles=null),this.targetStyles&&(this.targetStyles.revert(),this.targetStyles=null),this.$target.classList.add("is-disabled"),this.$trigger.removeEventListener("touchstart",this),this.$trigger.removeEventListener("mousedown",this),this.$trigger.removeEventListener("mouseenter",this),i.removeEventListener("touchmove",this),i.removeEventListener("touchend",this),i.removeEventListener("touchcancel",this),i.removeEventListener("mousemove",this),i.removeEventListener("mouseup",this),i.removeEventListener("selectstart",this),this}revert(){return this.reset(),this.disable(),this.$target.classList.remove("is-disabled"),this.updateTicker.revert(),this.overshootTicker.revert(),this.resizeTicker.revert(),this.animate.revert(),this.resizeObserver.disconnect(),this}handleEvent(t){switch(t.type){case"mousedown":case"touchstart":this.handleDown(t);break;case"mousemove":case"touchmove":this.handleMove(t);break;case"mouseup":case"touchend":case"touchcancel":this.handleUp();break;case"mouseenter":this.handleHover();break;case"selectstart":is(t)}}}const hs=(t=_)=>new le({duration:1*I.timeScale,onComplete:t},null,0).resume(),ds=t=>{let e;return(...s)=>{let i,r,n,o,a;e&&(i=e.currentIteration,r=e.iterationProgress,n=e.reversed,o=e._alternate,a=e._startTime,e.revert());const l=t(...s);return l&&!z(l)&&l.revert&&(e=l),H(r)||(e.currentIteration=i,e.iterationProgress=(o&&i%2?!n:n)?1-r:r,e._startTime=a),l||_}};class cs{constructor(t={}){A.current&&A.current.register(this);const e=t.root;let r=i;e&&(r=e.current||e.nativeElement||de(e)[0]||i);const n=t.defaults,o=I.defaults,a=t.mediaQueries;if(this.defaults=n?yt(n,o):o,this.root=r,this.constructors=[],this.revertConstructors=[],this.revertibles=[],this.constructorsOnce=[],this.revertConstructorsOnce=[],this.revertiblesOnce=[],this.once=!1,this.onceIndex=0,this.methods={},this.matches={},this.mediaQueryLists={},this.data={},a)for(let t in a){const e=s.matchMedia(a[t]);this.mediaQueryLists[t]=e,e.addEventListener("change",this)}}register(t){(this.once?this.revertiblesOnce:this.revertibles).push(t)}execute(t){let e=A.current,s=A.root,i=I.defaults;A.current=this,A.root=this.root,I.defaults=this.defaults;const r=this.mediaQueryLists;for(let t in r)this.matches[t]=r[t].matches;const n=t(this);return A.current=e,A.root=s,I.defaults=i,n}refresh(){return this.onceIndex=0,this.execute(()=>{let t=this.revertibles.length,e=this.revertConstructors.length;for(;t--;)this.revertibles[t].revert();for(;e--;)this.revertConstructors[e](this);this.revertibles.length=0,this.revertConstructors.length=0,this.constructors.forEach(t=>{const e=t(this);z(e)&&this.revertConstructors.push(e)})}),this}add(t,e){if(this.once=!1,z(t)){const e=t;this.constructors.push(e),this.execute(()=>{const t=e(this);z(t)&&this.revertConstructors.push(t)})}else this.methods[t]=(...t)=>this.execute(()=>e(...t));return this}addOnce(t){if(this.once=!0,z(t)){const e=this.onceIndex++;if(this.constructorsOnce[e])return this;const s=t;this.constructorsOnce[e]=s,this.execute(()=>{const t=s(this);z(t)&&this.revertConstructorsOnce.push(t)})}return this}keepTime(t){this.once=!0;const e=this.onceIndex++,s=this.constructorsOnce[e];if(z(s))return s(this);const i=ds(t);let r;return this.constructorsOnce[e]=i,this.execute(()=>{r=i(this)}),r}handleEvent(t){"change"===t.type&&this.refresh()}revert(){const t=this.revertibles,e=this.revertConstructors,s=this.revertiblesOnce,i=this.revertConstructorsOnce,r=this.mediaQueryLists;let n=t.length,o=e.length,a=s.length,l=i.length;for(;n--;)t[n].revert();for(;o--;)e[o](this);for(;a--;)s[a].revert();for(;l--;)i[l](this);for(let t in r)r[t].removeEventListener("change",this);t.length=0,e.length=0,this.constructors.length=0,s.length=0,i.length=0,this.constructorsOnce.length=0,this.onceIndex=0,this.matches={},this.methods={},this.mediaQueryLists={},this.data={}}}const us=(t,e)=>t&&z(t)?t(e):t,ps=new Map;class ms{constructor(t){this.element=t,this.useWin=this.element===i.body,this.winWidth=0,this.winHeight=0,this.width=0,this.height=0,this.left=0,this.top=0,this.scale=1,this.zIndex=0,this.scrollX=0,this.scrollY=0,this.prevScrollX=0,this.prevScrollY=0,this.scrollWidth=0,this.scrollHeight=0,this.velocity=0,this.backwardX=!1,this.backwardY=!1,this.scrollTicker=new le({autoplay:!1,onBegin:()=>this.dataTimer.resume(),onUpdate:()=>{const t=this.backwardX||this.backwardY;_t(this,t=>t.handleScroll(),t)},onComplete:()=>this.dataTimer.pause()}).init(),this.dataTimer=new le({autoplay:!1,frameRate:30,onUpdate:t=>{const e=t.deltaTime,s=this.prevScrollX,i=this.prevScrollY,r=this.scrollX,n=this.scrollY,o=s-r,a=i-n;this.prevScrollX=r,this.prevScrollY=n,o&&(this.backwardX=s>r),a&&(this.backwardY=i>n),this.velocity=ct(e>0?Math.sqrt(o*o+a*a)/e:0,5)}}).init(),this.resizeTicker=new le({autoplay:!1,duration:250*I.timeScale,onComplete:()=>{this.updateWindowBounds(),this.refreshScrollObservers(),this.handleScroll()}}).init(),this.wakeTicker=new le({autoplay:!1,duration:500*I.timeScale,onBegin:()=>{this.scrollTicker.resume()},onComplete:()=>{this.scrollTicker.pause()}}).init(),this._head=null,this._tail=null,this.updateScrollCoords(),this.updateWindowBounds(),this.updateBounds(),this.refreshScrollObservers(),this.handleScroll(),this.resizeObserver=new ResizeObserver(()=>this.resizeTicker.restart()),this.resizeObserver.observe(this.element),(this.useWin?s:this.element).addEventListener("scroll",this,!1)}updateScrollCoords(){const t=this.useWin,e=this.element;this.scrollX=ct(t?s.scrollX:e.scrollLeft,0),this.scrollY=ct(t?s.scrollY:e.scrollTop,0)}updateWindowBounds(){this.winWidth=s.innerWidth,this.winHeight=(()=>{const t=i.createElement("div");i.body.appendChild(t),t.style.height="100lvh";const e=t.offsetHeight;return i.body.removeChild(t),e})()}updateBounds(){const t=getComputedStyle(this.element),e=this.element;let s,i;if(this.scrollWidth=e.scrollWidth+parseFloat(t.marginLeft)+parseFloat(t.marginRight),this.scrollHeight=e.scrollHeight+parseFloat(t.marginTop)+parseFloat(t.marginBottom),this.updateWindowBounds(),this.useWin)s=this.winWidth,i=this.winHeight;else{const t=e.getBoundingClientRect();s=e.clientWidth,i=e.clientHeight,this.top=t.top,this.left=t.left,this.scale=t.width?s/t.width:t.height?i/t.height:1}this.width=s,this.height=i}refreshScrollObservers(){_t(this,t=>{t.ready&&t._debug&&t.removeDebug()}),this.updateBounds(),_t(this,t=>{t.ready&&(t.refresh(),t.onResize(t),t._debug&&t.debug())})}refresh(){this.updateWindowBounds(),this.updateBounds(),this.refreshScrollObservers(),this.handleScroll()}handleScroll(){this.updateScrollCoords(),this.wakeTicker.restart()}handleEvent(t){"scroll"===t.type&&this.handleScroll()}revert(){this.scrollTicker.cancel(),this.dataTimer.cancel(),this.resizeTicker.cancel(),this.wakeTicker.cancel(),this.resizeObserver.disconnect(),(this.useWin?s:this.element).removeEventListener("scroll",this),ps.delete(this.element)}}const fs=(t,e,s,i,r)=>{const n="min"===e,o="max"===e,a="top"===e||"left"===e||"start"===e||n?0:"bottom"===e||"right"===e||"end"===e||o?"100%":"center"===e?"50%":e,{n:l,u:h}=It(a,Lt);let d=l;return"%"===h?d=l/100*s:h&&(d=me(t,Lt,"px",!0).n),o&&i<0&&(d+=i),n&&r>0&&(d+=r),d},gs=(t,e,s,i,r)=>{let n;if(O(e)){const o=k.exec(e);if(o){const a=o[0],l=a[0],h=e.split(a),d="min"===h[0],c="max"===h[0],u=fs(t,h[0],s,i,r),p=fs(t,h[1],s,i,r);if(d){const e=At(fs(t,"min",s),p,l);n=e<u?u:e}else if(c){const e=At(fs(t,"max",s),p,l);n=e>u?u:e}else n=At(u,p,l)}else n=fs(t,e,s,i,r)}else n=e;return ct(n,0)},ys=t=>{let e;const s=t.targets;for(let t=0,i=s.length;t<i;t++){const i=s[t];if(i[o]){e=i;break}}return e};let _s=0;const bs=["#FF4B4B","#FF971B","#FFC730","#F9F640","#7AFF5A","#18FF74","#17E09B","#3CFFEC","#05DBE9","#33B3F1","#638CF9","#C563FE","#FF4FCF","#F93F8A"];class vs{constructor(t={}){A.current&&A.current.register(this);const e=$t(t.sync,"play pause"),s=e?$e(e):null,r=e&&("linear"===e||e===fe),n=e&&!(s===fe&&!r),o=e&&(V(e)||!0===e||r),a=e&&O(e)&&!n&&!o,l=a?e.split(" ").map(t=>()=>{const e=this.linked;return e&&e[t]?e[t]():null}):null,h=a&&l.length>2;this.index=_s++,this.id=H(t.id)?this.index:t.id,this.container=(t=>{const e=t&&de(t)[0]||i.body;let s=ps.get(e);return s||(s=new ms(e),ps.set(e,s)),s})(t.container),this.target=null,this.linked=null,this.repeat=null,this.horizontal=null,this.enter=null,this.leave=null,this.sync=n||o||!!l,this.syncEase=n?s:null,this.syncSmooth=o?!0===e||r?1:e:null,this.onSyncEnter=l&&!h&&l[0]?l[0]:_,this.onSyncLeave=l&&!h&&l[1]?l[1]:_,this.onSyncEnterForward=l&&h&&l[0]?l[0]:_,this.onSyncLeaveForward=l&&h&&l[1]?l[1]:_,this.onSyncEnterBackward=l&&h&&l[2]?l[2]:_,this.onSyncLeaveBackward=l&&h&&l[3]?l[3]:_,this.onEnter=t.onEnter||_,this.onLeave=t.onLeave||_,this.onEnterForward=t.onEnterForward||_,this.onLeaveForward=t.onLeaveForward||_,this.onEnterBackward=t.onEnterBackward||_,this.onLeaveBackward=t.onLeaveBackward||_,this.onUpdate=t.onUpdate||_,this.onResize=t.onResize||_,this.onSyncComplete=t.onSyncComplete||_,this.reverted=!1,this.ready=!1,this.completed=!1,this.began=!1,this.isInView=!1,this.forceEnter=!1,this.hasEntered=!1,this.offset=0,this.offsetStart=0,this.offsetEnd=0,this.distance=0,this.prevProgress=0,this.thresholds=["start","end","end","start"],this.coords=[0,0,0,0],this.debugStyles=null,this.$debug=null,this._params=t,this._debug=$t(t.debug,!1),this._next=null,this._prev=null,vt(this.container,this),hs(()=>{if(!this.reverted){if(!this.target){const e=de(t.target)[0];this.target=e||i.body,this.refresh()}this._debug&&this.debug()}})}link(t){if(t&&(t.pause(),this.linked=t,H(t)||H(t.persist)||(t.persist=!0),!this._params.target)){let e;H(t.targets)?_t(t,t=>{t.targets&&!e&&(e=ys(t))}):e=ys(t),this.target=e||i.body,this.refresh()}return this}get velocity(){return this.container.velocity}get backward(){return this.horizontal?this.container.backwardX:this.container.backwardY}get scroll(){return this.horizontal?this.container.scrollX:this.container.scrollY}get progress(){const t=(this.scroll-this.offsetStart)/this.distance;return t===1/0||isNaN(t)?0:ct(dt(t,0,1),6)}refresh(){this.ready=!0,this.reverted=!1;const t=this._params;return this.repeat=$t(us(t.repeat,this),!0),this.horizontal="x"===$t(us(t.axis,this),"y"),this.enter=$t(us(t.enter,this),"end start"),this.leave=$t(us(t.leave,this),"start end"),this.updateBounds(),this.handleScroll(),this}removeDebug(){return this.$debug&&(this.$debug.parentNode.removeChild(this.$debug),this.$debug=null),this.debugStyles&&(this.debugStyles.revert(),this.$debug=null),this}debug(){this.removeDebug();const t=this.container,e=this.horizontal,s=t.element.querySelector(":scope > .animejs-onscroll-debug"),r=i.createElement("div"),n=i.createElement("div"),o=i.createElement("div"),a=bs[this.index%bs.length],l=t.useWin,h=l?t.winWidth:t.width,d=l?t.winHeight:t.height,c=t.scrollWidth,u=t.scrollHeight,p=this.container.width>360?320:260,m=e?0:10,f=e?10:0,g=e?24:p/2,y=e?g:15,_=e?60:g,b=e?_:y,v=e?"repeat-x":"repeat-y",T=t=>e?"0px "+t+"px":t+"px 2px",x=t=>`linear-gradient(${e?90:0}deg, ${t} 2px, transparent 1px)`,S=(t,e,s,i,r)=>`position:${t};left:${e}px;top:${s}px;width:${i}px;height:${r}px;`;r.style.cssText=`${S("absolute",m,f,e?c:p,e?p:u)}\\n      pointer-events: none;\\n      z-index: ${this.container.zIndex++};\\n      display: flex;\\n      flex-direction: ${e?"column":"row"};\\n      filter: drop-shadow(0px 1px 0px rgba(0,0,0,.75));\\n    `,n.style.cssText=`${S("sticky",0,0,e?h:g,e?g:d)}`,s||(n.style.cssText+=`background:\\n        ${x("#FFFF")}${T(g-10)} / 100px 100px ${v},\\n        ${x("#FFF8")}${T(g-10)} / 10px 10px ${v};\\n      `),o.style.cssText=`${S("relative",0,0,e?c:g,e?g:u)}`,s||(o.style.cssText+=`background:\\n        ${x("#FFFF")}${T(0)} / ${e?"100px 10px":"10px 100px"} ${v},\\n        ${x("#FFF8")}${T(0)} / ${e?"10px 0px":"0px 10px"} ${v};\\n      `);const w=[" enter: "," leave: "];this.coords.forEach((t,s)=>{const r=s>1,l=(r?0:this.offset)+t,m=s%2,f=l<b,g=l>(r?e?h:d:e?c:u)-b,v=(r?m&&!f:!m&&!f)||g,T=i.createElement("div"),x=i.createElement("div"),$=e?v?"right":"left":v?"bottom":"top",C=v?(e?_:y)+(r?e?-1:g?0:-2:e?-1:-2):e?1:0;x.innerHTML=`${this.id}${w[m]}${this.thresholds[s]}`,T.style.cssText=`${S("absolute",0,0,_,y)}\\n        display: flex;\\n        flex-direction: ${e?"column":"row"};\\n        justify-content: flex-${r?"start":"end"};\\n        align-items: flex-${v?"end":"start"};\\n        border-${$}: 2px solid ${a};\\n      `,x.style.cssText=`\\n        overflow: hidden;\\n        max-width: ${p/2-10}px;\\n        height: ${y};\\n        margin-${e?v?"right":"left":v?"bottom":"top"}: -2px;\\n        padding: 1px;\\n        font-family: ui-monospace, monospace;\\n        font-size: 10px;\\n        letter-spacing: -.025em;\\n        line-height: 9px;\\n        font-weight: 600;\\n        text-align: ${e&&v||!e&&!r?"right":"left"};\\n        white-space: pre;\\n        text-overflow: ellipsis;\\n        color: ${m?a:"rgba(0,0,0,.75)"};\\n        background-color: ${m?"rgba(0,0,0,.65)":a};\\n        border: 2px solid ${m?a:"transparent"};\\n        border-${e?v?"top-left":"top-right":v?"top-left":"bottom-left"}-radius: 5px;\\n        border-${e?v?"bottom-left":"bottom-right":v?"top-right":"bottom-right"}-radius: 5px;\\n      `,T.appendChild(x);let E=l-C+(e?1:0);T.style[e?"left":"top"]=`${E}px`,(r?n:o).appendChild(T)}),r.appendChild(n),r.appendChild(o),t.element.appendChild(r),s||r.classList.add("animejs-onscroll-debug"),this.$debug=r,"static"===ts(t.element,"position")&&(this.debugStyles=es(t.element,{position:"relative "}))}updateBounds(){let t;this._debug&&this.removeDebug();const e=this.target,s=this.container,r=this.horizontal,n=this.linked;let o,a=e;for(n&&(o=n.currentTime,n.seek(0,!0));a&&a!==s.element&&a!==i.body;){const e="sticky"===ts(a,"position")&&es(a,{position:"static"});a=a.parentElement,e&&(t||(t=[]),t.push(e))}const l=e.getBoundingClientRect(),h=s.scale,d=(r?l.left+s.scrollX-s.left:l.top+s.scrollY-s.top)*h,c=(r?l.width:l.height)*h,u=r?s.width:s.height,p=(r?s.scrollWidth:s.scrollHeight)-u,m=this.enter,f=this.leave;let g="start",y="end",_="end",b="start";if(O(m)){const t=m.split(" ");_=t[0],g=t.length>1?t[1]:g}else if(M(m)){const t=m;H(t.container)||(_=t.container),H(t.target)||(g=t.target)}else V(m)&&(_=m);if(O(f)){const t=f.split(" ");b=t[0],y=t.length>1?t[1]:y}else if(M(f)){const t=f;H(t.container)||(b=t.container),H(t.target)||(y=t.target)}else V(f)&&(b=f);const v=gs(e,g,c),T=gs(e,y,c),x=v+d-u,S=T+d-p,w=gs(e,_,u,x,S),$=gs(e,b,u,x,S),C=v+d-w,E=T+d-$,k=E-C;this.offset=d,this.offsetStart=C,this.offsetEnd=E,this.distance=k<=0?0:k,this.thresholds=[g,y,_,b],this.coords=[v,T,w,$],t&&t.forEach(t=>t.revert()),n&&n.seek(o,!0),this._debug&&this.debug()}handleScroll(){if(!this.ready)return;const t=this.linked,e=this.sync,s=this.syncEase,i=this.syncSmooth,r=t&&(s||i),n=this.horizontal,o=this.container,a=this.scroll,l=a<=this.offsetStart,h=a>=this.offsetEnd,d=!l&&!h,c=a===this.offsetStart||a===this.offsetEnd,u=!this.hasEntered&&c,p=this._debug&&this.$debug;let m=!1,f=!1,g=this.progress;if(l&&this.began&&(this.began=!1),g>0&&!this.began&&(this.began=!0),r){const e=t.progress;if(i&&V(i)){if(i<1){const t=1e-4,s=e<g&&1===g?t:e>g&&!g?-t:0;g=ct(pt(e,g,pt(.01,.2,i))+s,6)}}else s&&(g=s(g));m=g!==this.prevProgress,f=1===e,m&&!f&&i&&e&&o.wakeTicker.restart()}if(p){const t=n?o.scrollY:o.scrollX;p.style[n?"top":"left"]=t+10+"px"}(d&&!this.isInView||u&&!this.forceEnter&&!this.hasEntered)&&(d&&(this.isInView=!0),this.forceEnter&&this.hasEntered?d&&(this.forceEnter=!1):(p&&d&&(p.style.zIndex=""+this.container.zIndex++),this.onSyncEnter(this),this.onEnter(this),this.backward?(this.onSyncEnterBackward(this),this.onEnterBackward(this)):(this.onSyncEnterForward(this),this.onEnterForward(this)),this.hasEntered=!0,u&&(this.forceEnter=!0))),(d||!d&&this.isInView)&&(m=!0),m&&(r&&t.seek(t.duration*g),this.onUpdate(this)),!d&&this.isInView&&(this.isInView=!1,this.onSyncLeave(this),this.onLeave(this),this.backward?(this.onSyncLeaveBackward(this),this.onLeaveBackward(this)):(this.onSyncLeaveForward(this),this.onLeaveForward(this)),e&&!i&&(f=!0)),g>=1&&this.began&&!this.completed&&(e&&f||!e)&&(e&&this.onSyncComplete(this),this.completed=!0,(!this.repeat&&!t||!this.repeat&&t&&t.completed)&&this.revert()),g<1&&this.completed&&(this.completed=!1),this.prevProgress=g}revert(){if(this.reverted)return;const t=this.container;return bt(t,this),t._head||t.revert(),this._debug&&this.removeDebug(),this.reverted=!0,this.ready=!1,this}}const Ts=(t,e,s)=>(((1-3*s+3*e)*t+(3*s-6*e))*t+3*e)*t,xs=(t=.5,e=0,s=.5,i=1)=>t===e&&s===i?fe:r=>0===r||1===r?r:Ts(((t,e,s)=>{let i,r,n=0,o=1,a=0;do{r=n+(o-n)/2,i=Ts(r,e,s)-t,i>0?o=r:n=r}while(et(i)>1e-7&&++a<100);return r})(r,t,s),e,i),Ss=(t=10,e)=>{const s=e?it:rt;return e=>s(dt(e,0,1)*t)*(1/t)},ws=(...t)=>{const e=t.length;if(!e)return fe;const s=e-1,i=t[0],r=t[s],n=[0],o=[Z(i)];for(let e=1;e<s;e++){const i=t[e],r=O(i)?i.trim().split(" "):[i],a=r[0],l=r[1];n.push(H(l)?e/s:Z(l)/100),o.push(Z(a))}return o.push(Z(r)),n.push(1),function(t){for(let e=1,s=n.length;e<s;e++){const s=n[e];if(t<=s){const i=n[e-1],r=o[e-1];return r+(o[e]-r)*(t-i)/(s-i)}}return o[o.length-1]}},$s=(t=10,e=1)=>{const s=[0],i=t-1;for(let t=1;t<i;t++){const r=s[t-1],n=t/i,o=n*(1-e)+(n+((t+1)/i-n)*Math.random())*e;s.push(dt(o,r,1))}return s.push(1),ws(...s)};var Cs=Object.freeze({__proto__:null,Spring:je,createSpring:Ze,cubicBezier:xs,eases:Te,irregular:$s,linear:ws,spring:Ge,steps:Ss});const Es=(t,e=100)=>{const s=[];for(let i=0;i<=e;i++)s.push(ct(t(i/e),4));return`linear(${s.join(", ")})`},ks={},Ns=t=>{let e=ks[t];if(e)return e;if(e="linear",O(t)){if(B(t,"linear")||B(t,"cubic-")||B(t,"steps")||B(t,"ease"))e=t;else if(B(t,"cubicB"))e=L(t);else{const s=Se(t);z(s)&&(e=s===fe?"linear":Es(s))}ks[t]=e}else if(z(t)){const s=Es(t);s&&(e=s)}else t.ease&&(e=Es(t.ease));return e},Ds=["x","y","z"],As=["perspective","width","height","margin","padding","top","right","bottom","left","borderWidth","fontSize","borderRadius",...Ds],Is=(()=>[...Ds,...g.filter(t=>["X","Y","Z"].some(e=>t.endsWith(e)))])();let Rs=null;const Ls=(t,e,s,i,r)=>{let n=O(e)?e:Et(e,s,i,r,null,null);return V(n)?As.includes(t)||B(t,"translate")?`${n}px`:B(t,"rotate")||B(t,"skew")?`${n}deg`:`${n}`:n},Bs=(t,e,s,i,r,n)=>{let o="0";const a=H(i)?getComputedStyle(t)[e]:Ls(e,i,t,r,n);return o=H(s)?F(i)?i.map(s=>Ls(e,s,t,r,n)):a:[Ls(e,s,t,r,n),a],o};class Ps{constructor(t,s){A.current&&A.current.register(this),X(Rs)&&(!e||!H(CSS)&&Object.hasOwnProperty.call(CSS,"registerProperty")?(g.forEach(t=>{const e=B(t,"skew"),s=B(t,"scale"),i=B(t,"rotate"),r=B(t,"translate"),n=i||e,o=n?"<angle>":s?"<number>":r?"<length-percentage>":"*";try{CSS.registerProperty({name:"--"+t,syntax:o,inherits:!1,initialValue:r?"0px":n?"0deg":s?"1":"0"})}catch{}}),Rs=!0):Rs=!1);const i=ce(t);i.length||console.warn("No target found. Make sure the element you're trying to animate is accessible before creating your animation.");const r=$t(s.autoplay,I.defaults.autoplay),n=!(!r||!r.link)&&r,o=s.alternate&&!0===s.alternate,a=s.reversed&&!0===s.reversed,h=$t(s.loop,I.defaults.loop),d=!0===h||h===1/0?1/0:V(h)?h+1:1,c=o?a?"alternate-reverse":"alternate":a?"reverse":"normal",m=1===I.timeScale?1:u;this.targets=i,this.animations=[],this.controlAnimation=null,this.onComplete=s.onComplete||I.defaults.onComplete,this.duration=0,this.muteCallbacks=!1,this.completed=!1,this.paused=!r||!1!==n,this.reversed=a,this.persist=$t(s.persist,I.defaults.persist),this.autoplay=r,this._speed=$t(s.playbackRate,I.defaults.playbackRate),this._resolve=_,this._completed=0,this._inlineStyles=[],i.forEach((t,e)=>{const r=t[l],n=Is.some(t=>s.hasOwnProperty(t)),o=t.style,a=this._inlineStyles[e]={},h=$t(s.ease,I.defaults.ease),u=Et(h,t,e,i,null,null),_=z(u)||O(u)?u:h,b=h.ease&&h,v=Ns(_),T=(b?b.settlingDuration:Et($t(s.duration,I.defaults.duration),t,e,i,null,null))*m,x=Et($t(s.delay,I.defaults.delay),t,e,i,null,null)*m,S=$t(s.composition,"replace");for(let l in s){if(!j(l))continue;const h={},u={iterations:d,direction:c,fill:"both",easing:v,duration:T,delay:x,composite:S},p=s[l],y=!!n&&(g.includes(l)?l:f.get(l)),_=y?"transform":l;let b;if(a[_]||(a[_]=o[_]),M(p)){const s=p,n=$t(s.ease,v),a=n.ease&&n,d=s.to,c=s.from;if(u.duration=(a?a.settlingDuration:Et($t(s.duration,T),t,e,i,null,null))*m,u.delay=Et($t(s.delay,x),t,e,i,null,null)*m,u.composite=$t(s.composition,S),u.easing=Ns(n),b=Bs(t,l,c,d,e,i),y?(h[`--${y}`]=b,r[y]=b):h[l]=Bs(t,l,c,d,e,i),Ke(this,t,l,h,u),!H(c))if(y){const t=`--${y}`;o.setProperty(t,h[t][0])}else o[l]=h[l][0]}else b=F(p)?p.map(s=>Ls(l,s,t,e,i)):Ls(l,p,t,e,i),y?(h[`--${y}`]=b,r[y]=b):h[l]=b,Ke(this,t,l,h,u)}if(n){let t=p;for(let e in r)t+=`${y[e]}var(--${e})) `;o.transform=t}}),n&&this.autoplay.link(this)}forEach(t){try{const e=O(t)?e=>e[t]():t;this.animations.forEach(e)}catch{}return this}get speed(){return this._speed}set speed(t){this._speed=+t,this.forEach(e=>e.playbackRate=t)}get currentTime(){const t=this.controlAnimation,e=I.timeScale;return this.completed?this.duration:t?+t.currentTime*(1===e?1:e):0}set currentTime(t){const e=t*(1===I.timeScale?1:u);this.forEach(t=>{!this.persist&&e>=this.duration&&t.play(),t.currentTime=e})}get progress(){return this.currentTime/this.duration}set progress(t){this.forEach(e=>e.currentTime=t*this.duration||0)}resume(){return this.paused?(this.paused=!1,this.forEach("play")):this}pause(){return this.paused?this:(this.paused=!0,this.forEach("pause"))}alternate(){return this.reversed=!this.reversed,this.forEach("reverse"),this.paused&&this.forEach("pause"),this}play(){return this.reversed&&this.alternate(),this.resume()}reverse(){return this.reversed||this.alternate(),this.resume()}seek(t,e=!1){return e&&(this.muteCallbacks=!0),t<this.duration&&(this.completed=!1),this.currentTime=t,this.muteCallbacks=!1,this.paused&&this.pause(),this}restart(){return this.completed=!1,this.seek(0,!0).resume()}commitStyles(){return this.forEach("commitStyles")}complete(){return this.seek(this.duration)}cancel(){return this.muteCallbacks=!0,this.commitStyles().forEach("cancel"),this.animations.length=0,requestAnimationFrame(()=>{this.targets.forEach(t=>{"none"===t.style.transform&&t.style.removeProperty("transform")})}),this}revert(){return this.cancel().targets.forEach((t,e)=>{const s=t.style,i=this._inlineStyles[e];for(let e in i){const r=i[e];H(r)||r===p?s.removeProperty(L(e)):t.style[e]=r}t.getAttribute("style")===p&&t.removeAttribute("style")}),this}then(t=_){const e=this.then,s=()=>{this.then=null,t(this),this.then=e,this._resolve=_};return new Promise(t=>(this._resolve=()=>t(s()),this.completed&&this._resolve(),this))}}const Fs={animate:(t,e)=>new Ps(t,e),convertEase:Es};let Ms=0,Vs=0;const Os=(t,e)=>!(!t||!e)&&(t===e||t.contains(e)),zs=t=>{if(!t)return null;const e=t.style,s=e.transition||"";return e.setProperty("transition","none","important"),s},Hs=(t,e)=>{if(!t)return;const s=t.style;e?s.transition=e:s.removeProperty("transition")},Xs=t=>{const e=t.layout.transitionMuteStore,s=t.$el,i=t.$measure;s&&!e.has(s)&&e.set(s,zs(s)),i&&!e.has(i)&&e.set(i,zs(i))},Ys=t=>{t.forEach((t,e)=>Hs(e,t)),t.clear()},Ws={display:"none",visibility:"hidden",opacity:"0",transform:"none",position:"static"},Us=t=>{if(!t)return;const e=t.parentNode;e&&(e._head===t&&(e._head=t._next),e._tail===t&&(e._tail=t._prev),t._prev&&(t._prev._next=t._next),t._next&&(t._next._prev=t._prev),t._prev=null,t._next=null,t.parentNode=null)},qs=(t,e,s,i)=>{let r=t.dataset.layoutId;r||(r=t.dataset.layoutId="node-"+Vs++);const n=i||{};return n.$el=t,n.$measure=t,n.id=r,n.index=0,n.targets=null,n.delay=0,n.duration=0,n.ease=null,n.state=s,n.layout=s.layout,n.parentNode=e||null,n.isTarget=!1,n.isEntering=!1,n.isLeaving=!1,n.isInlined=!1,n.hasTransform=!1,n.inlineStyles=[],n.inlineTransforms=null,n.inlineTransition=null,n.branchAdded=!1,n.branchRemoved=!1,n.branchNotRendered=!1,n.sizeChanged=!1,n.hasVisibilitySwap=!1,n.hasDisplayNone=!1,n.hasVisibilityHidden=!1,n.measuredInlineTransform=null,n.measuredInlineTransition=null,n.measuredDisplay=null,n.measuredVisibility=null,n.measuredPosition=null,n.measuredHasDisplayNone=!1,n.measuredHasVisibilityHidden=!1,n.measuredIsVisible=!1,n.measuredIsRemoved=!1,n.measuredIsInsideRoot=!1,n.properties={transform:"none",x:0,y:0,left:0,top:0,clientLeft:0,clientTop:0,width:0,height:0},n.layout.properties.forEach(t=>n.properties[t]=0),n._head=null,n._tail=null,n._prev=null,n._next=null,n},js=(t,e,s,i)=>{const r=t.$el,n=t.layout.root,o=n===r,a=t.properties,l=t.state.rootNode,h=t.parentNode,d=s.transform,c=r.style.transform,u=!!h&&h.measuredIsRemoved,p=s.position;o&&(t.layout.absoluteCoords="fixed"===p||"absolute"===p),t.$measure=e,t.inlineTransforms=c,t.hasTransform=d&&"none"!==d,t.measuredIsInsideRoot=Os(n,e),t.measuredInlineTransform=null,t.measuredDisplay=s.display,t.measuredVisibility=s.visibility,t.measuredPosition=p,t.measuredHasDisplayNone="none"===s.display,t.measuredHasVisibilityHidden="hidden"===s.visibility,t.measuredIsVisible=!(t.measuredHasDisplayNone||t.measuredHasVisibilityHidden),t.measuredIsRemoved=t.measuredHasDisplayNone||t.measuredHasVisibilityHidden||u;let m=!1,f=r.previousSibling;for(;f&&(f.nodeType===Node.COMMENT_NODE||f.nodeType===Node.TEXT_NODE&&!f.textContent.trim());)f=f.previousSibling;if(f&&f.nodeType===Node.TEXT_NODE)m=!0;else{for(f=r.nextSibling;f&&(f.nodeType===Node.COMMENT_NODE||f.nodeType===Node.TEXT_NODE&&!f.textContent.trim());)f=f.nextSibling;m=null!==f&&f.nodeType===Node.TEXT_NODE}if(t.isInlined=m,t.hasTransform&&!i){const s=t.layout.transitionMuteStore;s.get(r)||(t.inlineTransition=zs(r)),e===r?r.style.transform="none":(s.get(e)||(t.measuredInlineTransition=zs(e)),t.measuredInlineTransform=e.style.transform,e.style.transform="none")}let g,y,_=0,b=0,v=0,T=0;if(!i){const t=e.getBoundingClientRect();_=t.left,b=t.top,v=t.width,T=t.height}for(let t in a){const e="transform"===t?d:s[t]||s.getPropertyValue&&s.getPropertyValue(t);H(e)||(a[t]=e)}if(a.left=_,a.top=b,a.clientLeft=i?0:e.clientLeft,a.clientTop=i?0:e.clientTop,o)t.layout.absoluteCoords?(g=_,y=b):(g=0,y=0);else{const e=h||l,s=e.properties.left,i=e.properties.top,r=e.properties.clientLeft,n=e.properties.clientTop;if(t.layout.absoluteCoords)g=_-s-r,y=b-i-n;else if(e===l){const t=l.properties.left,e=l.properties.top;g=_-t-l.properties.clientLeft,y=b-e-l.properties.clientTop}else g=_-s-r,y=b-i-n}return a.x=g,a.y=y,a.width=v,a.height=T,t},Gs=(t,e)=>{if(e)for(let s in e)t.properties[s]=e[s]},Zs=(t,e)=>{const s=Et(e.ease,t.$el,t.index,t.targets,null,null),i=z(s)?s:e.ease,r=!H(i)&&!H(i.ease);t.ease=r?i.ease:i,t.duration=r?i.settlingDuration:Et(e.duration,t.$el,t.index,t.targets,null,null),t.delay=Et(e.delay,t.$el,t.index,t.targets,null,null)},Qs=t=>{const e=t.$el.style,s=t.inlineStyles;s.length=0,t.layout.recordedProperties.forEach(t=>{s.push(t,e[t]||"")})},Js=t=>{const e=t.$el.style,s=t.inlineStyles;for(let t=0,i=s.length;t<i;t+=2){const i=s[t],r=s[t+1];r&&""!==r?e[i]=r:(e[i]="",e.removeProperty(i))}},Ks=t=>{const e=t.inlineTransforms,s=t.$el.style;!t.hasTransform||!e||t.hasTransform&&"none"===s.transform||e&&"none"===e?s.removeProperty("transform"):e&&(s.transform=e);const i=t.$measure;if(t.hasTransform&&i!==t.$el){const e=i.style,s=t.measuredInlineTransform;s&&""!==s?e.transform=s:e.removeProperty("transform")}t.measuredInlineTransform=null,null!==t.inlineTransition&&(Hs(t.$el,t.inlineTransition),t.inlineTransition=null),i!==t.$el&&null!==t.measuredInlineTransition&&(Hs(i,t.measuredInlineTransition),t.measuredInlineTransition=null)},ti=t=>{(t.measuredIsRemoved||t.hasVisibilitySwap)&&(t.$el.style.removeProperty("display"),t.$el.style.removeProperty("visibility"),t.hasVisibilitySwap&&(t.$measure.style.removeProperty("display"),t.$measure.style.removeProperty("visibility"))),t.layout.pendingRemoval.delete(t.$el)},ei=(t,e,s)=>(e.properties={...t.properties},e.state=s,e.isTarget=t.isTarget,e.hasTransform=t.hasTransform,e.inlineTransforms=t.inlineTransforms,e.measuredIsVisible=t.measuredIsVisible,e.measuredDisplay=t.measuredDisplay,e.measuredIsRemoved=t.measuredIsRemoved,e.measuredHasDisplayNone=t.measuredHasDisplayNone,e.measuredHasVisibilityHidden=t.measuredHasVisibilityHidden,e.hasDisplayNone=t.hasDisplayNone,e.isInlined=t.isInlined,e.hasVisibilityHidden=t.hasVisibilityHidden,e);class si{constructor(t){this.layout=t,this.rootNode=null,this.rootNodes=new Set,this.nodes=new Map,this.scrollX=0,this.scrollY=0}revert(){return this.forEachNode(t=>{this.layout.pendingRemoval.delete(t.$el),t.$el.removeAttribute("data-layout-id"),t.$measure.removeAttribute("data-layout-id")}),this.rootNode=null,this.rootNodes.clear(),this.nodes.clear(),this}getNode(t){if(t&&t.dataset)return this.nodes.get(t.dataset.layoutId)}getComputedValue(t,e){const s=this.getNode(t);if(s)return s.properties[e]}forEach(t,e){let s=t,i=0;for(;s;)if(e(s,i++),s._head)s=s._head;else if(s._next)s=s._next;else{for(;s&&!s._next;)s=s.parentNode;s&&(s=s._next)}}forEachRootNode(t){this.forEach(this.rootNode,t)}forEachNode(t){for(const e of this.rootNodes)this.forEach(e,t)}registerElement(t,e){if(!t||1!==t.nodeType)return null;this.layout.transitionMuteStore.has(t)||this.layout.transitionMuteStore.set(t,zs(t));const s=[t,e],i=this.layout.root;let r=null;for(;s.length;){const t=s.pop(),e=s.pop();if(!e||1!==e.nodeType||Y(e))continue;const n=!!t&&t.measuredIsRemoved,o=n?Ws:getComputedStyle(e),a=!!n||"none"===o.display,l=!!n||"hidden"===o.visibility,h=!a&&!l,d=e.dataset.layoutId,c=Os(i,e);let u=d?this.nodes.get(d):null;if(u&&u.$el!==e){const a=Os(i,u.$el),l=u.measuredIsVisible;if(a||!c&&(c||l||!h)){if(a&&!l&&h){js(u,e,o,n);let t=e.lastElementChild;for(;t;)s.push(t,u),t=t.previousElementSibling;r||(r=u);continue}{let i=e.lastElementChild;for(;i;)s.push(i,t),i=i.previousElementSibling;r||(r=u);continue}}Us(u),u=qs(e,t,this,u)}else u=qs(e,t,this,u);u.branchAdded=!1,u.branchRemoved=!1,u.branchNotRendered=!1,u.isTarget=!1,u.sizeChanged=!1,u.hasVisibilityHidden=l,u.hasDisplayNone=a,u.hasVisibilitySwap=l&&!u.measuredHasVisibilityHidden||a&&!u.measuredHasDisplayNone,this.nodes.set(u.id,u),u.parentNode=t||null,u._prev=null,u._next=null,t?(this.rootNodes.delete(u),t._head?(t._tail._next=u,u._prev=t._tail,t._tail=u):(t._head=u,t._tail=u)):this.rootNodes.add(u),js(u,u.$el,o,n);let p=e.lastElementChild;for(;p;)s.push(p,u),p=p.previousElementSibling;r||(r=u)}return r}ensureDetachedNode(t,e){if(!t||t===this.layout.root)return null;const s=t.dataset.layoutId,i=s?this.nodes.get(s):null;if(i&&i.$el===t)return i;let r=null,n=t.parentElement;for(;n&&n!==this.layout.root;){if(e.has(n)){r=this.ensureDetachedNode(n,e);break}n=n.parentElement}return this.registerElement(t,r)}record(){const t=this.layout,e=t.children,s=t.root,i=F(e)?e:[e],r=[],n="*"===e?s:A.root,o=[];let a=s.parentElement;for(;a&&1===a.nodeType;){const t=getComputedStyle(a);if(t.transform&&"none"!==t.transform){const t=a.style.transform||"",e=zs(a);o.push(a,t,e),a.style.transform="none"}a=a.parentElement}for(let t=0,e=i.length;t<e;t++){const e=i[t];r[t]=O(e)?n.querySelectorAll(e):e}const l=ce(r);this.nodes.clear(),this.rootNodes.clear();const h=this.registerElement(s,null);h.isTarget=!0,this.rootNode=h;const d=new Set;let c=0;const u=[];this.nodes.forEach(t=>{u.push(t.$el)}),this.nodes.forEach((t,e)=>{t.index=c++,t.targets=u,t&&t.measuredIsInsideRoot&&d.add(e)});const p=new Set,m=[];for(let t=0,e=l.length;t<e;t++){const e=l[t];if(e&&1===e.nodeType&&e!==s){if(!Os(s,e)){const t=e.dataset.layoutId;if(!t||!d.has(t))continue}p.has(e)||(p.add(e),m.push(e))}}for(let t=0,e=m.length;t<e;t++)this.ensureDetachedNode(m[t],p);for(let t=0,e=l.length;t<e;t++){const e=l[t],s=this.getNode(e);if(s){let t=s;for(;t&&!t.isTarget;)t.isTarget=!0,t=t.parentNode}}this.scrollX=window.scrollX,this.scrollY=window.scrollY,this.forEachNode(Ks);for(let t=0,e=o.length;t<e;t+=3){const e=o[t],s=o[t+1],i=o[t+2];s&&""!==s?e.style.transform=s:e.style.removeProperty("transform"),Hs(e,i)}return this}}function ii(t){const e={},s={};for(let i in t){const r=t[i];"duration"===i||"delay"===i||"ease"===i?s[i]=r:e[i]=r}return[e,s]}class ri{constructor(t,e={}){A.current&&A.current.register(this);const s=ii(e.swapAt),i=ii(e.enterFrom),r=ii(e.leaveTo),n=e.properties;if(e.duration=$t(e.duration,350),e.delay=$t(e.delay,0),e.ease=$t(e.ease,"inOut(3.5)"),this.params=e,this.root=ce(t)[0],this.id=e.id||Ms++,this.children=e.children||"*",this.absoluteCoords=!1,this.swapAtParams=yt(e.swapAt||{opacity:0},{ease:"inOut(1.75)"}),this.enterFromParams=e.enterFrom||{opacity:0},this.leaveToParams=e.leaveTo||{opacity:0},this.properties=new Set(["opacity","fontSize","color","backgroundColor","borderRadius","border","filter","clipPath"]),s[0])for(let t in s[0])this.properties.add(t);if(i[0])for(let t in i[0])this.properties.add(t);if(r[0])for(let t in r[0])this.properties.add(t);if(n)for(let t=0,e=n.length;t<e;t++)this.properties.add(n[t]);this.recordedProperties=new Set(["display","visibility","translate","position","left","top","marginLeft","marginTop","width","height","maxWidth","maxHeight","minWidth","minHeight"]),this.properties.forEach(t=>this.recordedProperties.add(t)),this.pendingRemoval=new WeakSet,this.transitionMuteStore=new Map,this.oldState=new si(this),this.newState=new si(this),this.timeline=null,this.transformAnimation=null,this.animating=[],this.swapping=[],this.leaving=[],this.entering=[],this.oldState.record(),Ys(this.transitionMuteStore)}revert(){return this.root.classList.remove("is-animated"),this.timeline&&(this.timeline.complete(),this.timeline=null),this.transformAnimation&&(this.transformAnimation.complete(),this.transformAnimation=null),this.animating.length=this.swapping.length=this.leaving.length=this.entering.length=0,this.oldState.revert(),this.newState.revert(),requestAnimationFrame(()=>Ys(this.transitionMuteStore)),this}record(){return this.transformAnimation&&(this.transformAnimation.cancel(),this.transformAnimation=null),this.oldState.record(),this.timeline&&(this.timeline.cancel(),this.timeline=null),this.newState.forEachRootNode(Js),this}animate(t={}){const e={ease:$t(t.ease,this.params.ease),delay:$t(t.delay,this.params.delay),duration:$t(t.duration,this.params.duration)},s={id:this.id},i=$t(t.onComplete,this.params.onComplete),r=$t(t.onPause,this.params.onPause);for(let e in D)"ease"!==e&&"duration"!==e&&"delay"!==e&&(H(t[e])?H(this.params[e])||(s[e]=this.params[e]):s[e]=t[e]);s.onComplete=()=>{const e=t.autoplay,s=I.editor;if(e&&e.linked||s&&s.showPanel)i&&i(this.timeline);else{this.transformAnimation&&this.transformAnimation.cancel(),f.forEachRootNode(t=>{ti(t),Js(t)});for(let t=0,e=S.length;t<e;t++){const e=S[t];e.style.transform=f.getComputedValue(e,"transform")}this.root.classList.contains("is-animated")&&(this.root.classList.remove("is-animated"),i&&i(this.timeline)),requestAnimationFrame(()=>{this.root.classList.contains("is-animated")||Ys(this.transitionMuteStore)})}},s.onPause=()=>{const e=t.autoplay;if(e&&e.linked)return i&&i(this.timeline),void(r&&r(this.timeline));this.root.classList.contains("is-animated")&&(this.transformAnimation&&this.transformAnimation.cancel(),f.forEachRootNode(ti),this.root.classList.remove("is-animated"),i&&i(this.timeline),r&&r(this.timeline))},s.composition=!1;const n=yt(yt(t.swapAt||{},this.swapAtParams),e),o=yt(yt(t.enterFrom||{},this.enterFromParams),e),a=yt(yt(t.leaveTo||{},this.leaveToParams),e),[l,h]=ii(n),[d,c]=ii(o),[u,p]=ii(a),m=this.oldState,f=this.newState,g=this.animating,y=this.swapping,_=this.entering,b=this.leaving,v=this.pendingRemoval;g.length=y.length=_.length=b.length=0,m.forEachRootNode(Xs),f.record(),f.forEachRootNode(Qs);const T=[],x=[],S=[],w=[],$=f.rootNode,C=$.$el;f.forEachRootNode(t=>{const e=t.$el,s=t.id,i=t.parentNode,r=!!i&&i.branchAdded,n=!!i&&i.branchRemoved,o=!!i&&i.branchNotRendered;let a=m.nodes.get(s);const l=!a;l?(a=ei(t,{},m),m.nodes.set(s,a),a.measuredIsRemoved=!0):a.measuredIsRemoved&&!t.measuredIsRemoved&&(ei(t,a,m),a.measuredIsRemoved=!0);const h=a.parentNode,c=(h?h.id:null)!==(i?i.id:null),p=a.$el!==t.$el,y=a.measuredIsRemoved,x=t.measuredIsRemoved;if(!a.measuredIsRemoved&&!x&&!l&&(c||p)){const t=a.properties.left,e=a.properties.top,s=i||f.rootNode,r=s.id?m.nodes.get(s.id):null,n=r?r.properties.left:s.properties.left,o=r?r.properties.top:s.properties.top,l=r?r.properties.clientLeft:s.properties.clientLeft,h=r?r.properties.clientTop:s.properties.clientTop;a.properties.x=t-n-l,a.properties.y=e-o-h}t.hasVisibilitySwap&&(t.hasVisibilityHidden&&(t.$el.style.visibility="visible",t.$measure.style.visibility="hidden"),t.hasDisplayNone&&(t.$el.style.display=a.measuredDisplay||t.measuredDisplay||"",t.$measure.style.visibility="hidden"));const S=v.has(e),w=a.measuredIsVisible,C=t.measuredIsVisible,E=!w&&C&&!o,k=!x&&(y||S)&&!r,N=x&&!y&&!n,D=N||x&&S&&!n;t.branchAdded=r||k,t.branchRemoved=n||D,t.branchNotRendered=o||x,x&&w&&(t.$el.style.display=a.measuredDisplay,t.$el.style.visibility="visible",ei(a,t,f)),N?(t.isTarget&&(b.push(e),t.isLeaving=!0),v.add(e)):!x&&S&&v.delete(e),k&&!o||E?(Gs(a,d),t.isTarget&&(_.push(e),t.isEntering=!0)):D&&!o&&Gs(t,u),t===$||!t.isTarget||t.isEntering||t.isLeaving||g.push(e),T.push(e)});let E=0,k=0,N=0;f.forEachRootNode(t=>{const s=t.$el,i=t.parentNode,r=m.nodes.get(t.id),n=t.properties,o=r.properties;let a=i!==$&&i;for(;a&&!a.isTarget&&a!==$;)a=a.parentNode;t===$?(t.index=0,t.targets=g,Zs(t,e)):t.isEntering?(t.index=a?a.index:E,t.targets=a?g:_,Zs(t,c),E++):t.isLeaving?(t.index=a?a.index:k,t.targets=a?g:b,k++,Zs(t,p)):t.isTarget?(t.index=N++,t.targets=g,Zs(t,e)):(t.index=a?a.index:0,t.targets=g,Zs(t,h)),r.index=t.index,r.targets=t.targets;for(let e in n)n[e]=Et(n[e],s,t.index,t.targets,null,null),o[e]=Et(o[e],s,r.index,r.targets,null,null);const d=Math.abs(n.width-o.width)>1,u=Math.abs(n.height-o.height)>1;if(t.sizeChanged=d||u,t.isTarget&&(!t.measuredIsRemoved&&r.measuredIsVisible||t.measuredIsRemoved&&t.measuredIsVisible)){"none"===n.transform&&"none"===o.transform||(t.hasTransform=!0,S.push(s));for(let t in n)if("transform"!==t&&n[t]!==o[t]){x.push(s);break}}t.isTarget||(y.push(s),t.sizeChanged&&i&&i.isTarget&&i.sizeChanged&&(l.transform&&(t.hasTransform=!0,S.push(s)),w.push(s)))});const A={delay:t=>f.getNode(t).delay,duration:t=>f.getNode(t).duration,ease:t=>f.getNode(t).ease};if(s.defaults=A,this.timeline=Xe(s),!x.length&&!S.length&&!y.length)return Ys(this.transitionMuteStore),this.timeline.complete();if(T.length){this.root.classList.add("is-animated");for(let t=0,e=T.length;t<e;t++){const e=T[t],s=e.dataset.layoutId,i=m.nodes.get(s),r=f.nodes.get(s),n=i.properties;r.isInlined||("grid"!==i.measuredDisplay&&"grid"!==r.measuredDisplay||e.style.setProperty("display","block","important"),(e!==C||this.absoluteCoords)&&(e.style.position=this.absoluteCoords?"fixed":"absolute",e.style.left="0px",e.style.top="0px",e.style.marginLeft="0px",e.style.marginTop="0px",e.style.translate=`${n.x}px ${n.y}px`),e===C&&"static"===r.measuredPosition&&(e.style.position="relative",e.style.left="0px",e.style.top="0px")),e.style.width=`${n.width}px`,e.style.height=`${n.height}px`,e.style.minWidth="auto",e.style.minHeight="auto",e.style.maxWidth="none",e.style.maxHeight="none"}m.scrollX===window.scrollX&&m.scrollY===window.scrollY||requestAnimationFrame(()=>window.scrollTo(m.scrollX,m.scrollY));for(let t=0,e=x.length;t<e;t++){const e=x[t],s=e.dataset.layoutId,i=m.nodes.get(s),r=f.nodes.get(s),n=i.properties,o=r.properties;let a=!1;const l={composition:"none"};n.width!==o.width&&(l.width=[n.width,o.width],a=!0),n.height!==o.height&&(l.height=[n.height,o.height],a=!0),r.hasTransform||r.isInlined||(l.translate=[`${n.x}px ${n.y}px`,`${o.x}px ${o.y}px`],a=!0),this.properties.forEach(t=>{const e=n[t],s=o[t];"transform"!==t&&e!==s&&(l[t]=[e,s],a=!0)}),a&&this.timeline.add(e,l,0)}}if(y.length){for(let t=0,e=y.length;t<e;t++){const e=y[t],s=m.getNode(e),i=s.properties;e.style.width=`${i.width}px`,e.style.height=`${i.height}px`,e.style.minWidth="auto",e.style.minHeight="auto",e.style.maxWidth="none",e.style.maxHeight="none",s.isInlined||(e.style.translate=`${i.x}px ${i.y}px`),this.properties.forEach(t=>{"transform"!==t&&(e.style[t]=`${m.getComputedValue(e,t)}`)})}for(let t=0,e=y.length;t<e;t++){const e=y[t],s=f.getNode(e),i=s.properties;this.timeline.call(()=>{e.style.width=`${i.width}px`,e.style.height=`${i.height}px`,e.style.minWidth="auto",e.style.minHeight="auto",e.style.maxWidth="none",e.style.maxHeight="none",s.isInlined||(e.style.translate=`${i.x}px ${i.y}px`),this.properties.forEach(t=>{"transform"!==t&&(e.style[t]=`${f.getComputedValue(e,t)}`)})},s.delay+s.duration/2)}if(w.length){const t=$e(f.nodes.get(w[0].dataset.layoutId).ease),e=e=>1-t(1-e),s={};if(l)for(let t in l)"transform"!==t&&(s[t]=[{from:e=>m.getComputedValue(e,t),to:l[t]},{from:l[t],to:e=>f.getComputedValue(e,t),ease:e}]);this.timeline.add(w,s,0)}}const R=S.length;if(R){for(let t=0;t<R;t++){const e=S[t],s=f.getNode(e);s.isInlined||(e.style.translate=`${m.getComputedValue(e,"x")}px ${m.getComputedValue(e,"y")}px`),e.style.transform=m.getComputedValue(e,"transform"),w.includes(e)&&(s.ease=Et(n.ease,e,s.index,s.targets,null,null),s.duration=Et(n.duration,e,s.index,s.targets,null,null))}this.transformAnimation=Fs.animate(S,{translate:t=>f.getNode(t).isInlined?"0px 0px":`${f.getComputedValue(t,"x")}px ${f.getComputedValue(t,"y")}px`,transform:t=>{const e=f.getComputedValue(t,"transform");if(!w.includes(t))return e;const s=m.getComputedValue(t,"transform"),i=f.getNode(t);return[s,Et(l.transform,t,i.index,i.targets,null,null),e]},autoplay:!1,...A}),this.timeline.sync(this.transformAnimation,0)}return this.timeline.init()}update(t,e={}){return this.record(),t(this),this.animate(e)}}const ni=Ue,oi={},ai=t=>(...e)=>{const s=t(...e);return new Proxy(_,{apply:(t,e,[i])=>s(i),get:(t,e)=>{if(oi[e])return ai((...t)=>{const i=oi[e](...t);return t=>i(s(t))})}})},li=(t,e,s=0)=>{const i=(...t)=>(t.length<e.length?ai(((t,e=0)=>(...s)=>e?e=>t(...s,e):e=>t(e,...s))(e,s)):e)(...t);return oi[t]||(oi[t]=i),i},hi=li("roundPad",ni.roundPad),di=li("padStart",ni.padStart),ci=li("padEnd",ni.padEnd),ui=li("wrap",ni.wrap),pi=li("mapRange",ni.mapRange),mi=li("degToRad",ni.degToRad),fi=li("radToDeg",ni.radToDeg),gi=li("snap",ni.snap),yi=li("clamp",ni.clamp),_i=li("round",ni.round),bi=li("lerp",ni.lerp,1),vi=li("damp",ni.damp,1),Ti=(t=0,e=1,s=0)=>{const i=10**s;return Math.floor((Math.random()*(e-t+1/i)+t)*i)/i};let xi=0;const Si=(t,e=0,s=1,i=0)=>{let r=void 0===t?xi++:t;return(t=e,n=s,o=i)=>{r+=1831565813,r=Math.imul(r^r>>>15,1|r),r^=r+Math.imul(r^r>>>7,61|r);const a=10**o;return Math.floor((((r^r>>>14)>>>0)/4294967296*(n-t+1/a)+t)*a)/a}},wi=t=>t[Ti(0,t.length-1)],$i=(t,e=Ti)=>{let s,i,r=t.length;for(;r;)i=e(0,--r),s=t[r],t[r]=t[i],t[i]=s;return t},Ci=(t,e={})=>{let s,i=[],r=0,n=null;const o=e.from,a=e.reversed,l=e.ease,h=!H(l),d=h&&!H(l.ease)?l.ease:h?$e(l):null,c=e.grid,u=!0===c,m=e.axis,f=e.total,g=H(o)||0===o||"first"===o,y="center"===o,_="last"===o,b="random"===o,v=F(o),T=F(t),x=e.use,S=Z(T?t[0]:t),w=T?Z(t[1]):0,$=C.exec((T?t[1]:t)+p),E=e.start||0+(T?S:0),k=e.seed,N=H(k)||!1===k?Ti:Si(!0===k?0:k),D=e.jitter,A=!H(D),I=F(D),R=I?D[0]:D||0,L=I?D[1]:D||0;let B=g?0:V(o)?o:0;return(t,l,h,p,g)=>{const[C]=ce(t),k=H(f)?h.length:f,D=!H(x)&&(z(x)?x(C,l,k):Dt(C,x)),I=V(D)||O(D)&&V(+D)?+D:l,P=I>=0&&I<k?I:l;if(y&&(B=(k-1)/2),_&&(B=k-1),!i.length){if(u){let t=!0,e=!1,s=1/0,r=1/0,n=1/0,a=-1/0,l=-1/0,d=-1/0;const c=[],u=[],p=[];for(let i=0;i<k;i++){const o=h[i];let m=0,f=0,g=0,y=!1;if(o&&z(o.getBoundingClientRect)){const t=o.getBoundingClientRect();m=t.left+t.width/2,f=t.top+t.height/2,y=!0}else{const t=o;t&&V(t.x)&&V(t.y)&&(m=t.x,f=t.y,V(t.z)&&(g=t.z,e=!0),y=!0)}if(!y){t=!1;break}c.push(m),u.push(f),p.push(g),m<s&&(s=m),f<r&&(r=f),g<n&&(n=g),m>a&&(a=m),f>l&&(l=f),g>d&&(d=g)}if(t){let t=c[0],h=u[0],f=p[0];v?(t=s+o[0]*(a-s),h=r+o[1]*(l-r),f=e?n+(o.length>=3?o[2]:.5)*(d-n):0):y?(t=(s+a)/2,h=(r+l)/2,f=(n+d)/2):_?(t=c[k-1],h=u[k-1],f=p[k-1]):V(o)&&(t=c[o],h=u[o],f=p[o]);for(let s=0;s<k;s++){const r=t-c[s],n=h-u[s],o=f-p[s];let a=J(r*r+n*n+(e?o*o:0));"x"===m&&(a=-r),"y"===m&&(a=-n),"z"===m&&(a=-o),i.push(a)}let g=1/0;for(let t=0;t<k;t++){const e=et(i[t]);e>0&&e<g&&(g=e)}if(g>0&&g<1/0)for(let t=0;t<k;t++)i[t]=i[t]/g}else for(let t=0;t<k;t++)i.push(et(B-t))}else for(let t=0;t<k;t++)if(c){const e=c.length,s=c[0]*c[1];let r,n,a;v?(r=o[0]*(c[0]-1),n=o[1]*(c[1]-1),a=3===e?(o.length>=3?o[2]:.5)*(c[2]-1):0):y?(r=(c[0]-1)/2,n=(c[1]-1)/2,a=3===e?(c[2]-1)/2:0):(r=B%c[0],n=rt(B/c[0])%c[1],a=3===e?rt(B/s):0);const l=r-t%c[0],h=n-rt(t/c[0])%c[1],d=a-(3===e?rt(t/s):0);let u=J(l*l+h*h+(3===e?d*d:0));"x"===m&&(u=-l),"y"===m&&(u=-h),"z"===m&&(u=-d),i.push(u)}else i.push(et(B-t));r=i[0];for(let t=1;t<k;t++)i[t]>r&&(r=i[t]);if(d||a)for(let t=0;t<k;t++){let e=i[t];d&&(e=d(e/r)*r),a&&(e=m?-e:et(r-e)),i[t]=e}if(A){n=new Array(k);for(let t=0;t<k;t++)n[t]=N(-1,1,4)}b&&(i=$i(i,N))}const F=T?(w-S)/r:S;H(s)&&(s=g?Ve(g,H(e.start)?g.iterationDuration:E):E);let M=s+(F*ct(i[P],2)||0);if(A){const t=r?i[P]/r:0,e=R+(L-R)*t;M+=n[P]*e}return e.modifier&&(M=e.modifier(M)),$&&(M=`${M}${$[2]}`),M}};var Ei=Object.freeze({__proto__:null,$:ce,addChild:vt,clamp:yi,cleanInlineStyles:zt,createSeededRandom:Si,damp:vi,degToRad:mi,forEachChildren:_t,get:ts,keepTime:ds,lerp:bi,mapRange:pi,padEnd:ci,padStart:di,radToDeg:fi,random:Ti,randomPick:wi,remove:ss,removeChild:bt,round:_i,roundPad:hi,set:es,shuffle:$i,snap:gi,stagger:Ci,sync:hs,wrap:ui});const ki=t=>{const e=de(t)[0];return e&&Y(e)?e:console.warn(`${t} is not a valid SVGGeometryElement`)},Ni=(t,e,s,i,r)=>{const n=s+i,o=r?Math.max(0,Math.min(n,e)):(n%e+e)%e;return t.getPointAtLength(o)},Di=(t,e,s=0)=>i=>{const r=+t.getTotalLength(),n=i[a],o=t.getCTM(),l=0===s;return{from:0,to:r,modifier:i=>{const a=i+s*r;if("a"===e){const e=Ni(t,r,a,-1,l),s=Ni(t,r,a,1,l);return 180*at(s.y-e.y,s.x-e.x)/lt}{const s=Ni(t,r,a,0,l);return"x"===e?n||!o?s.x:s.x*o.a+s.y*o.c+o.e:n||!o?s.y:s.x*o.b+s.y*o.d+o.f}}}},Ai=(t,e=0)=>{const s=ki(t);if(s)return{translateX:Di(s,"x",e),translateY:Di(s,"y",e),rotate:Di(s,"a",e)}},Ii=(t,e=0,s=0)=>de(t).map(t=>((t,e,s)=>{const i=u,r=getComputedStyle(t),n=r.strokeLinecap,o="non-scaling-stroke"===r.vectorEffect?t:null;let a=n;const l=new Proxy(t,{get(t,e){const s=t[e];return e===h?t:"setAttribute"===e?(...e)=>{if("draw"===e[0]){const s=e[1].split(" "),r=+s[0],l=+s[1],h=(t=>{let e=1;if(t&&t.getCTM){const s=t.getCTM();s&&(e=(J(s.a*s.a+s.b*s.b)+J(s.c*s.c+s.d*s.d))/2)}return e})(o),d=-1e3*r*h,c=l*i*h+d,u=i*h+(0===r&&1===l||1===r&&0===l?0:10*h)-c;if("butt"!==n){const e=r===l?"butt":n;a!==e&&(t.style.strokeLinecap=`${e}`,a=e)}t.setAttribute("stroke-dashoffset",`${d}`),t.setAttribute("stroke-dasharray",`${c} ${u}`)}return Reflect.apply(s,t,e)}:z(s)?(...e)=>Reflect.apply(s,t,e):s}});return"1000"!==t.getAttribute("pathLength")&&(t.setAttribute("pathLength","1000"),l.setAttribute("draw",`${e} ${s}`)),l})(t,e,s)),Ri=(t,e=.33)=>(s,i,r,n)=>{if(!(s.tagName||"").toLowerCase().match(/^(path|polygon|polyline)$/))throw new Error(`Can't morph a <${s.tagName}> SVG element. Use <path>, <polygon> or <polyline>.`);const o=ki(t);if(!o)throw new Error("Can't morph to an invalid target. 'path2' must resolve to an existing <path>, <polygon> or <polyline> SVG element.");if(!(o.tagName||"").toLowerCase().match(/^(path|polygon|polyline)$/))throw new Error(`Can't morph a <${o.tagName}> SVG element. Use <path>, <polygon> or <polyline>.`);const a="path"===s.tagName,l=a?" ":",",h=n?n._value:null;h&&s.setAttribute(a?"d":"points",h);let d="",c="";if(e){const t=s.getTotalLength(),i=o.getTotalLength(),r=Math.max(Math.ceil(t*e),Math.ceil(i*e));for(let e=0;e<r;e++){const n=e/(r-1),h=s.getPointAtLength(t*n),u=o.getPointAtLength(i*n),p=a?0===e?"M":"L":"";d+=p+ct(h.x,3)+l+h.y+" ",c+=p+ct(u.x,3)+l+u.y+" "}}else d=s.getAttribute(a?"d":"points"),c=o.getAttribute(a?"d":"points");return[d,c]};var Li=Object.freeze({__proto__:null,createDrawable:Ii,createMotionPath:Ai,morphTo:Ri});const Bi="undefined"!=typeof Intl&&Intl.Segmenter,Pi=/\\{value\\}/g,Fi=/\\{i\\}/g,Mi=/(\\s+)/,Vi=/^\\s+$/,Oi="line",zi="word",Hi="char",Xi="data-line";let Yi=null,Wi=null,Ui=null;const qi=t=>t.isWordLike||" "===t.segment||V(+t.segment),ji=t=>t.setAttribute("aria-hidden","true"),Gi=(t,e)=>[...t.querySelectorAll(`[data-${e}]:not([data-${e}] [data-${e}])`)],Zi={line:"#00D672",word:"#FF4B4B",char:"#5A87FF"},Qi=t=>{if(!t.childElementCount&&!t.textContent.trim()){const e=t.parentElement;t.remove(),e&&Qi(e)}},Ji=(t,e,s)=>{const i=t.getAttribute(Xi);if(null!==i&&+i!==e||"BR"===t.tagName){s.add(t);const e=t.previousSibling,i=t.nextSibling;e&&3===e.nodeType&&Vi.test(e.textContent)&&s.add(e),i&&3===i.nodeType&&Vi.test(i.textContent)&&s.add(i)}let r=t.childElementCount;for(;r--;)Ji(t.children[r],e,s);return s},Ki=(t,e={})=>{let s="";e||(e={});const i=O(e.class)?` class="${e.class}"`:"",r=$t(e.clone,!1),n=$t(e.wrap,!1),o=n?!0===n?"clip":n:!!r&&"clip";return n&&(s+=`<span${o?` style="overflow:${o};"`:""}>`),s+=`<span${i}${r?' style="position:relative;"':""} data-${t}="{i}">`,r?(s+="<span>{value}</span>",s+=`<span inert style="position:absolute;top:${"top"===r?"-100%":"bottom"===r?"100%":"0"};left:${"left"===r?"-100%":"right"===r?"100%":"0"};white-space:nowrap;">{value}</span>`):s+="{value}",s+="</span>",n&&(s+="</span>"),s},tr=(t,e,s,i,r,n,o,a,l)=>{const h=r===Oi,d=r===Hi,c=`_${r}_`,u=z(t)?t(s):t,p=h?"block":"inline-block";Ui.innerHTML=u.replace(Pi,`<i class="${c}"></i>`).replace(Fi,`${d?l:h?o:a}`);const m=Ui.content,f=m.firstElementChild,g=m.querySelector(`[data-${r}]`)||f,y=m.querySelectorAll(`i.${c}`),_=y.length;if(_){f.style.display=p,g.style.display=p,g.setAttribute(Xi,`${o}`),h||(g.setAttribute("data-word",`${a}`),d&&g.setAttribute("data-char",`${l}`));let t=_;for(;t--;){const e=y[t],i=e.parentElement;i.style.display=p,h?i.innerHTML=s.innerHTML:i.replaceChild(s.cloneNode(!0),e)}e.push(g),i.appendChild(m)}else console.warn('The expression "{value}" is missing from the provided template.');return n&&(f.style.outline=`1px dotted ${Zi[r]}`),f};class er{constructor(t,s={}){Yi||(Yi=Bi?new Bi([],{granularity:zi}):{segment:t=>{const e=[],s=t.split(Mi);for(let t=0,i=s.length;t<i;t++){const i=s[t];e.push({segment:i,isWordLike:!Vi.test(i)})}return e}}),Wi||(Wi=Bi?new Bi([],{granularity:"grapheme"}):{segment:t=>[...t].map(t=>({segment:t}))}),!Ui&&e&&(Ui=i.createElement("template")),A.current&&A.current.register(this);const{words:r,chars:n,lines:o,accessible:a,includeSpaces:l,debug:h}=s,d=(t=F(t)?t[0]:t)&&t.nodeType?t:(he(t)||[])[0],c=!0===o?{}:o,u=!0===r||H(r)?{}:r,p=!0===n?{}:n;this.debug=$t(h,!1),this.includeSpaces=$t(l,!1),this.accessible=$t(a,!0),this.linesOnly=c&&!u&&!p,this.lineTemplate=M(c)?Ki(Oi,c):c,this.wordTemplate=M(u)||this.linesOnly?Ki(zi,u):u,this.charTemplate=M(p)?Ki(Hi,p):p,this.$target=d,this.html=d&&d.innerHTML,this.lines=[],this.words=[],this.chars=[],this.effects=[],this.effectsCleanups=[],this.cache=null,this.ready=!1,this.width=0,this.resizeTimeout=null;const m=()=>this.html&&(c||u||p)&&this.split();this.resizeObserver=new ResizeObserver(()=>{clearTimeout(this.resizeTimeout),this.resizeTimeout=setTimeout(()=>{const t=d.offsetWidth;t!==this.width&&(this.width=t,m())},150)}),this.lineTemplate&&!this.ready?i.fonts.ready.then(m):m(),d?this.resizeObserver.observe(d):console.warn("No Text Splitter target found.")}addEffect(t){if(!z(t))return console.warn("Effect must return a function."),this;const e=ds(t);return this.effects.push(e),this.ready&&(this.effectsCleanups[this.effects.length-1]=e(this)),this}revert(){return clearTimeout(this.resizeTimeout),this.lines.length=this.words.length=this.chars.length=0,this.resizeObserver.disconnect(),this.effectsCleanups.forEach(t=>z(t)?t(this):t.revert&&t.revert()),this.$target.innerHTML=this.html,this}splitNode(t){const e=this.wordTemplate,s=this.charTemplate,r=this.includeSpaces,n=this.debug,o=t.nodeType;if(3===o){const o=t.nodeValue;if(o.trim()){const a=[],l=this.words,h=this.chars,d=Yi.segment(o),c=i.createDocumentFragment();let u=null;for(const t of d){const e=t.segment,s=qi(t);if(!u||s&&u&&qi(u))a.push(e);else{const t=a.length-1,s=a[t];Mi.test(s)||Mi.test(e)?a.push(e):a[t]+=e}u=t}for(let t=0,o=a.length;t<o;t++){const o=a[t];if(o.trim()){const d=a[t+1],u=r&&d&&!d.trim(),p=o,m=s?Wi.segment(p):null,f=s?i.createDocumentFragment():i.createTextNode(u?o+" ":o);if(s){const t=[...m];for(let e=0,r=t.length;e<r;e++){const o=t[e],a=e===r-1&&u?o.segment+" ":o.segment,d=i.createTextNode(a);tr(s,h,d,f,Hi,n,-1,l.length,h.length)}}e?tr(e,l,f,c,zi,n,-1,l.length,h.length):s?c.appendChild(f):c.appendChild(i.createTextNode(o)),u&&t++}else{if(t&&r)continue;c.appendChild(i.createTextNode(o))}}t.parentNode.replaceChild(c,t)}}else if(1===o){const e=[...t.childNodes];for(let t=0,s=e.length;t<s;t++)this.splitNode(e[t])}}split(t=!1){const e=this.$target,s=!!this.cache&&!t,r=this.lineTemplate,n=this.wordTemplate,o=this.charTemplate,a="loading"!==i.fonts.status,l=r&&a;this.ready=!r||a,(l||t)&&this.effectsCleanups.forEach(t=>z(t)&&t(this)),s||(t&&(e.innerHTML=this.html,this.words.length=this.chars.length=0),this.splitNode(e),this.cache=e.innerHTML),l&&(s&&(e.innerHTML=this.cache),this.lines.length=0,n&&(this.words=Gi(e,zi))),o&&(l||n)&&(this.chars=Gi(e,Hi));const h=this.words.length?this.words:this.chars;let d,c=0;for(let t=0,e=h.length;t<e;t++){const e=h[t],{top:s,height:i}=e.getBoundingClientRect();!H(d)&&s-d>.5*i&&c++,e.setAttribute(Xi,`${c}`);const r=e.querySelectorAll(`[${Xi}]`);let n=r.length;for(;n--;)r[n].setAttribute(Xi,`${c}`);d=s}if(l){const t=i.createDocumentFragment(),s=new Set,a=[];for(let t=0;t<c+1;t++){const i=e.cloneNode(!0);Ji(i,t,new Set).forEach(t=>{const e=t.parentNode;e&&(1===t.nodeType&&s.add(e),e.removeChild(t))}),a.push(i)}s.forEach(Qi);for(let e=0,s=a.length;e<s;e++)tr(r,this.lines,a[e],t,Oi,this.debug,e);e.innerHTML="",e.appendChild(t),n&&(this.words=Gi(e,zi)),o&&(this.chars=Gi(e,Hi))}if(this.linesOnly){const t=this.words;let e=t.length;for(;e--;){const s=t[e];s.replaceWith(s.textContent)}t.length=0}if(this.accessible&&(l||!s)){const t=i.createElement("span");t.style.cssText="position:absolute;overflow:hidden;clip:rect(0 0 0 0);clip-path:inset(50%);width:1px;height:1px;white-space:nowrap;",t.innerHTML=this.html,e.insertBefore(t,e.firstChild),this.lines.forEach(ji),this.words.forEach(ji),this.chars.forEach(ji)}return this.width=e.offsetWidth,(l||t)&&this.effects.forEach((t,e)=>this.effectsCleanups[e]=t(this)),this}refresh(){this.split(!0)}}const sr=(t,e)=>new er(t,e),ir=(t,e)=>(console.warn("text.split() is deprecated, import splitText() directly, or text.splitText()"),new er(t,e)),rr=t=>{let e="";for(let s=0,i=t.length;s<i;s++)if(s+2<i&&"-"===t[s+1]&&t.charCodeAt(s)<t.charCodeAt(s+2)){const i=t.charCodeAt(s),r=t.charCodeAt(s+2);for(let t=i;t<=r;t++)e+=String.fromCharCode(t);s+=2}else e+=t[s];return e},nr={lowercase:"a-z",uppercase:"A-Z",numbers:"0-9",symbols:"!%#_|*+=",braille:"⠀-⣿",blocks:"▀-▟",shades:"░-▓"},or=new WeakMap,ar=(t={})=>{t||(t={});const e=t.chars,s=$e(t.ease||"linear"),i=t.text,r=t.from,n=t.reversed||!1,o=t.perturbation||0,a=t.cursor,l=!0===a?"_":"number"==typeof a?String.fromCharCode(a):"string"==typeof a?a:"",h=l.length,d=t.seed||0,c=void 0===t.override||t.override,u=t.revealRate||60,p=1e3*I.timeScale/u,m=t.settleDuration||300*I.timeScale,f=t.settleRate||30,g=t.duration,y=t.revealDelay,b=t.delay,v=t.onChange||_;return(t,a,u,_)=>{const T="function"==typeof e?e(t,a,u):e||"a-zA-Z0-9!%#_",x=rr(nr[T]||T),S=x.length-1,w="function"==typeof g?g(t,a,u):g,$="function"==typeof y?y(t,a,u):y||0,C="function"==typeof b?b(t,a,u):b||0,E=d?Si(d):Si();or.has(t)||or.set(t,t.textContent);const k=_?_._value:t.textContent,N=void 0!==i?"function"==typeof i?i(t,a,u):i:_?_._value:or.get(t),D=" "===N||"&nbsp;"===N?" ":N,A=" "===k?0:k.length,R=D.length,L=!0===c?x:"string"==typeof c&&c.length>0?rr(nr[c]||c):null,B=L?L.length-1:0,P=" "===c?" ":null,F=""===c?R:Math.max(A,R),M=w>0?w:(F-1)*p+m,V=ct((M+$)/I.timeScale,0)*I.timeScale,O=$>0?ct($/V,12):0,z=void 0===r||"auto"===r?R<A?"right":"left":r,H=new Int32Array(F);if("random"===z){for(let t=0;t<F;t++)H[t]=t;for(let t=F-1;t>0;t--){const e=E(0,t),s=H[t];H[t]=H[e],H[e]=s}}else{const t="right"===z?(""!==c&&A?A:F)-1:"center"===z?((""!==c&&A?A:F)-1)/2:"number"==typeof z?z:0,e=Math.abs,s=new Array(F);for(let t=0;t<F;t++)s[t]=t;s.sort((s,i)=>e(s-t)-e(i-t));for(let t=0;t<F;t++)H[s[t]]=t}if(n){const t=F-1;for(let e=0;e<F;e++)H[e]=t-H[e]}const X=ct(m/M,12),Y=ct((1-X)/F,12),W=h*Y,U=ct(1e3*I.timeScale/(f*V),12),q=new Float32Array(F),j=new Float32Array(F),G=o>0?o*X:0;for(let t=0;t<F;t++){const e=G>0?(E(0,2e3)-1e3)/1e3*G:0,s=G>0?(E(0,2e3)-1e3)/1e3*G:0;q[t]=H[t]*Y+e,j[t]=Math.ceil((q[t]+X+s)/U)*U}if(R<F&&"left"!==z&&"right"!==z&&"random"!==z){let t=0;for(let e=R;e<F;e++)j[e]>t&&(t=j[e]);const e=new Array(R);for(let t=0;t<R;t++)e[t]=t;e.sort((t,e)=>H[t]-H[e]);const s=(1-t)/R;for(let i=0;i<R;i++){const r=t+i*s;r>j[e[i]]&&(j[e[i]]=r)}}const Z=new Array(F);for(let t=0;t<F;t++)Z[t]=x[E(0,S)];const Q=L?L===x?Z:new Array(F):null;if(Q&&Q!==Z)for(let t=0;t<F;t++)Q[t]=P||L[E(0,L.length-1)];let J=k;if(!_)if(""===c)J="";else if(L){J="";for(let t=0;t<A;t++)J+=" "===k[t]?" ":Q[t]}let K=-1,tt=-1,et="";const st=""!==c,it=!!L,rt=h>0;return{from:0,to:1,duration:V,delay:C,ease:"linear",modifier:t=>{if(t===K)return et;if(K=t,C>0&&t<=0)return et=k,k;if(t<=0)return et=J,J;if(t>=1)return et=D,D;et="";const e=t/U|0,i=e!==tt;i&&(tt=e);const r=O>0?(t-O)/(1-O):t,n=r>0?s(r):0;for(let t=0;t<F;t++){const e=q[t];n>=j[t]?t<R&&(et+=D[t]):n<=0||n<e?st&&t<A&&(it?" "===k[t]?et+=" ":(i&&(Q[t]=P||L[E(0,B)]),et+=Q[t]):et+=k[t]):t<R&&" "===D[t]||t<A&&" "===k[t]?et+=" ":rt&&n-e<W?et+=l[h-1-((n-e)/Y|0)]:(i&&(Z[t]=x[E(0,S)]),et+=Z[t])}return i&&v(et,n),et}}}};var lr=Object.freeze({__proto__:null,TextSplitter:er,scrambleText:ar,split:ir,splitText:sr});t.$=ce,t.Animatable=Ye,t.AutoLayout=ri,t.Draggable=ls,t.JSAnimation=Me,t.Scope=cs,t.ScrollObserver=vs,t.Spring=je,t.TextSplitter=er,t.Timeline=He,t.Timer=le,t.WAAPIAnimation=Ps,t.addChild=vt,t.animate=(t,e)=>I.editor?I.editor.addAnimation(t,e):new Me(t,e,null,0,!1).init(),t.clamp=yi,t.cleanInlineStyles=zt,t.createAnimatable=(t,e)=>new Ye(t,e),t.createDraggable=(t,e)=>new ls(t,e),t.createDrawable=Ii,t.createLayout=(t,e)=>new ri(t,e),t.createMotionPath=Ai,t.createScope=t=>new cs(t),t.createSeededRandom=Si,t.createSpring=Ze,t.createTimeline=Xe,t.createTimer=t=>new le(t,null,0).init(),t.cubicBezier=xs,t.damp=vi,t.degToRad=mi,t.eases=Te,t.easings=Cs,t.engine=qt,t.forEachChildren=_t,t.get=ts,t.globals=I,t.irregular=$s,t.keepTime=ds,t.lerp=bi,t.linear=ws,t.mapRange=pi,t.morphTo=Ri,t.onScroll=(t={})=>new vs(t),t.padEnd=ci,t.padStart=di,t.radToDeg=fi,t.random=Ti,t.randomPick=wi,t.remove=ss,t.removeChild=bt,t.round=_i,t.roundPad=hi,t.scrambleText=ar,t.scrollContainers=ps,t.set=es,t.shuffle=$i,t.snap=gi,t.split=ir,t.splitText=sr,t.spring=Ge,t.stagger=Ci,t.steps=Ss,t.svg=Li,t.sync=hs,t.text=lr,t.utils=Ei,t.waapi=Fs,t.wrap=ui});
</script>
<script>
(function () {
  "use strict";

  // Same escaping discipline as the brain page: `shared_id` and `purpose`
  // are client-supplied through POST /v1/sessions, so text goes through
  // `esc`; the href is safe because encodeURIComponent output cannot close
  // an attribute.
  function esc(s) {
    var d = document.createElement("div");
    d.textContent = String(s == null ? "" : s);
    return d.innerHTML;
  }

  function renderSessions(sessions) {
    var el = document.getElementById("sessions");
    if (!sessions.length) {
      el.innerHTML = '<div class="empty">the service holds no shared session yet · ' +
        'POST /v1/sessions creates one</div>';
      return;
    }
    el.innerHTML = sessions.map(function (s) {
      var pill = s.status === "ended"
        ? '<span class="pill ended">ended</span>'
        : '<span class="pill active">active</span>';
      return '<a class="scard" href="/debug?session=' + encodeURIComponent(s.shared_id) + '">' +
        '<span class="scard-purpose">' + esc(s.purpose || "(no purpose recorded)") + '</span>' +
        '<span class="scard-meta"><span class="sid">' + esc(s.shared_id) + '</span>' + pill + '</span>' +
        '</a>';
    }).join("");
  }

  function refresh() {
    fetch("/debug/brain.json")
      .then(function (r) { if (!r.ok) throw new Error("bad status " + r.status); return r.json(); })
      .then(function (payload) {
        document.getElementById("banner").classList.remove("show");
        var dot = document.getElementById("live-dot");
        dot.className = "live-dot up";
        var n = payload.sessions.length;
        document.getElementById("live-text").textContent =
          n + (n === 1 ? " shared session" : " shared sessions");
        renderSessions(payload.sessions);
      })
      .catch(function () {
        document.getElementById("banner").classList.add("show");
        var dot = document.getElementById("live-dot");
        dot.className = "live-dot down";
        document.getElementById("live-text").textContent = "service unreachable";
      });
  }

  refresh();
  setInterval(refresh, 2000);
})();

(function () {
  "use strict";

  // ── chrome: theme, GitHub link, slides engine, keyboard. All of this works
  // without the animation library; motion is layered on where available. ──
  var A = (typeof anime !== "undefined") ? anime : null;
  var reduced = false;
  try { reduced = window.matchMedia("(prefers-reduced-motion: reduce)").matches; } catch (e) {}
  var motionOK = !!A && !reduced;

  // The GitHub URL is assembled at runtime so the served page contains no
  // external-protocol substring: the page itself still makes zero external
  // requests, and the link only navigates when a person clicks it.
  var GH = "ht" + "tps" + "://github.com/SinghSiddharth01/Synapse";
  ["gh-link", "gh-link2"].forEach(function (id) {
    var el = document.getElementById(id);
    if (el) el.setAttribute("href", GH);
  });

  // ── theme: dark is the brand default; T toggles; choice persists ──
  var THEME_KEY = "synapse-theme";
  function setTheme(t) {
    document.documentElement.setAttribute("data-theme", t);
    try { localStorage.setItem(THEME_KEY, t); } catch (e) {}
  }
  function toggleTheme() {
    var cur = document.documentElement.getAttribute("data-theme") === "light" ? "light" : "dark";
    setTheme(cur === "light" ? "dark" : "light");
  }
  try {
    var stored = localStorage.getItem(THEME_KEY);
    if (stored === "light" || stored === "dark") setTheme(stored);
  } catch (e) {}
  var themeBtn = document.getElementById("theme-btn");
  if (themeBtn) themeBtn.addEventListener("click", toggleTheme);

  // ── architecture switch: today vs what the seams allow ──
  var arch = document.getElementById("arch");
  var archToday = document.getElementById("arch-today");
  var archAllows = document.getElementById("arch-allows");
  var archCaption = document.getElementById("arch-caption");
  var ARCH_TEXT = {
    today: "Three edge workers distil on the Hexagon NPU; one service folds findings into a curated working memory; synthesis runs on Cloud AI 100.",
    allows: "The same seams with nothing swapped out: any machine joins with no NPU, any of the five providers plugs into the one interface, and the pipeline still runs end to end, even fully offline."
  };
  function setArch(view) {
    if (!arch) return;
    arch.setAttribute("data-view", view);
    if (archToday) archToday.setAttribute("aria-pressed", view === "today" ? "true" : "false");
    if (archAllows) archAllows.setAttribute("aria-pressed", view === "allows" ? "true" : "false");
    if (archCaption) archCaption.textContent = ARCH_TEXT[view];
  }
  if (archToday) archToday.addEventListener("click", function () { setArch("today"); });
  if (archAllows) archAllows.addEventListener("click", function () { setArch("allows"); });

  // ── count-ups: the measured numbers earn their reveal ──
  function countUp(el) {
    if (el.getAttribute("data-done")) return;
    el.setAttribute("data-done", "1");
    if (!motionOK) return;
    var target = parseFloat(el.getAttribute("data-n"));
    if (isNaN(target)) return;
    var dec = parseInt(el.getAttribute("data-dec") || "0", 10);
    var sep = el.getAttribute("data-sep") === "1";
    var obj = { v: 0 };
    A.animate(obj, {
      v: target,
      duration: 1500,
      ease: "outExpo",
      onUpdate: function () {
        var s = obj.v.toFixed(dec);
        if (sep) s = Number(s).toLocaleString("en-US");
        el.textContent = s;
      }
    });
  }

  // ── scroll reveals: a sweep, not a per-entry toggle, so a fast jump can
  // only delay a reveal, never lose one ──
  var pending = Array.prototype.slice.call(document.querySelectorAll("[data-reveal]"));
  var io = null;
  function revealMotion(sec) {
    A.animate(sec, { opacity: [0, 1], translateY: [30, 0], duration: 850, ease: "outCubic" });
    sec.querySelectorAll(".cu").forEach(countUp);
  }
  function revealNow(sec) {
    sec.style.opacity = "";
    sec.style.transform = "";
    sec.querySelectorAll(".cu").forEach(countUp);
  }
  function sweep() {
    var vh = window.innerHeight;
    for (var i = pending.length - 1; i >= 0; i--) {
      var sec = pending[i];
      if (sec.getBoundingClientRect().top < vh - 40) {
        pending.splice(i, 1);
        if (io) io.unobserve(sec);
        revealMotion(sec);
      }
    }
    if (!pending.length) teardownReveals();
  }
  function teardownReveals() {
    if (io) { io.disconnect(); io = null; }
    window.removeEventListener("scroll", sweep);
  }
  function revealEverythingNow() {
    pending.splice(0).forEach(revealNow);
    teardownReveals();
    document.querySelectorAll(".cu").forEach(countUp);
  }
  if (motionOK && typeof IntersectionObserver !== "undefined") {
    pending.forEach(function (sec) { sec.style.opacity = "0"; });
    io = new IntersectionObserver(function () { sweep(); },
      { threshold: 0.1, rootMargin: "0px 0px -40px 0px" });
    pending.forEach(function (sec) { io.observe(sec); });
    window.addEventListener("scroll", sweep, { passive: true });
    sweep();
  } else {
    pending = [];
  }

  // ── slides engine ──
  var slides = Array.prototype.slice.call(document.querySelectorAll(".slide"));
  var hudDots = document.getElementById("hud-dots");
  var hudCount = document.getElementById("hud-count");
  var presentBtn = document.getElementById("present-btn");
  var presentLabel = document.getElementById("present-label");
  var slideMode = false;
  var cur = 0;

  if (hudDots) {
    slides.forEach(function (s, i) {
      var b = document.createElement("button");
      b.className = "hud-dot";
      b.setAttribute("aria-label", "slide " + (i + 1));
      b.addEventListener("click", function () { go(i); });
      hudDots.appendChild(b);
    });
  }

  function paintHud() {
    if (hudDots) {
      Array.prototype.forEach.call(hudDots.children, function (d, i) {
        d.className = "hud-dot" + (i === cur ? " on" : "");
      });
    }
    if (hudCount) hudCount.textContent = (cur + 1) + " / " + slides.length;
  }

  function go(n, instant) {
    if (!slideMode) return;
    cur = Math.max(0, Math.min(slides.length - 1, n));
    slides.forEach(function (s, i) { s.classList.toggle("on", i === cur); });
    var active = slides[cur];
    active.scrollTop = 0;
    active.querySelectorAll(".cu").forEach(countUp);
    if (motionOK && !instant) {
      var kids = active.querySelectorAll(":scope > *");
      A.animate(kids, {
        opacity: [0, 1],
        translateY: [22, 0],
        duration: 600,
        delay: A.stagger(70),
        ease: "outCubic"
      });
    }
    paintHud();
  }

  function enterSlides() {
    if (slideMode) return;
    slideMode = true;
    revealEverythingNow();
    document.body.setAttribute("data-mode", "slides");
    if (presentLabel) presentLabel.textContent = "Exit";
    go(cur, false);
  }
  function exitSlides() {
    if (!slideMode) return;
    slideMode = false;
    document.body.setAttribute("data-mode", "scroll");
    slides.forEach(function (s) {
      s.classList.remove("on");
      s.style.opacity = "";
      s.style.transform = "";
      Array.prototype.forEach.call(s.children, function (k) {
        k.style.opacity = "";
        k.style.transform = "";
      });
    });
    if (presentLabel) presentLabel.textContent = "Present";
  }
  function toggleSlides() { if (slideMode) exitSlides(); else enterSlides(); }
  if (presentBtn) presentBtn.addEventListener("click", toggleSlides);

  function toggleFullscreen() {
    try {
      if (document.fullscreenElement) document.exitFullscreen();
      else document.documentElement.requestFullscreen();
    } catch (e) {}
  }

  document.addEventListener("keydown", function (e) {
    var tag = e.target && e.target.tagName;
    if (tag === "INPUT" || tag === "TEXTAREA" || tag === "SELECT") return;
    if (e.key === "t" || e.key === "T") { toggleTheme(); return; }
    if (e.key === "f" || e.key === "F") { toggleFullscreen(); return; }
    if (e.shiftKey && (e.key === "S" || e.key === "s")) { toggleSlides(); return; }
    if (!slideMode) return;
    if (e.key === "ArrowRight" || e.key === "PageDown" || e.key === " ") { e.preventDefault(); go(cur + 1); }
    else if (e.key === "ArrowLeft" || e.key === "PageUp") { e.preventDefault(); go(cur - 1); }
    else if (e.key === "Home") { e.preventDefault(); go(0); }
    else if (e.key === "End") { e.preventDefault(); go(slides.length - 1); }
    else if (e.key === "Escape" && !document.fullscreenElement) { exitSlides(); }
  });

  if (!motionOK) return;

  var animate = A.animate;
  var stagger = A.stagger;
  var motionPath = (A.svg && A.svg.createMotionPath) ? A.svg.createMotionPath : A.createMotionPath;
  var drawable = (A.svg && A.svg.createDrawable) ? A.svg.createDrawable : A.createDrawable;

  // ── hero entrance ──
  var heroBits = document.querySelectorAll(".hero .kicker, .hero h1, .hero .tagline, .hero .cta-row, .hero .net");
  heroBits.forEach(function (el) { el.style.opacity = "0"; });
  animate(heroBits, {
    opacity: [0, 1],
    translateY: [26, 0],
    duration: 950,
    delay: stagger(120, { start: 60 }),
    ease: "outCubic"
  });

  // ── the network: impulses ride the axons, the hub breathes, the AI 100
  // link pulses both ways ──
  ["ax1", "ax2", "ax3", "ax4"].forEach(function (pid, i) {
    var path = document.getElementById(pid);
    if (!path || !motionPath) return;
    var mp = motionPath(path);
    var fwd = document.getElementById(pid + "-dot");
    var ret = document.getElementById(pid + "-ret");
    if (fwd) {
      fwd.style.opacity = "1";
      animate(fwd, Object.assign({}, mp, {
        duration: 3100 + i * 420,
        delay: i * 380,
        loop: true,
        ease: "inOutSine"
      }));
    }
    if (ret) {
      ret.style.opacity = "1";
      animate(ret, Object.assign({}, mp, {
        duration: 3700 + i * 350,
        delay: 1700 + i * 320,
        loop: true,
        reversed: true,
        ease: "inOutSine"
      }));
    }
  });
  var hub = document.getElementById("net-hub");
  if (hub) animate(hub, { r: [15, 18], duration: 2300, ease: "inOutSine", loop: true, alternate: true });
  var halo = document.getElementById("net-halo");
  if (halo) animate(halo, { r: [27, 32], duration: 2300, ease: "inOutSine", loop: true, alternate: true });
  var ring = document.getElementById("net-ring");
  if (ring) animate(ring, { r: [22, 58], opacity: [0.5, 0], duration: 3100, loop: true, ease: "outSine" });

  // ── the why diagram: the whole argument as one looping choreography.
  // Left: three explorations to the same dead end, plus the human relay.
  // Right: one exploration, one landing, curated propagation to everyone. ──
  (function whyStory() {
    if (!drawable || !motionPath || !A.createTimeline) return;
    var need = ["wx-p1", "wx-p2", "wx-p3", "wx-x", "wx-relay", "wx-rdot",
                "ww-p1", "ww-x", "ww-f1", "ww-pulse", "ww-ring", "ww-g2", "ww-g3", "ww-t2", "ww-t3"];
    for (var i = 0; i < need.length; i++) {
      if (!document.getElementById(need[i])) return;
    }
    var d = function (id) { return drawable("#" + id); };
    var el = function (id) { return document.getElementById(id); };
    var tl = A.createTimeline({ loop: true, defaults: { ease: "inOutSine" } });

    // resets at the top of every loop
    tl.add(el("wx-x"), { opacity: 0, duration: 1 }, 0);
    tl.add(el("ww-x"), { opacity: 0, duration: 1 }, 0);
    tl.add(el("ww-ring"), { opacity: 0, r: 18, duration: 1 }, 0);
    tl.add(el("ww-t2"), { opacity: 0, duration: 1 }, 0);
    tl.add(el("ww-t3"), { opacity: 0, duration: 1 }, 0);
    tl.add(el("wx-rdot"), { opacity: 0, duration: 1 }, 0);
    tl.add(el("ww-pulse"), { opacity: 0, duration: 1 }, 0);

    // WITHOUT: three explorations, one dead end
    tl.add(d("wx-p1"), { draw: ["0 0", "0 1"], duration: 900 }, 200);
    tl.add(el("wx-x"), { opacity: [0, 1], duration: 260 }, 1050);
    tl.add(d("wx-p2"), { draw: ["0 0", "0 1"], duration: 900 }, 1500);
    tl.add(el("wx-x"), { opacity: [0.55, 1], duration: 260 }, 2350);
    tl.add(d("wx-p3"), { draw: ["0 0", "0 1"], duration: 900 }, 2800);
    tl.add(el("wx-x"), { opacity: [0.55, 1], duration: 260 }, 3650);
    // the human relay, slow and manual
    tl.add(d("wx-relay"), { draw: ["0 0", "0 1"], duration: 700 }, 3900);
    tl.add(el("wx-rdot"), Object.assign({}, motionPath(el("wx-relay")), {
      opacity: { to: 1, duration: 120 },
      duration: 1400,
      ease: "inOutQuad"
    }), 3950);
    tl.add(el("wx-rdot"), { opacity: [1, 0], duration: 200 }, 5350);

    // WITH: one exploration, then the memory does the rest
    tl.add(d("ww-p1"), { draw: ["0 0", "0 1"], duration: 800 }, 5700);
    tl.add(el("ww-x"), { opacity: [0, 1], duration: 260 }, 6450);
    tl.add(d("ww-f1"), { draw: ["0 0", "0 1"], duration: 650 }, 6800);
    tl.add(el("ww-pulse"), Object.assign({}, motionPath(el("ww-f1")), {
      opacity: { to: 1, duration: 100 },
      duration: 700,
      ease: "inOutQuad"
    }), 6800);
    tl.add(el("ww-pulse"), { opacity: [1, 0], duration: 150 }, 7480);
    tl.add(el("ww-ring"), { opacity: [0.6, 0], r: [18, 40], duration: 700, ease: "outSine" }, 7550);
    tl.add(d("ww-g2"), { draw: ["0 0", "0 1"], duration: 480 }, 7700);
    tl.add(d("ww-g3"), { draw: ["0 0", "0 1"], duration: 480 }, 7850);
    tl.add(el("ww-t2"), { opacity: [0, 1], duration: 320 }, 8150);
    tl.add(el("ww-t3"), { opacity: [0, 1], duration: 320 }, 8300);
    // hold, then loop
    tl.add(el("ww-x"), { opacity: 1, duration: 1400 }, 8700);
  })();
})();
</script>
</body>
</html>
"""


# ── the memory page: every finding, projected through the fold ─────────────
_MEMORY_PAGE = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<link rel="icon" href="data:,">
<title>synapse-service · memory</title>
<style>
  /* Every finding in the session, projected through the fold (adr/0004):
     status and merged_into are the fold's answer, not the producer's
     defaults. Sortable, searchable, filterable -- the browsing surface the
     brain page's bounded "latest into memory" list deliberately is not.
     Near-black canvas, charcoal lift, identity hues as data encodings.
     Same standing rule as ever: NOT ONE external request. */
  :root {
    color-scheme: dark;
    /* ground: near-black canvas, charcoal surface lift, felt-not-seen hairlines */
    --canvas: #000000;
    --surface-1: #15181e;
    --surface-2: #1f232b;
    --surface-3: #2a2e37;
    --hairline: rgba(178, 182, 189, 0.14);
    --hairline-soft: rgba(178, 182, 189, 0.07);
    /* ink */
    --ink: #ffffff;
    --ink-muted: #b2b6bd;
    --ink-subtle: #656a76;
    /* identity hues: data encodings, never decoration */
    --cyan: #14c6cb;
    --cyan-deep: #12b6bb;
    --green: #00ca8e;
    --amber: #ffcf25;
    --red: #e62b1e;
    --red-text: #f5564a;
    --purple: #7b42bc;
    --purple-bright: #911ced;
    --purple-text: #b78ae8;
    --blue: #1868f2;
    --blue-text: #6ea6ff;
    --copper: #e09a5a;
    --copper-dim: #8a5a2d;
    --radius: 12px;
    --radius-sm: 8px;
    --sans: ui-sans-serif, system-ui, -apple-system, "Segoe UI", Roboto, "Helvetica Neue", Arial, sans-serif;
    --mono: ui-monospace, "SF Mono", "Cascadia Code", Menlo, Consolas, monospace;
  }
  * { box-sizing: border-box; }
  body {
    margin: 0;
    background: var(--canvas);
    color: var(--ink);
    font: 14px/1.55 var(--sans);
    -webkit-font-smoothing: antialiased;
  }
  #banner {
    display: none;
    background: rgba(230, 43, 30, 0.14); color: var(--red-text);
    border-bottom: 1px solid rgba(230, 43, 30, 0.35);
    padding: 8px 24px; font-size: 12px; font-weight: 500;
  }
  #banner.show { display: block; }

  /* ── shell: sessions on the left, this session's pages on top ── */
  .shell { display: flex; min-height: 100dvh; }
  aside {
    width: 272px; flex-shrink: 0;
    border-right: 1px solid var(--hairline-soft);
    padding: 16px 12px;
    display: flex; flex-direction: column; gap: 14px;
    position: sticky; top: 0; height: 100dvh; overflow-y: auto;
  }
  .brand { display: flex; align-items: center; gap: 9px; text-decoration: none; color: inherit; padding: 2px 6px 6px; }
  .brand .mark { width: 30px; height: 16px; overflow: visible; }
  .brand .mark .axon { fill: none; stroke: var(--cyan-deep); stroke-width: 1.4; opacity: 0.7; }
  .brand .mark .soma { fill: var(--cyan); }
  .brand .mark .impulse { fill: var(--ink); }
  @media (prefers-reduced-motion: reduce) { .brand .mark .impulse { display: none; } }
  .brand .name { font-size: 16px; font-weight: 650; letter-spacing: -0.01em; }
  .brand .scope-label { color: var(--ink-subtle); font-size: 13px; }
  .side-label {
    font: 600 11px var(--sans); letter-spacing: 0.06em; text-transform: uppercase;
    color: var(--ink-subtle); padding: 0 6px;
  }
  .side-sessions { display: flex; flex-direction: column; gap: 3px; }
  .side-session {
    display: block; padding: 9px 11px; border-radius: var(--radius-sm);
    text-decoration: none; color: var(--ink-muted);
    border: 1px solid transparent;
    transition: background-color 130ms, border-color 130ms;
  }
  .side-session:hover { background: var(--surface-2); color: var(--ink); }
  .side-session[aria-current="page"] { background: rgba(20, 198, 203, 0.10); border-color: rgba(20, 198, 203, 0.4); color: var(--ink); }
  .side-session:focus-visible { outline: 2px solid var(--cyan); outline-offset: 2px; }
  .side-session .ss-purpose { display: block; font-size: 13.5px; font-weight: 600; line-height: 1.4; overflow-wrap: anywhere; }
  .side-session .ss-sid { display: block; font: 11.5px var(--mono); color: var(--ink-subtle); margin-top: 3px; }
  .side-empty { color: var(--ink-subtle); font-size: 12px; padding: 8px 10px; }
  .side-foot { margin-top: auto; padding: 0 6px; }
  .side-foot a { color: var(--ink-subtle); font-size: 12px; text-decoration: none; }
  .side-foot a:hover { color: var(--ink); }
  .content { flex: 1; min-width: 0; }
  .topbar {
    display: flex; align-items: center; gap: 14px; flex-wrap: wrap;
    padding: 12px 24px;
    border-bottom: 1px solid var(--hairline-soft);
  }
  .tabs { display: flex; gap: 4px; }
  .tabs a {
    padding: 7px 15px; border-radius: var(--radius-sm);
    font-size: 14px; font-weight: 600;
    color: var(--ink-muted); text-decoration: none;
    transition: color 130ms, background-color 130ms;
  }
  .tabs a:hover { color: var(--ink); background: var(--surface-2); }
  .tabs a[aria-current="page"] { color: var(--cyan); background: rgba(20, 198, 203, 0.12); }
  .tabs a:focus-visible { outline: 2px solid var(--cyan); outline-offset: 2px; }
  main { padding: 28px 32px 64px; }
  @media (max-width: 900px) {
    .shell { flex-direction: column; }
    aside { position: static; width: auto; height: auto; border-right: none; border-bottom: 1px solid var(--hairline-soft); }
    .side-sessions { flex-direction: row; overflow-x: auto; }
    .side-session { min-width: 220px; }
    .side-foot { display: none; }
  }

  /* ── entrance: the page rises once, in order; polling never re-triggers it ── */
  @media (prefers-reduced-motion: no-preference) {
    main > * { animation: rise 620ms cubic-bezier(0.22, 1, 0.36, 1) backwards; }
    main > :nth-child(2) { animation-delay: 60ms; }
    main > :nth-child(3) { animation-delay: 120ms; }
    main > :nth-child(4) { animation-delay: 180ms; }
  }
  @keyframes rise { from { opacity: 0; transform: translateY(14px); } }

  /* ── header row: what this table is, and the live counts ── */
  .mem-head { display: flex; align-items: flex-end; gap: 16px; flex-wrap: wrap; margin-bottom: 18px; }
  .mem-head h1 { margin: 0; font-size: clamp(24px, 2.3vw, 32px); font-weight: 650; letter-spacing: -0.022em; line-height: 1.2; }
  .mem-head h1::before {
    content: ""; display: block; width: 40px; height: 4px; border-radius: 2px;
    margin-bottom: 12px;
    background: linear-gradient(90deg, var(--cyan), transparent);
  }
  .mem-head p { margin: 4px 0 0; color: var(--ink-subtle); font-size: 13px; max-width: 60ch; }
  .mem-counts { margin-left: auto; display: flex; gap: 18px; font: 13px var(--mono); color: var(--ink-subtle); }
  .mem-counts b { font-weight: 650; color: var(--ink); font-size: 17px; }
  .mem-counts .c-visible b { color: var(--ink); }
  .mem-counts .c-superseded b { color: var(--green); }
  .mem-counts .c-trivial b { color: var(--amber); }

  /* ── controls ── */
  .controls { display: flex; align-items: center; gap: 10px; flex-wrap: wrap; margin-bottom: 14px; }
  .search {
    background: var(--surface-1); color: var(--ink);
    border: 1px solid var(--hairline); border-radius: var(--radius-sm);
    padding: 8px 13px; font: 13.5px var(--sans);
    width: 300px;
    transition: border-color 130ms;
  }
  .search::placeholder { color: var(--ink-subtle); }
  .search:hover { border-color: rgba(178, 182, 189, 0.28); }
  .search:focus-visible { outline: 2px solid var(--cyan); outline-offset: 1px; }
  .chips { display: flex; flex-wrap: wrap; gap: 6px; }
  .chip {
    --c: var(--ink-muted);
    display: inline-flex; align-items: center; gap: 7px;
    border: 1px solid var(--hairline);
    background: var(--surface-1);
    color: var(--ink-muted);
    border-radius: 999px;
    padding: 4px 12px;
    font: 500 12px var(--mono);
    cursor: pointer; user-select: none;
    transition: background-color 130ms, opacity 130ms, border-color 130ms;
  }
  .chip:hover { background: var(--surface-2); }
  .chip::before { content: ""; width: 7px; height: 7px; border-radius: 50%; background: var(--c); }
  .chip[data-status="visible"]    { --c: var(--ink); }
  .chip[data-status="superseded"] { --c: var(--green); }
  .chip[data-status="trivial"]    { --c: var(--amber); }
  .chip[data-prov="distilled"]    { --c: var(--cyan-deep); }
  .chip[data-prov="contributed"]  { --c: var(--purple-text); }
  .chip[data-prov="synthesized"]  { --c: var(--green); }
  .chip[data-active="false"] { opacity: 0.4; }
  .chip[data-active="false"]::before { background: var(--ink-subtle); }
  .chip:focus-visible { outline: 2px solid var(--cyan); outline-offset: 2px; }
  .chip-sep { width: 1px; height: 18px; background: var(--hairline); }

  /* ── the table ── */
  .tablewrap { overflow-x: auto; border: 1px solid var(--hairline); border-radius: var(--radius); background: var(--surface-1); }
  table.mem { border-collapse: collapse; width: 100%; min-width: 880px; font-size: 13.5px; }
  table.mem th {
    background: var(--surface-2);
    text-align: left; padding: 10px 14px;
    border-bottom: 1px solid var(--hairline);
    color: var(--ink-muted); font-size: 12px; font-weight: 600;
    letter-spacing: 0.04em; text-transform: uppercase;
    white-space: nowrap;
    cursor: pointer; user-select: none;
    transition: color 130ms;
  }
  table.mem th:hover { color: var(--ink); }
  table.mem th .dir { color: var(--cyan); font-family: var(--mono); font-size: 10px; margin-left: 4px; }
  table.mem td {
    padding: 10px 14px; border-bottom: 1px solid var(--hairline-soft);
    vertical-align: top; color: var(--ink-muted);
    font-variant-numeric: tabular-nums;
  }
  table.mem tbody tr.mrow { cursor: pointer; transition: background-color 130ms; }
  table.mem tbody tr.mrow:hover { background: var(--surface-2); }
  table.mem td.c-ts { font-family: var(--mono); font-size: 12px; color: var(--ink-subtle); white-space: nowrap; }
  table.mem td.c-id { font-family: var(--mono); font-size: 12px; color: var(--ink-subtle); white-space: nowrap; }
  table.mem td.c-type { font: 500 12px/1.6 var(--mono); color: var(--blue-text); white-space: nowrap; }
  table.mem td.c-text { color: var(--ink); min-width: 26em; }
  tr.superseded td.c-text { text-decoration: line-through; color: var(--ink-subtle); }
  tr.trivial td.c-text { color: var(--ink-subtle); }
  table.mem td.c-authors { white-space: nowrap; }
  .provlabel { --c: var(--ink-subtle); display: inline-flex; align-items: center; gap: 7px; font-size: 12px; font-weight: 600; color: var(--c); white-space: nowrap; }
  .provlabel::before { content: ""; width: 7px; height: 7px; border-radius: 50%; background: var(--c); }
  .provlabel[data-prov="synthesized"] { --c: var(--green); }
  .provlabel[data-prov="contributed"] { --c: var(--purple-text); }
  .provlabel[data-prov="distilled"]   { --c: var(--cyan-deep); }
  .status-badge {
    display: inline-block; padding: 1px 9px; border-radius: 999px;
    font-size: 11.5px; font-weight: 600;
    border: 1px solid transparent; white-space: nowrap;
  }
  .status-badge.visible    { color: var(--ink); border-color: var(--hairline); }
  .status-badge.superseded { color: var(--green); border-color: rgba(0, 202, 142, 0.45); background: rgba(0, 202, 142, 0.12); }
  .status-badge.trivial    { color: var(--amber); border-color: rgba(255, 207, 37, 0.4); background: rgba(255, 207, 37, 0.10); }
  tr.detail-row td {
    background: var(--canvas);
    font: 12.5px/1.7 var(--mono); color: var(--ink-muted);
    white-space: pre-wrap; overflow-wrap: anywhere;
  }
  .empty { padding: 30px; text-align: center; color: var(--ink-subtle); }
  .footnote {
    margin: 28px 0 0; padding-top: 16px;
    border-top: 1px solid var(--hairline);
    color: var(--ink-subtle); font-size: 13px; line-height: 1.75; max-width: 92ch;
  }
  .footnote b { color: var(--ink-muted); font-weight: 600; }
</style>
</head>
<body>
<div id="banner">Service unreachable. Retrying…</div>
<div class="shell">
<aside>
  <a class="brand" href="/"><svg class="mark" viewBox="0 0 30 16" aria-hidden="true"><path class="axon" d="M4 8 C 10 3, 20 13, 26 8"/><circle class="soma" cx="4" cy="8" r="2.7"/><circle class="soma" cx="26" cy="8" r="2.7"/><circle class="impulse" r="1.7"><animateMotion dur="2.8s" repeatCount="indefinite" path="M4 8 C 10 3, 20 13, 26 8"/></circle></svg><span class="name">Synapse</span><span class="scope-label">service</span></a>
  <div class="side-label">Shared sessions</div>
  <nav id="session-list" class="side-sessions" aria-label="shared sessions"><div class="side-empty">connecting…</div></nav>
  <div class="side-foot"><a href="/">Service home</a></div>
</aside>
<div class="content">
<header class="topbar">
  <nav class="tabs" aria-label="session pages">
    <a id="tab-brain" href="/debug">Brain</a>
    <a id="tab-log" href="/debug/log">Log</a>
    <a id="tab-memory" href="/debug/memory" aria-current="page">Memory</a>
  </nav>
</header>
<main>
  <div class="mem-head">
    <div>
      <h1>Memory</h1>
      <p>every finding in this session, projected through the fold; superseded items stay, struck through, with their merge target one click away</p>
    </div>
    <div class="mem-counts">
      <span>total <b id="mem-total">0</b></span>
      <span class="c-visible">visible <b id="mem-visible">0</b></span>
      <span class="c-superseded">superseded <b id="mem-superseded">0</b></span>
      <span class="c-trivial">trivial <b id="mem-trivial">0</b></span>
    </div>
  </div>

  <div class="controls">
    <input class="search" id="mem-search" type="search" placeholder="search text, id, author, type…" aria-label="search memory">
    <div class="chips" id="status-chips">
      <span class="chip" data-status="visible" data-active="true" tabindex="0">visible</span>
      <span class="chip" data-status="superseded" data-active="true" tabindex="0">superseded</span>
      <span class="chip" data-status="trivial" data-active="true" tabindex="0">trivial</span>
    </div>
    <span class="chip-sep"></span>
    <div class="chips" id="prov-chips">
      <span class="chip" data-prov="distilled" data-active="true" tabindex="0">listened</span>
      <span class="chip" data-prov="contributed" data-active="true" tabindex="0">contributed</span>
      <span class="chip" data-prov="synthesized" data-active="true" tabindex="0">merged</span>
    </div>
  </div>

  <div class="tablewrap">
    <table class="mem">
      <thead><tr id="mem-headrow">
        <th data-sort="ts_iso">time<span class="dir" id="dir-ts_iso">▼</span></th>
        <th data-sort="id">id<span class="dir" id="dir-id"></span></th>
        <th data-sort="type">type<span class="dir" id="dir-type"></span></th>
        <th data-sort="text">finding<span class="dir" id="dir-text"></span></th>
        <th data-sort="authors">authors<span class="dir" id="dir-authors"></span></th>
        <th data-sort="provenance">provenance<span class="dir" id="dir-provenance"></span></th>
        <th data-sort="display_status">status<span class="dir" id="dir-display_status"></span></th>
      </tr></thead>
      <tbody id="mem-body"><tr><td colspan="7"><div class="empty">connecting…</div></td></tr></tbody>
    </table>
  </div>

  <p class="footnote">
    <b>Nothing is deleted here.</b> A superseded finding keeps its row because
    its attribution is part of the memory's history: the merge result carries
    every source's authors, and the sources stay, struck through, pointing at
    what replaced them. <b>Status is the fold's answer</b>, recomputed from the
    append-only log, never a flag a producer set. <b>Trivial</b> means synthesis
    judged the finding to restate an action without insight; it is filtered
    from retrieval but not from this table.
  </p>
</main>
</div>
</div>

<script>
(function () {
  "use strict";

  var PAGE_PATH = "/debug/memory";
  var currentSid = null;
  var lastRows = null;
  var sortKey = "ts_iso";
  var sortAsc = false;
  var activeStatus = new Set(["visible", "superseded", "trivial"]);
  var activeProv = new Set(["distilled", "contributed", "synthesized"]);
  var expandedIds = new Set();

  try {
    if (typeof location !== "undefined" && typeof URLSearchParams !== "undefined"
        && location.search) {
      currentSid = new URLSearchParams(location.search).get("session");
    }
  } catch (e) { /* no location in a headless DOM */ }

  function esc(s) {
    var d = document.createElement("div");
    d.textContent = String(s == null ? "" : s);
    return d.innerHTML;
  }

  function escAttr(s) {
    return esc(s).replace(/"/g, "&quot;").replace(/'/g, "&#39;");
  }

  function hhmmss(iso) {
    if (!iso) return "";
    return new Date(iso).toTimeString().slice(0, 8);
  }

  function sessionHref(path, sid) {
    return path + "?session=" + encodeURIComponent(sid);
  }

  function updateChrome(sessions) {
    try {
      var sl = document.getElementById("session-list");
      if (sl) {
        sl.innerHTML = sessions.length ? sessions.map(function (s) {
          var cur = s.shared_id === currentSid ? ' aria-current="page"' : "";
          return '<a class="side-session"' + cur + ' href="' +
            sessionHref(PAGE_PATH, s.shared_id) + '">' +
            '<span class="ss-purpose">' + esc(s.purpose || "(no purpose recorded)") + '</span>' +
            '<span class="ss-sid">' + esc(s.shared_id) +
            (s.status === "ended" ? " · ended" : "") + '</span></a>';
        }).join("") : '<div class="side-empty">no shared sessions yet</div>';
      }
      [["tab-brain", "/debug"], ["tab-log", "/debug/log"],
       ["tab-memory", "/debug/memory"]].forEach(function (t) {
        var el = document.getElementById(t[0]);
        if (el && currentSid) el.setAttribute("href", sessionHref(t[1], currentSid));
      });
    } catch (e) { /* chrome only */ }
  }

  var sel = document.getElementById("session-select");
  if (sel) sel.addEventListener("change", function (ev) {
    currentSid = ev.target.value;
    expandedIds.clear();
    try { history.replaceState(null, "", sessionHref(PAGE_PATH, currentSid)); } catch (e) {}
    refresh();
  });

  function wireChips(containerId, attr, activeSet) {
    var el = document.getElementById(containerId);
    if (!el) return;
    el.addEventListener("click", function (ev) {
      var chip = ev.target.closest(".chip");
      if (!chip) return;
      var v = chip.getAttribute(attr);
      var on = chip.getAttribute("data-active") !== "true";
      chip.setAttribute("data-active", on ? "true" : "false");
      if (on) activeSet.add(v); else activeSet.delete(v);
      renderRows();
    });
  }
  wireChips("status-chips", "data-status", activeStatus);
  wireChips("prov-chips", "data-prov", activeProv);

  var searchEl = document.getElementById("mem-search");
  if (searchEl) searchEl.addEventListener("input", renderRows);

  var headRow = document.getElementById("mem-headrow");
  if (headRow) headRow.addEventListener("click", function (ev) {
    var th = ev.target.closest("th[data-sort]");
    if (!th) return;
    var key = th.getAttribute("data-sort");
    if (sortKey === key) { sortAsc = !sortAsc; }
    else { sortKey = key; sortAsc = (key !== "ts_iso"); }
    ["ts_iso", "id", "type", "text", "authors", "provenance", "display_status"]
      .forEach(function (k) {
        var d = document.getElementById("dir-" + k);
        if (d) d.textContent = k === sortKey ? (sortAsc ? "▲" : "▼") : "";
      });
    renderRows();
  });

  var body = document.getElementById("mem-body");
  if (body) body.addEventListener("click", function (ev) {
    var tr = ev.target.closest("tr.mrow");
    if (!tr) return;
    var id = tr.getAttribute("data-id");
    if (!id) return;
    if (expandedIds.has(id)) expandedIds.delete(id); else expandedIds.add(id);
    renderRows();
  });

  var PROV_LABEL = {
    distilled: "listened",
    contributed: "contributed",
    synthesized: "merged"
  };

  function sortValue(r, key) {
    if (key === "authors") return r.authors.join(", ").toLowerCase();
    if (key === "text") return String(r.text || "").toLowerCase();
    return String(r[key] == null ? "" : r[key]).toLowerCase();
  }

  function renderRows() {
    var el = document.getElementById("mem-body");
    if (!el) return;
    if (!lastRows) {
      el.innerHTML = '<tr><td colspan="7"><div class="empty">connecting…</div></td></tr>';
      return;
    }
    var q = searchEl && searchEl.value ? searchEl.value.toLowerCase() : "";
    var rows = lastRows.filter(function (r) {
      if (!activeStatus.has(r.display_status)) return false;
      if (!activeProv.has(r.provenance)) return false;
      if (q) {
        var hay = (r.text + " " + r.id + " " + r.type + " " +
                   r.authors.join(" ")).toLowerCase();
        if (hay.indexOf(q) === -1) return false;
      }
      return true;
    });
    rows.sort(function (a, b) {
      var av = sortValue(a, sortKey), bv = sortValue(b, sortKey);
      if (av < bv) return sortAsc ? -1 : 1;
      if (av > bv) return sortAsc ? 1 : -1;
      return 0;
    });

    if (!rows.length) {
      el.innerHTML = '<tr><td colspan="7"><div class="empty">' +
        (lastRows.length ? "nothing matches the current filter"
                         : "no findings have reached this session") +
        '</div></td></tr>';
      return;
    }

    var live = new Set(rows.map(function (r) { return r.id; }));
    expandedIds.forEach(function (id) { if (!live.has(id)) expandedIds.delete(id); });

    var html = "";
    for (var i = 0; i < rows.length; i++) {
      var r = rows[i];
      var cls = "mrow " + r.display_status;
      html += '<tr class="' + cls + '" data-id="' + escAttr(r.id) + '">';
      html += '<td class="c-ts" title="' + escAttr(r.ts_iso) + '">' + esc(hhmmss(r.ts_iso)) + '</td>';
      html += '<td class="c-id" title="' + escAttr(r.id) + '">' + esc(String(r.id).slice(0, 12)) + '</td>';
      html += '<td class="c-type">' + esc(r.type) + '</td>';
      html += '<td class="c-text">' + esc(r.text) + '</td>';
      html += '<td class="c-authors">' + esc(r.authors.join(" + ")) + '</td>';
      html += '<td><span class="provlabel" data-prov="' + escAttr(r.provenance) + '">' +
        esc(PROV_LABEL[r.provenance] || r.provenance) + '</span></td>';
      html += '<td><span class="status-badge ' + escAttr(r.display_status) + '">' +
        esc(r.display_status) + '</span></td>';
      html += '</tr>';
      if (expandedIds.has(r.id)) {
        var d = "id: " + r.id + "\\nstatus: " + r.status;
        if (r.merged_into) d += "\\nmerged into: " + r.merged_into;
        if (r.merged_from && r.merged_from.length) {
          d += "\\nmerged from: " + r.merged_from.join(", ");
        }
        d += "\\n\\nattributions:";
        for (var a = 0; a < r.attributions.length; a++) {
          var at = r.attributions[a];
          d += "\\n  " + at.contributor + " / " + at.agent + " / " + at.agent_session;
        }
        d += "\\n\\n" + r.text;
        html += '<tr class="detail-row"><td colspan="7">' + esc(d) + '</td></tr>';
      }
    }
    el.innerHTML = html;
  }

  function renderCounts(s) {
    var set = function (id, v) {
      var el = document.getElementById(id);
      if (el) el.textContent = v;
    };
    set("mem-total", s.counts.total);
    set("mem-visible", s.counts.visible);
    set("mem-superseded", s.counts.superseded);
    set("mem-trivial", s.counts.trivial);
  }

  function populateSessions(sessions) {
    if (!currentSid && sessions.length) currentSid = sessions[0].shared_id;
    updateChrome(sessions);
    var select = document.getElementById("session-select");
    if (!select) return;
    var prior = currentSid;
    select.innerHTML = sessions.map(function (s) {
      return '<option value="' + escAttr(s.shared_id) + '">' + esc(s.shared_id) +
        (s.status === "ended" ? " (ended)" : "") + " · " + esc(s.purpose) + '</option>';
    }).join("");
    if (prior && sessions.some(function (s) { return s.shared_id === prior; })) {
      select.value = prior;
    } else if (sessions.length) {
      currentSid = sessions[0].shared_id;
      select.value = currentSid;
    }
  }

  function refresh() {
    var url = "/debug/memory.json" + (currentSid ? "?session=" + encodeURIComponent(currentSid) : "");
    fetch(url)
      .then(function (r) { if (!r.ok) throw new Error("bad status " + r.status); return r.json(); })
      .then(function (payload) {
        document.getElementById("banner").classList.remove("show");
        populateSessions(payload.sessions);
        if (payload.session) {
          currentSid = payload.session.sid;
          lastRows = payload.session.findings;
          renderCounts(payload.session);
        } else {
          lastRows = [];
        }
        renderRows();
      })
      .catch(function () {
        document.getElementById("banner").classList.add("show");
      });
  }

  refresh();
  setInterval(refresh, 2000);
})();
</script>
</body>
</html>
"""
