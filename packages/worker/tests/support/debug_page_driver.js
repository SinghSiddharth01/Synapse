// Drives the worker /debug page's real client-side script (spliced in below
// at the __EXTRACTED_SCRIPT__ marker by test_debug_page_js.py) under a fake
// DOM (minidom.js, concatenated before this file) to pin the one behavior a
// pure-HTML assertion can't see: an expanded llm entry must survive the 1s
// poll's full innerHTML rebuild.

["chips", "feed", "banner", "stat-ticks", "stat-wal", "stat-held", "stat-held-age",
 "npu-seg", "npu-elapsed"].forEach(function (id) { seed("div", id); });

var LLM_CALL = {
  ts_iso: "2026-08-05T10:00:00.000Z",
  component: "distiller",
  provider_id: "npu",
  input_tokens: 120,
  output_tokens: 40,
  latency_ms: 8000,
  schema_valid: true,
  ok: true,
  prompt_preview: "some prompt preview text",
  output_preview: "some output preview text",
};

var SNAPSHOT = {
  now: "2026-08-05T10:00:01.000Z",
  current: null,
  ticks: [],
  events: [],
  llm: [LLM_CALL],
};

// The extracted script calls refresh() once, synchronously, as the very
// last thing it does while loading -- so the first response must already be
// queued before that code below runs.
var fetchQueue = [SNAPSHOT];
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

// ---- scenario: click to expand, then let the 1s poll fire again ----
function wait(ms) { return new Promise(function (r) { setTimeout(r, ms); }); }

function findLlmEntries() {
  return document.getElementById("feed").querySelectorAll('.entry[data-tag="llm"]');
}

async function main() {
  await wait(50); // let the script's own immediate refresh() resolve

  var entries = findLlmEntries();
  if (entries.length !== 1) {
    throw new Error("expected 1 llm entry after first render, got " + entries.length);
  }
  document.getElementById("feed").dispatchClick(entries[0]);
  var expandedAfterClick = entries[0].classList.contains("expanded");

  fetchQueue.push(SNAPSHOT); // simulate the 1s poll firing again, data unchanged
  intervals[1000]();
  await wait(50);

  var entriesAfterPoll = findLlmEntries();
  var expandedAfterPoll = entriesAfterPoll.length === 1
    && entriesAfterPoll[0].classList.contains("expanded");
  var detailReparented = entriesAfterPoll.length === 1
    && entriesAfterPoll[0].children.some(function (c) { return c.classList.contains("detail"); });

  console.log(JSON.stringify({
    expandedAfterClick: expandedAfterClick,
    expandedAfterPoll: expandedAfterPoll,
    detailReparented: detailReparented,
  }));
}

main().catch(function (e) {
  console.error("DRIVER_ERROR: " + (e && e.stack || e));
  process.exit(1);
});
