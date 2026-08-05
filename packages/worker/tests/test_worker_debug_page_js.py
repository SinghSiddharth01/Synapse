"""The worker /debug page's client-side JS, actually executed.

Every other test in this package only checks the served HTML source (e.g.
`test_debug_page_returns_html_with_required_ids`'s `'id="feed"' in body`).
That never runs the <script> tag, so it cannot see the page's headline
interaction ("clicking an llm entry expands its previews/token/latency
detail" -- Plan Task 3) silently break: the page polls
`/debug/stats.json` every second and rebuilds the whole feed with
`innerHTML`, and until this test existed nothing restored `.expanded` after
that rebuild -- an expanded entry collapsed again within one second,
regardless of whether the underlying data had changed at all.

This test extracts the REAL <script> body from a live DebugServer (same
`_get` real-HTTP fixture the rest of this file uses) and runs it under a
hand-rolled DOM (support/minidom.js) via Node, so it exercises the exact
code the browser would run, not a reimplementation of it. Skipped, not
failed, when `node` isn't on PATH.
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
import urllib.request
from pathlib import Path

import pytest
from synapse_providers import CallLog

from synapse_worker.debug_server import DebugServer
from synapse_worker.stats import StatsBuffer

SUPPORT_DIR = Path(__file__).parent / "support"

pytestmark = pytest.mark.skipif(
    shutil.which("node") is None,
    reason="node not on PATH -- this test executes the page's real client-side JS",
)


def _get_html(url: str) -> str:
    with urllib.request.urlopen(url, timeout=5) as resp:  # noqa: S310 - localhost only
        return resp.read().decode("utf-8")


def _extract_script(html: str) -> str:
    m = re.search(r"<script>(.*)</script>", html, re.S)
    assert m is not None, "served page has no <script> block"
    return m.group(1)


def _run_scenario(script: str, tmp_path: Path, driver: str = "debug_page_driver.js") -> dict:
    combined = "\n".join([
        (SUPPORT_DIR / "minidom.js").read_text(encoding="utf-8"),
        (SUPPORT_DIR / driver).read_text(encoding="utf-8")
        .replace("/*__EXTRACTED_SCRIPT__*/", script),
    ])
    js_file = tmp_path / "scenario.js"
    js_file.write_text(combined, encoding="utf-8")
    result = subprocess.run(  # noqa: S603 - fixed argv, no shell, test-only
        ["node", str(js_file)], capture_output=True, text=True, timeout=15
    )
    assert result.returncode == 0, (
        f"driver failed:\nstdout={result.stdout}\nstderr={result.stderr}"
    )
    last_line = result.stdout.strip().splitlines()[-1]
    return json.loads(last_line)


def test_an_expanded_llm_entry_survives_the_next_poll(tmp_path) -> None:
    stats = StatsBuffer(CallLog())
    server = DebugServer(stats, port=0)
    try:
        port = server.start()
        body = _get_html(f"http://127.0.0.1:{port}/debug")
    finally:
        server.stop()

    outcome = _run_scenario(_extract_script(body), tmp_path)

    assert outcome["expandedAfterClick"] is True
    # The bug: a re-render (the page's 1s poll) rebuilds #feed with
    # innerHTML and, pre-fix, never restored .expanded -- a user got under a
    # second to read a prompt/output preview before it silently collapsed.
    assert outcome["expandedAfterPoll"] is True
    assert outcome["detailReparented"] is True


def test_push_node_shows_held_when_nonzero_and_clears_when_it_drops_to_zero(tmp_path) -> None:
    """The re-join envelope's dashboard-honesty requirement (STATE.md trap
    #8 / E7 Chain B1's Done-when: "dashboards show held/compaction
    honestly"). The PUSH node's sent/queued count alone can't distinguish
    "nothing is wrong" from "N findings are silently stuck behind a
    session mismatch" -- this pins that the sub-label actually says so, and
    stops saying so the moment `held` clears."""
    stats = StatsBuffer(CallLog())
    server = DebugServer(stats, port=0)
    try:
        port = server.start()
        body = _get_html(f"http://127.0.0.1:{port}/debug")
    finally:
        server.stop()

    outcome = _run_scenario(_extract_script(body), tmp_path, driver="debug_page_driver_held.js")

    assert outcome["valueWithHeld"] == "3 / 1"
    assert "held (other session)" in outcome["subWithHeld"]
    assert "2" in outcome["subWithHeld"]

    assert outcome["valueAfterCleared"] == "1 / 0"
    assert outcome["subAfterCleared"] == "sent / queued"
