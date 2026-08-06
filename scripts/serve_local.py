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

# Windows writes piped stdout in the locale codepage (cp1252), not UTF-8. This
# script is documented as a DIRECT invocation (README.md "Run it"), so the
# installers' PYTHONUTF8=1 cannot be relied on to be present — someone who
# already has a checkout runs this by hand. Two things here need it: the banner
# below, and everything runtime-supplied that flows into it — contributor names
# and the service's own session purposes, neither of which this file controls.
# Same guard as scripts/run_npu_eval.py:36-40.
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

REPO = Path(__file__).resolve().parent.parent
STATE = REPO / ".synapse"
BIN = Path(sys.executable).parent

# The synthesis output budget, and the read timeout that has to travel with it.
# Both are passed to the service below; see the comment there for why the
# provider's own defaults could not stay.
SYNTHESIS_MAX_TOKENS = 1600
SYNTHESIS_TIMEOUT_S = 180

SERVICE_URL = "http://127.0.0.1:8899"
ORCHESTRATOR_URL = "http://127.0.0.1:8787"
STANDIN_URL = "http://127.0.0.1:18181/v1"
NPU_URL = "http://127.0.0.1:18181/v1"   # geniex serve listens here too

processes: list[tuple[str, subprocess.Popen]] = []


def service_env_for(*, npu: bool, model_url: str) -> dict[str, str]:
    """The environment that decides which synthesizer the service builds.

    Extracted from `main` so it can be asserted. It was an inline dict, and the
    half of 282fd07 that chooses `npu` over `aic100` from `--npu` therefore had
    no test at all — grep for `serve_local` across `packages/` and `tests/`
    returned exactly one hit, and it was a docstring reference. Pure by
    construction: no environment reads, no I/O, so a test is a single call.

    The synthesizer has to speak whatever is actually listening on `model_url`.
    The stand-in serves BOTH /chat/completions and /completions (see
    local_model_server.py's docstring), so aic100 -- which needs /completions --
    works against it. `geniex serve` does NOT: it has no /completions route at
    all, so aic100 against a real NPU 410s on every synthesis call and the
    host's own queries come back empty with a 200. Observed live 2026-08-06,
    and confirmed against Qualcomm's endpoint list.
    """
    if npu:
        return {
            "SYNAPSE_SYNTHESIZER": "npu",
            "SYNAPSE_BASE_URL": model_url,
        }
    return {
        "SYNAPSE_SYNTHESIZER": "aic100",
        "INFERENCE_CLOUD_BASE_URL": model_url,
        "INFERENCE_CLOUD_API_KEY": "local-stand-in",
        "INFERENCE_CLOUD_MODEL": "local-stand-in",
        # Derived, not chosen — see synapse_service.synthesis.SynthesisBudget
        # and the [capability."Llama-3.3-70B"] record in config/synapse.toml.
        # AIC100Provider's own 800/60s defaults are what broke synthesis on
        # 2026-08-06: a 500-word working memory is ~850 tokens, so the memory
        # alone overran the cap and every round came back truncated while
        # findings kept landing. 1600 buys back the intended 500-word memory
        # with ~700 tokens of verdict room; 180s keeps the longer generation
        # clear of the read timeout (two rounds measured at 48.5s and 51.7s
        # against the old 60s default).
        "INFERENCE_CLOUD_MAX_TOKENS": str(SYNTHESIS_MAX_TOKENS),
        "INFERENCE_CLOUD_TIMEOUT": str(SYNTHESIS_TIMEOUT_S),
    }


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


