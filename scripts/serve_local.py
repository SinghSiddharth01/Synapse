"""Run the stack for REAL use — the three processes an MCP client needs.

`demo_local.py` performs a walkthrough with scripted contributors. This does
not: it starts the service, the orchestrator, and (unless you have an NPU) the
model stand-in, binds a Shared Session, and then gets out of the way so a live
Claude Code session can actually use `mcp__synapse__query` and
`mcp__synapse__contribute`.

    uv run python scripts/serve_local.py                 # new Shared Session
    uv run python scripts/serve_local.py --shared-id sh-abc123   # join the team's
    uv run python scripts/serve_local.py --npu           # real GenieX, no stand-in

Then in the project you want shared memory in:

    claude mcp add --transport http --scope project synapse http://127.0.0.1:8787/mcp

…start a new Claude Code session, approve the server, and `/mcp` should show
`synapse` connected. See `packs/claude-code/INSTALL.md`.

WHY THE BINDING IS WRITTEN HERE RATHER THAN BY `synapse-worker join`: `join`
binds whatever live transcript detection finds, which is the transcript of
whatever agent session is running right now. That is correct for the product
and wrong for a machine where a session transcript may hold secrets that were
pasted into a chat. This writes the same binding file against a scratch path,
so the MCP tools have a Shared Session to speak for while nothing is ever
pointed at a real transcript. Run `synapse-worker join` yourself, and then
`synapse-worker run`, when you want the passive path too.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
STATE = REPO / ".synapse"
BIN = Path(sys.executable).parent

SERVICE_URL = "http://127.0.0.1:8899"
ORCHESTRATOR_URL = "http://127.0.0.1:8787"
STANDIN_URL = "http://127.0.0.1:18181/v1"
NPU_URL = "http://127.0.0.1:18181/v1"   # geniex serve listens here too

processes: list[tuple[str, subprocess.Popen]] = []


def _anthropic_key() -> str | None:
    """The Anthropic key, from the environment or `secrets.jsonc`.

    Sourced HERE rather than in `AnthropicProvider` on purpose: the house
    pattern is that scripts read `secrets.jsonc` and packages read the
    environment (aic100.py takes INFERENCE_CLOUD_API_KEY and knows nothing
    about repo layout; local_model_server.py does the file reading). Keeping it
    that way means `packages/` never grows knowledge of where this checkout
    keeps its credentials.

    Without this, a teammate who reasonably puts their key in the `anthropic`
    block of secrets.jsonc — where every other credential in this project
    lives — gets "Could not resolve authentication method" with nothing
    pointing at the cause. The block was read by nothing at all.

    Never returned to a caller that prints it: only injected into a child's
    environment.
    """
    import json
    import re

    if key := os.environ.get("ANTHROPIC_API_KEY"):
        return key
    secrets = REPO / "secrets.jsonc"
    if not secrets.exists():
        return None
    try:
        data = json.loads(re.sub(r"^\s*//.*$", "", secrets.read_text(), flags=re.MULTILINE))
    except json.JSONDecodeError:
        return None
    key = (data.get("anthropic") or {}).get("api_key")
    return str(key) if key else None


def lan_ip() -> str | None:
    """This machine's address on the LAN, for teammates to point at.

    A UDP socket to a routable address picks the interface the OS would
    actually use, without sending a packet — more reliable than parsing
    `ifconfig`, and it works the same on macOS and Windows.
    """
    import socket

    probe = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        probe.connect(("8.8.8.8", 80))
        return probe.getsockname()[0]
    except OSError:
        return None
    finally:
        probe.close()


def _lan_address() -> str:
    """The address a teammate should type, or a placeholder that reads as one.

    Never returns None: this string goes into a command line we print for
    someone else to run, and "None" in the middle of a URL is worse than an
    obvious placeholder they will notice and ask about.
    """
    return lan_ip() or "<this-machine-lan-ip>"


def http(method: str, url: str, body: dict | None = None, timeout: float = 30.0):
    request = urllib.request.Request(
        url, data=json.dumps(body).encode() if body is not None else None,
        headers={"Content-Type": "application/json"}, method=method)
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return json.loads(response.read() or b"null")


def wait_for(url: str, process: subprocess.Popen | None, name: str, seconds: float = 25.0) -> bool:
    deadline = time.time() + seconds
    while time.time() < deadline:
        if process is not None and process.poll() is not None:
            raise SystemExit(f"{name} exited — see {STATE / 'logs' / (name + '.log')}")
        try:
            urllib.request.urlopen(url, timeout=2.0).read()
            return True
        except urllib.error.HTTPError:
            return True
        except (urllib.error.URLError, TimeoutError, OSError):
            time.sleep(0.4)
    return False


def spawn(name: str, argv: list[str], env: dict[str, str] | None = None) -> subprocess.Popen:
    (STATE / "logs").mkdir(parents=True, exist_ok=True)
    log = (STATE / "logs" / f"{name}.log").open("w")
    process = subprocess.Popen(argv, cwd=REPO, env={**os.environ, **(env or {})},
                               stdout=log, stderr=subprocess.STDOUT)
    processes.append((name, process))
    return process


def stop_all() -> None:
    for _, process in reversed(processes):
        if process.poll() is None:
            process.terminate()
    for _, process in reversed(processes):
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            process.kill()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--shared-id", help="join an existing Shared Session instead of creating one")
    parser.add_argument("--host", default="0.0.0.0", metavar="ADDR",
                        help="what the SERVICE binds to. Defaults to 0.0.0.0 so "
                             "teammates on the LAN can reach it; pass 127.0.0.1 to "
                             "keep it to this machine. 0.0.0.0 exposes it to your "
                             "network so teammates' orchestrators can reach it. "
                             "The service is the only piece worth sharing: it needs "
                             "no NPU and no key.")
    parser.add_argument("--service-url", metavar="URL",
                        help="use a service someone ELSE is hosting instead of "
                             "starting one (e.g. http://192.168.4.44:8899). Starts "
                             "only the model seam and your own orchestrator, which "
                             "is the correct shape: one orchestrator per laptop, "
                             "stamping YOUR attribution.")
    parser.add_argument("--contributor", default=os.environ.get("USER", "me"))
    parser.add_argument("--purpose", default="live session")
    parser.add_argument("--live", action="store_true",
                        help="proxy the model seam to the real instance in "
                             "secrets.jsonc, so retrieval is actually RANKED by a "
                             "model and `contribute` can distil arbitrary prose. "
                             "Costs real requests (~20/hour/key).")
    parser.add_argument("--distiller-model", default="Llama-3.1-8B",
                        help="--live: the small model standing where the NPU sits")
    parser.add_argument("--synthesizer-model", default="Llama-3.3-70B",
                        help="--live: the large model standing where the cloud sits")
    parser.add_argument("--distiller", choices=("npu", "anthropic"), default="npu",
                        help="which model does the distilling. `anthropic` uses "
                             "Claude Opus 5 with YOUR OWN key, so several people can "
                             "run the full loop at once instead of contending for the "
                             "one NPU box and the ~20-req/hour Cirrascale key.")
    parser.add_argument("--npu", action="store_true",
                        help="a real model is already serving on :18181 (geniex serve) — "
                             "do not start the stand-in")
    args = parser.parse_args(argv)

    model_url = NPU_URL if args.npu else STANDIN_URL

    if args.npu:
        if not wait_for(f"{model_url}/models", None, "npu", seconds=3):
            raise SystemExit("--npu given but nothing is serving on :18181. "
                             "Start `geniex serve` first, or drop --npu.")
        print(f"model      using what is already on {model_url}", flush=True)
    else:
        argv_model = [sys.executable, "scripts/local_model_server.py"]
        if args.live:
            argv_model += ["--mode", "proxy",
                           "--distiller-model", args.distiller_model,
                           "--synthesizer-model", args.synthesizer_model]
        model = spawn("model", argv_model)
        if not wait_for(f"{model_url}/models", model, "model"):
            raise SystemExit("the model stand-in did not come up")
        if args.live:
            print(f"model      LIVE — proxying to the host in secrets.jsonc; "
                  f"distil {args.distiller_model}, synthesis {args.synthesizer_model}",
                  flush=True)
            print("           retrieval is really ranked now, and `contribute` can "
                  "distil your own words. Budget ~20 requests/hour/key.", flush=True)
        else:
            print(f"model      stand-in on {model_url} (replays this repo's corpus; "
                  f"not a model, not the NPU — retrieval ranking is identity)",
                  flush=True)

    if args.service_url:
        service_url = args.service_url.rstrip("/")
        if not wait_for(f"{service_url}/debug", None, "service", seconds=8):
            raise SystemExit(
                f"nothing is answering at {service_url}. Ask whoever is hosting "
                f"to start it, and check they bound it to the LAN rather than "
                f"127.0.0.1 (scripts/serve_local.py does that by default)."
            )
        print(f"service    JOINING {service_url} (someone else is hosting it)",
              flush=True)
        service = None
    else:
        service_url = SERVICE_URL
        service = spawn("service", [str(BIN / "synapse-service"),
                                    "--host", args.host], {
            "SYNAPSE_SYNTHESIZER": "aic100",
            "INFERENCE_CLOUD_BASE_URL": model_url,
            "INFERENCE_CLOUD_API_KEY": "local-stand-in",
            "INFERENCE_CLOUD_MODEL": "local-stand-in",
        })
        if not wait_for(f"{service_url}/debug", service, "service"):
            raise SystemExit("the service did not come up")
        print(f"service    {service_url}  · dashboard {service_url}/debug", flush=True)
        if args.host not in ("127.0.0.1", "localhost", "::1"):
            print(f"           reachable on the LAN at http://{_lan_address()}:8899 "
                  f"— no auth, so anyone who can reach this port can read and "
                  f"write the team's memory", flush=True)

    if args.shared_id:
        shared_id = args.shared_id
        http("POST", f"{service_url}/v1/sessions",
             {"purpose": args.purpose, "created_by": args.contributor,
              "shared_id": shared_id})
    elif args.service_url:
        # Adopt the session the host already has. Creating one here would put
        # this developer alone in a Shared Session nobody else is in, which is
        # the exact outcome joining exists to avoid — and it would look like
        # success: a working stack, an empty memory, no error anywhere.
        listed = (http("GET", f"{service_url}/debug/stats.json") or {}).get("sessions") or []
        if not listed:
            raise SystemExit(
                f"{service_url} is up but holds no Shared Session yet. Ask the host "
                f"for the id and pass --shared-id.")
        shared_id = listed[0]["shared_id"]
        print(f"           adopted the host's session: {shared_id}"
              + (f"  (of {len(listed)}; pass --shared-id to pick another)"
                 if len(listed) > 1 else ""), flush=True)
    else:
        shared_id = http("POST", f"{service_url}/v1/sessions",
                         {"purpose": args.purpose,
                          "created_by": args.contributor})["shared_id"]
    http("POST", f"{service_url}/v1/sessions/{shared_id}/members",
         {"contributor": args.contributor})

    from synapse_contracts import SessionBinding, write_binding
    scratch = STATE / "scratch-transcript.jsonl"
    scratch.parent.mkdir(parents=True, exist_ok=True)
    scratch.touch(exist_ok=True)
    write_binding(STATE / "bindings" / "claude-code.json", SessionBinding(
        agent_session_id=f"as-{args.contributor}",
        shared_id=shared_id, contributor=args.contributor, agent="claude-code",
        transcript_path=str(scratch), pinned_at=datetime.now(timezone.utc)))

    orchestrator_env = {"SYNAPSE_BASE_URL": model_url,
                        "SYNAPSE_DISTILLER": args.distiller}
    if args.distiller == "anthropic":
        key = _anthropic_key()
        if not key:
            raise SystemExit(
                "--distiller anthropic needs a key. Either export "
                "ANTHROPIC_API_KEY, or put it in secrets.jsonc:\n"
                '  "anthropic": { "api_key": "sk-ant-..." }\n'
                "secrets.jsonc is gitignored; the key is never printed or logged.")
        orchestrator_env["ANTHROPIC_API_KEY"] = key
        print("distiller  Claude Opus 5 (your own key — no shared rate limit). "
              "Only synthesis still spends the Cirrascale budget.", flush=True)

    orchestrator = spawn("orchestrator", [
        str(BIN / "synapse-orchestrator"), "--port", "8787",
        "--service-url", service_url, "--state-dir", str(STATE),
    ], orchestrator_env)
    if not wait_for(f"{ORCHESTRATOR_URL}/mcp", orchestrator, "orchestrator"):
        raise SystemExit("the orchestrator did not come up")

    print(f"orchestr.  {ORCHESTRATOR_URL}/mcp  ← point Claude Code here", flush=True)
    print(f"session    {shared_id}  (contributor: {args.contributor})", flush=True)
    print(flush=True)
    print("  In the project you want shared memory in:", flush=True)
    print("    claude mcp add --transport http --scope project synapse "
          f"{ORCHESTRATOR_URL}/mcp")
    print("  then start a NEW Claude Code session and approve the server.", flush=True)
    print("  Already registered and showing 'failed'? Just pick Reconnect in /mcp.", flush=True)
    print(flush=True)
    print(f"  Tools: mcp__synapse__query · mcp__synapse__contribute", flush=True)
    print(f"  Logs:  {STATE / 'logs'}", flush=True)

    if not args.service_url and args.host not in ("127.0.0.1", "localhost", "::1"):
        # One orchestrator per laptop is not a deployment preference, it is what
        # keeps attribution honest: a teammate pointing their agent at THIS
        # orchestrator would have their findings stamped with MY binding — my
        # contributor, my agent session — so their own work would be suppressed
        # from them and credited to me.
        print(flush=True)
        print("  Give a teammate THIS, so they run their own orchestrator "
              "against your service:", flush=True)
        print(f"    uv run python scripts/serve_local.py \\", flush=True)
        print(f"      --service-url http://{_lan_address()}:8899 \\", flush=True)
        print(f"      --shared-id {shared_id} \\", flush=True)
        print(f"      --contributor <their-name>        # add --npu if they have one",
              flush=True)
        print("  They must NOT point Claude Code at your :8787 — one orchestrator "
              "per laptop, or their findings get stamped as yours.", flush=True)

    print(flush=True)
    print("  Ctrl-C to stop.", flush=True)

    try:
        while True:
            time.sleep(3600)
    except KeyboardInterrupt:
        pass
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except KeyboardInterrupt:
        print("\nstopping", flush=True)
    finally:
        stop_all()
