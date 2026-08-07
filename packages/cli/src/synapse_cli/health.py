"""`synapse health` — what is configured, and what is actually running.

Two sections, deliberately in this order:

  configured   every key the client reads, with its value (or "unset"), and
               a live ping of service.url — configuration drift is the #1
               support question ("it worked at the office"), so the answer
               must be one command.
  running      the orchestrator, the model seam, the edge worker, the
               binding, and the prerequisites this machine's configured arms
               actually need.

PASS/WARN/FAIL per line, exit 1 only on FAIL. WARN means "runs, but you
should know". `--json` emits the same result document for scripts.
"""

from __future__ import annotations

import argparse
import json
import platform
import shutil
import sys
from dataclasses import asdict, dataclass

from synapse_contracts import userconfig

from synapse_cli.net import ping_service, ping_worker, port_listening
from synapse_cli.stack import MODEL_PORT, ORCHESTRATOR_PORT, WORKER_DEBUG_PORT


@dataclass
class Result:
    name: str
    status: str          # PASS | WARN | FAIL
    detail: str
    remedy: str = ""


def _check_config(cfg: dict[str, str]) -> list[Result]:
    results: list[Result] = []
    url = cfg.get("service.url")
    if not url:
        results.append(Result(
            "config service.url", "FAIL", "unset",
            "synapse config set service.url http://<host>:8899"))
    else:
        ok, detail = ping_service(url)
        results.append(Result(
            "config service.url",
            "PASS" if ok else "WARN",
            f"{url} — {'reachable' if ok else 'NOT reachable'} ({detail})",
            "" if ok else "is the server up? `synapse-server up` on the host; "
                          "re-point with `synapse config set service.url …`"))
    contributor = cfg.get("user.contributor")
    results.append(Result(
        "config user.contributor",
        "PASS" if contributor else "WARN",
        contributor or "unset — findings would be attributed to the OS user",
        "" if contributor else "synapse config set user.contributor <name>"))
    distiller = cfg.get("client.distiller")
    results.append(Result(
        "config client.distiller",
        "PASS" if distiller else "WARN",
        distiller or "unset — `synapse up` defaults to listen (read-only)",
        "" if distiller else "synapse config set client.distiller "
                             "npu|anthropic|claude-cli|listen"))
    return results


def _check_running(cfg: dict[str, str]) -> list[Result]:
    results: list[Result] = []
    orches = port_listening(ORCHESTRATOR_PORT)
    results.append(Result(
        "orchestrator :8787",
        "PASS" if orches else "WARN",
        "answering — agents can query" if orches else "not running",
        "" if orches else "`synapse up` starts it (nothing runs at install "
                          "time, by design)"))
    distiller = cfg.get("client.distiller") or "listen"
    if distiller == "npu":
        seam = port_listening(MODEL_PORT)
        results.append(Result(
            "model seam :18181",
            "PASS" if seam else "WARN",
            "answering" if seam else "not running (started by `synapse up`)",
            "" if seam else "`synapse up` launches and supervises "
                            "`geniex serve`"))
    # The Edge Worker, under exactly the condition `up` spawns it under
    # (up.py: `worker != "off" and distiller != "listen"`). Reporting a
    # missing worker on a machine that configured itself not to run one would
    # be a WARN nobody can act on — and worse, it would train the reader to
    # ignore the line on the machines where it means something.
    worker_cfg = cfg.get("client.worker") or "on"
    if worker_cfg != "off" and distiller != "listen":
        alive, detail = ping_worker(WORKER_DEBUG_PORT)
        results.append(Result(
            f"edge worker :{WORKER_DEBUG_PORT}",
            "PASS" if alive else "WARN",
            detail if alive else f"not answering — {detail}",
            "" if alive else "`synapse up` starts it. If the orchestrator "
                             "above is up, the worker died after it: see the "
                             "worker log named in `synapse up`'s output. "
                             "Querying still works; only passive capture from "
                             "this machine stops."))
    else:
        why = ("client.worker=off" if worker_cfg == "off"
               else "client.distiller=listen — read-only, nothing to distil")
        results.append(Result(
            "edge worker", "PASS",
            f"not started, by configuration ({why})"))

    state = userconfig.state_dir()
    bindings = sorted((state / "bindings").glob("*.json")) if \
        (state / "bindings").is_dir() else []
    results.append(Result(
        "session binding",
        "PASS" if bindings else "WARN",
        (f"{len(bindings)} binding(s) in {state / 'bindings'}"
         if bindings else "none — this machine has not joined a Shared Session"),
        "" if bindings else "`synapse up` (adopts the service's session, or "
                            "--shared-id / --purpose)"))
    return results


def _check_prereqs(cfg: dict[str, str]) -> list[Result]:
    results: list[Result] = []
    results.append(Result(
        "python", "PASS",
        f"{platform.python_version()} ({sys.executable})"))
    uv = shutil.which("uv")
    results.append(Result(
        "uv", "PASS" if uv else "WARN",
        uv or "not on PATH (only needed to install/update Synapse itself)",
        "" if uv else "curl -LsSf https://astral.sh/uv/install.sh | sh"))
    distiller = cfg.get("client.distiller") or "listen"
    claude = shutil.which("claude")
    claude_needed = distiller == "claude-cli"
    results.append(Result(
        "claude CLI",
        "PASS" if claude else ("FAIL" if claude_needed else "WARN"),
        claude or "not on PATH",
        "" if claude else
        ("client.distiller=claude-cli needs it — install Claude Code, or "
         "`synapse config set client.distiller listen`" if claude_needed
         else "needed for MCP registration and the claude-cli arm")))
    if distiller == "npu":
        geniex = shutil.which("geniex")
        results.append(Result(
            "geniex", "PASS" if geniex else "FAIL",
            geniex or "not on PATH",
            "" if geniex else "client.distiller=npu needs GenieX on this "
                              "machine — install it, or switch arms: "
                              "`synapse config set client.distiller claude-cli`"))
    return results


def gather(cfg: dict[str, str] | None = None) -> list[Result]:
    cfg = userconfig.load() if cfg is None else cfg
    return _check_config(cfg) + _check_running(cfg) + _check_prereqs(cfg)


def cmd_health(args: argparse.Namespace) -> int:
    cfg = userconfig.load()
    results = gather(cfg)
    if args.json:
        doc = {
            "config_path": str(userconfig.config_path()),
            "config": cfg,
            "results": [asdict(r) for r in results],
        }
        print(json.dumps(doc, indent=2))
    else:
        print(f"synapse health — config: {userconfig.config_path()}"
              f"{'' if userconfig.config_path().is_file() else '  (missing — run `synapse configure`)'}")
        print()
        width = max(len(r.name) for r in results)
        for r in results:
            print(f"{r.status:<5} {r.name:<{width}}  {r.detail}")
            if r.remedy:
                print(f"      {'':<{width}}  -> {r.remedy}")
        fails = sum(r.status == "FAIL" for r in results)
        warns = sum(r.status == "WARN" for r in results)
        print()
        print(f"{sum(r.status == 'PASS' for r in results)} pass, "
              f"{warns} warn, {fails} fail")
    return 1 if any(r.status == "FAIL" for r in results) else 0


def add_parser(sub: argparse._SubParsersAction) -> None:
    health = sub.add_parser(
        "health", help="show what is configured and what is running")
    health.add_argument("--json", action="store_true",
                        help="machine-readable result document")
    health.set_defaults(func=cmd_health)
