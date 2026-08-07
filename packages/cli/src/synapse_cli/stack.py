"""Process plumbing for `synapse up` — spawn, wait, claim ports, supervise.

Ported from scripts/serve_local.py (the repo's in-checkout runner) with the
repo paths parameterized, so the installed client needs no checkout. The
supervision rule, the strike counts and the restart ledger are the same ones
that file documents at length; the comments here keep only the operational
essentials — see serve_local.py for the full derivations.

Services started here are NEVER daemons: everything is a child of the
foreground `synapse up`, logs to <state>/logs/<name>.log, and dies with it.
"""

from __future__ import annotations

import json
import os
import shutil
import signal
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request
from collections import deque
from datetime import datetime, timezone
from pathlib import Path

MODEL_PORT = 18181
ORCHESTRATOR_PORT = 8787
# `up` spawns `synapse-worker run` without --debug-port and sets no
# SYNAPSE_DEBUG_PORT, so the worker's debug server lands on its own
# DEFAULT_DEBUG_PORT. Mirrored here rather than imported: the CLI does not
# depend on synapse-worker, and `health` must be able to look for a worker on
# a machine where the worker package is not installed at all.
WORKER_DEBUG_PORT = 8790

PROBE_INTERVAL_S = 15
PROBE_TIMEOUT_S = 5.0
DEATH_STRIKES = 4
RESTART_DELAYS_S = (0, 30, 120)
RESTART_WINDOW_S = 600
GAVE_UP_REPEAT_S = 60

OK, SUSPECT, DEAD, GAVE_UP = "OK", "SUSPECT", "DEAD", "GAVE_UP"

# The venv's own bin dir — where synapse-orchestrator / synapse-worker live,
# whichever venv (uv tool, repo .venv) this CLI itself was installed into.
# On Windows CreateProcess appends .exe on its own.
BIN = Path(sys.executable).parent


class ProcessSet:
    """The children of one `synapse up`, plus where their logs go."""

    def __init__(self, state_dir: Path) -> None:
        self.state_dir = state_dir
        self.logs_dir = state_dir / "logs"
        self.processes: list[tuple[str, subprocess.Popen]] = []

    def spawn(self, name: str, argv: list[str],
              env: dict[str, str] | None = None,
              append: bool = False) -> subprocess.Popen:
        """Child with stdout+stderr in <logs>/<name>.log. `append` is what a
        RESTART passes — truncating would destroy the dying process's last
        words, the only evidence of why the seam died."""
        self.logs_dir.mkdir(parents=True, exist_ok=True)
        path = self.logs_dir / f"{name}.log"
        log = path.open("a" if append else "w")
        if append:
            log.write(f"\n===== restarted by the supervisor at "
                      f"{datetime.now(timezone.utc).isoformat(timespec='seconds')}"
                      f" — everything above is the PREVIOUS {name} =====\n")
            log.flush()
        process = subprocess.Popen(argv, env={**os.environ, **(env or {})},
                                   stdout=log, stderr=subprocess.STDOUT)
        self.processes.append((name, process))
        return process

    def stop_all(self) -> None:
        for _, process in reversed(self.processes):
            if process.poll() is None:
                process.terminate()
        for _, process in reversed(self.processes):
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()

    def log_path(self, name: str) -> Path:
        return self.logs_dir / f"{name}.log"


def http(method: str, url: str, body: dict | None = None, timeout: float = 30.0):
    request = urllib.request.Request(
        url, data=json.dumps(body).encode() if body is not None else None,
        headers={"Content-Type": "application/json"}, method=method)
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return json.loads(response.read() or b"null")


def wait_for(url: str, process: subprocess.Popen | None, name: str,
             log_path: Path | None = None, seconds: float = 25.0) -> bool:
    deadline = time.time() + seconds
    while time.time() < deadline:
        if process is not None and process.poll() is not None:
            where = f" — see {log_path}" if log_path else ""
            raise SystemExit(f"{name} exited{where}")
        try:
            urllib.request.urlopen(url, timeout=2.0).read()
            return True
        except urllib.error.HTTPError:
            return True
        except (urllib.error.URLError, TimeoutError, OSError):
            time.sleep(0.4)
    return False


def claim_ports(ports: list[int]) -> None:
    """Refuse to start when a port we must bind is held. A second `synapse up`
    on the same machine would otherwise overwrite the first's binding and
    silently become the same participant."""
    taken = []
    for port in ports:
        probe = socket.socket()
        probe.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            probe.bind(("127.0.0.1", port))
        except OSError:
            taken.append(port)
        finally:
            probe.close()
    if taken:
        raise SystemExit(
            f"ports already in use: {', '.join(str(p) for p in taken)}\n"
            "Another Synapse client is running on this machine — one "
            "`synapse up` per laptop. Stop it first (Ctrl-C in its terminal), "
            "or check `synapse health`.")


