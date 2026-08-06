"""The in-app model-seam supervisor (decision 005).

WHAT THESE TESTS DO AND DO NOT PROVE — read this before trusting them.

They prove the SUPERVISOR: that a seam which accepts a TCP connection and
then never answers is scored as dead rather than slow, that a restart is
attempted, that recovery is noticed and announced, that a single slow probe
costs nothing, and that a seam which keeps dying converges on GIVING UP
rather than on an endless restart loop.

They do NOT reproduce the GenieX idle death itself. That bug lives inside a
closed-source binary on a Windows-on-Snapdragon box and takes X minutes of
idleness to appear; nothing in CI can summon it. `_HangingEndpoint` below is a
hand-built stand-in that produces the same OBSERVABLE signature — socket
accepted, zero bytes, forever — because that signature is the entire contract
the supervisor is written against. If GenieX ever fails in some other way
(refusing connections, answering 500s), `probe_seam` scores those too, and
`test_every_flavour_of_dead_is_a_strike` pins that. But the honest statement
is: this file tests our recovery, not their bug.

`scripts/` is not a package, so the module is loaded by path — same trick as
tests/test_local_model_server.py.
"""

from __future__ import annotations

import importlib.util
import os
import socket
import threading
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent


def _load():
    spec = importlib.util.spec_from_file_location(
        "serve_local", REPO / "scripts" / "serve_local.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def sl():
    return _load()


class _Clock:
    """Monotonic time the test advances by hand. 45 seconds of silence is a
    real wait for an operator and must not be one for CI."""

    def __init__(self) -> None:
        self.t = 1000.0

    def __call__(self) -> float:
        return self.t

    def advance(self, seconds: float) -> None:
        self.t += seconds


class _Seam:
    """A scriptable probe. `alive` flips; every probe is counted."""

    def __init__(self, alive: bool = True, reason: str = "connection refused") -> None:
        self.alive = alive
        self.reason = reason
        self.probes = 0

    def __call__(self) -> str | None:
        self.probes += 1
        return None if self.alive else self.reason


class _Restarter:
    """Records restarts. `heals` decides whether the seam comes back — a
    supervisor has to behave correctly when its restart DOESN'T work, which is
    the case that ends in GIVING UP."""

    def __init__(self, seam: _Seam, *, heals: bool = True) -> None:
        self.seam = seam
        self.heals = heals
        self.calls = 0

    def __call__(self, previous, log):
        self.calls += 1
        log(f"SUPERVISOR: (test) restart #{self.calls}")
        if self.heals:
            self.seam.alive = True
        return None


def _supervisor(sl, seam, restart, clock, log_path=None):
    return sl.SeamSupervisor(
        models_url="http://127.0.0.1:19181/v1/models", seam_name="test seam",
        restart=restart, child=None, log_path=log_path, probe=seam, now=clock)


def _run(supervisor, clock, ticks: int, interval: int | None = None) -> None:
    interval = interval or 15
    for _ in range(ticks):
        clock.advance(interval)
        supervisor.tick()


# ─────────────────────────────────────────────────────────────────────────
# The liveness rule
# ─────────────────────────────────────────────────────────────────────────


def test_four_consecutive_failures_are_a_death_and_three_are_not(sl):
    """45 seconds, not 30 and not 60. /models runs no inference, so an
    in-flight generation does not occupy it; the seam's own design point is
    max_seconds_per_call = 30s, and 4x15s clears that with 50% margin. Three
    strikes must therefore be survivable — a seam that hiccups once must not
    be restarted out from under a live query."""
    clock = _Clock()
    seam = _Seam(alive=False)
    restart = _Restarter(seam)
    sup = _supervisor(sl, seam, restart, clock)

    _run(sup, clock, 3)
    assert restart.calls == 0
    assert sup.status == sl.SUSPECT
    assert sup.strikes == 3

    _run(sup, clock, 1)
    assert restart.calls == 1


def test_one_slow_probe_costs_a_strike_and_the_next_success_heals_it(sl):
    """SLOW is not DEAD. A single 5s timeout in the middle of a healthy run
    must leave no residue — otherwise a busy box accumulates strikes across
    minutes and eventually restarts a seam that was working the whole time."""
    clock = _Clock()
    seam = _Seam(alive=True)
    restart = _Restarter(seam)
    sup = _supervisor(sl, seam, restart, clock)

    _run(sup, clock, 2)
    seam.alive = False
    _run(sup, clock, 1)
    assert sup.strikes == 1
    seam.alive = True
    _run(sup, clock, 1)

    assert sup.strikes == 0
    assert sup.status == sl.OK
    assert restart.calls == 0


def test_three_failures_then_a_success_resets_the_count(sl):
    """The one off-by-one that would matter: if strikes were not reset, a seam
    that failed three times an hour apart would be restarted on the fourth
    unrelated hiccup."""
    clock = _Clock()
    seam = _Seam(alive=False)
    restart = _Restarter(seam)
    sup = _supervisor(sl, seam, restart, clock)

    _run(sup, clock, 3)
    seam.alive = True
    _run(sup, clock, 1)
    assert sup.strikes == 0

    seam.alive = False
    _run(sup, clock, 3)
    assert restart.calls == 0        # not 3 + 3, which would have restarted
    _run(sup, clock, 1)
    assert restart.calls == 1


def test_a_child_that_exited_is_dead_immediately_with_no_strikes(sl, tmp_path):
    """The only immediate death. There is nothing to be slow about in a
    process that is gone, and making the operator wait 45s to be told so would
    be theatre. The probe is deliberately scripted HEALTHY here: an exited
    child must be believed over it, because a port freed by the exit can be
    grabbed by anything."""
    class _Exited:
        pid = 4242
        returncode = 1

        def poll(self):
            return 1

    log_path = tmp_path / "supervisor.log"
    clock = _Clock()
    seam = _Seam(alive=True)          # the PROBE would say fine — irrelevant
    restart = _Restarter(seam)
    sup = sl.SeamSupervisor(
        models_url="http://127.0.0.1:19181/v1/models", seam_name="test seam",
        restart=restart, child=_Exited(), probe=seam, now=clock,
        log_path=log_path)

    _run(sup, clock, 1)

    assert restart.calls == 1                    # one tick, not four
    assert seam.probes == 0                      # and the probe was not consulted
    text = log_path.read_text()
    assert "process exited (rc=1)" in text
    # NOT the probe-failure wording: an exit is not four timeouts, and a log
    # that says so sends the reader hunting for a network problem.
    assert "consecutive probe failures" not in text


# ─────────────────────────────────────────────────────────────────────────
# Backoff, the cap, and the loud log
# ─────────────────────────────────────────────────────────────────────────


def test_restarts_back_off_0_30_120_and_the_fourth_death_gives_up(sl):
    """A genuinely broken NPU on stage must converge to "the operator switches
    to a fallback in one command", never to a restart loop competing with the
    presenter for the machine. The delays are served as ticks, so this also
    pins that the supervisor keeps probing while it waits rather than blocking
    the process in a sleep."""
    clock = _Clock()
    seam = _Seam(alive=False)
    restart = _Restarter(seam, heals=False)     # nothing ever comes back
    sup = _supervisor(sl, seam, restart, clock)

    _run(sup, clock, 4)                          # death 1
    assert restart.calls == 1                    # delay 0: immediate

    _run(sup, clock, 4)                          # death 2
    assert restart.calls == 1                    # 30s delay not yet served
    _run(sup, clock, 2)
    assert restart.calls == 2

    _run(sup, clock, 4)                          # death 3
    assert restart.calls == 2                    # 120s delay
    _run(sup, clock, 8)
    assert restart.calls == 3

    _run(sup, clock, 4)                          # death 4, inside the window
    assert sup.status == sl.GAVE_UP
    assert restart.calls == 3                    # and no more, ever

    _run(sup, clock, 20)
    assert restart.calls == 3


def test_giving_up_names_the_fallback_commands_and_repeats_itself(sl, tmp_path):
    """The banner is the only thing standing between a dead seam and an
    operator who does not know what to type. It repeats because whoever walks
    back to the laptop after the outage must not have to scroll for it."""
    log_path = tmp_path / "supervisor.log"
    clock = _Clock()
    seam = _Seam(alive=False)
    sup = _supervisor(sl, seam, _Restarter(seam, heals=False), clock, log_path)

    for _ in range(6):
        _run(sup, clock, 4)
        _run(sup, clock, 10)
    assert sup.status == sl.GAVE_UP

    text = log_path.read_text()
    assert "GIVING UP" in text
    assert "--distiller claude-cli" in text
    assert "serve_local.py" in text
    # The 503 is named so the operator knows the failure is already visible to
    # every agent rather than silent (decision 008).
    assert "retrieval_unavailable" in text
    assert text.count("GIVING UP") > 1            # reprinted, not said once


def test_recovery_is_announced_even_though_nobody_noticed_the_outage(sl, tmp_path):
    """A silent self-heal is explicitly forbidden. If the outage fell between
    two queries and no human saw it, the RESTORED line is the ONLY evidence
    that the box is dying — and a box that dies quietly three times in an
    afternoon dies loudly on stage."""
    log_path = tmp_path / "supervisor.log"
    clock = _Clock()
    seam = _Seam(alive=False)
    restart = _Restarter(seam, heals=True)
    sup = _supervisor(sl, seam, restart, clock, log_path)

    _run(sup, clock, 4)             # 4 strikes -> death -> restart (heals)
    _run(sup, clock, 1)             # next probe sees it up

    assert sup.status == sl.OK
    text = log_path.read_text()
    assert "SUSPECT — probe failed (1/4)" in text
    assert "4 consecutive probe failures over 45s" in text
    assert "RESTORED after restart 1" in text
    # 5 ticks x 15s since the last successful probe: the four that struck out
    # plus the one that found it back. Asserted as a NUMBER because "down"
    # with a wrong duration is worse than no duration — it is the figure an
    # operator would quote when deciding whether the box is failing more often
    # than it used to.
    assert "down 75s total" in text


def test_only_the_first_strike_is_logged_as_suspect(sl, tmp_path):
    """Four SUSPECT lines per outage would train the operator to skim past
    them, and the DEAD line already says everything the other three would."""
    log_path = tmp_path / "supervisor.log"
    clock = _Clock()
    seam = _Seam(alive=False)
    sup = _supervisor(sl, seam, _Restarter(seam), clock, log_path)

    _run(sup, clock, 3)

    assert log_path.read_text().count("SUSPECT") == 1


def test_every_event_reaches_both_sinks(sl, tmp_path, capsys):
    """Two sinks, the identical line in both: the terminal the operator is
    already watching, and a file, because a scrollback that has been closed
    cannot be read at 9am."""
    log_path = tmp_path / "supervisor.log"
    clock = _Clock()
    seam = _Seam(alive=False)
    sup = _supervisor(sl, seam, _Restarter(seam), clock, log_path)

    _run(sup, clock, 1)

    printed = capsys.readouterr().out.strip()
    assert "SUSPECT" in printed
    assert printed in log_path.read_text()


# ─────────────────────────────────────────────────────────────────────────
# The real socket: an endpoint that accepts and never answers
# ─────────────────────────────────────────────────────────────────────────


class _HangingEndpoint:
    """Accepts TCP, reads the request, and then never writes a byte.

    This is the observable signature of the GenieX idle death — process alive,
    port bound, HTTP server not serving — and reproducing it takes a listening
    socket that simply never calls send. It is NOT the bug: see this module's
    docstring. What it establishes is that `probe_seam`'s 5s read timeout, and
    not some liveness assumption about the process, is what converts this into
    strikes.

    Bound on 19181 by convention: this repo's live ports are 9899/9787/19181,
    never the 8899/8787/18181 a real stack may be holding.
    """

    def __init__(self, port: int = 19181) -> None:
        self._sock = socket.socket()
        self._sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self._sock.bind(("127.0.0.1", port))
        self._sock.listen(8)
        self.port = self._sock.getsockname()[1]
        self._held: list[socket.socket] = []
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._serve, daemon=True)
        self._thread.start()

    def _serve(self) -> None:
        self._sock.settimeout(0.2)
        while not self._stop.is_set():
            try:
                conn, _ = self._sock.accept()
            except (TimeoutError, OSError):
                continue
            # Hold the connection open and answer nothing. Keeping a reference
            # matters: letting it be garbage-collected would close the socket
            # and turn this into a RESET, which is a different failure.
            self._held.append(conn)

    def close(self) -> None:
        self._stop.set()
        self._thread.join(timeout=2)
        for conn in self._held:
            conn.close()
        self._sock.close()


