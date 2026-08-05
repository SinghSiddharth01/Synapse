"""Service /debug routes — session selector, log tail, verdicts, tagged feed.

Read-only by construction: both routes are mounted GET-only, so Starlette
itself answers 405 to anything else — this module never writes to `store`,
never calls `synthesizer` or `query_findings`, and never touches a provider.
It only reads what already happened: the log (`store.log_entries`), the fold
(`store.view`), and the `CallLog`/`Feed` that `api.py`'s real routes recorded
into as a side effect of doing their own work.

CONTEXT.md: "the log IS the merge/topic feed" — `log_tail` below reads
`store.log_entries` directly rather than building a second feed alongside it.
The `merges`/`queries` feed *is* a second, smaller feed, but a deliberately
different one: it exists because CONTEXT.md's Finding Log has no concept of
an LLM call or a teammate's question, and those are exactly what an
instrumented dashboard needs to show.
"""

from __future__ import annotations

from collections import deque
from datetime import datetime, timezone
from typing import Any

from starlette.requests import Request
from starlette.responses import HTMLResponse, JSONResponse
from starlette.routing import Route

from synapse_providers import CallLog

from synapse_service.log import FindingAppended, MarkedTrivial, Merged, TopicAssigned, TopicSplit
from synapse_service.store import InMemoryStore

MAX_FEED = 200
MAX_LOG_TAIL = 200


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
        "queries": feed.snapshot(tag="query", session=sid),
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
    }


def debug_routes(store: InMemoryStore, call_log: CallLog, feed: Feed) -> list[Route]:
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

    async def debug_page(request: Request) -> HTMLResponse:
        return HTMLResponse(_PAGE)

    return [
        Route("/debug/stats.json", stats_json, methods=["GET"]),
        Route("/debug", debug_page, methods=["GET"]),
    ]


class _Empty:
    purpose = ""


_EMPTY = _Empty()


_PAGE = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<title>synapse-service — debug</title>
<style>
  :root {
    color-scheme: dark;
    --bg: #0b0d10;
    --panel: #14171c;
    --panel-2: #191d23;
    --border: #262b33;
    --text: #e6e9ef;
    --dim: #8b93a1;
    --dimmer: #5b6472;
    --accent: #5eb0ff;
    --k-finding: #6b7280;
    --k-merged: #4ade80;
    --k-trivial: #f5b942;
    --k-topic: #60a5fa;
    --tag-llm: #f5b942;
    --tag-query: #a78bfa;
    --tag-synthesis: #4ade80;
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
    background: #3a1219;
    color: #ffb4c0;
    border-bottom: 1px solid #6b1a29;
    padding: 8px 16px;
    font-weight: 600;
    letter-spacing: 0.02em;
  }
  #banner.show { display: block; }
  header {
    padding: 18px 20px 14px;
    border-bottom: 1px solid var(--border);
    display: flex;
    align-items: center;
    justify-content: space-between;
    gap: 16px;
    flex-wrap: wrap;
  }
  header h1 {
    margin: 0;
    font-size: 15px;
    font-weight: 600;
    letter-spacing: 0.01em;
  }
  header h1 .dot {
    display: inline-block;
    width: 8px; height: 8px;
    border-radius: 50%;
    background: var(--k-merged);
    margin-right: 8px;
    box-shadow: 0 0 6px var(--k-merged);
  }
  #session-select {
    background: var(--panel);
    color: var(--text);
    border: 1px solid var(--border);
    border-radius: 6px;
    padding: 5px 10px;
    font: 12px var(--mono);
  }
  main {
    padding: 16px 20px 40px;
    max-width: 1100px;
    margin: 0 auto;
  }
  .strip {
    display: grid;
    grid-template-columns: repeat(auto-fit, minmax(120px, 1fr));
    gap: 10px;
    margin-bottom: 16px;
  }
  .card {
    background: var(--panel);
    border: 1px solid var(--border);
    border-radius: 8px;
    padding: 10px 14px;
  }
  .card .label {
    color: var(--dim);
    font-size: 10px;
    text-transform: uppercase;
    letter-spacing: 0.08em;
    margin-bottom: 4px;
  }
  .card .value { font-size: 18px; font-weight: 600; }
  details.wm {
    background: var(--panel);
    border: 1px solid var(--border);
    border-radius: 8px;
    padding: 10px 14px;
    margin-bottom: 16px;
  }
  details.wm summary {
    cursor: pointer;
    color: var(--dim);
    font-size: 11px;
    text-transform: uppercase;
    letter-spacing: 0.08em;
  }
  details.wm .wm-body {
    margin-top: 10px;
    white-space: pre-wrap;
    overflow-wrap: anywhere;
    color: var(--text);
  }
  .topics {
    display: flex;
    flex-wrap: wrap;
    gap: 6px;
    margin-bottom: 16px;
  }
  .topic-chip {
    border: 1px solid var(--border);
    background: var(--panel);
    border-radius: 999px;
    padding: 3px 10px;
    font-size: 11px;
    color: var(--dim);
  }
  .topic-chip b { color: var(--text); }
  h2.section {
    font-size: 11px;
    text-transform: uppercase;
    letter-spacing: 0.08em;
    color: var(--dim);
    margin: 22px 0 8px;
  }
  #log-tail, #feed {
    background: var(--panel);
    border: 1px solid var(--border);
    border-radius: 8px;
    overflow: hidden;
  }
  .entry {
    padding: 8px 14px;
    border-bottom: 1px solid var(--border);
    display: flex;
    gap: 10px;
    align-items: baseline;
  }
  .entry:last-child { border-bottom: none; }
  .entry .pos { color: var(--dimmer); flex-shrink: 0; width: 3.5em; text-align: right; }
  .entry .ts { color: var(--dimmer); flex-shrink: 0; }
  .entry .kind, .entry .tag {
    flex-shrink: 0;
    border-radius: 4px;
    padding: 1px 7px;
    font-size: 10px;
    font-weight: 700;
    text-transform: uppercase;
    letter-spacing: 0.03em;
    color: var(--bg);
  }
  .entry[data-kind="FindingAppended"] .kind { background: var(--k-finding); }
  .entry[data-kind="Merged"] .kind         { background: var(--k-merged); }
  .entry[data-kind="MarkedTrivial"] .kind  { background: var(--k-trivial); }
  .entry[data-kind="TopicAssigned"] .kind  { background: var(--k-topic); }
  .entry[data-kind="TopicSplit"] .kind     { background: var(--k-topic); }
  .entry[data-tag="llm"] .tag        { background: var(--tag-llm); }
  .entry[data-tag="query"] .tag      { background: var(--tag-query); }
  .entry[data-tag="synthesis"] .tag  { background: var(--tag-synthesis); }
  .entry .summary { flex: 1; overflow-wrap: anywhere; }
  .entry[data-tag] { cursor: pointer; }
  .entry .detail {
    display: none;
    margin: 8px 14px 12px;
    padding: 10px 12px;
    background: var(--panel-2);
    border: 1px solid var(--border);
    border-radius: 6px;
    font-size: 12px;
    color: var(--dim);
    white-space: pre-wrap;
    overflow-wrap: anywhere;
  }
  .entry.expanded .detail { display: block; }
  .chips { display: flex; flex-wrap: wrap; gap: 6px; margin-bottom: 12px; }
  .chip {
    border: 1px solid var(--border);
    background: var(--panel);
    color: var(--dim);
    border-radius: 999px;
    padding: 4px 11px;
    font-size: 11px;
    font-family: var(--mono);
    cursor: pointer;
    user-select: none;
  }
  .chip[data-active="true"] { color: var(--bg); border-color: transparent; }
  .chip[data-tag="llm"][data-active="true"]       { background: var(--tag-llm); }
  .chip[data-tag="query"][data-active="true"]     { background: var(--tag-query); }
  .chip[data-tag="synthesis"][data-active="true"] { background: var(--tag-synthesis); }
  .empty { padding: 24px; text-align: center; color: var(--dimmer); }
