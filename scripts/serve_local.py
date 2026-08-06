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

    service = spawn("service", [str(BIN / "synapse-service")], {
        "SYNAPSE_SYNTHESIZER": "aic100",
        "INFERENCE_CLOUD_BASE_URL": model_url,
        "INFERENCE_CLOUD_API_KEY": "local-stand-in",
        "INFERENCE_CLOUD_MODEL": "local-stand-in",
    })
    if not wait_for(f"{SERVICE_URL}/debug", service, "service"):
        raise SystemExit("the service did not come up")
    print(f"service    {SERVICE_URL}  · dashboard {SERVICE_URL}/debug", flush=True)

    if args.shared_id:
        shared_id = args.shared_id
        http("POST", f"{SERVICE_URL}/v1/sessions",
             {"purpose": args.purpose, "created_by": args.contributor,
              "shared_id": shared_id})
    else:
        shared_id = http("POST", f"{SERVICE_URL}/v1/sessions",
                         {"purpose": args.purpose,
                          "created_by": args.contributor})["shared_id"]
    http("POST", f"{SERVICE_URL}/v1/sessions/{shared_id}/members",
         {"contributor": args.contributor})

    from synapse_contracts import SessionBinding, write_binding
    scratch = STATE / "scratch-transcript.jsonl"
    scratch.parent.mkdir(parents=True, exist_ok=True)
    scratch.touch(exist_ok=True)
    write_binding(STATE / "bindings" / "claude-code.json", SessionBinding(
        agent_session_id=f"as-{args.contributor}",
        shared_id=shared_id, contributor=args.contributor, agent="claude-code",
        transcript_path=str(scratch), pinned_at=datetime.now(timezone.utc)))

    orchestrator = spawn("orchestrator", [
        str(BIN / "synapse-orchestrator"), "--port", "8787",
        "--service-url", SERVICE_URL, "--state-dir", str(STATE),
    ], {"SYNAPSE_BASE_URL": model_url})
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