def claim_ports(ports: list[int]) -> None:
    """Refuse to start if something already holds a port we need.

    Runs BEFORE anything is written. Without it, a second `serve_local` on the
    same machine — a listener joining alongside the host, say — sails past the
    orchestrator bind failure (the EXISTING orchestrator answers the health
    check, so it looks up), and by then it has already overwritten
    `.synapse/bindings/claude-code.json` with ITS contributor. The host's own
    agent silently becomes somebody else. Observed doing exactly that
    2026-08-06.
    """
    import socket

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
            "Another Synapse is running on this machine. Stop it first, or — if "
            "you are trying to listen alongside a host you are already running — "
            "you do not need a second one: your existing orchestrator is already "
            "connected to that session."
        )


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
    parser.add_argument("--distiller", choices=("npu", "anthropic", "claude-cli"),
                        default="npu",
                        help="which model does the distilling. `anthropic` uses Claude "
                             "Opus 5 with your own API key; `claude-cli` uses the local "
                             "`claude` binary on your own SUBSCRIPTION, needing no "
                             "credential at all. Either lets several people run the "
                             "full loop at once instead of contending for the one NPU "
                             "box and the ~20-req/hour Cirrascale key.")
    parser.add_argument("--claude-model", metavar="MODEL",
                        help="which Claude model does the distilling, for "
                             "--distiller anthropic or claude-cli. The two want "
                             "different spellings: `anthropic` takes a full "
                             "Messages API id (claude-haiku-4-5-20251001), "
                             "`claude-cli` takes the short alias the binary "
                             "accepts (haiku, sonnet, opus). Omitted, each "
                             "provider uses its own default (claude-opus-5, "
                             "sonnet). Ignored by --distiller npu, which errors "
                             "rather than pretending it was honoured.")
    parser.add_argument("--listen", action="store_true",
                        help="READ-ONLY: join without any model at all. Queries and "
                             "the arrival briefing work — the HOST's service does "
                             "the ranking — but `contribute` cannot distil and will "
                             "say so. For anyone with no NPU and no key.")
    parser.add_argument("--npu", action="store_true",
                        help="a real model is already serving on :18181 (geniex serve) — "
                             "do not start the stand-in")
    args = parser.parse_args(argv)

    # Fail rather than ignore. `--claude-model haiku --distiller npu` is someone
    # who believes they are running Haiku; silently distilling on the NPU instead
    # would send them into a demo with a wrong mental model of which arm produced
    # which finding -- and the findings themselves carry no record of the model,
    # so nothing downstream would contradict them either.
    if args.claude_model and args.distiller == "npu":
        raise SystemExit(
            "--claude-model only applies to --distiller anthropic or claude-cli.\n"
            f"You asked for the NPU arm and model {args.claude_model!r}; pick one.")

    needed = [8787] + ([] if args.service_url else [8899]) + \
             ([] if (args.listen or args.npu) else [18181])
    claim_ports(needed)

    model_url = NPU_URL if args.npu else STANDIN_URL

    if args.listen:
        # Nothing is started here on purpose. Reading needs no model: the
        # briefing is an HTTP GET against the host's watermark, and `query`
        # is ranked by the host's service, not by anything local. Only the
        # write path needs a distiller, and `contribute` already fails soft
        # and says so rather than crashing (server.py's guard) — verified
        # against an orchestrator with nothing listening on the model port.
        print("model      NONE — listen-only. Queries and the briefing work; "
              "`contribute` will decline, because distilling your prose is the "
              "one thing that needs a model on YOUR machine.", flush=True)
    elif args.npu:
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
        service_env = service_env_for(npu=args.npu, model_url=model_url)
        service = spawn("service", [str(BIN / "synapse-service"),
                                    "--host", args.host], service_env)
        if not wait_for(f"{service_url}/debug", service, "service"):
            raise SystemExit("the service did not come up")
        print(f"service    {service_url}  · dashboard {service_url}/debug", flush=True)
        if args.host not in ("127.0.0.1", "localhost", "::1"):
            print(f"           reachable on the LAN at http://{_lan_address()}:8899 "
                  f"— no auth, so anyone who can reach this port can read and "
                  f"write the team's memory", flush=True)

    if args.shared_id:
        shared_id = args.shared_id
        served = http("POST", f"{service_url}/v1/sessions",
                      {"purpose": args.purpose, "created_by": args.contributor,
                       "shared_id": shared_id}) or {}
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
        served = listed[0]
        shared_id = served["shared_id"]
        print(f"           adopted the host's session: {shared_id}"
              + (f"  (of {len(listed)}; pass --shared-id to pick another)"
                 if len(listed) > 1 else ""), flush=True)
    else:
        served = http("POST", f"{service_url}/v1/sessions",
                      {"purpose": args.purpose,
                       "created_by": args.contributor}) or {}
        shared_id = served["shared_id"]
    http("POST", f"{service_url}/v1/sessions/{shared_id}/members",
         {"contributor": args.contributor})

    # Retain the session's identity the way the MCP tools do (server.py's
    # `_remember`). Found 2026-08-06 by the lifecycle audit: `record_session`
    # had exactly ONE production call site — the create_session/join_session
    # MCP tools — so a session stood up by THIS script, which is the path
    # docs/JOIN.md documents and therefore where most sessions actually come
    # from, left no local record at all. `.synapse/sessions.json` did not exist
    # on either checkout on the author's machine. The consequence is quiet:
    # after a restart, `synapse-orchestrator resync` recreates the session with
    # `created_by=null` and the creator-only end gate falls through to the
    # membership arm, so the fix that exists to keep the real creator loses
    # them on the one path everybody uses.
    #
    # Records what the SERVICE returned, never what we sent. `POST /v1/sessions`
    # is create-or-return, so against a session that already exists the body is
    # the ORIGINAL creator and purpose — recording our own arguments would
    # overwrite a teammate's ownership with ours on every `--shared-id` join.
    # `record_session` is first-write-wins per field and never raises, so a
    # `None` here (an older service that does not report it, or the adopt path
    # reading a stats.json without the field) leaves what is already known
    # alone rather than erasing it.
    from synapse_orchestrator.session_meta import record_session
    record_session(STATE, shared_id,
                   created_by=served.get("created_by"),
                   purpose=served.get("purpose"))

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
    # --claude-model routes to a DIFFERENT env var per arm, because the two
    # providers name models differently and neither can accept the other's
    # spelling: AnthropicProvider talks to the Messages API and wants a full id
    # ("claude-haiku-4-5-20251001"), while ClaudeCLIProvider shells out to
    # `claude --model` and wants the short alias ("haiku"). One flag, two
    # destinations, so nobody has to remember which shape belongs where.
    #
    # Left unset it stays absent from the child env, so each provider falls back
    # to its own DEFAULT_MODEL (claude-opus-5 / sonnet) rather than this script
    # duplicating those constants and drifting from them.
    if args.distiller == "anthropic":
        key = _anthropic_key()
        if not key:
            raise SystemExit(
                "--distiller anthropic needs a key. Either export "
                "ANTHROPIC_API_KEY, or put it in secrets.jsonc:\n"
                '  "anthropic": { "api_key": "sk-ant-..." }\n'
                "secrets.jsonc is gitignored; the key is never printed or logged.")
        orchestrator_env["ANTHROPIC_API_KEY"] = key
        if args.claude_model:
            orchestrator_env["SYNAPSE_ANTHROPIC_MODEL"] = args.claude_model
        # Named, never hardcoded. Until 2026-08-06 this line read "Claude Opus 5"
        # unconditionally, so a run that had set SYNAPSE_ANTHROPIC_MODEL in the
        # ambient environment -- which `spawn` merges, so it took effect -- was
        # told on stdout that it was using a model it was not. A banner that can
        # be wrong about the one thing the operator chose is worse than no banner.
        print(f"distiller  {args.claude_model or 'claude-opus-5 (default)'} via the "
              "Messages API on your own key — no shared rate limit. "
              "Only synthesis still spends the Cirrascale budget.", flush=True)

    if args.distiller == "claude-cli":
        if args.claude_model:
            orchestrator_env["SYNAPSE_CLAUDE_CLI_MODEL"] = args.claude_model
        print(f"distiller  {args.claude_model or 'sonnet (default)'} via the local "
              "`claude` binary on your SUBSCRIPTION — no API key spent at all. "
              "Only synthesis still spends the Cirrascale budget.", flush=True)

    orchestrator = spawn("orchestrator", [
        str(BIN / "synapse-orchestrator"), "--port", "8787",
        "--service-url", service_url, "--state-dir", str(STATE),
        # The SAME identity this script just wrote into the binding above, and
        # the same one it registered with the service. Omitted (2026-08-06),
        # the orchestrator fell back to `getpass.getuser()`, which agrees with
        # `--contributor` only by luck: docs/JOIN.md tells a teammate to run
        # `--contributor akhil` on a box whose $USER is `akhilm`. While a
        # binding exists nothing is wrong -- `server._identity()` reads
        # `binding.contributor`. The moment there is no binding (after
        # `leave_session`, or a first `create_session` from an unbound state)
        # the orchestrator creates and re-binds under the OS name instead, and
        # from then on `query` sends a contributor the service has never seen:
        # `last_seen` is 0 so the briefing reports the whole Shared Memory as
        # new (the exact regression spec Testing item 4 exists for),
        # self-suppression stops recognising this person's own findings (item
        # 5), and `end_session` on a session created under the other name draws
        # a 403 naming a stranger. None of that errors -- silence is the
        # failure mode the identity re-key was built to remove.
        "--contributor", args.contributor,
    ], orchestrator_env)
    if not wait_for(f"{ORCHESTRATOR_URL}/mcp", orchestrator, "orchestrator"):
        raise SystemExit("the orchestrator did not come up")

    # ASCII arrow on purpose. The guard at the top of this file covers the
    # general case, but this one line is the single most-read thing the script
    # prints, and it should survive a console that predates any of it.
    print(f"orchestr.  {ORCHESTRATOR_URL}/mcp  <- point Claude Code here", flush=True)
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
        # Printed as ONE unwrapped line per OS, deliberately. The previous
        # version used backslash continuations, which are not pasteable into a
        # chat window and presumed the teammate already had a checkout and a
        # synced venv — the two things they are least likely to have. These
        # lines install uv, clone, sync, and join, from nothing. `sh -s --` is
        # load-bearing: it is what lets arguments through the pipe.
        lan = _lan_address()
        raw = "https://raw.githubusercontent.com/SinghSiddharth01/Synapse/main"
        print(flush=True)
        print("  Give a teammate ONE of these — it installs and joins in a "
              "single paste:", flush=True)
        print(flush=True)
        print("  macOS / Linux:", flush=True)
        print(f"    curl -LsSf {raw}/install.sh | sh -s -- --service-url "
              f"http://{lan}:8899 --shared-id {shared_id} "
              f"--contributor <their-name>", flush=True)
        print(flush=True)
        print("  Windows (PowerShell):", flush=True)
        print(f"    & ([scriptblock]::Create((irm {raw}/install.ps1))) "
              f"-ServiceUrl http://{lan}:8899 -SharedId {shared_id} "
              f"-Contributor <their-name>", flush=True)
        print(flush=True)
        print("  Already cloned and synced? Then just:", flush=True)
        print(f"    uv run python scripts/serve_local.py --service-url "
              f"http://{lan}:8899 --shared-id {shared_id} "
              f"--contributor <their-name>", flush=True)
        print(flush=True)
        print("  Add --npu (or -Npu) if they have one.", flush=True)
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
