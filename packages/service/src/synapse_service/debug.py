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
  /* The teal side of the device boundary — teal = cloud/service, the worker
     page is the copper side. Sessions are the top-level entity: the sidebar
     lists them, the tabs are this session's subpages, and ?session= makes
     every view deep-linkable. Same standing rule as ever: NOT ONE external
     request. */
  :root {
    color-scheme: dark;
    --background: #0a1214;
    --card: #101b1e;
    --inset: #0a1214;
    --muted: #152528;
    --border: #22383d;
    --hairline: #1a2b2f;
    --foreground: #e9f1f1;
    --muted-foreground: #9fb5b6;
    --faint: #647a7d;
    --accent: #56c8cf;
    --accent-dim: #2a5f64;
    --k-finding: #9fb5b6;
    --k-merged: #71c07f;
    --k-trivial: #d3ab55;
    --k-topic: #7ea9db;
    --tag-llm: #56c8cf;
    --tag-query: #b49fd6;
    --tag-synthesis: #71c07f;
    /* Red, and the only red on the page: a query_failed means the retrieval
       backend is down and every answer the team is getting is a 503. */
    --tag-query-failed: #e5695f;
    --red: #e5695f;
    --radius: 10px;
    --sans: ui-sans-serif, system-ui, -apple-system, "Segoe UI", Roboto, "Helvetica Neue", Arial, sans-serif;
    --mono: ui-monospace, "SF Mono", "Cascadia Code", Menlo, Consolas, monospace;
  }
  * { box-sizing: border-box; }
  body {
    margin: 0;
    background: var(--background);
    color: var(--foreground);
    font: 14px/1.55 var(--sans);
    -webkit-font-smoothing: antialiased;
  }
  #banner {
    display: none;
    background: #2a1210; color: #f0a49b;
    border-bottom: 1px solid #4a201c;
    padding: 8px 24px; font-size: 12px; font-weight: 500;
  }
  #banner.show { display: block; }

  /* ── shell: sessions on the left, this session's pages on top ── */
  .shell { display: flex; min-height: 100dvh; }
  aside {
    width: 268px; flex-shrink: 0;
    border-right: 1px solid var(--hairline);
    padding: 14px 12px;
    display: flex; flex-direction: column; gap: 12px;
    position: sticky; top: 0; height: 100dvh; overflow-y: auto;
  }
  .brand { display: flex; align-items: center; gap: 8px; text-decoration: none; color: inherit; padding: 2px 6px 6px; }
  .brand .mark { width: 30px; height: 16px; overflow: visible; }
  .brand .mark .axon { fill: none; stroke: var(--accent-dim); stroke-width: 1.4; }
  .brand .mark .soma { fill: var(--accent); }
  .brand .mark .impulse { fill: var(--foreground); }
  @media (prefers-reduced-motion: reduce) { .brand .mark .impulse { display: none; } }
  .brand .name { font-size: 15px; font-weight: 600; letter-spacing: -0.01em; }
  .brand .scope-label { color: var(--muted-foreground); font-size: 13px; }
  .side-label {
    font: 600 11px var(--mono); letter-spacing: 0.1em; text-transform: uppercase;
    color: var(--faint); padding: 0 6px;
  }
  .side-sessions { display: flex; flex-direction: column; gap: 2px; }
  .side-session {
    display: block; padding: 8px 10px; border-radius: 8px;
    text-decoration: none; color: var(--muted-foreground);
    border: 1px solid transparent;
    transition: background-color 120ms;
  }
  .side-session:hover { background: var(--muted); color: var(--foreground); }
  .side-session[aria-current="page"] { background: var(--muted); border-color: var(--border); color: var(--foreground); }
  .side-session:focus-visible { outline: 2px solid var(--accent); outline-offset: 2px; }
  .side-session .ss-purpose { display: block; font-size: 13.5px; font-weight: 550; line-height: 1.35; overflow-wrap: anywhere; }
  .side-session .ss-sid { display: block; font: 11.5px var(--mono); color: var(--faint); margin-top: 3px; }
  .side-empty { color: var(--faint); font-size: 12px; padding: 8px 10px; }
  .side-foot { margin-top: auto; padding: 0 6px; }
  .side-foot a { color: var(--faint); font-size: 12px; text-decoration: none; }
  .side-foot a:hover { color: var(--foreground); }
  .content { flex: 1; min-width: 0; }
  .topbar {
    display: flex; align-items: center; gap: 14px; flex-wrap: wrap;
    padding: 10px 24px;
    border-bottom: 1px solid var(--hairline);
  }
  .tabs { display: flex; gap: 4px; }
  .tabs a {
    padding: 6px 14px; border-radius: 7px;
    font-size: 14px; font-weight: 500;
    color: var(--muted-foreground); text-decoration: none;
  }
  .tabs a:hover { color: var(--foreground); background: var(--muted); }
  .tabs a[aria-current="page"] { color: var(--foreground); background: var(--muted); }
  .tabs a:focus-visible { outline: 2px solid var(--accent); outline-offset: 2px; }
  main { padding: 24px 32px 64px; }
  @media (max-width: 900px) {
    .shell { flex-direction: column; }
    aside { position: static; width: auto; height: auto; border-right: none; border-bottom: 1px solid var(--hairline); }
    .side-sessions { flex-direction: row; overflow-x: auto; }
    .side-session { min-width: 220px; }
    .side-foot { display: none; }
  }

  /* ── the pipeline, as stat cards: ingest → fold → merge → topics → query ── */
  .stats { display: grid; grid-template-columns: 1fr 1.7fr 1fr 1fr 1fr; gap: 12px; margin-bottom: 16px; }
  .stat {
    background: var(--card);
    border: 1px solid var(--border);
    border-radius: var(--radius);
    padding: 14px 16px 12px;
    min-width: 0;
  }
  .stat-label { color: var(--muted-foreground); font-size: 13px; font-weight: 500; margin-bottom: 6px; }
  .stat-value {
    font: 600 26px/1.2 var(--mono);
    font-variant-numeric: tabular-nums;
    letter-spacing: -0.02em;
  }
  .stat-sub { color: var(--faint); font-size: 12px; margin-top: 4px; }
  .stat.fold {
    border-color: var(--accent-dim);
    background: linear-gradient(160deg, rgba(86, 200, 207, 0.08), rgba(86, 200, 207, 0) 55%), var(--card);
  }
  .stat.fold .stat-label { color: var(--accent); }
  .stat.fold .v-visible    { color: var(--foreground); }
  .stat.fold .v-superseded { color: var(--k-merged); }
  .stat.fold .v-trivial    { color: var(--k-trivial); }
  .stat.fold .sep { color: var(--faint); font-weight: 400; padding: 0 5px; }

  .topics { display: flex; flex-wrap: wrap; gap: 6px; margin-bottom: 16px; }
  .topic-chip {
    display: inline-flex; align-items: center; gap: 6px;
    background: var(--muted);
    border-radius: 6px; padding: 3px 9px;
    font-size: 12px; font-weight: 500; color: var(--muted-foreground);
  }
  .topic-chip b { color: var(--k-topic); font-weight: 600; font-family: var(--mono); }

  details.wm {
    background: var(--card); border: 1px solid var(--border);
    border-radius: var(--radius); padding: 12px 16px; margin-bottom: 8px;
  }
  details.wm summary { cursor: pointer; color: var(--muted-foreground); font-size: 12px; font-weight: 500; }
  details.wm summary:hover { color: var(--foreground); }
  details.wm summary:focus-visible { outline: 2px solid var(--accent); outline-offset: 2px; }
  details.wm .wm-body {
    margin-top: 10px; white-space: pre-wrap; overflow-wrap: anywhere;
    color: var(--muted-foreground); max-width: 72ch; line-height: 1.6;
  }

  .section-head { display: flex; align-items: flex-end; gap: 16px; flex-wrap: wrap; margin: 24px 0 10px; }
  .section-head h2 { margin: 0; font-size: 17px; font-weight: 600; letter-spacing: -0.01em; }
  .section-head p { margin: 2px 0 0; color: var(--faint); font-size: 13px; }
  .section-head .grow { flex: 1; }

  .search {
    background: transparent; color: var(--foreground);
    border: 1px solid var(--border); border-radius: 7px;
    padding: 6px 11px; font: 13px var(--sans);
    width: 240px;
  }
  .search::placeholder { color: var(--faint); }
  .search:focus-visible { outline: 2px solid var(--accent); outline-offset: 1px; }
  .orderbtn {
    background: transparent; color: var(--muted-foreground);
    border: 1px solid var(--border); border-radius: 7px;
    padding: 6px 12px; font: 500 12px var(--mono);
    cursor: pointer;
  }
  .orderbtn:hover { background: var(--muted); color: var(--foreground); }
  .orderbtn:focus-visible { outline: 2px solid var(--accent); outline-offset: 2px; }

  #log-tail, #feed {
    background: var(--card);
    border: 1px solid var(--border);
    border-radius: var(--radius);
    overflow: hidden;
  }
  .entry {
    --c: var(--muted-foreground);
    padding: 8px 16px 8px 14px;
    border-bottom: 1px solid var(--hairline);
    border-left: 2px solid var(--c);
    display: flex; gap: 12px; align-items: baseline;
    flex-wrap: wrap;
    transition: background-color 120ms;
  }
  .entry:last-child { border-bottom: none; }
  .entry[data-kind="FindingAppended"] { --c: transparent; }
  .entry[data-kind="Merged"]          { --c: var(--k-merged); background: rgba(113, 192, 127, 0.05); }
  .entry[data-kind="MarkedTrivial"]   { --c: var(--k-trivial); }
  .entry[data-kind="TopicAssigned"]   { --c: var(--k-topic); }
  .entry[data-kind="TopicSplit"]      { --c: var(--k-topic); }
  .entry[data-kind="SessionEnded"]    { --c: var(--red); }
  .entry[data-tag="llm"]       { --c: var(--tag-llm); }
  .entry[data-tag="query"]     { --c: var(--tag-query); }
  .entry[data-tag="synthesis"] { --c: var(--tag-synthesis); }
  .entry[data-tag="query_failed"] { --c: var(--tag-query-failed); }
  .entry .pos { color: var(--faint); flex-shrink: 0; width: 3.2em; text-align: right; font: 12px/1.6 var(--mono); }
  .entry .ts { color: var(--faint); flex-shrink: 0; font: 12px/1.6 var(--mono); }
  .entry .kind, .entry .tag {
    flex-shrink: 0;
    font: 500 12px/1.6 var(--mono);
    color: var(--c);
  }
  .entry .kind { width: 11em; }
  .entry .tag { width: 7em; }
  .entry[data-kind="FindingAppended"] .kind { color: var(--k-finding); }
  .entry[data-kind="FindingAppended"] .summary { color: var(--muted-foreground); }
  .entry[data-kind="Merged"] .summary { color: var(--foreground); font-weight: 500; }
  .entry .summary { flex: 1; overflow-wrap: anywhere; color: var(--muted-foreground); }
  /* structured activity columns */
  .entry .main { flex: 1; min-width: 16em; overflow-wrap: anywhere; color: var(--muted-foreground); }
  .entry[data-tag="llm"] .main { color: var(--foreground); }
  .entry .metrics { flex-shrink: 0; font: 12px/1.6 var(--mono); color: var(--faint); }
  .entry .okbadge {
    flex-shrink: 0;
    display: inline-block; padding: 0 7px; border-radius: 6px;
    font: 600 10px/1.7 var(--sans);
    border: 1px solid transparent;
  }
  .entry .okbadge.ok   { color: var(--k-merged); border-color: #274d38; }
  .entry .okbadge.fail { color: var(--red); border-color: #55231e; background: rgba(229, 105, 95, 0.08); }
  .entry[data-tag] { cursor: pointer; }
  .entry[data-tag]:hover { background: var(--muted); }
  /* One selector for both homes a .detail can have: re-parented INSIDE an
     llm entry by the script, or left as the entry's next SIBLING (query
     entries) -- the sibling case must not leak as bare visible text. */
  .detail {
    display: none;
    margin: 8px 0 4px;
    padding: 10px 12px;
    background: var(--inset);
    border: 1px solid var(--hairline);
    border-radius: 7px;
    font: 13px/1.65 var(--mono); color: var(--muted-foreground);
    white-space: pre-wrap; overflow-wrap: anywhere;
    flex-basis: 100%;
  }
  #feed > .detail { margin: 0 16px 10px; }
  .entry.expanded .detail,
  .entry.expanded + .detail { display: block; }
  .chips { display: flex; flex-wrap: wrap; gap: 6px; }
  .chip {
    --c: var(--muted-foreground);
    display: inline-flex; align-items: center; gap: 7px;
    border: 1px solid var(--border);
    background: var(--card);
    color: var(--muted-foreground);
    border-radius: 7px;
    padding: 4px 11px;
    font: 500 12px var(--mono);
    cursor: pointer; user-select: none;
    transition: background-color 120ms, opacity 120ms;
  }
  .chip:hover { background: var(--muted); }
  .chip::before {
    content: "";
    width: 7px; height: 7px; border-radius: 50%;
    background: var(--c);
  }
  .chip[data-tag="llm"]       { --c: var(--tag-llm); }
  .chip[data-tag="query"]     { --c: var(--tag-query); }
  .chip[data-tag="synthesis"] { --c: var(--tag-synthesis); }
  .chip[data-tag="query_failed"] { --c: var(--tag-query-failed); }
  .chip[data-kind] { --c: var(--k-finding); }
  .chip[data-kind="Merged"] { --c: var(--k-merged); }
  .chip[data-kind="MarkedTrivial"] { --c: var(--k-trivial); }
  .chip[data-kind*="Topic"] { --c: var(--k-topic); }
  .chip[data-kind="SessionEnded"] { --c: var(--red); }
  .chip[data-active="false"] { opacity: 0.4; }
  .chip[data-active="false"]::before { background: var(--faint); }
  .chip:focus-visible { outline: 2px solid var(--accent); outline-offset: 2px; }
  .empty { padding: 28px; text-align: center; color: var(--faint); }
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
  /* The teal side of the device boundary — teal = cloud/service, the worker
     page is the copper side. Sessions are the top-level entity: the sidebar
     lists them, the tabs are this session's subpages, and ?session= makes
     every view deep-linkable. Same standing rule as ever: NOT ONE external
     request. */
  :root {
    color-scheme: dark;
    --background: #0a1214;
    --card: #101b1e;
    --inset: #0a1214;
    --muted: #152528;
    --border: #22383d;
    --hairline: #1a2b2f;
    --foreground: #e9f1f1;
    --muted-foreground: #9fb5b6;
    --faint: #647a7d;
    --accent: #56c8cf;
    --accent-dim: #2a5f64;
    --k-merged: #71c07f;
    --k-trivial: #d3ab55;
    --k-topic: #7ea9db;
    --tag-query: #b49fd6;
    --red: #e5695f;
    --radius: 10px;
    --sans: ui-sans-serif, system-ui, -apple-system, "Segoe UI", Roboto, "Helvetica Neue", Arial, sans-serif;
    --mono: ui-monospace, "SF Mono", "Cascadia Code", Menlo, Consolas, monospace;
  }
  * { box-sizing: border-box; }
  body {
    margin: 0;
    background: var(--background);
    color: var(--foreground);
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
    background: #2a1210; color: #f0a49b;
    border-bottom: 1px solid #4a201c;
    padding: 8px 24px; font-size: 12px; font-weight: 500;
  }
  #banner.show { display: block; }

  /* ── shell: sessions on the left, this session's pages on top ── */
  .shell { display: flex; min-height: 100dvh; }
  aside {
    width: 268px; flex-shrink: 0;
    border-right: 1px solid var(--hairline);
    padding: 14px 12px;
    display: flex; flex-direction: column; gap: 12px;
    position: sticky; top: 0; height: 100dvh; overflow-y: auto;
  }
  .brand { display: flex; align-items: center; gap: 8px; text-decoration: none; color: inherit; padding: 2px 6px 6px; }
  .brand .mark { width: 30px; height: 16px; overflow: visible; }
  .brand .mark .axon { fill: none; stroke: var(--accent-dim); stroke-width: 1.4; }
  .brand .mark .soma { fill: var(--accent); }
  .brand .mark .impulse { fill: var(--foreground); }
  @media (prefers-reduced-motion: reduce) { .brand .mark .impulse { display: none; } }
  .brand .name { font-size: 15px; font-weight: 600; letter-spacing: -0.01em; }
  .brand .scope-label { color: var(--muted-foreground); font-size: 13px; }
  .side-label {
    font: 600 11px var(--mono); letter-spacing: 0.1em; text-transform: uppercase;
    color: var(--faint); padding: 0 6px;
  }
  .side-sessions { display: flex; flex-direction: column; gap: 2px; }
  .side-session {
    display: block; padding: 8px 10px; border-radius: 8px;
    text-decoration: none; color: var(--muted-foreground);
    border: 1px solid transparent;
    transition: background-color 120ms;
  }
  .side-session:hover { background: var(--muted); color: var(--foreground); }
  .side-session[aria-current="page"] { background: var(--muted); border-color: var(--border); color: var(--foreground); }
  .side-session:focus-visible { outline: 2px solid var(--accent); outline-offset: 2px; }
  .side-session .ss-purpose { display: block; font-size: 13.5px; font-weight: 550; line-height: 1.35; overflow-wrap: anywhere; }
  .side-session .ss-sid { display: block; font: 11.5px var(--mono); color: var(--faint); margin-top: 3px; }
  .side-empty { color: var(--faint); font-size: 12px; padding: 8px 10px; }
  .side-foot { margin-top: auto; padding: 0 6px; }
  .side-foot a { color: var(--faint); font-size: 12px; text-decoration: none; }
  .side-foot a:hover { color: var(--foreground); }
  .content { flex: 1; min-width: 0; }
  .topbar {
    display: flex; align-items: center; gap: 14px; flex-wrap: wrap;
    padding: 10px 24px;
    border-bottom: 1px solid var(--hairline);
  }
  .tabs { display: flex; gap: 4px; }
  .tabs a {
    padding: 6px 14px; border-radius: 7px;
    font-size: 14px; font-weight: 500;
    color: var(--muted-foreground); text-decoration: none;
  }
  .tabs a:hover { color: var(--foreground); background: var(--muted); }
  .tabs a[aria-current="page"] { color: var(--foreground); background: var(--muted); }
  .tabs a:focus-visible { outline: 2px solid var(--accent); outline-offset: 2px; }
  main { padding: 24px 32px 64px; }
  @media (max-width: 900px) {
    .shell { flex-direction: column; }
    aside { position: static; width: auto; height: auto; border-right: none; border-bottom: 1px solid var(--hairline); }
    .side-sessions { flex-direction: row; overflow-x: auto; }
    .side-session { min-width: 220px; }
    .side-foot { display: none; }
  }

  .section-head { margin: 26px 0 10px; }
  .section-head h2 { margin: 0; font-size: 17px; font-weight: 600; letter-spacing: -0.01em; }
  .section-head p { margin: 2px 0 0; color: var(--faint); font-size: 13px; }
  .note { color: var(--faint); font-weight: 400; font-size: 11px; }

  /* ── identity strip: what this brain is for ── */
  .identity {
    border-left: 2px solid var(--accent-dim);
    padding: 2px 0 2px 16px;
    margin-bottom: 20px;
  }
  .identity-label { color: var(--faint); font-size: 11px; font-weight: 500; margin-bottom: 4px; }
  .identity .purpose {
    font-size: 23px; line-height: 1.4; font-weight: 600;
    letter-spacing: -0.01em;
    color: var(--foreground); max-width: 68ch;
  }
  .identity .ident { color: var(--faint); font: 12px/1.7 var(--mono); margin-top: 6px; }
  .identity .ident b { color: var(--muted-foreground); font-weight: 500; }
  .pill {
    display: inline-block; padding: 1px 8px; border-radius: 6px;
    font: 600 10px/1.6 var(--sans);
    border: 1px solid;
  }
  .pill.active { color: var(--k-merged); border-color: #274d38; background: rgba(113, 192, 127, 0.08); }
  .pill.ended  { color: var(--red);     border-color: #55231e; background: rgba(229, 105, 95, 0.08); }

  /* ── vitals as stat cards ── */
  .stats { display: grid; grid-template-columns: 1fr 1fr 1.7fr 1fr 1fr; gap: 12px; margin-bottom: 18px; }
  .stat {
    background: var(--card);
    border: 1px solid var(--border);
    border-radius: var(--radius);
    padding: 14px 16px 12px;
    min-width: 0;
  }
  .stat-label { color: var(--muted-foreground); font-size: 13px; font-weight: 500; margin-bottom: 6px; }
  .stat-value {
    font: 600 26px/1.2 var(--mono);
    font-variant-numeric: tabular-nums;
    letter-spacing: -0.02em;
  }
  .stat-sub { color: var(--faint); font-size: 12px; margin-top: 4px; }
  .stat.fold {
    border-color: var(--accent-dim);
    background: linear-gradient(160deg, rgba(86, 200, 207, 0.08), rgba(86, 200, 207, 0) 55%), var(--card);
  }
  .stat.fold .stat-label { color: var(--accent); }
  .stat.fold .v-visible    { color: var(--foreground); }
  .stat.fold .v-superseded { color: var(--k-merged); }
  .stat.fold .v-trivial    { color: var(--k-trivial); }
  .stat.fold .sep { color: var(--faint); font-weight: 400; padding: 0 5px; }
  /* The synthesis key's three states. "unknown" is dim, not green: no
     headers seen is not the same claim as headroom seen. */
  #stat-ratelimit[data-state="unknown"]   { color: var(--faint); }
  #stat-ratelimit[data-state="ok"]        { color: var(--k-merged); font-size: 21px; }
  #stat-ratelimit[data-state="throttled"] { color: var(--red); font-size: 21px; }

  /* ── working memory: the hero card ── */
  .panel {
    background: var(--card); border: 1px solid var(--border);
    border-radius: var(--radius); overflow: hidden;
  }
  .panel.wm { border-color: var(--accent-dim); }
  .panel-head {
    display: flex; align-items: baseline; justify-content: space-between;
    gap: 12px; flex-wrap: wrap;
    padding: 12px 16px; border-bottom: 1px solid var(--hairline);
  }
  .panel-head h2 { margin: 0; font-size: 14px; font-weight: 600; letter-spacing: -0.01em; }
  .panel-head .meta { color: var(--faint); font: 11px var(--mono); }
  .wm-body {
    padding: 14px 16px; white-space: pre-wrap; overflow-wrap: anywhere;
    color: var(--foreground); max-width: 74ch; line-height: 1.65;
  }
  .wm-body.empty-note { color: var(--faint); font-style: italic; }
  .rev-head {
    padding: 8px 16px; border-top: 1px solid var(--hairline);
    background: var(--muted);
    color: var(--muted-foreground); font-size: 11px; font-weight: 500;
  }
  .rev {
    display: flex; gap: 14px; align-items: baseline; flex-wrap: wrap;
    padding: 7px 16px; border-bottom: 1px solid var(--hairline);
    cursor: pointer;
    font: 13px/1.6 var(--mono);
    transition: background-color 120ms;
  }
  .rev:hover { background: var(--muted); }
  .rev:last-child { border-bottom: none; }
  .rev .v { color: var(--accent); width: 3em; flex-shrink: 0; }
  .rev .ts { color: var(--faint); font-size: 11px; width: 7em; flex-shrink: 0; }
  .rev .w { color: var(--muted-foreground); width: 5em; flex-shrink: 0; }
  .rev .d { width: 5em; flex-shrink: 0; }
  .rev .d.up { color: var(--k-merged); }
  .rev .d.down { color: var(--k-trivial); }
  .rev .d.unk { color: var(--faint); }
  .rev .caret { color: var(--faint); margin-left: auto; font-size: 11px; }
  .rev .full {
    display: none; flex-basis: 100%;
    margin: 8px 0 4px; padding: 10px 12px;
    background: var(--inset); border: 1px solid var(--hairline); border-radius: 7px;
    color: var(--muted-foreground); white-space: pre-wrap; overflow-wrap: anywhere;
    font: 12px/1.6 var(--sans);
  }
  .rev.expanded .full { display: block; }
  .rev-note { padding: 12px 16px; color: var(--faint); font-size: 12px; }

  /* ── participants ── */
  .tablewrap { overflow-x: auto; border: 1px solid var(--border); border-radius: var(--radius); background: var(--card); }
  table.roster { border-collapse: collapse; width: 100%; min-width: 820px; font-size: 13.5px; }
  table.roster th {
    text-align: left; padding: 9px 12px;
    border-bottom: 1px solid var(--border);
    color: var(--muted-foreground); font-size: 13px; font-weight: 500;
    white-space: nowrap;
  }
  table.roster td {
    padding: 9px 12px; border-bottom: 1px solid var(--hairline);
    vertical-align: top; color: var(--muted-foreground);
    font-variant-numeric: tabular-nums;
  }
  table.roster tbody tr { transition: background-color 120ms; }
  table.roster tbody tr:hover { background: var(--muted); }
  table.roster tr:last-child td { border-bottom: none; }
  table.roster td.who { color: var(--foreground); font-weight: 500; white-space: nowrap; }
  table.roster td.num { text-align: right; font-family: var(--mono); }
  .adot {
    display: inline-block; width: 7px; height: 7px; border-radius: 50%;
    margin-right: 8px; background: var(--faint);
  }
  .adot.hot  { background: var(--k-merged); animation: adot-pulse 2s ease-in-out infinite; }
  @keyframes adot-pulse {
    0%, 100% { box-shadow: 0 0 0 0 rgba(113, 192, 127, 0.45); }
    50% { box-shadow: 0 0 0 4px rgba(113, 192, 127, 0); }
  }
  @media (prefers-reduced-motion: reduce) { .adot.hot { animation: none; } }
  .adot.warm { background: var(--k-trivial); }
  .adot.cold { background: var(--faint); }
  .adot.none { background: transparent; border: 1px solid var(--faint); }
  .state {
    display: inline-block; padding: 1px 8px; border-radius: 6px;
    font-size: 11.5px; font-weight: 600;
    border: 1px solid transparent;
  }
  .state.active    { color: var(--k-merged); border-color: #274d38; background: rgba(113, 192, 127, 0.08); }
  .state.listening { color: var(--tag-query); border-color: #46395c; background: rgba(180, 159, 214, 0.08); }
  .state.left      { color: var(--muted-foreground); border-color: var(--border); }
  /* The honest unknown, dimmest of the four and dotted-underlined so it
     reads as "there is a caveat here" rather than as a verdict. */
  .state.unregistered {
    color: var(--faint); padding: 0; border: none; border-radius: 0;
    border-bottom: 1px dotted var(--faint); cursor: help; font-weight: 500;
  }
  .scope { color: var(--faint); font-size: 11px; }
  .none { color: var(--faint); }

  /* ── latest into memory ── */
  #recent { background: var(--card); border: 1px solid var(--border); border-radius: var(--radius); overflow: hidden; }
  .row {
    --c: var(--faint);
    display: flex; gap: 12px; align-items: baseline; flex-wrap: wrap;
    padding: 9px 16px 9px 14px;
    border-bottom: 1px solid var(--hairline);
    border-left: 2px solid var(--c);
    cursor: pointer;
    transition: background-color 120ms;
  }
  .row:hover { background: var(--muted); }
  .row:last-child { border-bottom: none; }
  .row[data-prov="synthesized"] { --c: var(--k-merged); }
  .row[data-prov="contributed"] { --c: var(--tag-query); }
  .row[data-prov="distilled"]   { --c: var(--accent-dim); }
  .row .ts { color: var(--faint); font: 12px/1.6 var(--mono); flex-shrink: 0; width: 6em; }
  .row .who { color: var(--foreground); font-weight: 500; flex-shrink: 0; width: 12em; overflow-wrap: anywhere; }
  .row .type {
    flex-shrink: 0; width: 9.5em;
    font: 500 12px/1.6 var(--mono);
    color: var(--k-topic);
  }
  .row .text { flex: 1; min-width: 14em; overflow-wrap: anywhere; color: var(--muted-foreground); }
  .row .prov {
    flex-shrink: 0; margin-left: auto;
    display: inline-flex; align-items: center; gap: 7px;
    font-size: 11px; font-weight: 500; color: var(--c);
  }
  .row .prov::before {
    content: ""; width: 7px; height: 7px; border-radius: 50%;
    background: var(--c);
  }
  .row.tombstone .text { text-decoration: line-through; color: var(--faint); }
  .row .full {
    display: none; flex-basis: 100%;
    margin: 8px 0 2px; padding: 10px 12px;
    background: var(--inset); border: 1px solid var(--hairline); border-radius: 7px;
    color: var(--muted-foreground); white-space: pre-wrap; overflow-wrap: anywhere;
    font: 11.5px/1.6 var(--mono);
  }
  .row.expanded .full { display: block; }

  .empty { padding: 28px; text-align: center; color: var(--faint); }
  .footnote {
    margin: 28px 0 0; padding-top: 16px;
    border-top: 1px solid var(--border);
    color: var(--faint); font-size: 13px; line-height: 1.7; max-width: 92ch;
  }
  .footnote b { color: var(--muted-foreground); font-weight: 500; }
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
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<link rel="icon" href="data:,">
<title>Synapse · service</title>
<style>
  /* The front door, sized for a demo screen: the network IS the hero. Teal
     is the cloud side of the device boundary, copper is every machine at the
     edge. Same standing rule as every page on this listener: NOT ONE
     external request. */
  :root {
    color-scheme: dark;
    --background: #0a1214;
    --card: #101b1e;
    --inset: #0a1214;
    --muted: #152528;
    --border: #22383d;
    --hairline: #1a2b2f;
    --foreground: #e9f1f1;
    --muted-foreground: #9fb5b6;
    --faint: #647a7d;
    --accent: #56c8cf;
    --accent-dim: #2a5f64;
    --copper: #cf9163;
    --copper-dim: #7d4f31;
    --k-merged: #71c07f;
    --red: #e5695f;
    --radius: 10px;
    --sans: ui-sans-serif, system-ui, -apple-system, "Segoe UI", Roboto, "Helvetica Neue", Arial, sans-serif;
    --mono: ui-monospace, "SF Mono", "Cascadia Code", Menlo, Consolas, monospace;
  }
  * { box-sizing: border-box; }
  body {
    margin: 0;
    background:
      radial-gradient(1200px 520px at 50% -10%, rgba(86, 200, 207, 0.09), transparent),
      radial-gradient(800px 400px at 8% 30%, rgba(207, 145, 99, 0.05), transparent),
      radial-gradient(800px 400px at 92% 30%, rgba(207, 145, 99, 0.05), transparent),
      var(--background);
    color: var(--foreground);
    font: 15px/1.6 var(--sans);
    -webkit-font-smoothing: antialiased;
  }
  #banner {
    display: none;
    background: #2a1210; color: #f0a49b;
    border-bottom: 1px solid #4a201c;
    padding: 8px 24px; font-size: 13px; font-weight: 500;
  }
  #banner.show { display: block; }
  header {
    display: flex; align-items: center; gap: 20px; flex-wrap: wrap;
    min-height: 60px;
    padding: 10px 32px;
    border-bottom: 1px solid var(--hairline);
  }
  .brand { display: flex; align-items: center; gap: 8px; }
  .brand .mark { width: 30px; height: 16px; overflow: visible; }
  .brand .mark .axon { fill: none; stroke: var(--accent-dim); stroke-width: 1.4; }
  .brand .mark .soma { fill: var(--accent); }
  .brand .mark .impulse { fill: var(--foreground); }
  .brand .name { font-size: 16px; font-weight: 600; letter-spacing: -0.01em; }
  .brand .scope-label { color: var(--muted-foreground); font-size: 14px; }
  nav.pages { display: flex; gap: 4px; margin-right: auto; }
  nav.pages a {
    padding: 6px 14px; border-radius: 7px;
    font-size: 14px; font-weight: 500;
    color: var(--muted-foreground); text-decoration: none;
  }
  nav.pages a:hover { color: var(--foreground); background: var(--muted); }
  nav.pages a[aria-current="page"] { color: var(--foreground); background: var(--muted); }
  nav.pages a:focus-visible { outline: 2px solid var(--accent); outline-offset: 2px; }
  main { padding: 56px 40px 80px; max-width: 1460px; margin: 0 auto; }

  /* ── hero: a poster, not a strip ── */
  .hero { text-align: center; }
  .hero h1 {
    margin: 0 auto 18px;
    font-size: clamp(40px, 4.6vw, 66px);
    line-height: 1.06; font-weight: 680;
    letter-spacing: -0.032em;
    max-width: 18ch;
    text-wrap: balance;
  }
  .hero .tagline {
    margin: 0 auto 28px;
    color: var(--muted-foreground); font-size: 19px; line-height: 1.55;
    max-width: 56ch;
  }
  .cta-row { display: flex; align-items: center; justify-content: center; gap: 12px; flex-wrap: wrap; }
  .btn {
    display: inline-block; padding: 12px 26px;
    border-radius: 9px; font-size: 15.5px; font-weight: 600;
    text-decoration: none;
    transition: transform 120ms, background-color 120ms;
  }
  .btn:active { transform: translateY(1px); }
  .btn.primary { background: var(--accent); color: #062023; }
  .btn.primary:hover { background: #6fd3d9; }
  .btn.ghost { color: var(--foreground); border: 1px solid var(--border); }
  .btn.ghost:hover { background: var(--muted); }
  .btn:focus-visible { outline: 2px solid var(--accent); outline-offset: 2px; }
  .live {
    display: flex; align-items: center; justify-content: center; gap: 10px;
    margin: 26px 0 0;
    color: var(--muted-foreground); font: 15px var(--mono);
  }
  .live-dot { width: 10px; height: 10px; border-radius: 50%; background: var(--faint); }
  .live-dot.up { background: var(--k-merged); animation: live-pulse 2s ease-in-out infinite; }
  .live-dot.down { background: var(--red); animation: none; }
  @keyframes live-pulse {
    0%, 100% { box-shadow: 0 0 0 0 rgba(113, 192, 127, 0.45); }
    50% { box-shadow: 0 0 0 6px rgba(113, 192, 127, 0); }
  }

  /* ── the network: many machines, one shared brain ── */
  .net { margin: 30px auto 0; }
  .net svg { width: 100%; height: auto; display: block; }
  .net .axon { fill: none; stroke: var(--accent-dim); stroke-width: 1.6; opacity: 0.85; }
  .net .soma-edge { fill: var(--copper); }
  .net .halo-edge { fill: none; stroke: var(--copper-dim); stroke-width: 1.2; opacity: 0.6; }
  .net .soma-core { fill: var(--accent); }
  .net .halo-core { fill: none; stroke: var(--accent-dim); stroke-width: 1.4; }
  .net .ring { fill: none; stroke: var(--accent); stroke-width: 1.2; }
  .net .impulse { fill: var(--foreground); }
  .net .impulse.back { fill: var(--accent); }
  .net text { font: 600 14px var(--mono); letter-spacing: 0.06em; fill: var(--foreground); }
  .net text.sub { font-weight: 400; font-size: 12.5px; letter-spacing: 0.02em; fill: var(--faint); }
  .net text.core-label { fill: var(--accent); letter-spacing: 0.14em; }
  .net-caption {
    margin: 10px 0 0; text-align: center;
    color: var(--faint); font-size: 14px;
  }
  @media (prefers-reduced-motion: reduce) {
    .impulse { display: none; }
    .ring { display: none; }
    .live-dot.up { animation: none; }
  }

  /* ── live sessions ── */
  h2 { margin: 64px 0 6px; font-size: 24px; font-weight: 650; letter-spacing: -0.02em; }
  .sub { color: var(--faint); font-size: 14px; margin: 0 0 20px; }
  #sessions { display: grid; grid-template-columns: repeat(auto-fill, minmax(360px, 1fr)); gap: 14px; }
  .scard {
    display: flex; flex-direction: column; gap: 12px;
    background: var(--card);
    border: 1px solid var(--border);
    border-radius: var(--radius);
    padding: 20px;
    text-decoration: none; color: inherit;
    transition: border-color 120ms, transform 120ms;
  }
  .scard:hover { border-color: var(--accent-dim); transform: translateY(-1px); }
  .scard:focus-visible { outline: 2px solid var(--accent); outline-offset: 2px; }
  .scard-purpose { font-weight: 600; font-size: 16.5px; line-height: 1.4; }
  .scard-meta { display: flex; align-items: center; justify-content: space-between; gap: 10px; }
  .scard-meta .sid { font: 12.5px var(--mono); color: var(--faint); overflow-wrap: anywhere; }
  .pill {
    display: inline-block; padding: 2px 10px; border-radius: 6px;
    font: 600 11.5px/1.7 var(--sans);
    border: 1px solid; flex-shrink: 0;
  }
  .pill.active { color: var(--k-merged); border-color: #274d38; background: rgba(113, 192, 127, 0.08); }
  .pill.ended  { color: var(--red);     border-color: #55231e; background: rgba(229, 105, 95, 0.08); }
  .empty {
    grid-column: 1 / -1;
    padding: 32px; text-align: center; color: var(--faint); font-size: 14px;
    background: var(--card); border: 1px solid var(--border); border-radius: var(--radius);
  }

  /* ── the problem: two failure modes, one anecdote ── */
  .why { display: grid; grid-template-columns: 1fr 1fr; gap: 14px; }
  .why-card {
    background: var(--card);
    border: 1px solid var(--border);
    border-radius: var(--radius);
    padding: 22px;
  }
  .why-card h3 { margin: 0 0 8px; font-size: 17px; font-weight: 650; letter-spacing: -0.01em; }
  .why-card p { margin: 0; color: var(--muted-foreground); font-size: 15px; line-height: 1.65; }
  .anecdote {
    margin: 14px 0 0; padding: 16px 20px;
    border-left: 2px solid var(--copper-dim);
    color: var(--muted-foreground); font-size: 15px; line-height: 1.6;
  }
  .anecdote b { color: var(--foreground); font-weight: 600; }

  /* ── the pipeline: four real stages ── */
  .pipe { display: grid; grid-template-columns: 1fr 1fr 1fr 1fr; gap: 14px; }
  .pipe-card {
    background: var(--card);
    border: 1px solid var(--border);
    border-radius: var(--radius);
    padding: 20px;
  }
  .pipe-card .k { font: 600 14px var(--mono); letter-spacing: 0.1em; margin-bottom: 4px; }
  .pipe-card .where { font-size: 12.5px; color: var(--faint); margin-bottom: 10px; }
  .pipe-card:nth-child(1) .k { color: var(--muted-foreground); }
  .pipe-card:nth-child(2) .k { color: var(--copper); }
  .pipe-card:nth-child(3) .k { color: var(--accent); }
  .pipe-card:nth-child(4) .k { color: var(--k-merged); }
  .pipe-card p { margin: 0; color: var(--muted-foreground); font-size: 14.5px; line-height: 1.6; }
  .pipe-note { margin: 14px 0 0; color: var(--faint); font-size: 14px; text-align: center; }

  /* ── measured, not claimed ── */
  .band { display: grid; grid-template-columns: 1fr 1fr 1fr 1fr; gap: 14px; }
  .band-card {
    background: var(--card);
    border: 1px solid var(--border);
    border-radius: var(--radius);
    padding: 20px;
  }
  .band-card .num {
    font: 600 30px/1.15 var(--mono);
    font-variant-numeric: tabular-nums;
    letter-spacing: -0.02em;
    color: var(--foreground);
    margin-bottom: 6px;
  }
  .band-card .num small { font-size: 17px; color: var(--muted-foreground); font-weight: 500; }
  .band-card .what { font-size: 14px; color: var(--muted-foreground); line-height: 1.55; }
  .band-card.hero-stat { border-color: var(--accent-dim); }
  .band-card.hero-stat .num { color: var(--accent); }
  .band-note { margin: 14px 0 0; color: var(--faint); font-size: 14px; line-height: 1.65; max-width: 100ch; }
  .band-note b { color: var(--muted-foreground); font-weight: 500; }

  /* ── division of labor ── */
  .split { display: grid; grid-template-columns: 1fr 1fr; gap: 14px; }
  .split-card {
    background: var(--card);
    border: 1px solid var(--border);
    border-top: 2px solid var(--copper);
    border-radius: var(--radius);
    padding: 22px;
  }
  .split-card.cloud { border-top-color: var(--accent); }
  .split-card h3 { margin: 0 0 4px; font-size: 17px; font-weight: 650; }
  .split-card .where { font: 12.5px var(--mono); color: var(--faint); margin-bottom: 10px; }
  .split-card p { margin: 0; color: var(--muted-foreground); font-size: 15px; line-height: 1.65; }
  .versatile {
    margin: 14px 0 0; padding: 14px 20px;
    background: var(--muted); border-radius: 8px;
    color: var(--muted-foreground); font-size: 15px; text-align: center;
  }
  .versatile b { color: var(--foreground); font-weight: 600; }

  /* ── use cases ── */
  .cases { display: grid; grid-template-columns: 1fr 1fr 1fr; gap: 14px; }
  .case {
    background: var(--card);
    border: 1px solid var(--border);
    border-radius: var(--radius);
    padding: 18px 20px;
  }
  .case b { display: block; font-size: 15.5px; font-weight: 650; margin-bottom: 4px; }
  .case span { color: var(--muted-foreground); font-size: 14px; line-height: 1.55; }

  /* ── the moment it works ── */
  .aha {
    border-left: 2px solid var(--accent-dim);
    padding: 6px 0 6px 26px;
    margin: 0;
  }
  .aha .setup { color: var(--muted-foreground); font-size: 17px; margin: 0 0 12px; max-width: 60ch; }
  .aha .quote {
    font-size: clamp(26px, 2.6vw, 36px); font-weight: 650; letter-spacing: -0.02em;
    color: var(--foreground); margin: 0 0 12px;
  }
  .aha .quote .said { color: var(--accent); }
  .aha .receipt { color: var(--faint); font-size: 14.5px; margin: 0; }

  /* ── runs anywhere ── */
  .deploy { display: grid; grid-template-columns: 1.1fr 1fr; gap: 14px; align-items: stretch; }
  .term {
    background: var(--inset);
    border: 1px solid var(--border);
    border-radius: var(--radius);
    padding: 20px 22px;
    font: 14px/1.9 var(--mono);
    color: var(--muted-foreground);
    overflow-x: auto;
  }
  .term .p { color: var(--accent); user-select: none; }
  .term .c { color: var(--faint); }
  .providers {
    background: var(--card);
    border: 1px solid var(--border);
    border-radius: var(--radius);
    padding: 20px 22px;
  }
  .providers h3 { margin: 0 0 8px; font-size: 16px; font-weight: 650; }
  .providers p { margin: 0 0 12px; color: var(--muted-foreground); font-size: 14.5px; line-height: 1.6; }
  .plugs { display: flex; flex-wrap: wrap; gap: 8px; }
  .plug {
    padding: 4px 12px; border-radius: 7px;
    border: 1px solid var(--border);
    font: 500 13px var(--mono); color: var(--muted-foreground);
  }
  .plug.live { border-color: var(--accent-dim); color: var(--accent); }

  @media (max-width: 1000px) {
    .pipe, .band { grid-template-columns: 1fr 1fr; }
    .cases { grid-template-columns: 1fr 1fr; }
  }
  @media (max-width: 700px) {
    .why, .pipe, .band, .split, .cases, .deploy { grid-template-columns: 1fr; }
    .net text { font-size: 17px; }
    .net text.sub { display: none; }
  }

  .footer {
    margin-top: 72px; padding-top: 20px;
    border-top: 1px solid var(--hairline);
    color: var(--faint); font-size: 14px; line-height: 1.7; max-width: 90ch;
  }
  .footer b { color: var(--muted-foreground); font-weight: 500; }
</style>
</head>
<body>
<div id="banner">Service unreachable. Retrying…</div>
<header>
  <div class="brand"><svg class="mark" viewBox="0 0 30 16" aria-hidden="true"><path class="axon" d="M4 8 C 10 3, 20 13, 26 8"/><circle class="soma" cx="4" cy="8" r="2.7"/><circle class="soma" cx="26" cy="8" r="2.7"/><circle class="impulse" r="1.7"><animateMotion dur="2.8s" repeatCount="indefinite" path="M4 8 C 10 3, 20 13, 26 8"/></circle></svg><span class="name">Synapse</span><span class="scope-label">service</span></div>
  <nav class="pages" aria-label="debug pages">
    <a href="/" aria-current="page">Home</a>
    <a href="/debug">Brain</a>
    <a href="/debug/log">Log</a>
    <a href="/debug/memory">Memory</a>
  </nav>
</header>
<main>
  <section class="hero">
    <h1>The shared brain for your agent team.</h1>
    <p class="tagline">Every machine distils what its agent learns. The service folds it into one memory. Every agent on the team can ask, and nothing arrives unprompted.</p>
    <div class="cta-row">
      <a class="btn primary" href="/debug">Open the brain</a>
      <a class="btn ghost" href="/debug/log">Tail the log</a>
      <a class="btn ghost" href="/debug/memory">Browse the memory</a>
    </div>
    <div class="live"><span class="live-dot" id="live-dot"></span><span id="live-text">connecting…</span></div>
  </section>

  <div class="net" aria-label="four machines, each running an agent and an edge worker, exchanging findings and answers with the shared Synapse service">
    <svg viewBox="0 0 1200 400">
      <path class="axon" d="M150 84 C 320 104, 440 160, 584 202"/>
      <path class="axon" d="M120 300 C 300 292, 450 252, 584 218"/>
      <path class="axon" d="M1050 84 C 880 104, 760 160, 616 202"/>
      <path class="axon" d="M1080 300 C 900 292, 750 252, 616 218"/>

      <circle class="halo-edge" cx="150" cy="84" r="15"/>
      <circle class="soma-edge" cx="150" cy="84" r="8"/>
      <circle class="halo-edge" cx="120" cy="300" r="15"/>
      <circle class="soma-edge" cx="120" cy="300" r="8"/>
      <circle class="halo-edge" cx="1050" cy="84" r="15"/>
      <circle class="soma-edge" cx="1050" cy="84" r="8"/>
      <circle class="halo-edge" cx="1080" cy="300" r="15"/>
      <circle class="soma-edge" cx="1080" cy="300" r="8"/>

      <circle class="ring" r="22" cx="600" cy="210" opacity="0">
        <animate attributeName="r" values="20;56" dur="3.2s" repeatCount="indefinite"/>
        <animate attributeName="opacity" values="0.5;0" dur="3.2s" repeatCount="indefinite"/>
      </circle>
      <circle class="halo-core" cx="600" cy="210" r="27"/>
      <circle class="soma-core" cx="600" cy="210" r="15"/>

      <circle class="impulse" r="3.6"><animateMotion dur="3.4s" repeatCount="indefinite" path="M150 84 C 320 104, 440 160, 584 202"/></circle>
      <circle class="impulse" r="3.6"><animateMotion dur="4.1s" begin="-1.3s" repeatCount="indefinite" path="M120 300 C 300 292, 450 252, 584 218"/></circle>
      <circle class="impulse" r="3.6"><animateMotion dur="3.8s" begin="-2.2s" repeatCount="indefinite" path="M1050 84 C 880 104, 760 160, 616 202"/></circle>
      <circle class="impulse" r="3.6"><animateMotion dur="4.4s" begin="-0.6s" repeatCount="indefinite" path="M1080 300 C 900 292, 750 252, 616 218"/></circle>

      <circle class="impulse back" r="3.2"><animateMotion dur="4.6s" begin="-2.9s" repeatCount="indefinite" keyPoints="1;0" keyTimes="0;1" calcMode="linear" path="M150 84 C 320 104, 440 160, 584 202"/></circle>
      <circle class="impulse back" r="3.2"><animateMotion dur="3.9s" begin="-1.1s" repeatCount="indefinite" keyPoints="1;0" keyTimes="0;1" calcMode="linear" path="M120 300 C 300 292, 450 252, 584 218"/></circle>
      <circle class="impulse back" r="3.2"><animateMotion dur="4.2s" begin="-3.4s" repeatCount="indefinite" keyPoints="1;0" keyTimes="0;1" calcMode="linear" path="M1050 84 C 880 104, 760 160, 616 202"/></circle>
      <circle class="impulse back" r="3.2"><animateMotion dur="3.6s" begin="-0.4s" repeatCount="indefinite" keyPoints="1;0" keyTimes="0;1" calcMode="linear" path="M1080 300 C 900 292, 750 252, 616 218"/></circle>

      <text x="150" y="44" text-anchor="middle">sid · claude-code</text>
      <text x="150" y="62" text-anchor="middle" class="sub">agent + edge worker</text>
      <text x="120" y="340" text-anchor="middle">aditya · claude-code</text>
      <text x="120" y="358" text-anchor="middle" class="sub">agent + edge worker</text>
      <text x="1050" y="44" text-anchor="middle">akhil · codex</text>
      <text x="1050" y="62" text-anchor="middle" class="sub">agent + edge worker</text>
      <text x="1080" y="340" text-anchor="middle">meera · claude-code</text>
      <text x="1080" y="358" text-anchor="middle" class="sub">agent + edge worker</text>

      <text x="600" y="268" text-anchor="middle" class="core-label">SYNAPSE SERVICE</text>
      <text x="600" y="288" text-anchor="middle" class="sub">one shared memory · the fold</text>
    </svg>
    <p class="net-caption">Findings flow in from every machine; answers flow back to whoever asks. Raw transcripts never cross the wire.</p>
  </div>

  <h2>Shared sessions</h2>
  <p class="sub">live from this service, refreshed every two seconds; each card opens that session's brain</p>
  <div id="sessions"><div class="empty">connecting to the service…</div></div>

  <h2>Why this exists</h2>
  <p class="sub">every engineer has a coding agent; every agent is blind to the team</p>
  <div class="why">
    <div class="why-card">
      <h3>Duplication</h3>
      <p>Agents redo each other's exploration and debugging. The same dead end gets discovered twice, three times, once per teammate, because no session can see what another session already ruled out.</p>
    </div>
    <div class="why-card">
      <h3>Asymmetry</h3>
      <p>Decisions, findings, and rule-outs don't propagate. A human has to copy-paste them between agents, and everything they don't relay is knowledge only one machine has.</p>
    </div>
  </div>
  <p class="anecdote">A systems engineer writes MATLAB; a software engineer ports it to C++. Every bug found on one side needs a manual message before the other's agent knows. <b>Synapse removes the relay step.</b> It's like joining the same Google Doc: real-time shared context, but for coding agents. Save time: never re-research what a teammate's agent already found. Save tokens: never re-run the same exploration twice.</p>

  <h2>What happens to a finding</h2>
  <p class="sub">four stages; the log page tails every one of them live</p>
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
  <p class="pipe-note">Not everything goes to an LLM. Triage and ranking are deterministic: that is the optimization, and findings are queryable the instant they land, with synthesis catching up behind them inside natural human think-time.</p>

  <h2>Measured, not claimed</h2>
  <p class="sub">our own numbers, from our own Snapdragon X Elite; nothing below is a projection</p>
  <div class="band">
    <div class="band-card hero-stat">
      <div class="num">0.1<small>% variance</small></div>
      <div class="what">NPU decode-rate variability across identical distillations, against 10.8% on GPU and 16.4% on CPU. An always-on background distiller that never steals your machine at an unpredictable moment.</div>
    </div>
    <div class="band-card">
      <div class="num">1,041 <small>tok/s</small></div>
      <div class="what">prefill on the compiled Qwen3-4B bundle, R² 0.9974 across a 19x range of prompt lengths. 5.3x faster than the interpreted path on the same silicon.</div>
    </div>
    <div class="band-card">
      <div class="num">2.2 <small>s</small></div>
      <div class="what">per distillation-shaped call on the production NPU model, at 16.9 tok/s decode, running entirely off your CPU and GPU.</div>
    </div>
    <div class="band-card">
      <div class="num">12.6-52.8 <small>s</small></div>
      <div class="what">measured cloud merge round-trip against Llama-3.3-70B, debounced and spend-governed so it never blocks a query.</div>
    </div>
  </div>
  <p class="band-note"><b>Power draw itself is unmeasured, and we say so.</b> The efficiency story we stand on is the one we measured: always-on work moved off your CPU and GPU onto the NPU, the silicon built for it, with a decode rate steady to one part in a thousand. No number on this page is nicer-sounding than the truth.</p>

  <h2>The division of labor</h2>
  <p class="sub">edge distillation on Snapdragon plus cloud synthesis on Cloud AI 100: the split this hardware was designed for</p>
  <div class="split">
    <div class="split-card">
      <h3>Edge</h3>
      <div class="where">Snapdragon X Elite · Hexagon NPU · GenieX</div>
      <p>4B-class models run locally, already optimized for the NPU. Your raw work never leaves the device, and the compile/test loop keeps the CPU and GPU to itself.</p>
    </div>
    <div class="split-card cloud">
      <h3>Cloud</h3>
      <div class="where">Llama-3.3-70B · Cloud AI 100</div>
      <p>Cross-team synthesis where the big model earns its cost: semantic dedup, conflict detection, and one bounded working memory rewritten per merge.</p>
    </div>
  </div>
  <p class="versatile"><b>Versatile by design.</b> Synapse runs anywhere. With Qualcomm hardware underneath, it gets steadier and cheaper.</p>

  <h2>What you'd use it for</h2>
  <p class="sub">the same memory, six shapes of team</p>
  <div class="cases">
    <div class="case"><b>Debugging together</b><span>one teammate's dead end is everyone's dead end</span></div>
    <div class="case"><b>Feature work</b><span>parallel building without parallel re-discovery</span></div>
    <div class="case"><b>Design and brainstorming</b><span>decisions propagate the moment they're made</span></div>
    <div class="case"><b>Status sharing</b><span>your agent already knows what the team did today</span></div>
    <div class="case"><b>Lab and dev</b><span>on-target context meets code context in one memory</span></div>
    <div class="case"><b>Asymmetric teammates</b><span>MATLAB author and C++ porter, no manual relay</span></div>
  </div>

  <h2>The moment it works</h2>
  <div class="aha">
    <p class="setup">A teammate joins late. Their agent is briefed on arrival. They start their own task, and before doing any work, their agent says:</p>
    <p class="quote"><span class="said">"Sid already ruled this out."</span></p>
    <p class="receipt">In live rehearsal the system merged two contributors' findings into one, unscripted. That merge is on the Memory page of this dashboard right now, struck-through sources and all.</p>
  </div>

  <h2>Runs anywhere. One command.</h2>
  <p class="sub">a teammate joins with one command and a session id</p>
  <div class="deploy">
    <div class="term">
      <div><span class="p">$</span> git clone … &amp;&amp; uv sync</div>
      <div><span class="p">$</span> uv run python scripts/serve_local.py --purpose "fix the flaky auth test"</div>
      <div><span class="p">$</span> claude mcp add synapse <span class="c">… one line, and the agent is briefed on join</span></div>
    </div>
    <div class="providers">
      <h3>Five providers, one seam</h3>
      <p>Every model call goes through one interface with five interchangeable plugs. No NPU? No cloud key? It still runs, end to end, on any machine.</p>
      <div class="plugs">
        <span class="plug live">NPU · GenieX</span>
        <span class="plug live">Cloud AI 100</span>
        <span class="plug">Anthropic API</span>
        <span class="plug">Claude CLI</span>
        <span class="plug">offline stand-in</span>
      </div>
    </div>
  </div>

  <p class="footer">
    <b>This page makes not one external request</b>: no CDN, no font, no image,
    so it can be opened on a demo machine with no network and still be itself.
    <b>Teal is the cloud side</b> of the device boundary; the Edge Worker's own
    debug page is the copper side. The brain, log, and memory pages behind the
    buttons above are read-only by construction: every route is mounted GET-only.
  </p>
</main>

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
          "service reachable · " + n + (n === 1 ? " shared session" : " shared sessions");
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
     Same standing rule as ever: NOT ONE external request. */
  :root {
    color-scheme: dark;
    --background: #0a1214;
    --card: #101b1e;
    --inset: #0a1214;
    --muted: #152528;
    --border: #22383d;
    --hairline: #1a2b2f;
    --foreground: #e9f1f1;
    --muted-foreground: #9fb5b6;
    --faint: #647a7d;
    --accent: #56c8cf;
    --accent-dim: #2a5f64;
    --k-merged: #71c07f;
    --k-trivial: #d3ab55;
    --k-topic: #7ea9db;
    --tag-query: #b49fd6;
    --red: #e5695f;
    --radius: 10px;
    --sans: ui-sans-serif, system-ui, -apple-system, "Segoe UI", Roboto, "Helvetica Neue", Arial, sans-serif;
    --mono: ui-monospace, "SF Mono", "Cascadia Code", Menlo, Consolas, monospace;
  }
  * { box-sizing: border-box; }
  body {
    margin: 0;
    background: var(--background);
    color: var(--foreground);
    font: 14px/1.55 var(--sans);
    -webkit-font-smoothing: antialiased;
  }
  #banner {
    display: none;
    background: #2a1210; color: #f0a49b;
    border-bottom: 1px solid #4a201c;
    padding: 8px 24px; font-size: 12px; font-weight: 500;
  }
  #banner.show { display: block; }

  /* ── shell: sessions on the left, this session's pages on top ── */
  .shell { display: flex; min-height: 100dvh; }
  aside {
    width: 268px; flex-shrink: 0;
    border-right: 1px solid var(--hairline);
    padding: 14px 12px;
    display: flex; flex-direction: column; gap: 12px;
    position: sticky; top: 0; height: 100dvh; overflow-y: auto;
  }
  .brand { display: flex; align-items: center; gap: 8px; text-decoration: none; color: inherit; padding: 2px 6px 6px; }
  .brand .mark { width: 30px; height: 16px; overflow: visible; }
  .brand .mark .axon { fill: none; stroke: var(--accent-dim); stroke-width: 1.4; }
  .brand .mark .soma { fill: var(--accent); }
  .brand .mark .impulse { fill: var(--foreground); }
  @media (prefers-reduced-motion: reduce) { .brand .mark .impulse { display: none; } }
  .brand .name { font-size: 15px; font-weight: 600; letter-spacing: -0.01em; }
  .brand .scope-label { color: var(--muted-foreground); font-size: 13px; }
  .side-label {
    font: 600 11px var(--mono); letter-spacing: 0.1em; text-transform: uppercase;
    color: var(--faint); padding: 0 6px;
  }
  .side-sessions { display: flex; flex-direction: column; gap: 2px; }
  .side-session {
    display: block; padding: 8px 10px; border-radius: 8px;
    text-decoration: none; color: var(--muted-foreground);
    border: 1px solid transparent;
    transition: background-color 120ms;
  }
  .side-session:hover { background: var(--muted); color: var(--foreground); }
  .side-session[aria-current="page"] { background: var(--muted); border-color: var(--border); color: var(--foreground); }
  .side-session:focus-visible { outline: 2px solid var(--accent); outline-offset: 2px; }
  .side-session .ss-purpose { display: block; font-size: 13.5px; font-weight: 550; line-height: 1.35; overflow-wrap: anywhere; }
  .side-session .ss-sid { display: block; font: 11.5px var(--mono); color: var(--faint); margin-top: 3px; }
  .side-empty { color: var(--faint); font-size: 12px; padding: 8px 10px; }
  .side-foot { margin-top: auto; padding: 0 6px; }
  .side-foot a { color: var(--faint); font-size: 12px; text-decoration: none; }
  .side-foot a:hover { color: var(--foreground); }
  .content { flex: 1; min-width: 0; }
  .topbar {
    display: flex; align-items: center; gap: 14px; flex-wrap: wrap;
    padding: 10px 24px;
    border-bottom: 1px solid var(--hairline);
  }
  .tabs { display: flex; gap: 4px; }
  .tabs a {
    padding: 6px 14px; border-radius: 7px;
    font-size: 14px; font-weight: 500;
    color: var(--muted-foreground); text-decoration: none;
  }
  .tabs a:hover { color: var(--foreground); background: var(--muted); }
  .tabs a[aria-current="page"] { color: var(--foreground); background: var(--muted); }
  .tabs a:focus-visible { outline: 2px solid var(--accent); outline-offset: 2px; }
  main { padding: 24px 32px 64px; }
  @media (max-width: 900px) {
    .shell { flex-direction: column; }
    aside { position: static; width: auto; height: auto; border-right: none; border-bottom: 1px solid var(--hairline); }
    .side-sessions { flex-direction: row; overflow-x: auto; }
    .side-session { min-width: 220px; }
    .side-foot { display: none; }
  }

  /* ── header row: what this table is, and the live counts ── */
  .mem-head { display: flex; align-items: flex-end; gap: 16px; flex-wrap: wrap; margin-bottom: 14px; }
  .mem-head h1 { margin: 0; font-size: 22px; font-weight: 650; letter-spacing: -0.015em; }
  .mem-head p { margin: 2px 0 0; color: var(--faint); font-size: 13px; max-width: 60ch; }
  .mem-counts { margin-left: auto; display: flex; gap: 16px; font: 13px var(--mono); color: var(--faint); }
  .mem-counts b { font-weight: 600; color: var(--foreground); }
  .mem-counts .c-visible b { color: var(--foreground); }
  .mem-counts .c-superseded b { color: var(--k-merged); }
  .mem-counts .c-trivial b { color: var(--k-trivial); }

  /* ── controls ── */
  .controls { display: flex; align-items: center; gap: 10px; flex-wrap: wrap; margin-bottom: 12px; }
  .search {
    background: transparent; color: var(--foreground);
    border: 1px solid var(--border); border-radius: 7px;
    padding: 7px 12px; font: 13.5px var(--sans);
    width: 300px;
  }
  .search::placeholder { color: var(--faint); }
  .search:focus-visible { outline: 2px solid var(--accent); outline-offset: 1px; }
  .chips { display: flex; flex-wrap: wrap; gap: 6px; }
  .chip {
    --c: var(--muted-foreground);
    display: inline-flex; align-items: center; gap: 7px;
    border: 1px solid var(--border);
    background: var(--card);
    color: var(--muted-foreground);
    border-radius: 7px;
    padding: 4px 11px;
    font: 500 12px var(--mono);
    cursor: pointer; user-select: none;
    transition: background-color 120ms, opacity 120ms;
  }
  .chip:hover { background: var(--muted); }
  .chip::before { content: ""; width: 7px; height: 7px; border-radius: 50%; background: var(--c); }
  .chip[data-status="visible"]    { --c: var(--foreground); }
  .chip[data-status="superseded"] { --c: var(--k-merged); }
  .chip[data-status="trivial"]    { --c: var(--k-trivial); }
  .chip[data-prov="distilled"]    { --c: var(--accent-dim); }
  .chip[data-prov="contributed"]  { --c: var(--tag-query); }
  .chip[data-prov="synthesized"]  { --c: var(--k-merged); }
  .chip[data-active="false"] { opacity: 0.4; }
  .chip[data-active="false"]::before { background: var(--faint); }
  .chip:focus-visible { outline: 2px solid var(--accent); outline-offset: 2px; }
  .chip-sep { width: 1px; height: 18px; background: var(--hairline); }

  /* ── the table ── */
  .tablewrap { overflow-x: auto; border: 1px solid var(--border); border-radius: var(--radius); background: var(--card); }
  table.mem { border-collapse: collapse; width: 100%; min-width: 880px; font-size: 13.5px; }
  table.mem th {
    text-align: left; padding: 9px 12px;
    border-bottom: 1px solid var(--border);
    color: var(--muted-foreground); font-size: 13px; font-weight: 500;
    white-space: nowrap;
    cursor: pointer; user-select: none;
  }
  table.mem th:hover { color: var(--foreground); }
  table.mem th .dir { color: var(--accent); font-family: var(--mono); font-size: 10px; margin-left: 4px; }
  table.mem td {
    padding: 9px 12px; border-bottom: 1px solid var(--hairline);
    vertical-align: top; color: var(--muted-foreground);
    font-variant-numeric: tabular-nums;
  }
  table.mem tbody tr.mrow { cursor: pointer; transition: background-color 120ms; }
  table.mem tbody tr.mrow:hover { background: var(--muted); }
  table.mem td.c-ts { font-family: var(--mono); font-size: 12px; color: var(--faint); white-space: nowrap; }
  table.mem td.c-id { font-family: var(--mono); font-size: 12px; color: var(--faint); white-space: nowrap; }
  table.mem td.c-type { font: 500 12px/1.6 var(--mono); color: var(--k-topic); white-space: nowrap; }
  table.mem td.c-text { color: var(--foreground); min-width: 26em; }
  tr.superseded td.c-text { text-decoration: line-through; color: var(--faint); }
  tr.trivial td.c-text { color: var(--faint); }
  table.mem td.c-authors { white-space: nowrap; }
  .provlabel { --c: var(--faint); display: inline-flex; align-items: center; gap: 7px; font-size: 12px; font-weight: 500; color: var(--c); white-space: nowrap; }
  .provlabel::before { content: ""; width: 7px; height: 7px; border-radius: 50%; background: var(--c); }
  .provlabel[data-prov="synthesized"] { --c: var(--k-merged); }
  .provlabel[data-prov="contributed"] { --c: var(--tag-query); }
  .provlabel[data-prov="distilled"]   { --c: var(--accent-dim); }
  .status-badge {
    display: inline-block; padding: 1px 8px; border-radius: 6px;
    font-size: 11.5px; font-weight: 600;
    border: 1px solid transparent; white-space: nowrap;
  }
  .status-badge.visible    { color: var(--foreground); border-color: var(--border); }
  .status-badge.superseded { color: var(--k-merged); border-color: #274d38; background: rgba(113, 192, 127, 0.08); }
  .status-badge.trivial    { color: var(--k-trivial); border-color: #4d3f1e; background: rgba(211, 171, 85, 0.08); }
  tr.detail-row td {
    background: var(--inset);
    font: 12.5px/1.7 var(--mono); color: var(--muted-foreground);
    white-space: pre-wrap; overflow-wrap: anywhere;
  }
  .empty { padding: 28px; text-align: center; color: var(--faint); }
  .footnote {
    margin: 24px 0 0; padding-top: 14px;
    border-top: 1px solid var(--border);
    color: var(--faint); font-size: 13px; line-height: 1.7; max-width: 92ch;
  }
  .footnote b { color: var(--muted-foreground); font-weight: 500; }
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
