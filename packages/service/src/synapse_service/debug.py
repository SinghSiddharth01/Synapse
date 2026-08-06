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
        elif contributor in members:
            row["state"] = "active"
        elif store.has_departed(sid, contributor):
            # Left, not deleted. Leaving is not ending: their findings stay in
            # the log attributed to them, so the row stays too.
            row["state"] = "left"
        else:
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


def _brain_payload(store: InMemoryStore, feed: Feed, wm_log: WorkingMemoryLog | None,
                   sid: str) -> dict[str, Any]:
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
    }


def debug_routes(store: InMemoryStore, call_log: CallLog, feed: Feed,
                 wm_log: WorkingMemoryLog | None = None) -> list[Route]:
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
        session = _brain_payload(store, feed, wm_log, sid) if sid is not None else None
        return JSONResponse({"sessions": sessions, "session": session})

    async def brain_page(request: Request) -> HTMLResponse:
        return HTMLResponse(_BRAIN_PAGE)

    async def log_page(request: Request) -> HTMLResponse:
        return HTMLResponse(_PAGE)

    return [
        Route("/debug/stats.json", stats_json, methods=["GET"]),
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
<title>synapse-service — debug</title>
<style>
  /* The teal side of the device boundary — matches docs/architecture.html:
     teal = cloud/service. The worker page is the copper side. */
  :root {
    color-scheme: dark;
    --bg: #0e1416;
    --panel: #131c1e;
    --panel-2: #1a2628;
    --border: #24363a;
    --text: #e2eaea;
    --dim: #8fa3a3;
    --dimmer: #5f7272;
    --teal: #5fc6cc;
    --teal-dim: #35747a;
    --k-finding: #8fa3a3;
    --k-merged: #84c88b;
    --k-trivial: #d6b45c;
    --k-topic: #7fa8d9;
    --tag-llm: #5fc6cc;
    --tag-query: #b49fd1;
    --tag-synthesis: #84c88b;
    /* Red, and the only red on the page: a query_failed means the retrieval
       backend is down and every answer the team is getting is a 503. */
    --tag-query-failed: #e06c75;
    --red: #e38c80;
    --mono: "SF Mono", ui-monospace, "Cascadia Code", Menlo, Consolas, monospace;
  }
  * { box-sizing: border-box; }
  body {
    margin: 0;
    background: var(--bg);
    color: var(--text);
    font: 13px/1.5 var(--mono);
    font-variant-numeric: tabular-nums;
    -webkit-font-smoothing: antialiased;
  }
  #banner {
    display: none;
    background: #15343d;
    color: #a9dbe0;
    border-bottom: 1px solid #2a5f6b;
    padding: 8px 16px;
    font-weight: 600;
    letter-spacing: 0.02em;
  }
  #banner.show { display: block; }
  header {
    display: flex; align-items: center; justify-content: space-between;
    gap: 16px; flex-wrap: wrap;
    padding: 14px 22px;
    border-bottom: 1px solid var(--border);
  }
  header h1 { margin: 0; font-size: 13px; font-weight: 700; letter-spacing: 0.06em; text-transform: uppercase; }
  header h1 .dot {
    display: inline-block; width: 7px; height: 7px; border-radius: 50%;
    background: var(--teal); margin-right: 9px;
    box-shadow: 0 0 8px var(--teal);
  }
  header h1 .side { color: var(--teal); }
  #session-select {
    background: var(--panel); color: var(--text);
    border: 1px solid var(--border); border-radius: 4px;
    padding: 5px 10px; font: 12px var(--mono);
    max-width: 46ch;
  }
  #session-select:focus-visible { outline: 2px solid var(--teal); outline-offset: 2px; }
  main { padding: 18px 22px 48px; max-width: 1160px; margin: 0 auto; }

  /* ── the journey rail: the service's slice, in real order ── */
  .rail { display: flex; align-items: stretch; gap: 0; margin-bottom: 16px; }
  .node {
    flex: 1;
    background: var(--panel);
    border: 1px solid var(--border);
    border-radius: 6px;
    padding: 10px 14px 9px;
    min-width: 0;
  }
  .link {
    flex: 0 0 22px; align-self: center; height: 1px;
    background: var(--teal-dim); position: relative;
  }
  .link::after {
    content: "";
    position: absolute; right: -1px; top: -3px;
    border-left: 5px solid var(--teal-dim);
    border-top: 3.5px solid transparent;
    border-bottom: 3.5px solid transparent;
  }
  .node .label {
    color: var(--dimmer);
    font-size: 9px; font-weight: 700;
    text-transform: uppercase; letter-spacing: 0.14em;
    margin-bottom: 3px;
  }
  .node .value { font-size: 20px; font-weight: 700; line-height: 1.2; }
  .node .sub { color: var(--dimmer); font-size: 10px; margin-top: 2px; }

  /* FOLD: the hero node — the brain's signature, three verdicts of the View */
  .node.fold { flex: 1.8; border-color: var(--teal-dim); }
  .node.fold .label { color: var(--teal); }
  .node.fold .value span { font-size: 20px; font-weight: 700; }
  .node.fold .value .v-visible    { color: var(--text); }
  .node.fold .value .v-superseded { color: var(--k-merged); }
  .node.fold .value .v-trivial    { color: var(--k-trivial); }
  .node.fold .value .sep { color: var(--dimmer); font-weight: 400; padding: 0 4px; }

  .topics { display: flex; flex-wrap: wrap; gap: 6px; margin-bottom: 14px; }
  .topic-chip {
    border: 1px solid var(--border); background: var(--panel);
    border-radius: 4px; padding: 3px 10px;
    font-size: 11px; color: var(--dim);
  }
  .topic-chip b { color: var(--k-topic); }

  details.wm {
    background: var(--panel); border: 1px solid var(--border);
    border-radius: 6px; padding: 10px 14px; margin-bottom: 16px;
  }
  details.wm summary {
    cursor: pointer; color: var(--dimmer);
    font-size: 10px; font-weight: 700;
    text-transform: uppercase; letter-spacing: 0.14em;
  }
  details.wm summary:focus-visible { outline: 2px solid var(--teal); outline-offset: 2px; }
  details.wm .wm-body {
    margin-top: 10px; white-space: pre-wrap; overflow-wrap: anywhere;
    color: var(--dim); max-width: 72ch;
  }

  h2.section {
    font-size: 10px; font-weight: 700;
    text-transform: uppercase; letter-spacing: 0.14em;
    color: var(--dimmer);
    margin: 22px 0 8px;
  }
  #log-tail, #feed {
    background: var(--panel);
    border: 1px solid var(--border);
    border-radius: 6px;
    overflow: hidden;
  }
  .entry {
    --c: var(--dim);
    padding: 7px 14px 7px 12px;
    border-bottom: 1px solid var(--panel-2);
    border-left: 2px solid var(--c);
    display: flex; gap: 10px; align-items: baseline;
    flex-wrap: wrap;
  }
  .entry:last-child { border-bottom: none; }
  .entry[data-kind="FindingAppended"] { --c: transparent; }
  .entry[data-kind="Merged"]          { --c: var(--k-merged); }
  .entry[data-kind="MarkedTrivial"]   { --c: var(--k-trivial); }
  .entry[data-kind="TopicAssigned"]   { --c: var(--k-topic); }
  .entry[data-kind="TopicSplit"]      { --c: var(--k-topic); }
  .entry[data-tag="llm"]       { --c: var(--tag-llm); }
  .entry[data-tag="query"]     { --c: var(--tag-query); }
  .entry[data-tag="synthesis"] { --c: var(--tag-synthesis); }
  .entry[data-tag="query_failed"] { --c: var(--tag-query-failed); }
  .entry .pos { color: var(--dimmer); flex-shrink: 0; width: 3.2em; text-align: right; font-size: 11px; }
  .entry .ts { color: var(--dimmer); flex-shrink: 0; font-size: 11px; }
  .entry .kind, .entry .tag {
    flex-shrink: 0;
    font-size: 10px; font-weight: 700;
    text-transform: uppercase; letter-spacing: 0.05em;
    color: var(--c);
  }
  .entry .kind { width: 10.5em; }
  .entry .tag { width: 6.5em; }
  .entry[data-kind="FindingAppended"] .kind { color: var(--k-finding); }
  .entry[data-kind="FindingAppended"] .summary { color: var(--dim); }
  .entry[data-kind="Merged"] .summary { color: var(--text); font-weight: 600; }
  .entry .summary { flex: 1; overflow-wrap: anywhere; }
  .entry[data-tag] { cursor: pointer; }
  .entry .detail {
    display: none;
    margin: 8px 0 4px;
    padding: 10px 12px;
    background: var(--bg);
    border: 1px solid var(--border);
    border-radius: 5px;
    font-size: 12px; color: var(--dim);
    white-space: pre-wrap; overflow-wrap: anywhere;
    flex-basis: 100%;
  }
  .entry.expanded .detail { display: block; }
  .chips { display: flex; flex-wrap: wrap; gap: 6px; margin-bottom: 12px; }
  .chip {
    --c: var(--dim);
    border: 1px solid var(--border);
    background: transparent;
    color: var(--dim);
    border-radius: 4px;
    padding: 3px 10px 3px 8px;
    font-size: 11px; font-family: var(--mono);
    cursor: pointer; user-select: none;
  }
  .chip::before {
    content: "";
    display: inline-block; width: 7px; height: 7px; border-radius: 50%;
    background: var(--c); margin-right: 7px;
  }
  .chip[data-tag="llm"]       { --c: var(--tag-llm); }
  .chip[data-tag="query"]     { --c: var(--tag-query); }
  .chip[data-tag="synthesis"] { --c: var(--tag-synthesis); }
  .chip[data-tag="query_failed"] { --c: var(--tag-query-failed); }
  .chip[data-active="false"] { opacity: 0.38; }
  .chip[data-active="false"]::before { background: var(--dimmer); }
  .chip:focus-visible { outline: 2px solid var(--teal); outline-offset: 2px; }
  .empty { padding: 26px; text-align: center; color: var(--dimmer); }
  @media (max-width: 760px) {
    .rail { flex-direction: column; gap: 8px; }
    .link { display: none; }
  }
