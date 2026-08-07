"""`synapse health` — the Edge Worker line.

The worker is the component `health` was missing: `up` spawns it (up.py),
`health` never looked for it, so "is Synapse actually capturing anything on
this machine" had no deterministic answer short of reading a log.

Two things are pinned here, and they are the two ways this line can lie:

  - it must only be expected under the same condition `up` spawns it under,
    or every listen-only machine grows a permanent WARN it cannot clear;
  - a port that answers is not proof of a worker. `/debug/stats.json` is
    parsed, so a stale process holding :8790 reads as FAIL-shaped, not PASS.
"""

from __future__ import annotations

import json
import socket
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest

from synapse_cli import health, net


def _line(results, prefix):
    matches = [r for r in results if r.name.startswith(prefix)]
    assert matches, f"no health line starting {prefix!r} in {[r.name for r in results]}"
    return matches[0]


# ---------------------------------------------------------------------------
# When the worker is expected — mirrors up.py's own spawn condition.
# ---------------------------------------------------------------------------

def test_ping_worker_reports_an_idling_worker_as_alive():
    """A worker under `synapse up` idles (--wait-for-binding) until a session
    is joined. Its dashboard answers with phase="waiting for a session", and
    that must read as ALIVE — the 2026-08-06 misdiagnosis was health calling
    exactly this state "the worker died after it". Real server, real probe."""
    from synapse_providers import CallLog
    from synapse_worker.debug_server import DebugServer
    from synapse_worker.stats import StatsBuffer

    stats = StatsBuffer(CallLog(), phase="waiting for a session")
    server = DebugServer(stats, 0)
    port = server.start()
    try:
        alive, detail = net.ping_worker(port)
    finally:
        server.stop()

    assert alive is True
    assert "waiting for a session" in detail


def test_worker_answering_is_a_pass_that_reports_what_it_has_done(monkeypatch):
    monkeypatch.setattr(health, "ping_worker",
                        lambda *a, **k: (True, "following transcripts — 7 tick(s) recorded"))
    results = health._check_running(
        {"client.distiller": "npu", "client.worker": "on"})

    line = _line(results, "edge worker")
    assert line.status == "PASS"
    assert "7 tick(s)" in line.detail


def test_worker_expected_but_silent_is_a_warn_that_says_what_still_works(monkeypatch):
    """A dead worker is not a dead Synapse: `query` reads shared memory over
    the orchestrator and is untouched. A WARN that did not say so would send
    someone restarting a stack whose useful half is running fine."""
    monkeypatch.setattr(health, "ping_worker",
                        lambda *a, **k: (False, "ConnectionRefusedError: [Errno 61]"))
    results = health._check_running(
        {"client.distiller": "claude-cli", "client.worker": "on"})

    line = _line(results, "edge worker")
    assert line.status == "WARN"
    assert "Querying still works" in line.remedy
    # Never FAIL: exit 1 is reserved for "this cannot work at all", and a
    # machine that can still read the team's memory is not that.
    assert line.status != "FAIL"


def test_worker_is_expected_when_client_worker_is_unset(monkeypatch):
    """up.py reads `cfg.get("client.worker") or "on"` — unset means ON. If
    health defaulted the other way it would stay quiet on exactly the default
    install, which is the one most people are running."""
    monkeypatch.setattr(health, "ping_worker", lambda *a, **k: (False, "refused"))
    results = health._check_running({"client.distiller": "npu"})
    assert _line(results, "edge worker").status == "WARN"


# ---------------------------------------------------------------------------
# When it is not — no unclearable WARNs.
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("cfg", [
    {"client.distiller": "npu", "client.worker": "off"},
    {"client.distiller": "listen", "client.worker": "on"},
    {},  # distiller unset defaults to listen, same as up.py
])
def test_worker_not_expected_is_a_pass_that_names_the_reason(cfg, monkeypatch):
    def _explode(*a, **k):
        raise AssertionError("must not probe for a worker it never asked for")
    monkeypatch.setattr(health, "ping_worker", _explode)

    line = _line(health._check_running(cfg), "edge worker")
    assert line.status == "PASS"
    assert "by configuration" in line.detail
    assert line.remedy == "", "nothing to remedy — this is the chosen setup"


# ---------------------------------------------------------------------------
# The probe itself, against a real socket.
# ---------------------------------------------------------------------------

class _Handler(BaseHTTPRequestHandler):
    payload: bytes = b"{}"
    status: int = 200

    def log_message(self, format, *args):  # noqa: A002 - stdlib signature
        pass

    def do_GET(self):  # noqa: N802 - stdlib method name
        self.send_response(self.status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(self.payload)))
        self.end_headers()
        self.wfile.write(self.payload)


def _serve(payload: bytes, status: int = 200):
    handler = type("H", (_Handler,), {"payload": payload, "status": status})
    server = HTTPServer(("127.0.0.1", 0), handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server


def test_ping_worker_reads_the_tick_count_out_of_stats_json():
    server = _serve(json.dumps(
        {"now": "2026-08-06T00:00:00Z", "ticks": [{}, {}, {}], "events": []}
    ).encode())
    try:
        alive, detail = net.ping_worker(server.server_address[1])
    finally:
        server.shutdown()
    assert alive is True
    assert "3 tick(s)" in detail


def test_ping_worker_rejects_something_else_holding_the_port():
    """The failure `--debug-port`'s own comment warns about: two workers
    racing for 8790, or an unrelated dev server on it. HTTP 200 from a
    non-worker must not read as a healthy worker."""
    server = _serve(b'{"hello": "not a worker"}')
    try:
        alive, detail = net.ping_worker(server.server_address[1])
    finally:
        server.shutdown()
    assert alive is False
    assert "not a Synapse worker" in detail


def test_ping_worker_rejects_a_port_that_answers_unparseably():
    server = _serve(b"<html>definitely not json</html>")
    try:
        alive, detail = net.ping_worker(server.server_address[1])
    finally:
        server.shutdown()
    assert alive is False
    assert "not a Synapse worker" in detail


def test_ping_worker_on_a_closed_port_never_raises():
    probe = socket.socket()
    probe.bind(("127.0.0.1", 0))
    closed = probe.getsockname()[1]
    probe.close()

    alive, detail = net.ping_worker(closed, timeout=1.0)
    assert alive is False
    assert detail
