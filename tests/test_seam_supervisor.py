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

import http.server
import importlib.util
import os
import socket
import subprocess
import sys
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
    """Monotonic time the test advances by hand. A minute of silence is a real
    wait for an operator and must not be one for CI."""

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


def _kill(supervisor, clock, sl, interval: int | None = None) -> None:
    """Drive exactly one death: `DEATH_STRIKES` consecutive failing ticks.

    Reads the constant rather than hard-coding 4. These tests are about the
    backoff ledger and the give-up rule, which are orthogonal to how many
    strikes buy a death — before this existed they all said `_run(sup, 4)`
    and turned into failures the moment the threshold moved, obscuring the
    behaviour they actually pin."""
    _run(supervisor, clock, sl.DEATH_STRIKES, interval)


@pytest.fixture
def multi_strike(sl, monkeypatch):
    """Four strikes, whatever the shipped constant is.

    The strike-then-heal machinery has to stay correct at any threshold — it
    is what `DEATH_STRIKES = 2` and up rely on, and the tail-latency argument
    that currently justifies 1 is a property of THIS workload, not of the
    supervisor. Pinning that machinery against the live constant would have
    deleted the tests along with the behaviour the first time the number
    changed; pinning it against an explicit 4 keeps it covered and lets
    `test_the_shipped_threshold_...` below own the shipped value."""
    monkeypatch.setattr(sl, "DEATH_STRIKES", 4)
    return 4


# ─────────────────────────────────────────────────────────────────────────
# The liveness rule
# ─────────────────────────────────────────────────────────────────────────


def test_the_shipped_threshold_tolerates_one_failure_and_dies_on_the_second(sl):
    """The CURRENT setting, pinned on its own so a change to it is deliberate.

    ⟨2026-08-07⟩ `PROBE_TIMEOUT_S` is 150s because geniex serves one request
    at a time: the probe queues behind whatever the seam is working through,
    and a timeout shorter than that restarts healthy hardware.

    It has to clear a retry CHAIN, not a single call. 55s was tried first and
    still struck three times in six minutes, each on a seam that came back on
    its own during the backoff — because a response truncated at the token cap
    returns unparseable JSON and the distiller immediately retries, so two
    generations run back to back behind one probe. 2 attempts x a 500-token
    response at 8-14 tok/s is ~72-124s.

    `DEATH_STRIKES` is 2, and the reason is NOT generation length. A 60s
    end-to-end budget would afford exactly one strike, and this was first
    written that way. But `probe_seam` scores four flavours of failure and
    only the timeout one actually waits: refused, reset and any non-200
    strike INSTANTLY. At one strike a single transient HTTP 500 restarts the
    seam and kills the live generation without having waited at all — the 55s
    buys nothing on those paths because they never reach it. The strikes are
    the only tolerance for the instant flavours, which is why the assertion
    below drives a `connection refused` (instant) rather than a timeout: it
    fails if the threshold ever drops back to 1."""
    assert sl.DEATH_STRIKES == 2
    # The load-bearing pair: a strike must cost longer than the seam's
    # slowest legitimate answer, or the rule restarts working hardware.
    # 124s is the slow end of a two-attempt retry chain (2 x 500 tokens at
    # 8 tok/s). Below it the supervisor restarts hardware that is working.
    assert sl.PROBE_TIMEOUT_S > 124, "must outlast a full distiller retry chain"
    # Detection is ~5min. Affordable only because a restart no longer loses
    # work (loop.py re-queues uncharged) — revisit this bound if that changes.
    assert sl.DEATH_STRIKES * (sl.PROBE_INTERVAL_S + sl.PROBE_TIMEOUT_S) <= 320

    clock = _Clock()
    seam = _Seam(alive=False)
    restart = _Restarter(seam)
    sup = _supervisor(sl, seam, restart, clock)

    _run(sup, clock, 1)
    assert restart.calls == 0, "one instant failure must not restart the seam"
    _run(sup, clock, 1)
    assert restart.calls == 1


def test_consecutive_failures_are_a_death_and_one_short_of_it_is_not(sl, multi_strike):
    """At any threshold above 1, the last strike is the one that kills and
    every strike before it is survivable — a seam that hiccups must not be
    restarted out from under a live query.

    ⟨post-review⟩ This docstring used to justify 4 as "4x15s = 45s clears the
    seam's own max_seconds_per_call = 30s with 50% margin". That reasoning was
    inherited from the design note and is wrong twice over:
    `max_seconds_per_call` is a segment-SIZING budget (prompt bytes / measured
    prefill rate), not a wall-clock bound on a seam call, and the real
    per-call bound on this seam is OpenAICompatibleProvider's 300s."""
    clock = _Clock()
    seam = _Seam(alive=False)
    restart = _Restarter(seam)
    sup = _supervisor(sl, seam, restart, clock)

    _run(sup, clock, multi_strike - 1)
    assert restart.calls == 0
    assert sup.status == sl.SUSPECT
    assert sup.strikes == multi_strike - 1

    _run(sup, clock, 1)
    assert restart.calls == 1


