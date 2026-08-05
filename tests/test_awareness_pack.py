"""Subprocess-level tests for the Claude Code awareness pack's freshness
pointer hook (`packs/claude-code/hooks/freshness_pointer.py`) — Plan D.6's
Pack row ("when the watermark moved since this agent last looked") and
architecture.html's `#awareness` signal ③ ("speak only on change").

Run as a REAL subprocess against a REAL local HTTP server, deliberately —
never imported and called in-process. The hook's two governing rules (fail
open, silence is the feature) are contracts a subprocess boundary can break
in ways an in-process call cannot: an exception that would merely propagate
up a Python call stack in-process instead has to leave *some* trace on
stdout/stderr/exit code once there is a process boundary in the way, and
that trace is exactly what these tests pin. "The hook must be stdlib-only
and fail-open — any error path is exit 0, empty stdout, fast" is pinned
here with running code, not left as a docstring's promise.
"""

from __future__ import annotations

import ast
import http.server
import json
import os
import socket
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
HOOK = ROOT / "packs" / "claude-code" / "hooks" / "freshness_pointer.py"

# Generous relative to the hook's own 2s HTTP timeout (Plan D.6: "capped
# around two seconds") -- this is the outer bound the PLAN itself states
# ("under 3s"), not this suite's own guess.
FAIL_OPEN_BUDGET_SECONDS = 3.0


def _write_binding(state_dir: Path, *, agent: str = "claude-code",
                   shared_id: str = "sess-a", agent_session_id: str = "as-1",
                   pinned_at: str = "2026-08-05T10:00:00.000000Z") -> None:
    """Same on-disk shape `synapse_contracts.binding.write_binding` produces
    (field names, and `pinned_at` as a fixed-width-microsecond UTC ISO
    string) -- hand-written here, deliberately not imported from
    `synapse_contracts`, because the hook under test must not depend on that
    package either, and a fixture that silently could would not prove it
    doesn't."""
    bindings_dir = state_dir / "bindings"
    bindings_dir.mkdir(parents=True, exist_ok=True)
    (bindings_dir / f"{agent}.json").write_text(json.dumps({
        "agent_session_id": agent_session_id,
        "shared_id": shared_id,
        "contributor": "sid",
        "agent": agent,
        "transcript_path": "/tmp/whatever.jsonl",
        "pinned_at": pinned_at,
    }), encoding="utf-8")


def _make_handler(get_response, captured_paths: list[str] | None = None):
    class Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self):  # noqa: N802 - stdlib's naming
            if captured_paths is not None:
                captured_paths.append(self.path)
            payload = get_response()
            if isinstance(payload, bytes):
                body = payload
            else:
                body = json.dumps(payload).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *args):  # silence test output
            pass

    return Handler


class _RunningServer:
    def __init__(self, handler_cls):
        self.server = http.server.HTTPServer(("127.0.0.1", 0), handler_cls)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.server.server_port}"

    def close(self) -> None:
        self.server.shutdown()
        self.thread.join(timeout=2)


@pytest.fixture
def watermark(request):
    """A local watermark server whose canned JSON response can be mutated
    between hook invocations within one test (`watermark.body["version"] =
    2`), and which records every request path it received."""
    body: dict = {"version": 1, "new_since": 0, "by_type": {}, "conflicts": 0,
                  "topics": [], "purpose": "", "members": []}
    paths: list[str] = []
    running = _RunningServer(_make_handler(lambda: body, paths))
    running.body = body  # type: ignore[attr-defined]
    running.paths = paths  # type: ignore[attr-defined]
    yield running
    running.close()


def _run_hook(state_dir: Path, service_url: str, *, extra_env: dict | None = None,
             timeout: float = 10.0) -> subprocess.CompletedProcess:
    env = {**os.environ, "SYNAPSE_STATE_DIR": str(state_dir),
           "SYNAPSE_SERVICE_URL": service_url}
    env.pop("CLAUDE_PROJECT_DIR", None)
    if extra_env:
        env.update(extra_env)
    return subprocess.run([sys.executable, str(HOOK)], env=env,
                          capture_output=True, text=True, timeout=timeout)


# ---------------------------------------------------------------------------
# Stdlib-only, as a hard requirement, not a docstring promise.
# ---------------------------------------------------------------------------

def test_hook_imports_nothing_outside_the_standard_library():
    tree = ast.parse(HOOK.read_text(encoding="utf-8"), filename=str(HOOK))
    imported: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported.add(node.module.split(".")[0])
    non_stdlib = imported - set(sys.stdlib_module_names) - {"__future__"}
    assert non_stdlib == set(), (
        f"packs/claude-code/hooks/freshness_pointer.py imports non-stdlib "
        f"modules {non_stdlib} -- the pack must run on a machine with no "
        f"project dependencies installed")


