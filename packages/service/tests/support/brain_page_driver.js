// Drives the BRAIN page's real client-side script (spliced in below at the
// __EXTRACTED_SCRIPT__ marker by test_service_brain_page_js.py) under the same
// fake DOM the log page's driver uses, to pin the behaviours a string
// assertion on the served HTML cannot see:
//
//   1. a revision the operator clicked open survives the 1s poll's full
//      innerHTML rebuild -- the exact defect the log page's driver exists for,
//      and this page rebuilds two lists rather than one;
//   2. the roster and the recent list actually render rows from a payload;
//   3. a participant with nothing known about them renders an em-dash rather
//      than a zero -- the honesty property the whole page is built on.

["banner", "session-select", "purpose", "ident",
 "stat-contributors", "stat-contributors-log", "stat-conversations",
 "stat-visible", "stat-superseded",
 "stat-trivial", "stat-conflicts", "stat-version", "stat-entries",
 "wm-meta", "wm-body", "rev-count", "revisions", "participants",
 "recent"].forEach(function (id) { seed("div", id); });

var NOW = new Date().toISOString();

var SESSION = {
  sid: "sh-1a2b3c4d",
  purpose: "ship the retrieval fix before Friday",
  created_by: "sid",
  status: "active",
  memory_version: 7,
  counts: {
    contributors: 2, contributors_in_log: 1, conversations: 2,
    visible: 18, superseded: 6, trivial: 2, conflicts: 1, log_entries: 63,
  },
  working_memory: {
    text: "the team is chasing a decode failure",
    words: 6,
    version: 7,
    updated_iso: NOW,
    revisions: [
      { version: 7, ts_iso: NOW, words: 6, delta_words: 2,
        text: "the team is chasing a decode failure" },
      { version: 6, ts_iso: NOW, words: 4, delta_words: null,
        text: "the team is chasing" },
    ],
  },
  participants: [
    { contributor: "sid", agent: "claude-code", agent_session: "as-window-1",
      contributions: 6, first_contribution_iso: NOW, last_contribution_iso: NOW,
      last_query_iso: NOW, last_query_scope: "conversation",
      last_seen_version: 6, behind: 1, state: "active" },
    { contributor: "sid", agent: "claude-code", agent_session: "as-window-2",
      contributions: 2, first_contribution_iso: NOW, last_contribution_iso: NOW,
      last_query_iso: null, last_query_scope: "none",
      last_seen_version: null, behind: null, state: "active" },
    { contributor: "aditya", agent: null, agent_session: null,
      contributions: 0, first_contribution_iso: null, last_contribution_iso: null,
      last_query_iso: null, last_query_scope: "none",
      last_seen_version: null, behind: null, state: "listening" },
    // In the log, never in `members`, no departure observed -- and carrying a
    // quote in the id that reaches a `title` attribute, because every one of
    // these strings comes off another teammate's machine unvalidated.
    { contributor: "mallory", agent: "codex",
      agent_session: 'as-q" onmouseover="x',
      contributions: 1, first_contribution_iso: NOW, last_contribution_iso: NOW,
      last_query_iso: null, last_query_scope: "none",
      last_seen_version: null, behind: null, state: "unregistered" },
  ],
  recent: [
    { id: "f-005a-01", ts_iso: NOW, type: "learning", provenance: "synthesized",
      text: "a merged finding", authors: ["sid", "aditya"],
      attributions: [
        { contributor: "sid", agent_session: "as-window-1", agent: "claude-code" },
        { contributor: "aditya", agent_session: "as-x", agent: "codex" },
      ],
      status: "kept", merged_into: null },
    { id: "f-005b-01", ts_iso: NOW, type: "decision", provenance: "contributed",
      text: "a contributed finding", authors: ["aditya"],
      attributions: [
        { contributor: "aditya", agent_session: "as-x", agent: "codex" },
      ],
      status: "kept", merged_into: null },
    // `FindingId` is a bare `str` (contracts/schemas.py) written by whoever
    // POSTed it, and it lands in `data-key`. If the page escapes only text
    // nodes, this breaks out of the attribute.
    { id: 'f-x" onmouseover="alert(1)', ts_iso: NOW, type: "gotcha",
      provenance: "distilled", text: "a hostile id", authors: ["mallory"],
      attributions: [
        { contributor: "mallory", agent_session: 'as-q" onmouseover="x',
          agent: "codex" },
      ],
      status: "kept", merged_into: null },
  ],
};

var PAYLOAD = {
  sessions: [
    { shared_id: "sh-1a2b3c4d", purpose: "p", status: "active" },
    // `shared_id` is client-supplied through `POST /v1/sessions` and lands in
    // an `<option value="...">`.
    { shared_id: 'sh-q" onfocus="alert(1)', purpose: "p2", status: "ended" },
  ],
  session: SESSION,
};