def test_one_slow_probe_costs_a_strike_and_the_next_success_heals_it(sl, multi_strike):
    """SLOW is not DEAD. A single timeout in the middle of a healthy run must
    leave no residue — otherwise a busy box accumulates strikes across minutes
    and eventually restarts a seam that was working the whole time.

    Exactly the failure that shipped: with a 5s timeout against 8-35s
    generations the box WAS busy, every generation cost a strike, and four in
    a row restarted a healthy seam. Healing is what kept that from happening
    sooner, so it stays covered even though the shipped threshold no longer
    reaches a second strike."""
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


def test_failures_short_of_death_then_a_success_resets_the_count(sl, multi_strike):
    """The one off-by-one that would matter: if strikes were not reset, a seam
    that failed three times an hour apart would be restarted on the fourth
    unrelated hiccup."""
    clock = _Clock()
    seam = _Seam(alive=False)
    restart = _Restarter(seam)
    sup = _supervisor(sl, seam, restart, clock)

    _run(sup, clock, multi_strike - 1)
    seam.alive = True
    _run(sup, clock, 1)
    assert sup.strikes == 0

    seam.alive = False
    _run(sup, clock, multi_strike - 1)
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
    the process in a sleep — and
    `test_a_seam_that_recovers_during_the_backoff_delay_is_not_restarted`
    below is what proves the probing has consequences rather than merely
    happening."""
    clock = _Clock()
    seam = _Seam(alive=False)
    restart = _Restarter(seam, heals=False)     # nothing ever comes back
    sup = _supervisor(sl, seam, restart, clock)

    _kill(sup, clock, sl)                          # death 1
    assert restart.calls == 1                    # delay 0: immediate

    _kill(sup, clock, sl)                          # death 2
    assert restart.calls == 1                    # 30s delay not yet served
    _run(sup, clock, 2)
    assert restart.calls == 2

    _kill(sup, clock, sl)                          # death 3
    assert restart.calls == 2                    # 120s delay
    _run(sup, clock, 8)
    assert restart.calls == 3

    _kill(sup, clock, sl)                          # death 4, inside the window
    assert sup.status == sl.GAVE_UP
    assert restart.calls == 3                    # and no more, ever

    _run(sup, clock, 20)
    assert restart.calls == 3


def test_the_backoff_delays_are_exactly_0_30_and_120_seconds(sl):
    """The ranges the test above pins — delay 2 in (15, 30], delay 3 in
    (105, 120] — are what a 15s tick can see, and they would also be satisfied
    by 16s and 106s. This drives the clock in 1s steps and reads the delay off
    the tick that actually fires, so the constants are pinned to the second."""
    clock = _Clock()
    seam = _Seam(alive=False)
    restart = _Restarter(seam, heals=False)
    sup = _supervisor(sl, seam, restart, clock)

    served: list[float] = []
    for expected_calls in (1, 2, 3):
        _kill(sup, clock, sl)                       # four strikes -> a death
        died_at = clock.t
        while restart.calls < expected_calls:
            _run(sup, clock, 1, interval=1)
            assert clock.t - died_at < 300, "the restart never fired"
        served.append(clock.t - died_at)

    assert served == list(sl.RESTART_DELAYS_S)


def test_a_seam_that_recovers_during_the_backoff_delay_is_not_restarted(sl, tmp_path):
    """The delay is for not hammering a failing seam, NOT for not looking at
    it. Until this was fixed the supervisor returned early for the whole of a
    120s delay and then restarted unconditionally — so a seam that came back
    on its own at second 3 was killed at second 120: a healthy seam torn down,
    a self-inflicted outage, and a restart spent on the ledger that shortened
    the leash for the next real death."""
    log_path = tmp_path / "supervisor.log"
    clock = _Clock()
    seam = _Seam(alive=False)
    restart = _Restarter(seam, heals=False)
    sup = _supervisor(sl, seam, restart, clock, log_path)

    _kill(sup, clock, sl)                  # death 1, delay 0 -> restarts at once
    assert restart.calls == 1
    _kill(sup, clock, sl)                  # death 2, now serving a 30s delay
    assert restart.calls == 1

    seam.alive = True                    # ...and it comes back by itself
    _run(sup, clock, 1)

    assert restart.calls == 1            # NOT torn down
    assert sup.status == sl.OK
    text = log_path.read_text()
    assert "CANCELLED" in text
    assert "nothing was killed" in text
    # Still on the ledger: two deaths in ten minutes is a dying box whether or
    # not the second one healed itself, and the cap must still see it.
    assert len(sup.restart_times) == 2


def test_a_restart_that_never_ran_does_not_get_the_credit_for_a_recovery(sl, tmp_path):
    """`RESTORED after restart 1` is a claim about causation. When the restart
    itself raised — `geniex` not on PATH, a permission error — no restart
    happened, so whatever brought the seam back was a human or luck. Saying
    otherwise tells the operator this script is covering a box it is not."""
    log_path = tmp_path / "supervisor.log"
    clock = _Clock()
    seam = _Seam(alive=False)

    def broken_restart(previous, log):
        raise RuntimeError("geniex not on PATH")

    sup = _supervisor(sl, seam, broken_restart, clock, log_path)

    _kill(sup, clock, sl)                  # death -> restart -> raises
    seam.alive = True                    # someone fixes it by hand
    _run(sup, clock, 1)

    text = log_path.read_text()
    assert "restart 1 raised RuntimeError: geniex not on PATH" in text
    assert "NOT by this script" in text
    assert "RESTORED after restart 1 —" not in text     # the false claim


def test_after_giving_up_it_still_watches_and_says_when_the_seam_returns(sl, tmp_path):
    """GIVING UP caps the RESTARTS, not the watching. The operator who runs
    the fallback command and repairs the seam must see the terminal say so —
    otherwise the last word on screen is a banner announcing an outage that is
    over, and the next person to read it walks the fallback path for nothing."""
    log_path = tmp_path / "supervisor.log"
    clock = _Clock()
    seam = _Seam(alive=False)
    restart = _Restarter(seam, heals=False)
    sup = _supervisor(sl, seam, restart, clock, log_path)

    for _ in range(4):                   # four deaths -> GAVE_UP
        _kill(sup, clock, sl)
        _run(sup, clock, 12)
    assert sup.status == sl.GAVE_UP
    probes_before = seam.probes

    seam.alive = True                    # the operator repairs it by hand
    _run(sup, clock, 1)

    assert seam.probes > probes_before   # it never stopped looking
    assert sup.status == sl.OK
    text = log_path.read_text()
    assert "ANSWERING again after GIVING UP" in text
    assert restart.calls == 3            # and it still restarted nothing


def _hung_socket_supervisor(sl, clock, log_path):
    """A supervisor whose probe hangs for the full timeout, then strikes —
    the signature this whole module exists for."""

    def slow_failing_probe():
        clock.advance(sl.PROBE_TIMEOUT_S)     # the read timeout, spent
        return (f"no response within {sl.PROBE_TIMEOUT_S:g}s "
                f"(socket accepted, nothing sent)")

    sup = _supervisor(sl, None, _Restarter(_Seam()), clock, log_path)
    sup._probe = slow_failing_probe
    return sup


def test_the_dead_line_reports_the_span_it_measured_not_a_constant(
        sl, tmp_path, multi_strike):
    """`(DEATH_STRIKES - 1) * PROBE_INTERVAL_S` is only the span when probes
    return instantly. A probe that fails by TIMING OUT spends its own timeout
    first, so on the hung socket this supervisor exists for the real span is
    longer — and the DEAD line is the number an operator quotes when deciding
    whether the box is failing more often than it used to. Measured, not
    derived: a constant would be wrong every time it mattered."""
    log_path = tmp_path / "supervisor.log"
    clock = _Clock()
    sup = _hung_socket_supervisor(sl, clock, log_path)

    _run(sup, clock, multi_strike)

    # Strikes begin at t+15 and the fourth tick begins at t+75: four cycles of
    # (15s sleep + 5s burnt in the probe), less the first sleep.
    span = (multi_strike - 1) * (15 + sl.PROBE_TIMEOUT_S)
    assert (f"{multi_strike} consecutive probe failures over {int(span)}s"
            in log_path.read_text())


def test_a_one_strike_death_reports_the_silence_not_a_zero_second_span(
        sl, tmp_path, monkeypatch):
    monkeypatch.setattr(sl, "DEATH_STRIKES", 1)
    """At the shipped `DEATH_STRIKES = 1` the first strike IS the death, so
    the span BETWEEN strikes is zero — and "1 consecutive probe failures over
    0s" reads as a seam that died instantly when it had in fact been silent
    for a cadence plus a full probe timeout. An operator sizing how sick the
    box is would read that number and conclude the opposite of the truth, so
    the one-strike line reports the silence instead."""
    log_path = tmp_path / "supervisor.log"
    clock = _Clock()
    sup = _hung_socket_supervisor(sl, clock, log_path)

    _run(sup, clock, 1)

    text = log_path.read_text()
    assert "over 0s" not in text
    # One cadence of sleep plus the timeout the probe burned before striking.
    assert f"no answer for {int(15 + sl.PROBE_TIMEOUT_S)}s" in text


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


def test_recovery_is_announced_even_though_nobody_noticed_the_outage(
        sl, tmp_path, multi_strike):
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

    Bound on an EPHEMERAL port (0), not on 19181. 19181 is this repo's
    reserved live-verification port, which means it is exactly the port a
    live-verification run next door is holding — and a fixture that binds a
    fixed port turns that into three ERRORS ("Address already in use") rather
    than three failures, i.e. a red suite caused by nothing being wrong.
    Reproduced: holding 19181 from another process turned this file into
    `12 passed, 3 errors`. Every caller already reads `self.port` back off
    `getsockname()`, so nothing else changes. SO_REUSEADDR is kept: it costs
    nothing and stops a TIME_WAIT socket from a previous run of THIS file
    biting on the rare occasions the kernel reissues the same port.
    """

    def __init__(self, port: int = 0) -> None:
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