</style>
</head>
<body>
<div id="banner">service unreachable — retrying…</div>
<header>
  <h1><span class="dot"></span>synapse-service <span class="side">· cloud</span></h1>
  <select id="session-select"><option>loading…</option></select>
</header>
<main>
  <div class="rail" aria-label="service pipeline: ingest to query, in order">
    <div class="node"><div class="label">Ingest</div><div class="value" id="stat-entries">0</div><div class="sub">log entries, append-only</div></div>
    <span class="link"></span>
    <div class="node fold">
      <div class="label">Fold — the View</div>
      <div class="value"><span class="v-visible" id="stat-visible">0</span><span class="sep">·</span><span class="v-superseded" id="stat-superseded">0</span><span class="sep">·</span><span class="v-trivial" id="stat-trivial">0</span></div>
      <div class="sub">visible · superseded · trivial</div>
    </div>
    <span class="link"></span>
    <div class="node"><div class="label">Merge</div><div class="value" id="stat-merges">0</div><div class="sub">v<span id="stat-version">0</span> · <span id="stat-conflicts">0</span> conflicts</div></div>
    <span class="link"></span>
    <div class="node"><div class="label">Topics</div><div class="value" id="stat-topics">0</div><div class="sub">geometry, labels only</div></div>
    <span class="link"></span>
    <div class="node"><div class="label">Query</div><div class="value" id="stat-queries">0</div><div class="sub">suppression-aware</div></div>
  </div>

  <div class="topics" id="topics"></div>

  <details class="wm">
    <summary>Working Memory</summary>
    <div class="wm-body" id="wm-body">(empty)</div>
  </details>

  <h2 class="section">Log tail — the merge/topic feed, tailed</h2>
  <div id="log-tail"><div class="empty">no entries yet</div></div>

  <h2 class="section">LLM · query · synthesis</h2>
  <div class="chips" id="chips">
    <span class="chip" data-tag="llm" data-active="true" tabindex="0">llm</span>
    <span class="chip" data-tag="query" data-active="true" tabindex="0">query</span>
    <span class="chip" data-tag="query_failed" data-active="true" tabindex="0">query_failed</span>
    <span class="chip" data-tag="synthesis" data-active="true" tabindex="0">synthesis</span>
  </div>
  <div id="feed"><div class="empty">waiting for the first event…</div></div>