// Snapshotted BEFORE the extracted script runs, because the page mutates the
// payload it is handed (`renderSession` stamps `memory_version_ref` onto every
// participant row). Comparing post-render keys against the server's would be
// comparing the fixture to itself plus the page's own additions.
var FIXTURE_KEYS = {
  session: Object.keys(SESSION).sort(),
  counts: Object.keys(SESSION.counts).sort(),
  working_memory: Object.keys(SESSION.working_memory).sort(),
  revision: Object.keys(SESSION.working_memory.revisions[0]).sort(),
  participant: Object.keys(SESSION.participants[0]).sort(),
  recent: Object.keys(SESSION.recent[0]).sort(),
  attribution: Object.keys(SESSION.recent[0].attributions[0]).sort(),
  sessions_row: Object.keys(PAYLOAD.sessions[0]).sort(),
};

// The extracted script calls refresh() once, synchronously, as the very last
// thing it does while loading -- so the first response must already be queued
// before that code below runs.
var fetchQueue = [PAYLOAD];
function fetch(url) {
  var payload = fetchQueue.length ? fetchQueue.shift() : null;
  return Promise.resolve({
    ok: true,
    status: 200,
    json: function () { return Promise.resolve(payload); },
  });
}

var intervals = {};
function setInterval(fn, ms) { intervals[ms] = fn; return ms; }

/*__EXTRACTED_SCRIPT__*/

// ---- scenario ----
function wait(ms) { return new Promise(function (r) { setTimeout(r, ms); }); }

function revisions() {
  return document.getElementById("revisions").querySelectorAll(".rev");
}

function rows() {
  return document.getElementById("recent").querySelectorAll(".row");
}

async function main() {
  await wait(50); // let the script's own immediate refresh() resolve

  var revs = revisions();
  if (revs.length !== 2) {
    throw new Error("expected 2 revisions after first render, got " + revs.length);
  }
  var recentRows = rows();
  var participantsHtml = document.getElementById("participants").innerHTML;

  document.getElementById("revisions").dispatchClick(revs[0]);
  var expandedAfterClick = revs[0].classList.contains("expanded");

  fetchQueue.push(PAYLOAD); // the 1s poll fires again, data unchanged
  intervals[1000]();
  await wait(50);

  var revsAfter = revisions();
  var expandedAfterPoll = revsAfter.length === 2
    && revsAfter[0].classList.contains("expanded")
    && !revsAfter[1].classList.contains("expanded");

  var captured = {
    recentHtml: document.getElementById("recent").innerHTML,
    selectHtml: document.getElementById("session-select").innerHTML,
    workingMemory: document.getElementById("wm-body").textContent,
    wmMeta: document.getElementById("wm-meta").textContent,
    expandedKey: revsAfter[0].getAttribute("data-key"),
  };

  // Third poll: the service now holds no session at all (restarted, or the
  // last one was never recreated). Every region must clear -- a "…" body over
  // a rail of stale numbers would read as a session with an empty memory.
  fetchQueue.push({ sessions: [], session: null });
  intervals[1000]();
  await wait(50);
  var empty = {
    purpose: document.getElementById("purpose").textContent,
    wmBody: document.getElementById("wm-body").textContent,
    contributors: document.getElementById("stat-contributors").textContent,
    version: document.getElementById("stat-version").textContent,
    revisions: document.getElementById("revisions").innerHTML,
  };

  console.log(JSON.stringify({
    emptyState: empty,
    revisionCount: revs.length,
    recentCount: recentRows.length,
    expandedAfterClick: expandedAfterClick,
    expandedAfterPoll: expandedAfterPoll,
    // Rendered by the first refresh, captured before the poll -- the real
    // first paint, not a re-render.
    rosterHtml: participantsHtml,
    provenances: recentRows.map(function (r) { return r.getAttribute("data-prov"); }),
    // Captured with a session still loaded, before the empty poll cleared
    // everything: the rendered HTML of the two lists that carry
    // client-supplied strings into attributes, plus the session picker.
    workingMemory: captured.workingMemory,
    wmMeta: captured.wmMeta,
    expandedKey: captured.expandedKey,
    recentHtml: captured.recentHtml,
    selectHtml: captured.selectHtml,
    // The fixture's own key names, echoed back so the Python side can
    // compare them against what `_brain_payload` really produces. Without
    // this the fixture is free to drift from the server and stay green.
    fixtureKeys: FIXTURE_KEYS,
  }));
}

main().catch(function (e) {
  console.error("DRIVER_ERROR: " + (e && e.stack || e));
  process.exit(1);
});