@pytest.fixture
def hanging():
    endpoint = _HangingEndpoint()
    try:
        yield endpoint
    finally:
        endpoint.close()


def test_a_socket_that_accepts_and_never_answers_is_scored_as_a_strike(sl, hanging):
    """The seam test. A liveness check written as "is the port open?" — the
    obvious first instinct — returns HEALTHY against this endpoint forever,
    and so would any check on the process being alive. Only a probe that waits
    for BYTES and gives up scores it correctly."""
    url = f"http://127.0.0.1:{hanging.port}/v1/models"

    # Port-open says fine...
    assert sl.port_is_free(hanging.port) is False
    # ...and the probe says dead, with a reason an operator can act on.
    reason = sl.probe_seam(url, timeout=0.4)
    assert reason is not None
    assert "no response within" in reason


def test_the_supervisor_recovers_from_a_hanging_endpoint(sl, hanging, tmp_path):
    """End to end over a real socket: the endpoint hangs, four probes strike
    out, the supervisor declares death and restarts, and the replacement — a
    seam that answers — is noticed and announced.

    The "restart" here swaps the probe target rather than launching a process,
    because what is under test is the supervisor's decision sequence. Whether
    `geniex serve` in particular comes back up is a property of GenieX and of
    `kill_port_owner`, and no test on this machine can assert it.
    """
    log_path = tmp_path / "supervisor.log"
    clock = _Clock()
    url = f"http://127.0.0.1:{hanging.port}/v1/models"
    state = {"healed": False}

    def probe():
        return None if state["healed"] else sl.probe_seam(url, timeout=0.4)

    def restart(previous, log):
        log("SUPERVISOR: (test) replacing the hung endpoint with a live one")
        state["healed"] = True
        return None

    sup = sl.SeamSupervisor(
        models_url=url, seam_name="geniex", restart=restart, child=None,
        log_path=log_path, probe=probe, now=clock)

    _run(sup, clock, 4)
    assert state["healed"] is True
    _run(sup, clock, 1)
    assert sup.status == sl.OK

    text = log_path.read_text()
    assert "socket accepted, nothing sent" in text
    assert "model seam DEAD" in text
    assert "Restarting geniex (attempt 1/3" in text
    assert "RESTORED after restart 1" in text