def _status_server(status: int, body: bytes = b"{}"):
    """A server that answers every GET with one status. Ephemeral port."""

    class _Handler(http.server.BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_message(self, *_args):
            pass

        def do_GET(self):                                  # noqa: N802
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

    server = http.server.HTTPServer(("127.0.0.1", 0), _Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server


def test_every_flavour_of_dead_is_a_strike(sl):
    """Refused connections and non-200s score too. The idle death is the
    failure this was built for, but a supervisor that only recognised ONE
    shape of dead would sit healthy through a GenieX that had started
    returning 500s.

    ⟨post-review⟩ This used to test the refused-connection arm only, while its
    docstring named 500s — so `probe_seam` returning None on every non-200
    passed the suite. All three arms are driven now: refused, 500, and 404
    (the shape a wrongly-routed seam produces)."""
    # Nothing listening at all.
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        free_port = probe.getsockname()[1]
    reason = sl.probe_seam(f"http://127.0.0.1:{free_port}/v1/models", timeout=0.4)
    assert reason is not None

    # Listening, and answering 500. urllib raises HTTPError for this, which is
    # a DIFFERENT code path in probe_seam from the URLError above.
    server = _status_server(500, b'{"error":"hexagon context lost"}')
    try:
        port = server.server_address[1]
        reason = sl.probe_seam(f"http://127.0.0.1:{port}/v1/models", timeout=2.0)
        assert reason == "HTTP 500"
    finally:
        server.shutdown()

    # Listening, and answering 404 — a seam on the port that is not this seam.
    server = _status_server(404)
    try:
        port = server.server_address[1]
        reason = sl.probe_seam(f"http://127.0.0.1:{port}/v1/models", timeout=2.0)
        assert reason == "HTTP 404"
    finally:
        server.shutdown()


def test_a_healthy_200_is_the_only_thing_that_is_not_a_strike(sl):
    """The positive control for the test above: with every non-200 scoring, a
    probe that scored 200s too would be a supervisor that restarts a working
    seam every 45 seconds."""
    server = _status_server(200, b'{"object":"list","data":[{"id":"m"}]}')
    try:
        port = server.server_address[1]
        assert sl.probe_seam(f"http://127.0.0.1:{port}/v1/models", timeout=2.0) is None
    finally:
        server.shutdown()


@pytest.mark.skipif(os.name == "nt", reason="the lsof arm; Windows uses netstat")
def test_the_port_owner_lookup_ignores_clients_and_finds_only_the_listener(
        sl, hanging):
    """The one that would have killed the demo. Plain `lsof -ti :PORT` matches
    a socket with that port at EITHER end, so it returns every CLIENT as well
    — and under `--npu` the service child holds an established connection to
    the seam for the whole of a generation, which is exactly the window in
    which the seam has gone silent and `kill_port_owner` is about to fire. The
    supervisor would have SIGKILLed the service it exists to keep working.

    Driven with a real connected client in a real separate process, because
    same-process sockets share a pid and cannot tell the two apart."""
    code = (
        "import socket, sys, time\n"
        f"s = socket.create_connection(('127.0.0.1', {hanging.port}))\n"
        "sys.stdout.write('connected\\n'); sys.stdout.flush()\n"
        "time.sleep(60)\n"
    )
    client = subprocess.Popen([sys.executable, "-c", code],
                              stdout=subprocess.PIPE, text=True)
    try:
        assert client.stdout.readline().strip() == "connected"
        pids = sl.port_owner_pids(hanging.port)
        assert os.getpid() in pids            # the listener, this process
        assert client.pid not in pids         # the client, NOT an owner
    finally:
        client.kill()
        client.wait(timeout=5)


# ─────────────────────────────────────────────────────────────────────────
# The restarters, the launcher, and the wiring
#
# Everything above tests the SeamSupervisor in isolation, with an injected
# `restart` callable — which left the real restarters, the `--npu` launcher,
# and the line in main() that builds a supervisor at all covered by nothing.
# Mutation-checked: before these, serve_local's tick could be replaced with
# `if supervisor is not None and False:` and the suite stayed green. A
# headline deliverable that can be fully unwired without a red test is not
# delivered.
# ─────────────────────────────────────────────────────────────────────────


class _FakeChild:
    def __init__(self, pid: int = 999) -> None:
        self.pid = pid
        self.returncode = None
        self.terminated = False

    def poll(self):
        return None

    def terminate(self):
        self.terminated = True

    def wait(self, timeout=None):
        return 0

    def kill(self):
        pass


def test_the_standin_restarter_respawns_the_identical_argv(sl, monkeypatch):
    """Identical matters. A restart that quietly dropped `--mode proxy` would
    leave a `--live` run replaying this repo's canned corpus while its banner
    still said LIVE — the same class of silent lie the whole workstream exists
    to close, reintroduced by the thing meant to recover from an outage."""
    argv = [sys.executable, "scripts/local_model_server.py", "--mode", "proxy",
            "--distiller-model", "Llama-3.1-8B",
            "--synthesizer-model", "Llama-3.3-70B"]
    spawned: list[tuple] = []

    def fake_spawn(name, spawn_argv, env=None, append=False):
        spawned.append((name, list(spawn_argv), append))
        return _FakeChild()

    monkeypatch.setattr(sl, "spawn", fake_spawn)
    monkeypatch.setattr(sl, "wait_for", lambda *a, **k: True)

    previous = _FakeChild()
    restart = sl.standin_restarter("http://127.0.0.1:19181/v1", argv)
    child = restart(previous, lambda line: None)

    assert previous.terminated is True
    assert spawned == [("model", argv, True)]
    assert child is not None


def test_the_geniex_restarter_clears_the_port_before_respawning(sl, monkeypatch):
    """Order is the whole point. The failure being recovered from leaves a
    LIVE process holding :18181, so there is no crash to free the port — spawn
    first and `geniex serve` dies on EADDRINUSE against the corpse of the seam
    it was replacing."""
    order: list[str] = []
    monkeypatch.setattr(sl.shutil, "which", lambda name: "/usr/local/bin/geniex")
    monkeypatch.setattr(sl, "kill_port_owner",
                        lambda port, log: order.append(f"killed {port}") or [7])

    def fake_spawn(name, argv, env=None, append=False):
        order.append(f"spawned {name} {' '.join(argv)} append={append}")
        return _FakeChild()

    monkeypatch.setattr(sl, "spawn", fake_spawn)
    monkeypatch.setattr(sl, "wait_for", lambda *a, **k: True)

    restart = sl.geniex_restarter("http://127.0.0.1:19181/v1")
    child = restart(None, lambda line: None)

    assert order == [f"killed {sl.MODEL_PORT}",
                     "spawned geniex /usr/local/bin/geniex serve append=True"]
    assert child is not None


def test_the_geniex_restarter_says_so_and_stops_when_geniex_is_gone(sl, monkeypatch):
    """No binary, no restart — and the supervisor must survive being told so,
    because its next probe is what turns this into the honest DEAD state
    rather than a crashed supervision loop."""
    monkeypatch.setattr(sl.shutil, "which", lambda name: None)
    monkeypatch.setattr(sl, "kill_port_owner", lambda port, log: [])
    monkeypatch.setattr(sl, "spawn", lambda *a, **k: pytest.fail(
        "spawned something without a `geniex` binary"))

    lines: list[str] = []
    assert sl.geniex_restarter("http://x/v1")(None, lines.append) is None
    assert any("not on PATH" in line for line in lines)


def test_the_seam_log_survives_a_restart(sl, monkeypatch, tmp_path):
    """`.open("w")` on every spawn truncated the child's log, so the dying
    seam's last words — the only artefact of the idle death anyone on the NPU
    box will ever have — were overwritten by its replacement's first line
    within a second of the crash. A restart appends; a fresh run still
    truncates, because yesterday's log explaining today's silence is worse
    than none."""
    monkeypatch.setattr(sl, "STATE", tmp_path / ".synapse")
    monkeypatch.setattr(sl, "processes", [])
    log = tmp_path / ".synapse" / "logs" / "seamtest.log"

    say = "import sys; sys.stdout.write(sys.argv[1] + chr(10))"
    sl.spawn("seamtest", [sys.executable, "-c", say,
                          "FATAL: hexagon context lost"]).wait(timeout=30)
    sl.spawn("seamtest", [sys.executable, "-c", say, "serving on 18181"],
             append=True).wait(timeout=30)

    text = log.read_text()
    assert "FATAL: hexagon context lost" in text     # the post-mortem survived
    assert "serving on 18181" in text
    assert "restarted by the supervisor" in text

    # ...and a fresh run (append=False) still starts clean.
    sl.spawn("seamtest", [sys.executable, "-c", say, "a new run"]).wait(timeout=30)
    assert "hexagon" not in log.read_text()


# ── the --npu launcher ───────────────────────────────────────────────────


def test_a_free_port_makes_serve_local_launch_geniex_itself(sl, monkeypatch, capsys):
    monkeypatch.setattr(sl, "port_is_free", lambda port: True)
    monkeypatch.setattr(sl.shutil, "which", lambda name: "/opt/geniex")
    spawned: list[list[str]] = []
    monkeypatch.setattr(sl, "spawn", lambda name, argv, env=None, append=False:
                        spawned.append(list(argv)) or _FakeChild())
    monkeypatch.setattr(sl, "wait_for", lambda *a, **k: True)

    child = sl.start_or_adopt_geniex("http://127.0.0.1:19181/v1")

    assert spawned == [["/opt/geniex", "serve"]]
    assert child is not None
    assert "LAUNCHED by this script" in capsys.readouterr().out


def test_a_free_port_with_no_geniex_binary_names_geniex_pull(sl, monkeypatch):
    """docs/JOIN.md quotes this sentence as the thing a teammate will see. It
    was quoted without ever having been produced."""
    monkeypatch.setattr(sl, "port_is_free", lambda port: True)
    monkeypatch.setattr(sl.shutil, "which", lambda name: None)
    monkeypatch.setattr(sl, "spawn", lambda *a, **k: pytest.fail("spawned"))

    with pytest.raises(SystemExit) as exit_info:
        sl.start_or_adopt_geniex("http://127.0.0.1:19181/v1")

    message = str(exit_info.value)
    assert "geniex pull" in message
    assert "NPU-RUNBOOK" in message


def test_a_geniex_that_never_answers_points_at_its_own_log(sl, monkeypatch):
    monkeypatch.setattr(sl, "port_is_free", lambda port: True)
    monkeypatch.setattr(sl.shutil, "which", lambda name: "/opt/geniex")
    monkeypatch.setattr(sl, "spawn",
                        lambda name, argv, env=None, append=False: _FakeChild())
    monkeypatch.setattr(sl, "wait_for", lambda *a, **k: False)

    with pytest.raises(SystemExit) as exit_info:
        sl.start_or_adopt_geniex("http://127.0.0.1:19181/v1")

    assert "geniex.log" in str(exit_info.value)


def test_a_port_that_answers_is_adopted_rather_than_refused(sl, monkeypatch, capsys):
    """ADOPT-UNOWNED. Refusing to boot against a teammate's hand-started
    `geniex serve` — the flow docs/NPU-RUNBOOK.md documents today — would mean
    refusing to boot on the exact machine that needs supervision most."""
    monkeypatch.setattr(sl, "port_is_free", lambda port: False)
    monkeypatch.setattr(sl, "wait_for", lambda *a, **k: True)
    monkeypatch.setattr(sl, "spawn", lambda *a, **k: pytest.fail(
        "spawned a second geniex on top of the one already serving"))

    assert sl.start_or_adopt_geniex("http://127.0.0.1:19181/v1") is None
    assert "ADOPTED" in capsys.readouterr().out


def test_a_port_held_by_something_silent_is_refused_and_named(sl, monkeypatch):
    """The already-idle-dead case at BOOT: something holds :18181 and answers
    nothing. Adopting it would mean supervising a corpse from the first
    second; the exit tells the operator to kill it, which is the one action
    that helps. docs/JOIN.md quotes this sentence too."""
    monkeypatch.setattr(sl, "port_is_free", lambda port: False)
    monkeypatch.setattr(sl, "wait_for", lambda *a, **k: False)

    with pytest.raises(SystemExit) as exit_info:
        sl.start_or_adopt_geniex("http://127.0.0.1:19181/v1")

    message = str(exit_info.value)
    assert "idle-dead" in message
    assert str(sl.MODEL_PORT) in message


# ── main() actually builds one, and actually ticks it ────────────────────


def test_serve_local_builds_a_supervisor_and_ticks_it_in_its_main_loop(
        sl, monkeypatch, tmp_path, capsys):
    """The wiring test, and the reason it exists: with every test above
    passing, `if supervisor is not None and False:` in the main loop kept the
    suite green. Every assertion here is about the seam between main() and
    SeamSupervisor — that one is constructed, that it is pointed at the model
    seam's /models route, and that the loop calls tick() on every sleep rather
    than sleeping for an hour as it used to."""
    probed: list[str] = []
    sleeps: list[float] = []

    def fake_probe(url, timeout=None):
        probed.append(url)
        return None                              # healthy: no restarts here

    def fake_sleep(seconds):
        sleeps.append(seconds)
        if len(sleeps) >= 3:
            raise KeyboardInterrupt

    monkeypatch.setattr(sl, "REPO", tmp_path)
    monkeypatch.setattr(sl, "STATE", tmp_path / ".synapse")
    monkeypatch.setattr(sl, "probe_seam", fake_probe)
    monkeypatch.setattr(sl.time, "sleep", fake_sleep)
    monkeypatch.setattr(sl, "claim_ports", lambda ports: None)
    monkeypatch.setattr(sl, "spawn",
                        lambda name, argv, env=None, append=False: _FakeChild())
    monkeypatch.setattr(sl, "wait_for", lambda *a, **k: True)
    monkeypatch.setattr(sl, "http", lambda method, url, body=None, timeout=30.0: {
        "shared_id": "sh-test", "created_by": "me", "purpose": "p"})

    assert sl.main(["--contributor", "tester", "--host", "127.0.0.1"]) == 0

    # It slept the PROBE interval, not the old hour...
    assert sleeps == [sl.PROBE_INTERVAL_S] * 3
    # ...and each sleep was followed by a tick that really probed the seam.
    assert probed == [f"{sl.STANDIN_URL}/models"] * 2
    assert "Supervising the model seam" in capsys.readouterr().out


def test_listen_only_starts_no_supervisor_because_there_is_no_seam(
        sl, monkeypatch, tmp_path):
    """The negative control for the wiring test. `--listen` runs no model at
    all, so a supervisor would probe a port nothing is meant to be on and
    report an outage every minute of a correctly-working session."""
    sleeps: list[float] = []

    def fake_sleep(seconds):
        sleeps.append(seconds)
        raise KeyboardInterrupt

    monkeypatch.setattr(sl, "REPO", tmp_path)
    monkeypatch.setattr(sl, "STATE", tmp_path / ".synapse")
    monkeypatch.setattr(sl.time, "sleep", fake_sleep)
    monkeypatch.setattr(sl, "claim_ports", lambda ports: None)
    monkeypatch.setattr(sl, "probe_seam", lambda *a, **k: pytest.fail(
        "probed a seam in --listen, where there is no model at all"))
    monkeypatch.setattr(sl, "spawn",
                        lambda name, argv, env=None, append=False: _FakeChild())
    monkeypatch.setattr(sl, "wait_for", lambda *a, **k: True)
    monkeypatch.setattr(sl, "http", lambda method, url, body=None, timeout=30.0: {
        "shared_id": "sh-test", "created_by": "me", "purpose": "p"})

    assert sl.main(["--listen", "--contributor", "tester",
                    "--host", "127.0.0.1"]) == 0
    assert sleeps == [3600]


def test_npu_live_and_service_url_is_refused_rather_than_half_honoured(
        sl, monkeypatch, tmp_path):
    """`--npu --live` configures the SERVICE to synthesize on the cloud, and
    `--service-url` means no service is started here — so the credentials were
    read, validated, and dropped on the floor while the banner still announced
    "synthesis LIVE on <host>". The silent drop this flag pair was fixed for,
    one combination further along, and worse: the key really was resolved."""
    monkeypatch.setattr(sl, "REPO", tmp_path)
    monkeypatch.setenv("INFERENCE_CLOUD_API_KEY", "k")
    monkeypatch.setenv("INFERENCE_CLOUD_BASE_URL", "https://cloud.example/v1")
    monkeypatch.setattr(sl, "claim_ports", lambda ports: pytest.fail("too late"))
    monkeypatch.setattr(sl, "spawn", lambda *a, **k: pytest.fail("too late"))

    with pytest.raises(SystemExit) as exit_info:
        sl.main(["--npu", "--live", "--service-url", "http://10.0.0.9:9899"])

    message = str(exit_info.value)
    assert "drop --live" in message
    assert "drop --service-url" in message


def _boot_once(sl, monkeypatch, tmp_path, argv):
    """Run main() through one supervised tick and hand back its banner."""
    sleeps: list[float] = []

    def fake_sleep(seconds):
        sleeps.append(seconds)
        raise KeyboardInterrupt

    monkeypatch.setattr(sl, "REPO", tmp_path)
    monkeypatch.setattr(sl, "STATE", tmp_path / ".synapse")
    monkeypatch.setattr(sl, "probe_seam", lambda *a, **k: None)
    monkeypatch.setattr(sl.time, "sleep", fake_sleep)
    monkeypatch.setattr(sl, "claim_ports", lambda ports: None)
    monkeypatch.setattr(sl, "spawn",
                        lambda name, argv_, env=None, append=False: _FakeChild())
    monkeypatch.setattr(sl, "wait_for", lambda *a, **k: True)
    monkeypatch.setattr(sl, "start_or_adopt_geniex", lambda url: _FakeChild())
    monkeypatch.setattr(sl, "http", lambda method, url, body=None, timeout=30.0: {
        "shared_id": "sh-test", "created_by": "me", "purpose": "p"})
    assert sl.main(argv + ["--contributor", "tester", "--host", "127.0.0.1"]) == 0


def test_live_says_out_loud_that_the_probe_does_not_cover_the_cloud(
        sl, monkeypatch, tmp_path, capsys):
    """⟨post-review⟩ In `--live` the thing on the model port is the local
    proxy, and `local_model_server.do_GET` answers /models from a STATIC
    payload — no upstream call, in every mode. So the probe proves the proxy
    is up and says nothing whatsoever about Cirrascale behind it, while the
    banner said, unqualified, "Supervising the model seam". That is
    reassurance without coverage: the exact shape this workstream exists to
    delete, printed by the fix for it."""
    _boot_once(sl, monkeypatch, tmp_path, ["--live"])

    banner = capsys.readouterr().out
    assert "Supervising the model seam" in banner
    assert "covers the LOCAL proxy only" in banner
    assert "Cirrascale outage is invisible to the supervisor" in banner


def test_the_npu_seam_carries_no_such_caveat_because_the_probe_is_the_model(
        sl, monkeypatch, tmp_path, capsys):
    """The negative control. Under `--npu` the thing answering /models IS the
    model, so the same note would be false and would teach the operator to
    discount a probe that is in fact conclusive."""
    _boot_once(sl, monkeypatch, tmp_path, ["--npu"])

    banner = capsys.readouterr().out
    assert "Supervising the model seam" in banner
    assert "LOCAL proxy only" not in banner


# ─────────────────────────────────────────────────────────────────────────
# The copy that actually ships
# ─────────────────────────────────────────────────────────────────────────


def test_the_installed_stack_carries_the_same_supervision_numbers():
    """`synapse up` runs synapse_cli.stack, NOT scripts/serve_local.py.

    Everything above loads serve_local by path, so before this existed the
    entire suite could stay green while the shipped supervisor ran the old
    15/5.0/4 — which is exactly what happened on the first attempt at this
    fix: the constants were corrected in the dev script only, the tests
    passed, and the machine's behaviour did not change. The two copies are
    maintained by hand (the CLI cannot import a loose script), so the thing
    worth pinning is that they have not drifted.
    """
    import importlib.util
    from pathlib import Path

    repo = Path(__file__).resolve().parent.parent
    stack_py = repo / "packages" / "cli" / "src" / "synapse_cli" / "stack.py"
    spec = importlib.util.spec_from_file_location("_stack_under_test", stack_py)
    stack = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(stack)

    serve_local = _load()
    for name in ("PROBE_INTERVAL_S", "PROBE_TIMEOUT_S", "DEATH_STRIKES",
                 "RESTART_DELAYS_S", "RESTART_WINDOW_S", "GAVE_UP_REPEAT_S"):
        assert getattr(stack, name) == getattr(serve_local, name), (
            f"{name} has drifted between the dev script and the installed "
            f"stack; `synapse up` would not behave like the tested copy")

    # And the pair the fix turns on, asserted directly rather than only by
    # equality — so reverting BOTH copies together still fails here.
    assert stack.PROBE_TIMEOUT_S > 124, "must outlast a full distiller retry chain"
    assert stack.DEATH_STRIKES >= 2, (
        "instant failure flavours (refused/reset/non-200) never reach the "
        "timeout, so more than one strike is the only tolerance for them")
