"""`synapse up` — start the client stack, in the foreground, from config.

What runs on this machine (and ONLY this machine's half — the service is the
server package's job, `synapse-server up`, wherever the team hosts it):

  model seam     GenieX on :18181 (client.distiller = npu, launched/adopted
                 and supervised), or no local model at all (anthropic /
                 claude-cli distil via API or the local `claude` binary;
                 listen runs nothing and `contribute` declines).
  orchestrator   :8787 — the MCP server agents point at.
  edge worker    the passive transcript follower (client.worker != off).

Nothing here is a daemon. Everything is a child of this process, dies with
Ctrl-C, and starts again on the next `synapse up`. Nothing is written at
install time; everything read here comes from `synapse configure` /
`synapse config`, overridable per-run by flags.
"""

from __future__ import annotations

import argparse
import os
import time
from datetime import datetime, timezone
from pathlib import Path

from synapse_contracts import userconfig

from synapse_cli import stack
from synapse_cli.net import lan_ip, ping_service

MODEL_URL = "http://127.0.0.1:18181/v1"
ORCHESTRATOR_URL = "http://127.0.0.1:8787"

DISTILLER_ARMS = ("npu", "anthropic", "claude-cli", "listen")


def _say(line: str = "") -> None:
    print(line, flush=True)


def _resolve(args: argparse.Namespace) -> dict[str, str]:
    """flags > config file. Anything unset that has no safe default is a
    loud, actionable error naming the exact `synapse config set` to run."""
    cfg = userconfig.load()
    service_url = (args.service_url or cfg.get("service.url") or "").rstrip("/")
    if not service_url:
        raise SystemExit(
            "no service URL configured. Where is the Synapse Service?\n"
            "  synapse config set service.url http://<host>:8899\n"
            "(whoever hosts it ran `synapse-server up` and can read the URL "
            "off that terminal), or pass --service-url for this run only.")
    contributor = (args.contributor or cfg.get("user.contributor")
                   or os.environ.get("USER") or os.environ.get("USERNAME") or "me")
    distiller = args.distiller or cfg.get("client.distiller") or "listen"
    if distiller not in DISTILLER_ARMS:
        raise SystemExit(
            f"client.distiller is {distiller!r}; valid: {', '.join(DISTILLER_ARMS)}.\n"
            f"  synapse config set client.distiller <arm>")
    worker = "off" if args.no_worker else (cfg.get("client.worker") or "on")
    return {"service_url": service_url, "contributor": contributor,
            "distiller": distiller, "worker": worker}


def _bootstrap_session(service_url: str, args: argparse.Namespace,
                       contributor: str, state_dir: Path) -> str:
    """Create, join or adopt the Shared Session, then persist its identity
    (session record + machine-scoped binding) exactly the way
    scripts/serve_local.py does — see that file for the full why on each."""
    if args.shared_id:
        served = stack.http(
            "POST", f"{service_url}/v1/sessions",
            {"purpose": args.purpose or "shared session",
             "created_by": contributor, "shared_id": args.shared_id}) or {}
        shared_id = args.shared_id
    elif args.purpose:
        served = stack.http(
            "POST", f"{service_url}/v1/sessions",
            {"purpose": args.purpose, "created_by": contributor}) or {}
        shared_id = served["shared_id"]
    else:
        # Neither --shared-id nor --purpose: adopt what the service already
        # holds. Creating a fresh session here would put this developer alone
        # in a Shared Session nobody else is in — the exact outcome joining
        # exists to avoid.
        try:
            listed = (stack.http("GET", f"{service_url}/debug/stats.json")
                      or {}).get("sessions") or []
        except Exception:
            listed = []
        if not listed:
            raise SystemExit(
                f"{service_url} holds no Shared Session yet (or its /debug "
                f"is disabled). Either start one:\n"
                f"  synapse up --purpose \"what the team is working on\"\n"
                f"or join one by id:\n"
                f"  synapse up --shared-id sh-xxxxxxxx")
        served = listed[0]
        shared_id = served["shared_id"]
        _say(f"session    adopted {shared_id}"
             + (f"  (of {len(listed)}; --shared-id picks another)"
                if len(listed) > 1 else ""))
    stack.http("POST", f"{service_url}/v1/sessions/{shared_id}/members",
               {"contributor": contributor})

    # Same persistence the MCP tools use — records what the SERVICE returned
    # (create-or-return semantics: the original creator), never what we sent.
    from synapse_orchestrator.session_meta import record_session
    record_session(state_dir, shared_id,
                   created_by=served.get("created_by"),
                   purpose=served.get("purpose"))

    # Machine-scoped binding: this machine is joined; any conversation here
    # speaks for it under its own real session id.
    from synapse_contracts import SessionBinding, write_binding
    scratch = state_dir / "scratch-transcript.jsonl"
    scratch.parent.mkdir(parents=True, exist_ok=True)
    scratch.touch(exist_ok=True)
    write_binding(state_dir / "bindings" / "claude-code.json", SessionBinding(
        agent_session_id=f"as-{contributor}",
        shared_id=shared_id, contributor=contributor, agent="claude-code",
        transcript_path=str(scratch), pinned_at=datetime.now(timezone.utc),
        scope="machine"))
    return shared_id