def port_is_free(port: int, host: str = "127.0.0.1") -> bool:
    probe = socket.socket()
    probe.settimeout(0.5)
    try:
        return probe.connect_ex((host, port)) != 0
    finally:
        probe.close()


def port_owner_pids(port: int) -> list[int]:
    """Whoever is LISTENING on `port`. [] on any failure — a supervisor that
    raises while recovering is worse than one that says it could not find the
    owner. `-sTCP:LISTEN` is load-bearing: without it lsof also returns the
    CLIENTS of the port, and the recovery would kill the service it is trying
    to keep alive."""
    try:
        if os.name == "nt":
            out = subprocess.run(["netstat", "-ano"], capture_output=True,
                                 text=True, timeout=10).stdout
            pids = set()
            for line in out.splitlines():
                parts = line.split()
                if (len(parts) >= 5 and parts[0].upper() == "TCP"
                        and parts[1].rsplit(":", 1)[-1] == str(port)
                        and parts[3].upper() == "LISTENING"
                        and parts[4].isdigit()):
                    pids.add(int(parts[4]))
            return sorted(pids)
        out = subprocess.run(["lsof", "-ti", f"tcp:{port}", "-sTCP:LISTEN"],
                             capture_output=True, text=True, timeout=10).stdout
        return sorted({int(tok) for tok in out.split() if tok.isdigit()})
    except (OSError, subprocess.SubprocessError, ValueError):
        return []


def _pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def kill_port_owner(port: int, log) -> list[int]:
    """SIGTERM the port's owner, SIGKILL what survives 5s. Every pid is
    logged with how it was found — killing a process the operator did not
    start must never happen silently."""
    pids = port_owner_pids(port)
    finder = ("netstat -ano" if os.name == "nt"
              else f"lsof -ti tcp:{port} -sTCP:LISTEN")
    if not pids:
        log(f"SUPERVISOR: nothing found holding :{port} (via {finder}) — "
            f"respawning without killing anything.")
        return []
    log(f"SUPERVISOR: killing the process holding :{port} — pid(s) "
        f"{', '.join(str(p) for p in pids)}, found via `{finder}`.")
    if os.name == "nt":
        for pid in pids:
            try:
                subprocess.run(["taskkill", "/F", "/PID", str(pid)],
                               capture_output=True, timeout=10)
            except (OSError, subprocess.SubprocessError):
                log(f"SUPERVISOR: taskkill failed for pid {pid}.")
        return pids
    for pid in pids:
        try:
            os.kill(pid, signal.SIGTERM)
        except OSError as exc:
            log(f"SUPERVISOR: SIGTERM to pid {pid} failed ({exc.__class__.__name__}).")
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline and any(_pid_alive(p) for p in pids):
        time.sleep(0.2)
    for pid in pids:
        if _pid_alive(pid):
            log(f"SUPERVISOR: pid {pid} ignored SIGTERM for 5s — SIGKILL.")
            try:
                os.kill(pid, signal.SIGKILL)
            except OSError:
                pass
    return pids


def probe_seam(models_url: str, timeout: float = PROBE_TIMEOUT_S) -> str | None:
    """None when the seam is ALIVE; a short reason string for one strike.
    GET /v1/models: zero tokens, zero inference, served by every seam that
    can sit on the model port."""
    try:
        with urllib.request.urlopen(models_url, timeout=timeout) as response:
            response.read()
            status = getattr(response, "status", None) or response.getcode()
            return None if status == 200 else f"HTTP {status}"
    except urllib.error.HTTPError as exc:
        return f"HTTP {exc.code}"
    except urllib.error.URLError as exc:
        reason = exc.reason
        if isinstance(reason, (TimeoutError, socket.timeout)):
            return f"no response within {timeout:g}s (socket accepted, nothing sent)"
        return f"{type(reason).__name__ if isinstance(reason, BaseException) else 'URLError'}: {reason}"
    except (TimeoutError, socket.timeout):
        return f"no response within {timeout:g}s (socket accepted, nothing sent)"
    except OSError as exc:
        return f"{exc.__class__.__name__}: {exc}"