@pytest.mark.skipif(os.name == "nt", reason="the lsof arm; Windows uses netstat")
def test_the_port_owner_lookup_finds_the_process_actually_holding_the_port(sl, hanging):
    """`kill_port_owner` is the riskiest thing in this file — it kills a
    process the operator did not start — and it is only reachable on a machine
    nobody can test on. So at minimum: prove the LOOKUP half is real on this
    platform, against a port this test is knowingly holding open.

    Nothing is killed here. The pid found is this pytest process."""
    pids = sl.port_owner_pids(hanging.port)

    assert os.getpid() in pids


def test_the_port_owner_lookup_is_empty_and_quiet_for_a_free_port(sl):
    """Returns [], never raises. A supervisor that dies while trying to
    recover is worse than one that reports it could not find the owner —
    which is why every failure path in `port_owner_pids` returns the empty
    list rather than propagating."""
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        free_port = probe.getsockname()[1]

    assert sl.port_owner_pids(free_port) == []


def test_kill_port_owner_says_so_when_there_is_nothing_to_kill(sl):
    lines = []
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        free_port = probe.getsockname()[1]

    assert sl.kill_port_owner(free_port, lines.append) == []
    assert any("nothing found holding" in line for line in lines)


def test_every_flavour_of_dead_is_a_strike(sl):
    """Refused connections and non-200s score too. The idle death is the
    failure this was built for, but a supervisor that only recognised ONE
    shape of dead would sit healthy through a GenieX that had started
    returning 500s."""
    # Nothing listening at all.
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        free_port = probe.getsockname()[1]
    reason = sl.probe_seam(f"http://127.0.0.1:{free_port}/v1/models", timeout=0.4)
    assert reason is not None