def cmd_up(args: argparse.Namespace) -> int:
    resolved = _resolve(args)
    service_url = resolved["service_url"]
    contributor = resolved["contributor"]
    distiller = resolved["distiller"]

    state_dir = userconfig.state_dir()
    state_dir.mkdir(parents=True, exist_ok=True)
    procs = stack.ProcessSet(state_dir)

    # The ping test, before anything is spawned: a stack brought up against a
    # dead URL fails minutes later in ways that read like local bugs.
    ok, detail = ping_service(service_url)
    if not ok:
        raise SystemExit(
            f"service    {service_url} is NOT reachable ({detail}).\n"
            f"Ask whoever hosts it to run `synapse-server up` (and to bind "
            f"the LAN, not 127.0.0.1), or point this client elsewhere:\n"
            f"  synapse config set service.url http://<host>:8899")
    _say(f"service    {service_url} — reachable ({detail})")

    # Only the orchestrator port is claimed outright — a held :18181 is a
    # legitimate state (`start_or_adopt_geniex` adopts it), and the service
    # port is not this machine's business at all.
    stack.claim_ports([stack.ORCHESTRATOR_PORT])

    supervisor: stack.SeamSupervisor | None = None
    orchestrator_env = {"SYNAPSE_DISTILLER": distiller if distiller != "listen" else "npu"}

    if distiller == "npu":
        model = stack.start_or_adopt_geniex(procs, MODEL_URL)
        orchestrator_env["SYNAPSE_BASE_URL"] = MODEL_URL
        supervisor = stack.SeamSupervisor(
            models_url=f"{MODEL_URL}/models", seam_name="geniex",
            restart=stack.geniex_restarter(procs, MODEL_URL), child=model,
            log_path=procs.logs_dir / "supervisor.log")
    elif distiller == "anthropic":
        if not os.environ.get("ANTHROPIC_API_KEY"):
            raise SystemExit(
                "client.distiller is 'anthropic' but ANTHROPIC_API_KEY is not "
                "set. Export it, or switch arms:\n"
                "  synapse config set client.distiller claude-cli")
        model_name = args.claude_model or userconfig.get("client.claude_model")
        if model_name:
            orchestrator_env["SYNAPSE_ANTHROPIC_MODEL"] = model_name
        _say(f"distiller  {model_name or 'claude-opus-5 (default)'} via the "
             f"Messages API on your own key")
    elif distiller == "claude-cli":
        import shutil as _shutil
        if not _shutil.which("claude"):
            raise SystemExit(
                "client.distiller is 'claude-cli' but `claude` is not on "
                "PATH. Install Claude Code, or switch arms:\n"
                "  synapse config set client.distiller listen")
        model_name = args.claude_model or userconfig.get("client.claude_model")
        if model_name:
            orchestrator_env["SYNAPSE_CLAUDE_CLI_MODEL"] = model_name
        _say(f"distiller  {model_name or 'sonnet (default)'} via the local "
             f"`claude` binary on your subscription — no API key at all")
    else:
        _say("model      NONE — listen-only. Queries and the briefing work; "
             "`contribute` will decline (distilling your prose needs a model "
             "on YOUR machine).")

    shared_id = _bootstrap_session(service_url, args, contributor, state_dir)

    orchestrator = procs.spawn("orchestrator", [
        str(stack.BIN / "synapse-orchestrator"),
        "--port", str(stack.ORCHESTRATOR_PORT),
        "--service-url", service_url,
        "--state-dir", str(state_dir),
        "--contributor", contributor,
    ], orchestrator_env)
    if not stack.wait_for(f"{ORCHESTRATOR_URL}/mcp", orchestrator,
                          "orchestrator", procs.log_path("orchestrator")):
        raise SystemExit("the orchestrator did not come up — see "
                         f"{procs.log_path('orchestrator')}")

    worker = None
    if resolved["worker"] != "off" and distiller != "listen":
        worker_env = {
            "SYNAPSE_STATE_DIR": str(state_dir),
            "SYNAPSE_SINK": "http",
            "SYNAPSE_UPSTREAM_URL": f"{ORCHESTRATOR_URL}/producer/findings",
            "SYNAPSE_DISTILLER": distiller,
        }
        if distiller == "npu":
            worker_env["SYNAPSE_BASE_URL"] = MODEL_URL
        worker = procs.spawn("worker", [
            str(stack.BIN / "synapse-worker"), "run",
            "--contributor", contributor, "--shared-id", shared_id,
        ], worker_env)

    _say(f"orchestr.  {ORCHESTRATOR_URL}/mcp  <- point Claude Code here")
    _say(f"session    {shared_id}  (contributor: {contributor})")
    if worker is not None:
        _say(f"worker     following this machine's agent transcripts "
             f"(log: {procs.log_path('worker')})")
    _say()
    _say("  In the project you want shared memory in (once per project):")
    _say(f"    claude mcp add --transport http --scope project synapse "
         f"{ORCHESTRATOR_URL}/mcp")
    _say("  …or `synapse configure --project <dir>` does it for you.")
    _say()
    lan = lan_ip() or "<this-machine-lan-ip>"
    _say("  A teammate joins with (after installing the client):")
    _say(f"    synapse config set service.url {service_url if '127.0.0.1' not in service_url else f'http://{lan}:8899'}")
    _say(f"    synapse up --shared-id {shared_id}")
    _say()
    _say(f"  Logs: {procs.logs_dir}")
    _say("  Ctrl-C to stop everything `synapse up` started.")

    try:
        while True:
            time.sleep(stack.PROBE_INTERVAL_S)
            if supervisor is not None:
                supervisor.tick()
            if orchestrator.poll() is not None:
                _say(f"synapse up: the orchestrator exited "
                     f"(rc={orchestrator.returncode}) — see "
                     f"{procs.log_path('orchestrator')}. Stopping.")
                return 1
            if worker is not None and worker.poll() is not None:
                # Not fatal: the commonest cause is "no agent transcript on
                # this machine yet" — queries still work through the
                # orchestrator; passive capture is what stopped.
                _say(f"synapse up: the worker exited "
                     f"(rc={worker.returncode}) — passive capture is off. "
                     f"See {procs.log_path('worker')}. The rest of the "
                     f"stack stays up.")
                worker = None
    except KeyboardInterrupt:
        _say("\nstopping")
        return 0
    finally:
        procs.stop_all()


def add_parser(sub: argparse._SubParsersAction) -> None:
    for name in ("up", "run"):
        up = sub.add_parser(
            name,
            help="start the client stack (orchestrator + worker + model seam) "
                 "in the foreground" + ("" if name == "up" else " — alias of `up`"))
        up.add_argument("--service-url", metavar="URL",
                        help="override service.url for this run only")
        up.add_argument("--contributor", metavar="NAME",
                        help="override user.contributor for this run only")
        up.add_argument("--distiller", choices=DISTILLER_ARMS,
                        help="override client.distiller for this run only")
        up.add_argument("--claude-model", metavar="MODEL",
                        help="model for the anthropic / claude-cli arms")
        up.add_argument("--shared-id", metavar="ID",
                        help="join this Shared Session (default: adopt the "
                             "service's existing one)")
        up.add_argument("--purpose", metavar="TEXT",
                        help="create a NEW Shared Session with this purpose")
        up.add_argument("--no-worker", action="store_true",
                        help="do not start the passive Edge Worker")
        up.set_defaults(func=cmd_up)
