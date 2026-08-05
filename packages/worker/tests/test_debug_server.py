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


def test_stats_json_matches_a_fresh_snapshot() -> None:
    stats = StatsBuffer(CallLog())
    stats.event("tick", "5 lines")
    server = DebugServer(stats, port=0)
    try:
        port = server.start()
        status, content_type, body = _get(f"http://127.0.0.1:{port}/debug/stats.json")
        assert status == 200
        assert "json" in content_type
        payload = json.loads(body)
        assert payload["events"][0]["tag"] == "tick"
        assert set(payload) == {"now", "current", "ticks", "events", "llm"}
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
