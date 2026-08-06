"""The rehearsal's port guard, pinned.

`scripts/rehearse_demo.py` used to hardcode 8899/8787 and boot its own service
and orchestrator on top of whatever was already listening there. The second
bind loses; `wait_up` still returns True because *something* answers; and every
beat below then asserts against a stranger's store -- while pushing the demo's
fixture corpus into that stranger's Shared Session. That is not hypothetical:
it is how fixture findings once reached a real session. Nothing about the
outcome was visible in the transcript, which is why the guard is asserted here
rather than left to the next person to notice.

Loaded by path because `scripts/` is not a package.
"""

from __future__ import annotations

import importlib.util
import socket
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent


def _load():
    spec = importlib.util.spec_from_file_location(
        "rehearse_demo", REPO / "scripts" / "rehearse_demo.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def rd():
    return _load()


PORTS = {"service": 8899, "orchestrator": 8787}


def _free(port: int, **_kw) -> bool:
    return False


def _busy(port: int, **_kw) -> bool:
    return True


def _only(*busy_ports: int):
    def probe(port: int, **_kw) -> bool:
        return port in busy_ports
    return probe


# ── the defaults are the demo's ports, unchanged ────────────────────────────

def test_defaults_are_the_demo_ports(rd):
    """The fix must not move the demo off the ports its docs and runbook name.

    docs/demo-script.md, docs/JOIN.md and the NPU runbook all quote 8899/8787;
    a fix that silently relocated the rehearsal would leave every one of them
    wrong while the rehearsal went green.
    """
    assert (rd.DEFAULT_SERVICE_PORT, rd.DEFAULT_ORCH_PORT) == (8899, 8787)
    assert rd.SVC == "http://127.0.0.1:8899"
    assert rd.ORCH == "http://127.0.0.1:8787"


# ── the guard's three branches ──────────────────────────────────────────────

def test_free_ports_proceed_and_say_so(rd):
    ok, lines = rd.preflight_ports(PORTS, adopt_running=False, probe=_free)
    assert ok
    # Loud on the happy path too: a transcript that only speaks up on failure
    # cannot distinguish "we booted our own" from "we said nothing".
    assert lines and "free" in lines[0]
    assert "8899" in lines[0] and "8787" in lines[0]


def test_an_occupied_port_refuses(rd):
    ok, lines = rd.preflight_ports(PORTS, adopt_running=False, probe=_busy)
    assert ok is False
    assert any("REFUSING" in line for line in lines)


@pytest.mark.parametrize("busy_port", [8899, 8787])
def test_either_port_alone_is_enough_to_refuse(rd, busy_port):
    """One occupied port is a refusal, not a warning.

    Both matter and for different reasons: a foreign service on 8899 is the
    one that receives the fixture pushes, and a foreign orchestrator on 8787
    is the one whose retained state-dir beats 7a-7c drive. Checking only the
    service port would leave the second half live.
    """
    ok, lines = rd.preflight_ports(PORTS, adopt_running=False,
                                   probe=_only(busy_port))
    assert ok is False
    blob = " ".join(lines)
    assert str(busy_port) in blob
    assert "ALREADY LISTENING" in blob


def test_the_refusal_names_the_ports_and_the_ways_out(rd):
    """The message has to be actionable at 2am, not merely correct."""
    _, lines = rd.preflight_ports(PORTS, adopt_running=False, probe=_busy)
    blob = " ".join(lines)
    assert "service port 8899" in blob and "orchestrator port 8787" in blob
    assert "--service-port" in blob and "--orch-port" in blob
    assert "--adopt-running" in blob


def test_adopt_running_proceeds_but_is_loud(rd):
    """The override exists; it does not get to be quiet.

    The old behaviour WAS this branch, taken unconditionally and unannounced.
    Restoring it as an opt-in is only an improvement if the transcript records
    that the measurement is of a stack this run did not boot.
    """
    ok, lines = rd.preflight_ports(PORTS, adopt_running=True, probe=_busy)
    assert ok
    blob = " ".join(lines)
    assert "--adopt-running" in blob and "ADOPTING" in blob
    assert "8899" in blob and "8787" in blob


def test_adopt_running_with_nothing_listening_still_announces(rd):
    ok, lines = rd.preflight_ports(PORTS, adopt_running=True, probe=_free)
    assert ok
    blob = " ".join(lines)
    assert "PORT GUARD OFF" in blob
    assert "nothing is listening" in blob


def test_guard_probes_every_port_it_was_given(rd):
    """No short-circuit: the report has to cover both, not stop at the first."""
    seen: list[int] = []

    def probe(port: int, **_kw) -> bool:
        seen.append(port)
        return True

    rd.preflight_ports(PORTS, adopt_running=False, probe=probe)
    assert sorted(seen) == sorted(PORTS.values())


# ── the probe itself, against a real socket ─────────────────────────────────

def test_port_is_listening_sees_a_real_listener(rd):
    """Asserted against an actual bound socket, not a mock.

    The whole guard rests on this predicate agreeing with `wait_up`'s notion
    of "something is there" -- both are plain TCP connects to 127.0.0.1, and
    a probe that disagreed would produce a guard that passes and a rehearsal
    that measures a stranger.
    """
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        listener.listen(1)
        port = listener.getsockname()[1]
        assert rd.port_is_listening(port) is True
    # Closed again: the same port must now read free. (A bound-but-not-
    # listening socket is not what this guard is about; an accepting one is.)
    assert rd.port_is_listening(port, timeout=0.25) is False
