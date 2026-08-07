"""Reachability probes shared by configure, up and health.

The rule for "reachable": ANY HTTP response counts, including 404 — a status
code proves something is listening and speaking HTTP, which is the question a
ping answers. Only connection failures and timeouts are unreachable. This is
the same convention scripts/serve_local.py's wait_for has always used.
"""

from __future__ import annotations

import json
import socket
import time
import urllib.error
import urllib.request


def ping(url: str, timeout: float = 5.0) -> tuple[bool, str]:
    """(reachable, detail). Never raises. `url` is probed as given."""
    started = time.perf_counter()
    try:
        with urllib.request.urlopen(url, timeout=timeout) as response:
            response.read()
            status = getattr(response, "status", None) or response.getcode()
        ms = int((time.perf_counter() - started) * 1000)
        return True, f"HTTP {status} in {ms}ms"
    except urllib.error.HTTPError as exc:
        ms = int((time.perf_counter() - started) * 1000)
        return True, f"HTTP {exc.code} in {ms}ms"
    except (urllib.error.URLError, TimeoutError, socket.timeout, OSError) as exc:
        reason = getattr(exc, "reason", exc)
        return False, f"{type(reason).__name__ if isinstance(reason, BaseException) else 'error'}: {reason}"
    except ValueError as exc:          # malformed URL
        return False, f"invalid URL: {exc}"


def ping_service(base_url: str, timeout: float = 5.0) -> tuple[bool, str]:
    """Ping a Synapse Service by its base URL.

    /debug first (the dashboard the service mounts by default), falling back
    to the bare base URL for a service started with --no-debug — a 404 from
    the API root still proves the service is there.
    """
    base = base_url.rstrip("/")
    ok, detail = ping(f"{base}/debug", timeout=timeout)
    if ok:
        return ok, detail
    return ping(base, timeout=timeout)


def ping_worker(port: int, host: str = "127.0.0.1",
                timeout: float = 5.0) -> tuple[bool, str]:
    """Probe the Edge Worker for proof it is alive *and* that it is ours.

    The worker is the one component a bare port check cannot answer for. It
    writes no pidfile, and its write-ahead log only advances when the
    transcript it follows does — so on a quiet afternoon a healthy worker and
    a dead one look identical by mtime. What it does have is the debug server
    `cmd_run` starts on DEFAULT_DEBUG_PORT: parsing `/debug/stats.json`
    settles both questions at once, because valid JSON carrying a `ticks` key
    is a Synapse worker and nothing else, and the tick count says how much it
    has actually done since it started.

    Deliberately stricter than `ping()`: there, any HTTP status proves
    something is listening, which is all a reachability question needs. Here a
    404 would mean something is on the worker's port that is not the worker,
    which is a FAIL dressed as a PASS — the exact mistake `--debug-port`'s own
    "stale worker still holding it" comment warns about.
    """
    url = f"http://{host}:{port}/debug/stats.json"
    try:
        with urllib.request.urlopen(url, timeout=timeout) as response:
            payload = json.loads(response.read().decode("utf-8"))
    except (urllib.error.URLError, TimeoutError, socket.timeout, OSError) as exc:
        reason = getattr(exc, "reason", exc)
        return False, f"{type(reason).__name__ if isinstance(reason, BaseException) else 'error'}: {reason}"
    except (ValueError, UnicodeDecodeError) as exc:
        return False, f"not a Synapse worker — :{port} answered unparseably ({exc})"
    if not isinstance(payload, dict) or "ticks" not in payload:
        return False, f"not a Synapse worker — something else holds :{port}"
    ticks = payload.get("ticks") or []
    return True, f"following transcripts — {len(ticks)} tick(s) recorded"


def port_listening(port: int, host: str = "127.0.0.1", timeout: float = 0.5) -> bool:
    probe = socket.socket()
    probe.settimeout(timeout)
    try:
        return probe.connect_ex((host, port)) == 0
    finally:
        probe.close()


def lan_ip() -> str | None:
    """This machine's LAN address, for the join line teammates paste.

    A UDP connect to a routable address picks the interface the OS would use,
    without sending a packet — works the same on macOS and Windows.
    """
    probe = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        probe.connect(("8.8.8.8", 80))
        return probe.getsockname()[0]
    except OSError:
        return None
    finally:
        probe.close()
