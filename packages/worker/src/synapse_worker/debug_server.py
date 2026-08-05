"""DebugServer — the worker's live /debug page.

stdlib only: `http.server.ThreadingHTTPServer` in a daemon thread. The worker
deliberately has no Starlette (see the service's debug.py for that side), so
this is a small hand-rolled handler instead.

Read-only by construction: GET is the only verb implemented. Every other verb
— POST, PUT, DELETE, PATCH — answers 405 without touching `stats` at all, and
the handler never calls a provider, only `StatsBuffer.snapshot()`. Those two
properties are pinned by test, not by intention (see test_debug_server.py).
"""

from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from synapse_worker.stats import StatsBuffer

_TRANSCRIPT_TOKEN = "%%TRANSCRIPT%%"

_PAGE = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<link rel="icon" href="data:,">
<title>synapse-worker — debug</title>
<style>
  /* The copper side of the device boundary — matches docs/architecture.html:
     copper = edge/on-device. The service page is the teal side. */
  :root {
    color-scheme: dark;
    --bg: #191512;
    --panel: #201a15;
    --panel-2: #27201a;
    --border: #352a20;
    --text: #eae3da;
    --dim: #a2968a;
    --dimmer: #6e6359;
    --copper: #e19256;
    --copper-dim: #8a5c38;
    --ok: #8fbf8a;
    --amber: #d6b45c;
    --red: #e38c80;
    --tag-tick: #7a7067;
    --tag-triage: #d6b45c;
    --tag-render: #93a8c4;
    --tag-llm: #e19256;
    --tag-push: #8fbf8a;
    --tag-error: #e38c80;
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
    background: #3d1a15;
    color: #f2b6ab;
    border-bottom: 1px solid #6b2a1f;
    padding: 8px 16px;
    font-weight: 600;
    letter-spacing: 0.02em;
  }
  #banner.show { display: block; }
  header {
    display: flex; align-items: baseline; justify-content: space-between;
    gap: 16px; flex-wrap: wrap;
    padding: 14px 22px;
    border-bottom: 1px solid var(--border);
  }
  header h1 { margin: 0; font-size: 13px; font-weight: 700; letter-spacing: 0.06em; text-transform: uppercase; }
  header h1 .dot {
    display: inline-block; width: 7px; height: 7px; border-radius: 50%;
    background: var(--copper); margin-right: 9px;
    box-shadow: 0 0 8px var(--copper);
  }
  header h1 .side { color: var(--copper); }
  header .transcript { color: var(--dimmer); font-size: 11px; overflow-wrap: anywhere; }
  main { padding: 18px 22px 48px; max-width: 1160px; margin: 0 auto; }

  /* ── the journey rail: this page's slice of the pipeline, in real order ── */
  .rail {
    display: flex; align-items: stretch; gap: 0;
    margin-bottom: 20px;
  }
  .node {
    flex: 1;
    background: var(--panel);
    border: 1px solid var(--border);
    border-radius: 6px;
    padding: 10px 14px 9px;
    min-width: 0;
  }
  .link {
    flex: 0 0 22px;
    align-self: center;
    height: 1px;
    background: var(--copper-dim);
    position: relative;
  }
  .link::after {
    content: "";
    position: absolute; right: -1px; top: -3px;
    border-left: 5px solid var(--copper-dim);
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

  /* NPU: the hero node. Copper when burning, quiet when idle. */
  .node.npu {
    flex: 1.8;
    border-color: var(--copper-dim);
    position: relative;
  }
  .node.npu .label { color: var(--copper); }
  #npu-now .seg { font-size: 13px; font-weight: 600; margin-top: 1px; overflow-wrap: anywhere; }
  #npu-now .seg.idle { color: var(--dimmer); font-weight: 400; }
  #npu-now .elapsed {
    font-size: 26px; font-weight: 700; color: var(--copper);
    line-height: 1.15; margin-top: 2px;
  }
  #npu-now .elapsed.idle { color: var(--dimmer); font-size: 15px; font-weight: 400; }
  #npu-now:has(.seg:not(.idle)) {
    border-color: var(--copper);
    animation: burn 2.2s ease-in-out infinite;
  }
  @keyframes burn {
    0%, 100% { box-shadow: 0 0 0 0 rgba(225, 146, 86, 0); }
    50%      { box-shadow: 0 0 18px 0 rgba(225, 146, 86, 0.28); }
  }
  @media (prefers-reduced-motion: reduce) {
    #npu-now:has(.seg:not(.idle)) { animation: none; box-shadow: 0 0 0 1px var(--copper); }
  }

  /* ── tag chips: outline + dot, quiet until you dim one ── */
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
    vertical-align: 0;
  }
  .chip[data-tag="tick"]   { --c: var(--tag-tick); }
  .chip[data-tag="triage"] { --c: var(--tag-triage); }
  .chip[data-tag="render"] { --c: var(--tag-render); }
  .chip[data-tag="llm"]    { --c: var(--tag-llm); }
  .chip[data-tag="push"]   { --c: var(--tag-push); }
  .chip[data-tag="error"]  { --c: var(--tag-error); }
  .chip[data-active="false"] { opacity: 0.38; }
  .chip[data-active="false"]::before { background: var(--dimmer); }
  .chip:focus-visible { outline: 2px solid var(--copper); outline-offset: 2px; }

  /* ── the feed: left-edge tint carries the tag, no rainbow pills ── */
  #feed {
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
  }
  .entry:last-child { border-bottom: none; }
  .entry[data-tag="tick"]   { --c: transparent; }
  .entry[data-tag="triage"] { --c: var(--tag-triage); }
  .entry[data-tag="render"] { --c: var(--tag-render); }
  .entry[data-tag="llm"]    { --c: var(--tag-llm); }
  .entry[data-tag="push"]   { --c: var(--tag-push); }
  .entry[data-tag="error"]  { --c: var(--tag-error); }
  .entry .ts { color: var(--dimmer); flex-shrink: 0; font-size: 11px; }
  .entry .tag {
    flex-shrink: 0; width: 4.6em;
    font-size: 10px; font-weight: 700;
    text-transform: uppercase; letter-spacing: 0.06em;
    color: var(--c);
  }
  .entry[data-tag="tick"] .tag { color: var(--tag-tick); }
  .entry .summary { flex: 1; overflow-wrap: anywhere; }
  .entry[data-tag="tick"] .summary { color: var(--dim); }
  .entry[data-tag="error"] .summary { color: var(--red); }
  .entry[data-tag="llm"] { cursor: pointer; }
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
  .entry { flex-wrap: wrap; }
  .entry.expanded .detail { display: block; }
  .empty { padding: 26px; text-align: center; color: var(--dimmer); }
  @media (max-width: 760px) {
    .rail { flex-direction: column; gap: 8px; }
    .link { display: none; }
  }
