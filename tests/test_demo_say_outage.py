"""⟨decision 008, post-review⟩ The hand-driven demo tool, on a dead seam.

`scripts/demo_say.py --ask` is the one script a presenter runs BY HAND, in
front of people, and it posts to the route decision 008 taught to answer 503
`retrieval_unavailable`. `urlopen` raises on a 503, and nothing caught it: the
loud failure the whole workstream exists to produce arrived on stage as
`urllib.error.HTTPError: HTTP Error 503: Service Unavailable` and a traceback,
which reads as "the demo tool is broken" rather than as "the model seam is
down" — the outage is the honest result and it has to look like one.

These drive `main()` with the network stubbed; nothing here opens a socket.
"""

from __future__ import annotations

import importlib.util
import io
import json
import urllib.error
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent


@pytest.fixture(scope="module")
def say():
    spec = importlib.util.spec_from_file_location(
        "demo_say", REPO / "scripts" / "demo_say.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _http_error(code: int, body: dict | None):
    payload = json.dumps(body).encode() if body is not None else b"<html>nope</html>"
    return urllib.error.HTTPError(
        "http://127.0.0.1:9899/v1/sessions/sh-1/query", code,
        "Service Unavailable", {}, io.BytesIO(payload))


@pytest.fixture
def _running(say, monkeypatch):
    monkeypatch.setattr(say, "require_running",
                        lambda: ({"aditya": "as-1"}, "sh-1"))


def test_a_dead_retriever_reads_as_an_outage_not_as_a_traceback(
        say, monkeypatch, capsys, _running):
    def raise_503(*_args, **_kwargs):
        raise _http_error(503, {"error": "retrieval_unavailable",
                                "provider": "npu",
                                "detail": "retrieval model call failed on npu: "
                                          "TimeoutError: read timed out"})

    monkeypatch.setattr(say, "http", raise_503)

    # Non-zero: the presenter's shell, and anything scripting this, must be
    # able to tell an outage from an answer.
    assert say.main(["--ask", "what about timing?"]) == 1

    out = capsys.readouterr().out
    assert "DOWN, NOT EMPTY" in out
    assert "npu" in out                       # names the seam that died
    assert "supervisor.log" in out            # ...and where to watch it recover
    # The sentence it must never print instead, and the shape it used to fail in.
    assert "0 finding(s) came back" not in out
    assert "Traceback" not in out


def test_any_other_http_error_still_says_what_it_was(
        say, monkeypatch, capsys, _running):
    """The 503 branch is not allowed to swallow the rest. A 500 from the
    service, or a 502 from something in front of it, is a different problem
    with a different fix, and a tool that reported every failure as "shared
    memory is down" would send the presenter to the NPU box for a bug in the
    service."""
    def raise_500(*_args, **_kwargs):
        raise _http_error(500, None)

    monkeypatch.setattr(say, "http", raise_500)

    with pytest.raises(SystemExit) as exit_info:
        say.main(["--ask", "what about timing?"])

    message = str(exit_info.value)
    assert "500" in message
    assert "DOWN, NOT EMPTY" not in capsys.readouterr().out