</style>
</head>
<body>
<div id="banner">service unreachable — retrying…</div>
<header>
  <h1><span class="dot"></span>synapse-service — debug</h1>
  <select id="session-select"><option>loading…</option></select>
</header>
<main>
  <div class="strip">
    <div class="card"><div class="label">Version</div><div class="value" id="stat-version">0</div></div>
    <div class="card"><div class="label">Visible</div><div class="value" id="stat-visible">0</div></div>
    <div class="card"><div class="label">Superseded</div><div class="value" id="stat-superseded">0</div></div>
    <div class="card"><div class="label">Trivial</div><div class="value" id="stat-trivial">0</div></div>
    <div class="card"><div class="label">Conflicts</div><div class="value" id="stat-conflicts">0</div></div>
  </div>

  <div class="topics" id="topics"></div>

  <details class="wm">
    <summary>Working Memory</summary>
    <div class="wm-body" id="wm-body">(empty)</div>
  </details>

  <h2 class="section">Log tail — the merge/topic feed, tailed</h2>
  <div id="log-tail"><div class="empty">no entries yet</div></div>

  <h2 class="section">LLM · query · synthesis feed</h2>
  <div class="chips" id="chips">
    <span class="chip" data-tag="llm" data-active="true">llm</span>
    <span class="chip" data-tag="query" data-active="true">query</span>
    <span class="chip" data-tag="synthesis" data-active="true">synthesis</span>
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

  function esc(s) {
    var d = document.createElement("div");
    d.textContent = String(s == null ? "" : s);
    return d.innerHTML;
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
      html += '<div class="entry" data-kind="' + esc(e.kind) + '">';
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
    session.queries.forEach(function (e) { merged.push({ tag: "query", ts: e.ts_iso, data: e }); });
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
        html += '<div class="' + cls + '" data-tag="llm" data-key="' + esc(key) + '">';
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
        html += '<div class="' + cls + '" data-tag="' + esc(m.tag) + '" data-key="' + esc(key) + '">';
        html += '<span class="ts">' + esc(hhmmss(e.ts_iso)) + '</span>';
        html += '<span class="tag">' + esc(m.tag) + '</span>';
        html += '<span class="summary">' + esc(e.summary) + '</span>';
        html += '</div>';
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

    var topicsEl = document.getElementById("topics");
    topicsEl.innerHTML = session.topics.length
      ? session.topics.map(function (t) {
          return '<span class="topic-chip"><b>' + esc(t.size) + '</b> ' + esc(t.label) + '</span>';
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
      return '<option value="' + esc(s.shared_id) + '">' + esc(s.shared_id) +
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