class SeamSupervisor:
    """Probes the model seam and restarts it. Same rule as serve_local.py
    decision 005: 4 consecutive failed /models probes = dead (a seam that
    cannot answer metadata for a minute is not busy, it is not serving);
    restarts at 0/30/120s, at most 3 per 10 minutes, then GIVE UP loudly and
    keep probing so recovery is announced."""

    def __init__(self, *, models_url: str, seam_name: str, restart,
                 child: subprocess.Popen | None = None,
                 log_path: Path | None = None, probe=None, now=time.monotonic,
                 fallbacks: str = "") -> None:
        self.models_url = models_url
        self.seam_name = seam_name
        self._restart = restart
        self.child = child
        self.log_path = log_path
        self._probe = probe or (lambda: probe_seam(models_url))
        self._now = now
        self._fallbacks = fallbacks or (
            "`synapse config set client.distiller claude-cli` (or anthropic) "
            "and re-run `synapse up` to keep distilling without this seam")

        self.status = OK
        self.strikes = 0
        self.last_ok = now()
        self.restart_times: deque[float] = deque()
        self._restart_at: float | None = None
        self._first_strike_at: float | None = None
        self._awaiting_restore: int | None = None
        self._restart_error: str | None = None
        self._last_gave_up_banner = 0.0

    def log(self, line: str) -> None:
        stamped = f"[{datetime.now(timezone.utc).strftime('%H:%M:%S')}Z] {line}"
        print(stamped, flush=True)
        if self.log_path is not None:
            try:
                self.log_path.parent.mkdir(parents=True, exist_ok=True)
                with self.log_path.open("a", encoding="utf-8") as handle:
                    handle.write(stamped + "\n")
            except OSError:
                pass

    def tick(self) -> None:
        now = self._now()

        if self.status == GAVE_UP:
            if self._probe() is None:
                self.log(
                    f"SUPERVISOR: model seam is ANSWERING again after GIVING "
                    f"UP — down {int(now - self.last_ok)}s. This process did "
                    f"not fix it; supervision resumes from here.")
                self.restart_times.clear()
                self._awaiting_restore = None
                self._restart_error = None
                self._alive(now)
                return
            if now - self._last_gave_up_banner >= GAVE_UP_REPEAT_S:
                self._banner_gave_up()
                self._last_gave_up_banner = now
            return

        if (self._restart_at is None and self.child is not None
                and self.child.poll() is not None):
            self.strikes = 0
            self._declare_dead(
                now, f"the {self.seam_name} process exited "
                     f"(rc={self.child.returncode})", immediate=True)
            return

        if self._restart_at is not None:
            if self._probe() is None:
                self.log(
                    f"SUPERVISOR: model seam came back on its own during the "
                    f"backoff delay — restart {len(self.restart_times)} "
                    f"CANCELLED. It stays on the ledger.")
                self._restart_at = None
                self._alive(now)
                return
            if now < self._restart_at:
                return
            self._restart_at = None
            self._perform_restart()
            return

        reason = self._probe()
        if reason is None:
            self._alive(now)
        else:
            self._strike(now, reason)

    def _alive(self, now: float) -> None:
        if self._awaiting_restore is not None:
            down = int(now - self.last_ok)
            if self._restart_error is None:
                self.log(f"SUPERVISOR: model seam RESTORED after restart "
                         f"{self._awaiting_restore} — down {down}s total.")
            else:
                self.log(f"SUPERVISOR: model seam RESTORED — down {down}s "
                         f"total — but NOT by this process: restart "
                         f"{self._awaiting_restore} itself failed "
                         f"({self._restart_error}).")
            self._awaiting_restore = None
            self._restart_error = None
        elif self.strikes:
            self.log(f"SUPERVISOR: model seam OK again after "
                     f"{self.strikes} failed probe(s) — no restart was needed.")
        self.strikes = 0
        self._first_strike_at = None
        self.status = OK
        self.last_ok = now

    def _strike(self, now: float, reason: str) -> None:
        self.strikes += 1
        if self.strikes == 1:
            self.status = SUSPECT
            self._first_strike_at = now
            self.log(f"SUPERVISOR: model seam SUSPECT — probe failed "
                     f"(1/{DEATH_STRIKES}): {reason}")
        if self.strikes >= DEATH_STRIKES:
            self._declare_dead(now, reason)

    def _declare_dead(self, now: float, reason: str, *,
                      immediate: bool = False) -> None:
        self.status = DEAD
        self.strikes = 0
        while self.restart_times and now - self.restart_times[0] > RESTART_WINDOW_S:
            self.restart_times.popleft()
        attempt = len(self.restart_times) + 1
        down = int(now - self.last_ok)
        if attempt > len(RESTART_DELAYS_S):
            self.status = GAVE_UP
            self._banner_gave_up()
            self._last_gave_up_banner = now
            return
        delay = RESTART_DELAYS_S[attempt - 1]
        span = int(now - self._first_strike_at) if self._first_strike_at else 0
        cause = (reason if immediate else
                 f"{DEATH_STRIKES} consecutive probe failures over {span}s "
                 f"(last OK {down}s ago; last reason: {reason})")
        self.log(
            f"SUPERVISOR: model seam DEAD — {cause}. "
            f"Restarting {self.seam_name} (attempt {attempt}/"
            f"{len(RESTART_DELAYS_S)} in this {RESTART_WINDOW_S // 60}-min "
            f"window, delay {delay}s).")
        self.restart_times.append(now)
        self._restart_at = now + delay
        if delay == 0:
            self._restart_at = None
            self._perform_restart()

    def _perform_restart(self) -> None:
        attempt = len(self.restart_times)
        self._restart_error = None
        try:
            self.child = self._restart(self.child, self.log)
        except SystemExit as exc:
            self.log(f"SUPERVISOR: restart {attempt} did not come up ({exc}).")
        except Exception as exc:                                # noqa: BLE001
            self._restart_error = f"{exc.__class__.__name__}: {exc}"
            self.log(f"SUPERVISOR: restart {attempt} raised "
                     f"{exc.__class__.__name__}: {exc}")
        self._awaiting_restore = attempt

    def _banner_gave_up(self) -> None:
        self.log(
            f"SUPERVISOR: GIVING UP — {len(RESTART_DELAYS_S)} restarts in "
            f"{RESTART_WINDOW_S // 60} minutes did not hold. The seam stays "
            f"DOWN and every query will say so (503 retrieval_unavailable). "
            f"Fallbacks: {self._fallbacks}.")