# ---------------------------------------------------------------------------
# Silence is the feature.
# ---------------------------------------------------------------------------

def test_silent_when_version_unchanged(tmp_path, watermark):
    state_dir = tmp_path / ".synapse"
    _write_binding(state_dir)
    watermark.body["version"] = 3

    first = _run_hook(state_dir, watermark.url)  # first-ever check: baseline only
    assert first.returncode == 0
    assert first.stdout == ""

    second = _run_hook(state_dir, watermark.url)  # unchanged since baseline
    assert second.returncode == 0
    assert second.stdout == ""


def test_speaks_once_when_the_version_moves_then_falls_silent_again(tmp_path, watermark):
    state_dir = tmp_path / ".synapse"
    _write_binding(state_dir)
    watermark.body["version"] = 1

    baseline = _run_hook(state_dir, watermark.url)
    assert baseline.stdout == ""  # first-ever check never speaks

    watermark.body["version"] = 4
    watermark.body["new_since"] = 2
    watermark.body["topics"] = [{"id": "t1", "size": 3, "label": "prompt caching"}]

    moved = _run_hook(state_dir, watermark.url)
    assert moved.returncode == 0
    payload = json.loads(moved.stdout)
    output = payload["hookSpecificOutput"]
    assert output["hookEventName"] == "UserPromptSubmit"
    context = output["additionalContext"]
    assert "v4" in context
    assert "prompt caching" in context

    again = _run_hook(state_dir, watermark.url)  # same v4 as last check: silent
    assert again.returncode == 0
    assert again.stdout == ""


def test_requests_the_bound_sessions_watermark_with_the_agent_session_param(tmp_path, watermark):
    state_dir = tmp_path / ".synapse"
    _write_binding(state_dir, shared_id="sess-xyz", agent_session_id="as-42")

    _run_hook(state_dir, watermark.url)

    assert len(watermark.paths) == 1
    assert watermark.paths[0].startswith("/v1/sessions/sess-xyz/watermark")
    assert "agent_session=as-42" in watermark.paths[0]


# ---------------------------------------------------------------------------
# Fail open, absolutely.
# ---------------------------------------------------------------------------

def test_fail_open_when_not_joined(tmp_path):
    state_dir = tmp_path / ".synapse"  # no bindings/ directory at all

    start = time.monotonic()
    result = _run_hook(state_dir, "http://127.0.0.1:1")
    elapsed = time.monotonic() - start

    assert result.returncode == 0
    assert result.stdout == ""
    assert result.stderr == ""
    assert elapsed < FAIL_OPEN_BUDGET_SECONDS


def test_fail_open_when_the_service_is_unreachable(tmp_path):
    state_dir = tmp_path / ".synapse"
    _write_binding(state_dir)

    # A port nothing is listening on -- a real connection-refused, which
    # fails fast, rather than an unroutable address that could hang on some
    # networks regardless of the hook's own timeout.
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.bind(("127.0.0.1", 0))
        dead_port = probe.getsockname()[1]

    start = time.monotonic()
    result = _run_hook(state_dir, f"http://127.0.0.1:{dead_port}")
    elapsed = time.monotonic() - start

    assert result.returncode == 0
    assert result.stdout == ""
    assert result.stderr == ""
    assert elapsed < FAIL_OPEN_BUDGET_SECONDS


def test_fail_open_when_the_service_accepts_the_connection_and_never_responds(tmp_path):
    """Connection-refused (the test above) fails instantly and proves
    nothing about the hook's own `_TIMEOUT_SECONDS` -- a TCP RST needs no
    timeout to notice. The failure mode that actually threatens the "under
    3s" budget, and the one Plan D.6 rule 1 names explicitly ("unreachable,
    SLOW, or returning nonsense"), is a service that accepts the connection
    and then just never answers: an overloaded service, a paused container,
    a wedged synthesis call. Only `_TIMEOUT_SECONDS` on the `urlopen` call
    can save this test's budget from that -- deleting it (or raising it well
    past 3s) is a one-line mutant that every other fail-open test in this
    file is blind to, because all of them fail fast for reasons that have
    nothing to do with the timeout."""
    state_dir = tmp_path / ".synapse"
    _write_binding(state_dir)

    listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    listener.bind(("127.0.0.1", 0))
    listener.listen(1)
    port = listener.getsockname()[1]

    def _accept_and_hang() -> None:
        try:
            conn, _addr = listener.accept()
        except OSError:
            return
        # Accept the TCP connection -- so this is NOT connection-refused --
        # then read the request and write back nothing, ever. The hook's
        # `urlopen` is left blocked on the response exactly the way it
        # would be against a wedged Synapse Service.
        try:
            time.sleep(FAIL_OPEN_BUDGET_SECONDS + 5)
        finally:
            conn.close()

    thread = threading.Thread(target=_accept_and_hang, daemon=True)
    thread.start()
    try:
        start = time.monotonic()
        result = _run_hook(state_dir, f"http://127.0.0.1:{port}")
        elapsed = time.monotonic() - start
    finally:
        listener.close()

    assert result.returncode == 0
    assert result.stdout == ""
    assert result.stderr == ""
    assert elapsed < FAIL_OPEN_BUDGET_SECONDS