</style>
</head>
<body>
<div id="banner">worker unreachable — retrying…</div>
<header>
  <h1><span class="dot"></span>synapse-worker <span class="side">· edge</span></h1>
  <div class="transcript">%%TRANSCRIPT%%</div>
</header>
<main>
  <div class="rail" aria-label="pipeline: transcript to push, in order">
    <div class="node"><div class="label">Transcript</div><div class="value" id="stat-ticks">0</div><div class="sub">ticks</div></div>
    <span class="link"></span>
    <div class="node"><div class="label">Held</div><div class="value" id="stat-held">0</div><div class="sub" id="stat-held-age">turn closed</div></div>
    <span class="link"></span>
    <div class="node"><div class="label">Triage</div><div class="value" id="stat-triage">0 · 0</div><div class="sub">keep · skip</div></div>
    <span class="link"></span>
    <div class="node npu" id="npu-now">
      <div class="label">NPU now</div>
      <div class="seg idle" id="npu-seg">idle</div>
      <div class="elapsed idle" id="npu-elapsed">—</div>
    </div>
    <span class="link"></span>
    <div class="node"><div class="label">Push</div><div class="value" id="stat-wal">0 / 0</div><div class="sub">sent / queued</div></div>
  </div>

  <div class="chips" id="chips">
    <span class="chip" data-tag="tick" data-active="true" tabindex="0">tick</span>
    <span class="chip" data-tag="triage" data-active="true" tabindex="0">triage</span>
    <span class="chip" data-tag="render" data-active="true" tabindex="0">render</span>
    <span class="chip" data-tag="llm" data-active="true" tabindex="0">llm</span>
    <span class="chip" data-tag="push" data-active="true" tabindex="0">push</span>
    <span class="chip" data-tag="error" data-active="true" tabindex="0">error</span>
  </div>

  <div id="feed"><div class="empty">waiting for the first tick…</div></div>