def _terminate_child(child: subprocess.Popen | None, name: str, log) -> None:
    if child is None or child.poll() is not None:
        return
    log(f"SUPERVISOR: stopping the previous {name} child (pid {child.pid}).")
    child.terminate()
    try:
        child.wait(timeout=5)
    except subprocess.TimeoutExpired:
        log(f"SUPERVISOR: pid {child.pid} ignored terminate for 5s — killing it.")
        child.kill()


def geniex_restarter(procs: ProcessSet, model_url: str):
    """Restart `geniex serve`, killing the port owner first — the observed
    failure leaves a live process holding the port with its HTTP server dead,
    so nothing can rebind until someone kills by port."""
    def restart(previous: subprocess.Popen | None, log):
        _terminate_child(previous, "geniex", log)
        kill_port_owner(MODEL_PORT, log)
        geniex = shutil.which("geniex")
        if geniex is None:
            log("SUPERVISOR: `geniex` is not on PATH, so this process cannot "
                "relaunch it. The seam stays DOWN.")
            return None
        child = procs.spawn("geniex", [geniex, "serve"], append=True)
        wait_for(f"{model_url}/models", child, "geniex",
                 procs.log_path("geniex"), 25)
        return child
    return restart


def start_or_adopt_geniex(procs: ProcessSet, model_url: str) -> subprocess.Popen | None:
    """If :18181 is free, launch `geniex serve` and own it; if something is
    already answering there, ADOPT it (supervise by probe; on a declared
    death, kill by port owner and replace with an owned child)."""
    if port_is_free(MODEL_PORT):
        geniex = shutil.which("geniex")
        if geniex is None:
            raise SystemExit(
                f"client.distiller is 'npu', nothing is serving on "
                f":{MODEL_PORT}, and `geniex` is not on PATH.\n"
                f"Install GenieX and `geniex pull` a model — or switch arms:\n"
                f"  synapse config set client.distiller claude-cli")
        child = procs.spawn("geniex", [geniex, "serve"])
        if not wait_for(f"{model_url}/models", child, "geniex",
                        procs.log_path("geniex"), 25):
            raise SystemExit(
                f"`geniex serve` was started but never answered "
                f"{model_url}/models within 25s — see "
                f"{procs.log_path('geniex')}.")
        print(f"model      GenieX LAUNCHED on {model_url} and supervised: "
              f"probed every {PROBE_INTERVAL_S}s, restarted if it stops "
              f"answering", flush=True)
        return child

    if not wait_for(f"{model_url}/models", None, "npu", None, seconds=3):
        raise SystemExit(
            f"something is holding :{MODEL_PORT} but not answering "
            f"{model_url}/models. If that is a GenieX that has gone "
            f"idle-dead, kill it and re-run `synapse up` — it will launch "
            f"and supervise its own.")
    print(f"model      ADOPTED what is already on {model_url} — supervised "
          f"by probe. If it stops answering, `synapse up` kills whatever "
          f"holds :{MODEL_PORT} and relaunches `geniex serve` itself.",
          flush=True)
    return None
