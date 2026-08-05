// Drives the worker /debug page's real client-side script (spliced in below
// at the __EXTRACTED_SCRIPT__ marker by test_worker_debug_page_js.py) to pin
// the re-join envelope's dashboard-honesty requirement (STATE.md trap #8 /
// plan E7 Chain B1): the PUSH node's sub-label must say "held (other
// session)" when the latest tick's `held` count is nonzero, and fall back to
// the plain "sent / queued" label when it's zero.

["chips", "feed", "banner", "stat-ticks", "stat-wal", "stat-wal-sub",
 "stat-held", "stat-held-age", "npu-seg", "npu-elapsed"].forEach(
  function (id) { seed("div", id); }
);

var SNAPSHOT_WITH_HELD = {
  now: "2026-08-05T10:00:01.000Z",
  current: null,
  ticks: [{ ts_iso: "2026-08-05T10:00:00.000Z",
            result: { sent: 3, pending_send: 1, held: 2, pending_events: 0 } }],
  events: [],
  llm: [],
};

var SNAPSHOT_NO_HELD = {
  now: "2026-08-05T10:00:02.000Z",
  current: null,
  ticks: [{ ts_iso: "2026-08-05T10:00:00.000Z",
            result: { sent: 3, pending_send: 1, held: 2, pending_events: 0 } },
          { ts_iso: "2026-08-05T10:00:01.000Z",
            result: { sent: 1, pending_send: 0, held: 0, pending_events: 0 } }],
  events: [],
  llm: [],
};

// The extracted script calls refresh() once, synchronously, as the very
// last thing it does while loading -- so the first response must already be
// queued before that code below runs.
var fetchQueue = [SNAPSHOT_WITH_HELD];
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

function wait(ms) { return new Promise(function (r) { setTimeout(r, ms); }); }

async function main() {
  await wait(50); // let the script's own immediate refresh() resolve

  var subWithHeld = document.getElementById("stat-wal-sub").textContent;
  var valueWithHeld = document.getElementById("stat-wal").textContent;

  fetchQueue.push(SNAPSHOT_NO_HELD); // simulate the next tick clearing `held`
  intervals[1000]();
  await wait(50);

  var subAfterCleared = document.getElementById("stat-wal-sub").textContent;
  var valueAfterCleared = document.getElementById("stat-wal").textContent;

  console.log(JSON.stringify({
    subWithHeld: subWithHeld,
    valueWithHeld: valueWithHeld,
    subAfterCleared: subAfterCleared,
    valueAfterCleared: valueAfterCleared,
  }));
}

main().catch(function (e) {
  console.error("DRIVER_ERROR: " + (e && e.stack || e));
  process.exit(1);
});