def test_fail_open_when_the_response_is_not_json(tmp_path):
    state_dir = tmp_path / ".synapse"
    _write_binding(state_dir)
    running = _RunningServer(_make_handler(lambda: b"not json at all"))
    try:
        result = _run_hook(state_dir, running.url)
    finally:
        running.close()

    assert result.returncode == 0
    assert result.stdout == ""
    assert result.stderr == ""


def test_fail_open_when_the_response_is_the_wrong_shape(tmp_path):
    state_dir = tmp_path / ".synapse"
    _write_binding(state_dir)
    # A list instead of an object -- same "response didn't match the
    # expected shape" failure class `synapse_orchestrator.briefing` guards
    # against, on a surface this hook trusts no more than that one does.
    running = _RunningServer(_make_handler(lambda: [1, 2, 3]))
    try:
        result = _run_hook(state_dir, running.url)
    finally:
        running.close()

    assert result.returncode == 0
    assert result.stdout == ""


def test_fail_open_when_the_binding_file_is_corrupt(tmp_path, watermark):
    state_dir = tmp_path / ".synapse"
    bindings_dir = state_dir / "bindings"
    bindings_dir.mkdir(parents=True)
    (bindings_dir / "claude-code.json").write_text("{not valid json", encoding="utf-8")

    result = _run_hook(state_dir, watermark.url)

    assert result.returncode == 0
    assert result.stdout == ""
    assert len(watermark.paths) == 0  # never even reached the network


def test_fail_open_when_the_state_file_is_corrupt(tmp_path, watermark):
    state_dir = tmp_path / ".synapse"
    _write_binding(state_dir)
    awareness_dir = state_dir / "awareness"
    awareness_dir.mkdir(parents=True)
    (awareness_dir / "freshness.json").write_text("{not valid json", encoding="utf-8")
    watermark.body["version"] = 9

    result = _run_hook(state_dir, watermark.url)

    assert result.returncode == 0
    assert result.stdout == ""  # corrupt state == no baseline == first-check silence


# ---------------------------------------------------------------------------
# Binding resolution mirrors synapse_orchestrator.cli._resolve_binding: most
# recently pinned product wins, and CLAUDE_PROJECT_DIR is honoured when the
# test-only SYNAPSE_STATE_DIR override is absent.
# ---------------------------------------------------------------------------

def test_the_most_recently_pinned_product_wins_when_two_are_bound(tmp_path, watermark):
    state_dir = tmp_path / ".synapse"
    _write_binding(state_dir, agent="claude-code", shared_id="sess-old",
                   agent_session_id="as-old", pinned_at="2026-08-05T09:00:00.000000Z")
    _write_binding(state_dir, agent="codex", shared_id="sess-new",
                   agent_session_id="as-new", pinned_at="2026-08-05T10:00:00.000000Z")

    _run_hook(state_dir, watermark.url)

    assert watermark.paths[0].startswith("/v1/sessions/sess-new/watermark")


def test_state_dir_resolves_from_claude_project_dir_when_no_override_is_set(tmp_path, watermark):
    project_dir = tmp_path / "project"
    project_dir.mkdir()
    _write_binding(project_dir / ".synapse")
    watermark.body["version"] = 1

    env = {**os.environ, "SYNAPSE_SERVICE_URL": watermark.url,
           "CLAUDE_PROJECT_DIR": str(project_dir)}
    env.pop("SYNAPSE_STATE_DIR", None)
    result = subprocess.run([sys.executable, str(HOOK)], env=env,
                            capture_output=True, text=True, timeout=10)

    assert result.returncode == 0
    assert len(watermark.paths) == 1
