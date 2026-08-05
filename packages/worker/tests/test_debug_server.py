"""DebugServer — stdlib http.server, real sockets, read-only pinned.

Port 0 everywhere: the OS picks an ephemeral free port so these tests never
collide with a real synapse-worker or with each other.
"""

from __future__ import annotations

import json
import urllib.error
import urllib.request

from synapse_providers import CallLog

from synapse_worker.debug_server import DebugServer
from synapse_worker.stats import StatsBuffer


def _get(url: str):
    with urllib.request.urlopen(url, timeout=5) as resp:  # noqa: S310 - localhost only
        return resp.status, resp.headers.get("Content-Type", ""), resp.read().decode("utf-8")


def test_stats_json_matches_a_fresh_snapshot_and_stays_read_only_across_gets() -> None:
    """`/debug/stats.json` must be a pure read of `StatsBuffer.snapshot()` --
    the docstring's "pinned by test, not by intention" claim, actually pinned.

    A snapshot taken BEFORE any GET (`before`) is compared against the
    response of each of several GETs (`now` stripped out, since that field
    is wall-clock and expected to differ). Comparing against a PRE-request
    snapshot, repeated across N requests, is what makes this bite: a do_GET
    branch that sneaks in `stats.event(...)` or `stats.distil_finished()`
    before serializing would grow `events` or clear `current` on the very
    first GET, and every GET after that would drift further -- either way
    `got == before` fails. (The previous version of this test only checked
    `payload["events"][0]["tag"]` and the key set, which a GET that mutates
    the buffer still satisfies -- confirmed by mutation: inserting exactly
    those two calls into the do_GET handler left it green.)
    """
    stats = StatsBuffer(CallLog())
    stats.event("tick", "5 lines")
    stats.distil_started("seg-1", 3)
    server = DebugServer(stats, port=0)
    try:
        port = server.start()
        before = stats.snapshot()
        before.pop("now")

        for _ in range(3):
            status, content_type, body = _get(f"http://127.0.0.1:{port}/debug/stats.json")
            assert status == 200
            assert "json" in content_type
            payload = json.loads(body)
            assert payload["events"][0]["tag"] == "tick"
            assert set(payload) == {"now", "current", "ticks", "events", "llm"}
            got = dict(payload)
            got.pop("now")
            assert got == before
    finally:
        server.stop()


def test_debug_page_returns_html_with_required_ids() -> None:
    stats = StatsBuffer(CallLog())
    server = DebugServer(stats, port=0)
    try:
        port = server.start()
        status, content_type, body = _get(f"http://127.0.0.1:{port}/debug")
        assert status == 200
        assert "text/html" in content_type
        assert 'id="feed"' in body
        assert 'id="npu-now"' in body
        # The re-join envelope's "held (other session)" display (STATE.md
        # trap #8) — see test_worker_debug_page_js.py for the executed-JS
        # coverage of what actually populates this sub-label.
        assert 'id="stat-wal-sub"' in body
        # Plan A.5 compaction's feed chip, the render tag's sibling — see
        # test_stats.py's test_compaction_runs_before_triage_and_the_render_
        # event_sees_its_output for what actually populates the feed.
        assert 'data-tag="compaction"' in body
    finally:
        server.stop()


def test_post_is_rejected_read_only() -> None:
    stats = StatsBuffer(CallLog())
    server = DebugServer(stats, port=0)
    try:
        port = server.start()
        req = urllib.request.Request(  # noqa: S310 - localhost only
            f"http://127.0.0.1:{port}/debug/stats.json", method="POST", data=b"{}"
        )
        try:
            urllib.request.urlopen(req, timeout=5)
            raise AssertionError("expected HTTPError 405")
        except urllib.error.HTTPError as exc:
            assert exc.code == 405
    finally:
        server.stop()


def test_unknown_path_is_404() -> None:
    stats = StatsBuffer(CallLog())
    server = DebugServer(stats, port=0)
    try:
        port = server.start()
        try:
            _get(f"http://127.0.0.1:{port}/nope")
            raise AssertionError("expected HTTPError 404")
        except urllib.error.HTTPError as exc:
            assert exc.code == 404
    finally:
        server.stop()


def test_stop_frees_the_port() -> None:
    stats = StatsBuffer(CallLog())
    server = DebugServer(stats, port=0)
    port = server.start()
    server.stop()

    try:
        _get(f"http://127.0.0.1:{port}/debug")
        raise AssertionError("expected the connection to be refused")
    except (urllib.error.URLError, ConnectionRefusedError):
        pass