</main>

<script>
(function () {
  "use strict";

  var activeTags = new Set(["tick", "triage", "render", "llm", "push", "error"]);
  var heldSince = null; // client-side clock: when pending_events last became > 0
  // Keys of currently-expanded llm entries, surviving the 1s poll's full
  // innerHTML rebuild -- without this, clicking an entry open only lasts
  // until the next refresh (up to 1s), never long enough to read a preview.
  var expandedKeys = new Set();

  function llmKey(c) {
    return c.ts_iso + "|" + c.component;
  }

  function esc(s) {
    var d = document.createElement("div");
    d.textContent = String(s == null ? "" : s);
    return d.innerHTML;
  }

  function hhmmss(iso) {
    if (!iso) return "";
    var d = new Date(iso);
    return d.toTimeString().slice(0, 8);
  }

  function fmtElapsed(ms) {
    var totalSec = Math.max(0, Math.floor(ms / 1000));
    var m = Math.floor(totalSec / 60);
    var s = totalSec % 60;
    return m + "m " + String(s).padStart(2, "0") + "s";
  }

  document.getElementById("chips").addEventListener("click", function (ev) {
    var chip = ev.target.closest(".chip");
    if (!chip) return;
    var tag = chip.getAttribute("data-tag");
    if (activeTags.has(tag)) {
      activeTags.delete(tag);
      chip.setAttribute("data-active", "false");
    } else {
      activeTags.add(tag);
      chip.setAttribute("data-active", "true");
    }
    applyFilter();
  });

  function applyFilter() {
    document.querySelectorAll("#feed .entry").forEach(function (el) {
      var tag = el.getAttribute("data-tag");
      el.style.display = activeTags.has(tag) ? "" : "none";
    });
  }

  document.getElementById("feed").addEventListener("click", function (ev) {
    var entry = ev.target.closest(".entry[data-tag='llm']");
    if (!entry) return;
    var key = entry.getAttribute("data-key");
    var nowExpanded = entry.classList.toggle("expanded");
    if (!key) return;
    if (nowExpanded) { expandedKeys.add(key); } else { expandedKeys.delete(key); }
  });

  function renderFeed(events) {
    var feed = document.getElementById("feed");
    if (!events.length) {
      feed.innerHTML = '<div class="empty">waiting for the first tick…</div>';
      return;
    }
    var html = "";
    for (var i = events.length - 1; i >= 0; i--) {
      var e = events[i];
      html += '<div class="entry" data-tag="' + esc(e.tag) + '">';
      html += '<span class="ts">' + esc(hhmmss(e.ts_iso)) + '</span>';
      html += '<span class="tag">' + esc(e.tag) + '</span>';
      html += '<span class="summary">' + esc(e.summary) + '</span>';
      html += '</div>';
    }
    feed.innerHTML = html;
    applyFilter();
  }

  function renderLlmFeed(events, calls) {
    // Weave llm calls into the same tagged feed the other events use, newest
    // first, using each call's own timestamp — same visual language, and
    // clicking one expands its previews/tokens/latency.
    var feed = document.getElementById("feed");
    // Drop any expanded key whose call fell out of the (bounded) ring buffer
    // -- otherwise expandedKeys would grow forever across a long session.
    var liveKeys = new Set(calls.map(llmKey));
    expandedKeys.forEach(function (k) { if (!liveKeys.has(k)) expandedKeys.delete(k); });
    var merged = events.map(function (e) { return { kind: "event", ts: e.ts_iso, data: e }; });
    calls.forEach(function (c) {
      merged.push({ kind: "llm", ts: c.ts_iso, data: c });
    });
    merged.sort(function (a, b) { return a.ts < b.ts ? -1 : a.ts > b.ts ? 1 : 0; });

    if (!merged.length) {
      feed.innerHTML = '<div class="empty">waiting for the first tick…</div>';
      return;
    }
    var html = "";
    for (var i = merged.length - 1; i >= 0; i--) {
      var m = merged[i];
      if (m.kind === "event") {
        var e = m.data;
        html += '<div class="entry" data-tag="' + esc(e.tag) + '">';
        html += '<span class="ts">' + esc(hhmmss(e.ts_iso)) + '</span>';
        html += '<span class="tag">' + esc(e.tag) + '</span>';
        html += '<span class="summary">' + esc(e.summary) + '</span>';
        html += '</div>';
      } else {
        var c = m.data;
        var ok = c.ok ? "ok" : "FAILED";
        var key = llmKey(c);
        var cls = "entry" + (expandedKeys.has(key) ? " expanded" : "");
        html += '<div class="' + cls + '" data-tag="llm" data-key="' + esc(key) + '">';
        html += '<span class="ts">' + esc(hhmmss(c.ts_iso)) + '</span>';
        html += '<span class="tag">llm</span>';
        html += '<span class="summary">' + esc(c.component) + " · " + esc(ok) +
          " · " + esc(c.input_tokens) + "→" + esc(c.output_tokens) + " tok · " +
          esc(c.latency_ms) + "ms</span>";
        html += '</div>';
        html += '<div class="detail">' +
          "provider: " + esc(c.provider_id) + "\\n" +
          "schema_valid: " + esc(c.schema_valid) + "\\n\\n" +
          "prompt:\\n" + esc(c.prompt_preview) + "\\n\\n" +
          "output:\\n" + esc(c.output_preview) +
          '</div>';
      }
    }
    feed.innerHTML = html;
    // The detail block just emitted belongs to the .entry right before it in
    // DOM order; re-attach it as a child so the expand toggle finds it. The
    // "expanded" class (and thus the detail's visibility) was already set
    // above from expandedKeys, so a re-render mid-poll doesn't collapse an
    // entry the user has open.
    var entries = feed.querySelectorAll('.entry[data-tag="llm"]');
    entries.forEach(function (entry) {
      var next = entry.nextElementSibling;
      if (next && next.classList.contains("detail")) {
        entry.appendChild(next);
      }
    });
    applyFilter();
  }

  function render(snapshot) {
    document.getElementById("banner").classList.remove("show");

    document.getElementById("stat-ticks").textContent = snapshot.ticks.length;
    var lastTick = snapshot.ticks.length ? snapshot.ticks[snapshot.ticks.length - 1].result : null;
    if (lastTick) {
      document.getElementById("stat-wal").textContent = lastTick.sent + " / " + lastTick.pending_send;
      document.getElementById("stat-held").textContent = lastTick.pending_events;
      if (lastTick.pending_events > 0) {
        if (heldSince === null) heldSince = Date.now();
        document.getElementById("stat-held-age").textContent =
          "open " + fmtElapsed(Date.now() - heldSince);
      } else {
        heldSince = null;
        document.getElementById("stat-held-age").textContent = "turn closed";
      }
    }

    var keep = 0, skip = 0;
    snapshot.events.forEach(function (e) {
      if (e.tag === "triage") {
        if (e.summary.indexOf("skip") === 0) { skip++; } else { keep++; }
      }
    });
    var triEl = document.getElementById("stat-triage");
    if (triEl) triEl.textContent = keep + " \u00b7 " + skip;

    var segEl = document.getElementById("npu-seg");
    var elapsedEl = document.getElementById("npu-elapsed");
    if (snapshot.current) {
      segEl.textContent = snapshot.current.segment_id + " (" + snapshot.current.events + " events)";
      segEl.classList.remove("idle");
      elapsedEl.classList.remove("idle");
      var started = new Date(snapshot.current.started_iso).getTime();
      elapsedEl.textContent = fmtElapsed(Date.now() - started);
    } else {
      segEl.textContent = "idle";
      segEl.classList.add("idle");
      elapsedEl.classList.add("idle");
      elapsedEl.textContent = "—";
    }

    renderLlmFeed(snapshot.events, snapshot.llm);
  }

  var lastSnapshot = null;
  var tickHandle = null;

  function tickElapsedOnly() {
    // Between polls, keep the NPU-now timer moving without waiting on fetch.
    if (lastSnapshot && lastSnapshot.current) {
      var started = new Date(lastSnapshot.current.started_iso).getTime();
      document.getElementById("npu-elapsed").textContent = fmtElapsed(Date.now() - started);
    }
  }

  function refresh() {
    fetch("/debug/stats.json")
      .then(function (r) {
        if (!r.ok) throw new Error("bad status " + r.status);
        return r.json();
      })
      .then(function (snapshot) {
        lastSnapshot = snapshot;
        render(snapshot);
      })
      .catch(function () {
        document.getElementById("banner").classList.add("show");
      });
  }

  refresh();
  setInterval(refresh, 1000);
  setInterval(tickElapsedOnly, 250);
})();
</script>
</body>
</html>
"""


def _make_handler(stats: StatsBuffer, transcript: str | None) -> type[BaseHTTPRequestHandler]:
    page_bytes = _PAGE.replace(_TRANSCRIPT_TOKEN, transcript or "(no transcript bound)").encode(
        "utf-8"
    )

    class Handler(BaseHTTPRequestHandler):
        server_version = "SynapseWorkerDebug/1"

        def log_message(self, format: str, *args: object) -> None:  # noqa: A002
            pass  # the debug server must not spam the worker's own logs

        def do_GET(self) -> None:  # noqa: N802 - stdlib method name
            if self.path == "/debug/stats.json":
                body = json.dumps(stats.snapshot()).encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
            elif self.path == "/debug":
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(page_bytes)))
                self.end_headers()
                self.wfile.write(page_bytes)
            else:
                self.send_error(404)

        def _reject(self) -> None:
            self.send_error(405, "read-only debug endpoint")

        do_POST = _reject  # noqa: N815 - stdlib method name
        do_PUT = _reject  # noqa: N815 - stdlib method name
        do_DELETE = _reject  # noqa: N815 - stdlib method name
        do_PATCH = _reject  # noqa: N815 - stdlib method name

    return Handler


class DebugServer:
    """A tiny read-only HTTP server over a `StatsBuffer`, in a daemon thread.

    `port=0` binds an OS-chosen ephemeral port — what the test suite uses so
    runs never collide. `.start()` returns the actual bound port.
    """

    def __init__(self, stats: StatsBuffer, port: int, *, transcript: str | None = None) -> None:
        self.stats = stats
        self.port = port
        self.transcript = transcript
        self._httpd: ThreadingHTTPServer | None = None
        self._thread: threading.Thread | None = None

    def start(self) -> int:
        handler = _make_handler(self.stats, self.transcript)
        self._httpd = ThreadingHTTPServer(("127.0.0.1", self.port), handler)
        self._thread = threading.Thread(
            target=self._httpd.serve_forever, name="synapse-worker-debug", daemon=True
        )
        self._thread.start()
        return self._httpd.server_address[1]

    def stop(self) -> None:
        if self._httpd is not None:
            self._httpd.shutdown()
            self._httpd.server_close()
            self._httpd = None
        if self._thread is not None:
            self._thread.join(timeout=2)
            self._thread = None