</main>

<script>
(function () {
  "use strict";

  var activeTags = new Set(["llm", "query", "synthesis"]);
  var currentSid = null;
  // Keys of currently-expanded feed entries, surviving the 1s poll's full
  // innerHTML rebuild -- without this, clicking an entry open only lasts
  // until the next refresh (up to 1s), never long enough to read a preview.
  var expandedKeys = new Set();

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

  document.getElementById("session-select").addEventListener("change", function (ev) {
    currentSid = ev.target.value;
    expandedKeys.clear();
    refresh();
  });

  document.getElementById("chips").addEventListener("click", function (ev) {
    var chip = ev.target.closest(".chip");
    if (!chip) return;
    var tag = chip.getAttribute("data-tag");
    if (activeTags.has(tag)) { activeTags.delete(tag); chip.setAttribute("data-active", "false"); }
    else { activeTags.add(tag); chip.setAttribute("data-active", "true"); }
    applyFilter();
  });

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
    if (!logTail.length) { el.innerHTML = '<div class="empty">no entries yet</div>'; return; }
    var html = "";
    for (var i = logTail.length - 1; i >= 0; i--) {
      var e = logTail[i];
      html += '<div class="entry" data-kind="' + escAttr(e.kind) + '">';
      html += '<span class="pos">#' + esc(e.position) + '</span>';
      html += '<span class="kind">' + esc(e.kind) + '</span>';
      html += '<span class="summary">' + esc(e.summary) + '</span>';
      html += '</div>';
    }
    el.innerHTML = html;
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

    if (!merged.length) { el.innerHTML = '<div class="empty">waiting for the first event…</div>'; return; }

    var html = "";
    for (var i = merged.length - 1; i >= 0; i--) {
      var m = merged[i];
      var key = entryKey(m.tag, m.ts);
      var cls = "entry" + (expandedKeys.has(key) ? " expanded" : "");
      if (m.tag === "llm") {
        var c = m.data;
        var ok = c.ok ? "ok" : "FAILED";
        html += '<div class="' + cls + '" data-tag="llm" data-key="' + escAttr(key) + '">';
        html += '<span class="ts">' + esc(hhmmss(c.ts_iso)) + '</span>';
        html += '<span class="tag">llm</span>';
        html += '<span class="summary">' + esc(c.component) + " · " + esc(ok) + " · " +
          esc(c.input_tokens) + "→" + esc(c.output_tokens) + " tok · " + esc(c.latency_ms) + "ms</span>";
        html += '</div>';
        html += '<div class="detail">' +
          "provider: " + esc(c.provider_id) + "\\n" +
          "schema_valid: " + esc(c.schema_valid) + "\\n\\n" +
          "prompt:\\n" + esc(c.prompt_preview) + "\\n\\n" +
          "output:\\n" + esc(c.output_preview) + '</div>';
      } else {
        var e = m.data;
        html += '<div class="' + cls + '" data-tag="' + escAttr(m.tag) + '" data-key="' + escAttr(key) + '">';
        html += '<span class="ts">' + esc(hhmmss(e.ts_iso)) + '</span>';
        html += '<span class="tag">' + esc(m.tag) + '</span>';
        html += '<span class="summary">' + esc(e.summary) + '</span>';
        html += '</div>';
        // A query's counts cannot answer "what did the asker get back?".
        // Expand one and the answer is right there, attribution included --
        // which is also how you see suppression bite rather than infer it.
        var d = e.detail || {};
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
      }
    }
    el.innerHTML = html;
    // Re-parent each just-emitted .detail block under the .entry right
    // before it, same trick as the worker page, so the click toggle finds
    // it. The "expanded" class was already set above from expandedKeys, so
    // a re-render mid-poll doesn't collapse an entry the user has open.
    el.querySelectorAll('.entry[data-tag="llm"]').forEach(function (entry) {
      var next = entry.nextElementSibling;
      if (next && next.classList.contains("detail")) entry.appendChild(next);
    });
    applyFilter();
  }

  function renderSession(session) {
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
    var select = document.getElementById("session-select");
    var prior = currentSid;
    select.innerHTML = sessions.map(function (s) {
      return '<option value="' + escAttr(s.shared_id) + '">' + esc(s.shared_id) +
        " — " + esc(s.purpose) + '</option>';
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
<title>synapse-service — shared memory</title>
<style>
  /* The teal side of the device boundary — matches docs/architecture.html:
     teal = cloud/service. The worker page is the copper side. */
  :root {
    color-scheme: dark;
    --bg: #0e1416;
    --panel: #131c1e;
    --panel-2: #1a2628;
    --border: #24363a;
    --text: #e2eaea;
    --dim: #8fa3a3;
    --dimmer: #5f7272;
    --teal: #5fc6cc;
    --teal-dim: #35747a;
    --k-merged: #84c88b;
    --k-trivial: #d6b45c;
    --k-topic: #7fa8d9;
    --tag-query: #b49fd1;
    --red: #e38c80;
    --mono: "SF Mono", ui-monospace, "Cascadia Code", Menlo, Consolas, monospace;
  }
  * { box-sizing: border-box; }
  body {
    margin: 0;
    background: var(--bg);
    color: var(--text);
    font: 13px/1.5 var(--mono);
    font-variant-numeric: tabular-nums;
    -webkit-font-smoothing: antialiased;
  }
  .sr-only {
    position: absolute; width: 1px; height: 1px;
    padding: 0; margin: -1px; overflow: hidden;
    clip: rect(0 0 0 0); white-space: nowrap; border: 0;
  }
  #banner {
    display: none;
    background: #15343d;
    color: #a9dbe0;
    border-bottom: 1px solid #2a5f6b;
    padding: 8px 16px;
    font-weight: 600;
    letter-spacing: 0.02em;
  }
  #banner.show { display: block; }
  header {
    display: flex; align-items: center; gap: 16px; flex-wrap: wrap;
    padding: 14px 22px;
    border-bottom: 1px solid var(--border);
  }
  header h1 { margin: 0; font-size: 13px; font-weight: 700; letter-spacing: 0.06em; text-transform: uppercase; }
  header h1 .dot {
    display: inline-block; width: 7px; height: 7px; border-radius: 50%;
    background: var(--teal); margin-right: 9px;
    box-shadow: 0 0 8px var(--teal);
  }
  header h1 .side { color: var(--teal); }
  nav.pages { display: flex; gap: 2px; margin-right: auto; }
  nav.pages a, nav.pages span {
    padding: 4px 11px; border-radius: 4px;
    font-size: 10px; font-weight: 700;
    text-transform: uppercase; letter-spacing: 0.12em;
    color: var(--dimmer); text-decoration: none;
    border: 1px solid transparent;
  }
  nav.pages a:hover { color: var(--text); background: var(--panel); }
  nav.pages a[aria-current="page"] {
    color: var(--teal); border-color: var(--teal-dim); background: var(--panel);
  }
  nav.pages a:focus-visible { outline: 2px solid var(--teal); outline-offset: 2px; }
  nav.pages span.soon { opacity: 0.35; cursor: default; }
  #session-select {
    background: var(--panel); color: var(--text);
    border: 1px solid var(--border); border-radius: 4px;
    padding: 5px 10px; font: 12px var(--mono);
    max-width: 46ch;
  }
  #session-select:focus-visible { outline: 2px solid var(--teal); outline-offset: 2px; }
  main { padding: 18px 22px 48px; max-width: 1160px; margin: 0 auto; }

  .label {
    color: var(--dimmer);
    font-size: 9px; font-weight: 700;
    text-transform: uppercase; letter-spacing: 0.14em;
    margin-bottom: 3px;
  }
  h2.section {
    font-size: 10px; font-weight: 700;
    text-transform: uppercase; letter-spacing: 0.14em;
    color: var(--dimmer);
    margin: 24px 0 8px;
  }
  h2.section .note, .note {
    font-weight: 400; letter-spacing: 0.02em; text-transform: none;
    color: var(--dimmer); font-size: 10px;
  }

  /* ── identity strip: what this brain is for ── */
  .identity {
    border-left: 2px solid var(--teal-dim);
    padding: 2px 0 2px 14px;
    margin-bottom: 18px;
  }
  .identity .purpose {
    font-size: 19px; line-height: 1.35; font-weight: 600;
    color: var(--text); max-width: 68ch;
  }
  .identity .ident { color: var(--dimmer); font-size: 11px; margin-top: 5px; }
  .identity .ident b { color: var(--dim); font-weight: 400; }
  .pill {
    display: inline-block; padding: 1px 7px; border-radius: 3px;
    font-size: 9px; font-weight: 700; letter-spacing: 0.12em;
    text-transform: uppercase; border: 1px solid;
  }
  .pill.active { color: var(--k-merged); border-color: #2f5b36; }
  .pill.ended  { color: var(--red);     border-color: #5f3630; }

  /* ── vitals rail ── */
  .rail { display: flex; align-items: stretch; gap: 10px; margin-bottom: 18px; }
  .node {
    flex: 1;
    background: var(--panel);
    border: 1px solid var(--border);
    border-radius: 6px;
    padding: 10px 14px 9px;
    min-width: 0;
  }
  .node .value { font-size: 20px; font-weight: 700; line-height: 1.2; }
  .node .sub { color: var(--dimmer); font-size: 10px; margin-top: 2px; }
  .node.fold { flex: 1.7; border-color: var(--teal-dim); }
  .node.fold .label { color: var(--teal); }
  .node.fold .value .v-visible    { color: var(--text); }
  .node.fold .value .v-superseded { color: var(--k-merged); }
  .node.fold .value .v-trivial    { color: var(--k-trivial); }
  .node.fold .value .sep { color: var(--dimmer); font-weight: 400; padding: 0 4px; }

  /* ── working memory: the hero ── */
  .panel {
    background: var(--panel); border: 1px solid var(--border);
    border-radius: 6px; overflow: hidden;
  }
  .panel.wm { border-color: var(--teal-dim); }
  .panel-head {
    display: flex; align-items: baseline; justify-content: space-between;
    gap: 12px; flex-wrap: wrap;
    padding: 11px 16px 9px; border-bottom: 1px solid var(--border);
  }
  .panel-head h2.section { margin: 0; color: var(--teal); }
  .panel-head .meta { color: var(--dimmer); font-size: 11px; }
  #wm-body {
    padding: 14px 16px; white-space: pre-wrap; overflow-wrap: anywhere;
    color: var(--text); max-width: 74ch; line-height: 1.65;
  }
  #wm-body.empty-note { color: var(--dimmer); font-style: italic; }
  .rev-head {
    padding: 7px 16px; border-top: 1px solid var(--border);
    background: var(--panel-2);
    color: var(--dimmer); font-size: 9px; font-weight: 700;
    text-transform: uppercase; letter-spacing: 0.14em;
  }
  .rev {
    display: flex; gap: 12px; align-items: baseline; flex-wrap: wrap;
    padding: 6px 16px; border-bottom: 1px solid var(--panel-2);
    cursor: pointer;
  }
  .rev:last-child { border-bottom: none; }
  .rev .v { color: var(--teal); width: 3em; flex-shrink: 0; }
  .rev .ts { color: var(--dimmer); font-size: 11px; width: 7em; flex-shrink: 0; }
  .rev .w { color: var(--dim); width: 5em; flex-shrink: 0; }
  .rev .d { width: 5em; flex-shrink: 0; }
  .rev .d.up { color: var(--k-merged); }
  .rev .d.down { color: var(--k-trivial); }
  .rev .d.unk { color: var(--dimmer); }
  .rev .caret { color: var(--dimmer); margin-left: auto; }
  .rev .full {
    display: none; flex-basis: 100%;
    margin: 8px 0 4px; padding: 10px 12px;
    background: var(--bg); border: 1px solid var(--border); border-radius: 5px;
    color: var(--dim); white-space: pre-wrap; overflow-wrap: anywhere;
  }
  .rev.expanded .full { display: block; }
  .rev-note { padding: 12px 16px; color: var(--dimmer); }

  /* ── participants ── */
  .tablewrap { overflow-x: auto; border: 1px solid var(--border); border-radius: 6px; background: var(--panel); }
  table.roster { border-collapse: collapse; width: 100%; min-width: 820px; }
  table.roster th {
    text-align: left; padding: 8px 12px;
    border-bottom: 1px solid var(--border);
    color: var(--dimmer); font-size: 9px; font-weight: 700;
    text-transform: uppercase; letter-spacing: 0.12em; white-space: nowrap;
  }
  table.roster td {
    padding: 9px 12px; border-bottom: 1px solid var(--panel-2);
    vertical-align: top; color: var(--dim);
  }
  table.roster tr:last-child td { border-bottom: none; }
  table.roster td.who { color: var(--text); font-weight: 600; white-space: nowrap; }
  table.roster td.num { text-align: right; }
  .adot {
    display: inline-block; width: 7px; height: 7px; border-radius: 50%;
    margin-right: 8px; background: var(--dimmer);
  }
  .adot.hot  { background: var(--k-merged); box-shadow: 0 0 7px #4d8a54; }
  .adot.warm { background: var(--k-trivial); }
  .adot.cold { background: var(--dimmer); }
  .adot.none { background: transparent; border: 1px solid var(--dimmer); }
  .state { font-size: 9px; font-weight: 700; letter-spacing: 0.1em; text-transform: uppercase; }
  .state.active    { color: var(--k-merged); }
  .state.listening { color: var(--tag-query); }
  .state.left      { color: var(--dim); }
  /* The honest unknown, dimmest of the four and dotted-underlined so it
     reads as "there is a caveat here" rather than as a verdict. */
  .state.unregistered {
    color: var(--dimmer); border-bottom: 1px dotted var(--dimmer); cursor: help;
  }
  .scope { color: var(--dimmer); font-size: 10px; }
  .none { color: var(--dimmer); }

  /* ── latest into memory ── */
  #recent { background: var(--panel); border: 1px solid var(--border); border-radius: 6px; overflow: hidden; }
  .row {
    --c: var(--dimmer);
    display: flex; gap: 12px; align-items: baseline; flex-wrap: wrap;
    padding: 9px 14px 9px 12px;
    border-bottom: 1px solid var(--panel-2);
    border-left: 2px solid var(--c);
    cursor: pointer;
  }
  .row:last-child { border-bottom: none; }
  .row[data-prov="synthesized"] { --c: var(--k-merged); }
  .row[data-prov="contributed"] { --c: var(--tag-query); }
  .row[data-prov="distilled"]   { --c: var(--teal-dim); }
  .row .ts { color: var(--dimmer); font-size: 11px; flex-shrink: 0; width: 6em; }
  .row .who { color: var(--text); flex-shrink: 0; width: 12em; overflow-wrap: anywhere; }
  .row .type {
    flex-shrink: 0; width: 9.5em;
    font-size: 10px; font-weight: 700; letter-spacing: 0.05em;
    text-transform: uppercase; color: var(--k-topic);
  }
  .row .text { flex: 1; min-width: 14em; overflow-wrap: anywhere; color: var(--dim); }
  .row .prov {
    flex-shrink: 0; margin-left: auto;
    font-size: 10px; font-weight: 700; letter-spacing: 0.05em;
    text-transform: uppercase; color: var(--c);
  }
  .row .prov::before {
    content: ""; display: inline-block;
    width: 7px; height: 7px; border-radius: 50%;
    background: var(--c); margin-right: 7px;
  }
  .row.tombstone .text { text-decoration: line-through; color: var(--dimmer); }
  .row .full {
    display: none; flex-basis: 100%;
    margin: 8px 0 2px; padding: 10px 12px;
    background: var(--bg); border: 1px solid var(--border); border-radius: 5px;
    color: var(--dim); white-space: pre-wrap; overflow-wrap: anywhere;
  }
  .row.expanded .full { display: block; }

  .empty { padding: 26px; text-align: center; color: var(--dimmer); }
  .footnote {
    margin: 26px 0 0; padding-top: 14px;
    border-top: 1px solid var(--border);
    color: var(--dimmer); font-size: 11px; line-height: 1.7; max-width: 92ch;
  }
  .footnote b { color: var(--dim); font-weight: 600; }
  @media (max-width: 760px) {
    .rail { flex-direction: column; }
  }
</style>
</head>
<body>
<div id="banner">service unreachable — retrying…</div>
<header>
  <h1><span class="dot"></span>synapse-service <span class="side">· cloud</span></h1>
  <nav class="pages" aria-label="debug pages">
    <a href="/debug" aria-current="page">brain</a>
    <a href="/debug/log">log</a>
    <span class="soon" title="the memory browser lands with W4b">memory</span>
  </nav>
  <select id="session-select" aria-label="shared session"><option>loading…</option></select>
</header>
<main>
  <section class="identity">
    <div class="label">Purpose</div>
    <div class="purpose" id="purpose">—</div>
    <div class="ident" id="ident"></div>
  </section>

  <div class="rail" aria-label="session vitals">
    <div class="node">
      <div class="label">Contributors</div>
      <div class="value" id="stat-contributors">0</div>
      <!-- BOTH numbers, because they routinely disagree and the difference is
           the interesting part: a raw `POST /findings` never registers
           anybody, so the log can name people the member list has never
           heard of. Showing only the first put a "0" directly above a table
           of contributors. -->
      <div class="sub">registered · <span id="stat-contributors-log">0</span> in the log</div>
    </div>
    <div class="node">
      <div class="label">Conversations</div>
      <div class="value" id="stat-conversations">0</div>
      <div class="sub">agent sessions seen in the log</div>
    </div>
    <div class="node fold">
      <div class="label">Memory — the fold</div>
      <div class="value"><span class="v-visible" id="stat-visible">0</span><span class="sep">·</span><span class="v-superseded" id="stat-superseded">0</span><span class="sep">·</span><span class="v-trivial" id="stat-trivial">0</span></div>
      <div class="sub">visible · superseded · trivial</div>
    </div>
    <div class="node">
      <div class="label">Conflicts</div>
      <div class="value" id="stat-conflicts">0</div>
      <div class="sub">v<span id="stat-version">0</span> · <span id="stat-entries">0</span> log entries</div>
    </div>
  </div>

  <section class="panel wm">
    <div class="panel-head">
      <h2 class="section">Working memory</h2>
      <div class="meta" id="wm-meta">—</div>
    </div>
    <div class="wm-body" id="wm-body">…</div>
    <div class="rev-head">Revisions <span class="note" id="rev-count"></span></div>
    <div id="revisions"></div>
  </section>

  <h2 class="section">Participants <span class="note">— one row per agent session; two windows of one person are two rows</span></h2>
  <div class="tablewrap"><div id="participants"></div></div>

  <h2 class="section">Latest into memory</h2>
  <div id="recent"><div class="empty">no findings have reached this session</div></div>

  <p class="footnote">
    <b>Activity dots are recency of the last observed contribution or query</b> —
    under 2 min, under 15 min, older. The service holds no heartbeat and no
    connection registry, so nothing on this page reports whether anyone is
    <i>connected</i>: it reports when they were last seen doing something.
    <b>Join and leave times are not recorded anywhere</b> — membership is a list
    with no timestamps — so participation is shown as state only, and
    <b>not a member</b> means exactly that: absent from the list with no
    departure observed, which is equally what a never-registered contributor
    and a service restart look like. Only <b>left</b> is a departure this
    process actually watched happen.
    <b>Memory position is per contributor, not per conversation</b>: the watermark
    is a fact about a person, so both of one person's windows show the same one.
    <b>Last query comes from a 200-event ring shared by every session</b>, so a
    busy stretch can push an older query out of it and the cell falls back to
    an em-dash — “not in the window”, never “never asked”.
    <b>Revision history is kept in this process</b> and is empty after a restart.
  </p>
</main>

<script>
(function () {
  "use strict";

  var currentSid = null;
  // Keys of expanded rows, surviving the 1s poll's full innerHTML rebuild --
  // keyed on the FINDING ID and the REVISION VERSION, both stable, so an open
  // row does not become a different row when the list shifts underneath it.
  var expandedKeys = new Set();

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

  document.getElementById("session-select").addEventListener("change", function (ev) {
    currentSid = ev.target.value;
    expandedKeys.clear();
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
    var select = document.getElementById("session-select");
    var prior = currentSid;
    select.innerHTML = sessions.map(function (s) {
      return '<option value="' + escAttr(s.shared_id) + '">' + esc(s.shared_id) +
        (s.status === "ended" ? " (ended)" : "") + " — " + esc(s.purpose) + '</option>';
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
