"""Run the whole system on one laptop, with both dashboards open.

Five processes, real sockets between them, nothing mocked but the silicon:

    local model stand-in  :18181   scripts/local_model_server.py
    synapse-service       :8899    append-only log, synthesis, retrieval  + /debug
    synapse-orchestrator  :8787    sole egress, durable relay
    synapse-worker        :8790    follows Aditya's transcript            + /debug
    synapse-worker        :8791    follows Sid's transcript               + /debug

Two scratch transcripts are written in this repo's `.demo/` directory and
appended to while the workers tail them, so detection, segmentation, triage,
distillation, the write-ahead log, egress through the orchestrator, synthesis
and retrieval all run for real. The transcript content is this repo's own
fixture corpus — including seg-005a and seg-005b, the two contributors who
independently hit the same ~40 ms DMA timing window, which is the merge the
demo is built around.

    uv run python scripts/demo_local.py              # offline; corpus replay
    uv run python scripts/demo_local.py --live       # real model behind both seams

`--live` points the stand-in at Cirrascale, so a real model does the
distillation and the synthesis. Budget: roughly 20 requests per hour per key.

This is NOT the NPU test. Nothing here measures anything — the machine has no
NPU and the stand-in is not a model. On-hardware testing is
`docs/plans/exec/2026-08-05-e8-npu-testing.md`.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import time
import urllib.error
import urllib.request
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

REPO = Path(__file__).resolve().parent.parent
DEMO = REPO / ".demo"
BIN = Path(sys.executable).parent

SERVICE_URL = "http://127.0.0.1:8899"
ORCHESTRATOR_URL = "http://127.0.0.1:8787"
MODEL_URL = "http://127.0.0.1:18181/v1"

# Who feeds what. seg-004 is the corpus's canonical triage SKIP (a linter's own
# summary line, no traceback) — it is here so the worker dashboard shows a skip
# with its reason, not only keeps.
FEED = {
    "aditya": {"port": 8790, "segments": ["seg-004", "seg-005a"]},
    "sid": {"port": 8791, "segments": ["seg-005b"]},
}

processes: list[tuple[str, subprocess.Popen]] = []


def say(message: str) -> None:
    print(f"\n\033[1m{message}\033[0m", flush=True)


def detail(message: str) -> None:
    print(f"    {message}", flush=True)


def http(method: str, url: str, body: dict | None = None, timeout: float = 60.0) -> Any:
    request = urllib.request.Request(
        url,
        data=json.dumps(body).encode() if body is not None else None,
        headers={"Content-Type": "application/json"},
        method=method,
    )
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return json.loads(response.read() or b"null")


def wait_for(url: str, process: subprocess.Popen, name: str, seconds: float = 25.0) -> bool:
    """Up, and up because OUR child is serving it.

    Checking only that something answers is not enough: a child that dies
    instantly on `EADDRINUSE` is indistinguishable from a healthy one, because
    whatever already holds the port answers happily. That failure mode drives
    the whole demo against a previous run's processes — and in `--live` it
    would print "proxying to Cirrascale" while a leftover replay stand-in
    answered every call from fixtures.
    """
    deadline = time.time() + seconds
    while time.time() < deadline:
        if process.poll() is not None:
            raise SystemExit(died(name))
        try:
            urllib.request.urlopen(url, timeout=2.0).read()
            return True
        except urllib.error.HTTPError:
            return True  # answered at all: it is up
        except (urllib.error.URLError, TimeoutError, OSError):
            time.sleep(0.4)
    return False


def claim_ports(ports: list[int]) -> None:
    """Refuse to start if anything already holds a port we need.

    Runs BEFORE `.demo/` is wiped. Without it, a second run deletes a live
    run's transcripts, per-worker WAL and orchestrator binding, and the
    surviving orchestrator then 503s every push (no binding to route to) while
    this script waits out its timeouts and reports zero findings.
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
            "Another walkthrough is probably still running. Stop it first "
            "(Ctrl-C in its terminal), or run:\n"
            "  pkill -f demo_local.py; pkill -f local_model_server; "
            "pkill -f synapse-service; pkill -f synapse-orchestrator; pkill -f synapse-worker"
        )


def spawn(name: str, argv: list[str], env: dict[str, str]) -> subprocess.Popen:
    log = (DEMO / "logs" / f"{name}.log").open("w")
    process = subprocess.Popen(
        argv, cwd=REPO, env={**os.environ, **env}, stdout=log, stderr=subprocess.STDOUT
    )
    processes.append((name, process))
    return process


def stop_all() -> None:
    for name, process in reversed(processes):
        if process.poll() is None:
            process.terminate()
    for name, process in reversed(processes):
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            process.kill()


def died(name: str) -> str:
    """The tail of a dead process's log, for an error message worth reading."""
    log = DEMO / "logs" / f"{name}.log"
    tail = log.read_text().splitlines()[-15:] if log.exists() else []
    return f"{name} exited. Last lines of {log}:\n" + "\n".join(f"      {t}" for t in tail)


# ---------------------------------------------------------------- transcripts

def transcript_lines(segment_id: str, session_id: str, start: datetime) -> list[str]:
    """One fixture Segment, rendered back into Claude Code's own JSONL dialect.

    The fixtures store parsed AgentEvents; the worker's job starts one step
    earlier, at the raw transcript. Rendering back means ClaudeCodeSource,
    the follower, and segmentation all do their real work rather than being
    bypassed by a hand-fed Segment.
    """
    segment = json.loads((REPO / "fixtures" / "segments" / f"{segment_id}.json").read_text())
    lines: list[str] = []
    tool_use_id = None

    for offset, event in enumerate(segment["events"]):
        stamp = (start + timedelta(seconds=offset)).isoformat().replace("+00:00", "Z")
        common = {
            "timestamp": stamp,
            "sessionId": session_id,
            "cwd": str(REPO),
            "gitBranch": "main",
        }
        kind, content = event["kind"], event["content"]

        if kind == "text":
            block: dict[str, Any] = {"type": "text", "text": content}
        elif kind == "thinking":
            block = {"type": "thinking", "thinking": content}
        elif kind == "tool_use":
            tool_use_id = f"toolu_{uuid.uuid4().hex[:12]}"
            block = {
                "type": "tool_use",
                "id": tool_use_id,
                "name": event.get("tool_name") or "Bash",
                "input": content,
            }
        else:  # tool_result — Claude Code records these under the user's turn
            block = {
                "type": "tool_result",
                "tool_use_id": tool_use_id or f"toolu_{uuid.uuid4().hex[:12]}",
                "content": content,
            }

        lines.append(json.dumps({
            **common,
            "type": event["role"],
            "message": {"role": event["role"], "content": [block]},
        }))
    return lines


def seed_transcript(path: Path, session_id: str) -> None:
    """Bookkeeping lines only. The worker attaches at the END of a transcript,
    so anything meant to be seen must be appended after it is following."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w") as handle:
        handle.write(json.dumps({"type": "file-history-snapshot", "sessionId": session_id}) + "\n")


def append_segment(path: Path, segment_id: str, session_id: str) -> None:
    with path.open("a") as handle:
        for line in transcript_lines(segment_id, session_id, datetime.now(timezone.utc)):
            handle.write(line + "\n")


# ---------------------------------------------------------------------- main

def service_stats(shared_id: str) -> dict[str, Any]:
    """The service's own dashboard feed, for the session under test. The
    payload nests one session under `session`; `sessions` lists the rest."""
    try:
        payload = http("GET", f"{SERVICE_URL}/debug/stats.json?session={shared_id}",
                       timeout=5.0) or {}
        return payload.get("session") or {}
    except Exception:  # noqa: BLE001 — the dashboard is optional instrumentation
        return {}


def wait_for_findings(shared_id: str, want: int, seconds: float) -> int:
    """Poll until `want` findings are visible in the fold, or time runs out.
    Returns how many actually arrived — the caller reports the real number
    either way, so a shortfall is reported rather than asserted away."""
    deadline = time.time() + seconds
    seen = 0
    while time.time() < deadline:
        seen = _appended(service_stats(shared_id))
        if seen >= want:
            return seen
        for name, process in processes:
            if process.poll() is not None:
                raise SystemExit(died(name))
        time.sleep(2.0)
    return seen


def _appended(stats: dict[str, Any]) -> int:
    """Distinct findings appended, read from the log rather than the fold: the
    fold's `visible` count drops back as synthesis supersedes originals, so
    counting it would read a successful merge as a lost finding."""
    return len({
        str(e.get("summary", "")).split(":")[0]
        for e in (stats.get("log_tail") or []) if e.get("kind") == "FindingAppended"
    })


def triage_record(port: int) -> list[str]:
    """What the worker's own dashboard says triage decided, and why."""
    try:
        stats = http("GET", f"http://127.0.0.1:{port}/debug/stats.json", timeout=5.0) or {}
    except Exception:  # noqa: BLE001
        return ["worker dashboard unreachable — triage record unavailable"]
    events = [e for e in (stats.get("events") or []) if e.get("tag") == "triage"]
    if not events:
        return ["no triage decision recorded — nothing reached triage at all"]
    return [f"triage: {e.get('summary')}" for e in events[-4:]]


def wait_for_merge(shared_id: str, seconds: float) -> list[dict[str, Any]]:
    """Poll for a `Merged` entry in the log.

    Necessary, not decorative: a push becomes visible in the fold at ingest,
    which is BEFORE the synthesis call it triggers has returned. Reading the
    log the moment a finding appears therefore reports "no merge" while the
    merge is still in flight — a false negative this demo hit for real.
    """
    deadline = time.time() + seconds
    while time.time() < deadline:
        entries = [e for e in (service_stats(shared_id).get("log_tail") or [])
                   if e.get("kind") == "Merged"]
        if entries:
            return entries
        time.sleep(2.0)
    return []


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--live", action="store_true",
                        help="proxy both model seams to Cirrascale (real model, real cost)")
    # No default host: the key and the host must come from the same place, or a
    # run quietly sends one instance's key to another. Omitted, the stand-in
    # takes both from secrets.jsonc's `inference_cloud` block.
    parser.add_argument("--upstream", help="override the base URL from secrets.jsonc")
    # Two models, because production has two: a small one where the NPU sits,
    # a large one where the cloud sits. Budget is per key, ~20 requests/hour.
    parser.add_argument("--distiller-model", default="Llama-3.1-8B")
    parser.add_argument("--synthesizer-model", default="Llama-3.3-70B")
    parser.add_argument("--interval", type=float, default=5.0, help="worker poll seconds")
    parser.add_argument("--idle-flush", type=float, default=10.0,
                        help="seconds of quiet before an open turn is flushed as a Segment")
    parser.add_argument("--pace", type=float, default=0.0, metavar="TOK_PER_SEC",
                        help="offline only: slow replies to this simulated decode "
                             "rate so the dashboard's live timer ticks. Fabricated — "
                             "it appears on the worker page as NPU latency, so leave "
                             "it off unless you are demonstrating the UI itself.")
    parser.add_argument("--exit-when-done", action="store_true",
                        help="stop everything after the walkthrough instead of leaving it up")
    args = parser.parse_args(argv)

    claim_ports([18181, 8899, 8787] + [plan["port"] for plan in FEED.values()])

    if DEMO.exists():
        shutil.rmtree(DEMO)
    (DEMO / "logs").mkdir(parents=True)

    say("1. the model seam — a stand-in where the NPU and the cloud would be")
    model_argv = [sys.executable, "scripts/local_model_server.py"]
    if args.live:
        model_argv += ["--mode", "proxy",
                       "--distiller-model", args.distiller_model,
                       "--synthesizer-model", args.synthesizer_model]
        if args.upstream:
            model_argv += ["--upstream", args.upstream]
        detail(f"LIVE: {args.upstream or 'the host in secrets.jsonc'}")
        detail(f"distilling with {args.distiller_model}, "
               f"synthesizing with {args.synthesizer_model}")
        detail("budget is roughly 20 requests/hour/key")
    else:
        detail("offline: answers replayed from this repo's fixture corpus")
        if args.pace:
            # Off by default, on purpose. The sleep lands inside the HTTP call,
            # so it becomes ModelResult.latency_ms and renders on the worker
            # dashboard under a hero node labelled "NPU now" — a fabricated
            # number in the exact spot a reader would take for a measurement,
            # in a repo whose rule is that no performance claim ships without
            # one. An obviously-instant response cannot be misread that way.
            model_argv += ["--simulate-npu-tok-per-sec", str(args.pace)]
            detail(f"pacing responses at a SIMULATED {args.pace} tok/s so the "
                   f"dashboard visibly ticks — this is not a measurement, and "
                   f"the NPU's own measured rate is ~13 tok/s")
    model = spawn("model", model_argv, {})
    if not wait_for(f"{MODEL_URL}/models", model, "model"):
        raise SystemExit(died("model"))
    detail(f"up on {MODEL_URL}")

    say("2. the service — append-only log, synthesis, retrieval")
    service = spawn("service", [str(BIN / "synapse-service")], {
        "SYNAPSE_SYNTHESIZER": "aic100",
        "INFERENCE_CLOUD_BASE_URL": MODEL_URL,
        "INFERENCE_CLOUD_API_KEY": "local-stand-in",
        # The stand-in rewrites `model` per endpoint on the way upstream, so
        # this is what the service *asks* for, not what ultimately answers.
        "INFERENCE_CLOUD_MODEL": args.synthesizer_model if args.live else "local-stand-in",
    })
    if not wait_for(f"{SERVICE_URL}/debug", service, "service"):
        raise SystemExit(died("service"))
    detail(f"up on {SERVICE_URL} — dashboard at {SERVICE_URL}/debug")

    say("3. a Shared Session")
    session = http("POST", f"{SERVICE_URL}/v1/sessions", {
        "purpose": "fec decode failure — local end-to-end walkthrough",
        "created_by": "siddsing",
    })
    shared_id = session["shared_id"]
    detail(f"shared_id {shared_id}")

    say("4. the orchestrator — the only process allowed to egress")
    # `synapse-worker join` binds whatever live transcript detection finds; these
    # transcripts are scratch files outside ~/.claude, so the binding that join
    # would have written is written here instead. Same file, same contents.
    from synapse_contracts import SessionBinding, write_binding

    orchestrator_state = DEMO / "orchestrator"
    write_binding(orchestrator_state / "bindings" / "claude-code.json", SessionBinding(
        agent_session_id="as-demo", shared_id=shared_id, contributor="siddsing",
        agent="claude-code", transcript_path=str(DEMO / "transcripts"),
        pinned_at=datetime.now(timezone.utc),
    ))
    orchestrator = spawn("orchestrator", [
        str(BIN / "synapse-orchestrator"), "--port", "8787",
        "--service-url", SERVICE_URL, "--state-dir", str(orchestrator_state),
    ], {})
    if not wait_for(f"{ORCHESTRATOR_URL}/producer/findings", orchestrator, "orchestrator"):
        raise SystemExit(died("orchestrator"))
    detail(f"up on {ORCHESTRATOR_URL} — bound to {shared_id}")

    say("5. two workers, each following a live transcript")
    sessions: dict[str, str] = {}
    for contributor, plan in FEED.items():
        agent_session = f"as-{contributor}-{uuid.uuid4().hex[:8]}"
        sessions[contributor] = agent_session
        path = DEMO / "transcripts" / f"{agent_session}.jsonl"
        seed_transcript(path, agent_session)
        worker = spawn(f"worker-{contributor}", [
            str(BIN / "synapse-worker"), "run",
            "--transcript", str(path),
            "--shared-id", shared_id,
            "--contributor", contributor,
            "--interval", str(args.interval),
            "--debug-port", str(plan["port"]),
        ], {
            "SYNAPSE_BASE_URL": MODEL_URL,
            "SYNAPSE_SINK": "http",
            "SYNAPSE_UPSTREAM_URL": f"{ORCHESTRATOR_URL}/producer/findings",
            "SYNAPSE_STATE_DIR": str(DEMO / f"worker-{contributor}"),
            "SYNAPSE_IDLE_FLUSH": str(args.idle_flush),
        })
        if not wait_for(f"http://127.0.0.1:{plan['port']}/debug", worker,
                        f"worker-{contributor}"):
            raise SystemExit(died(f"worker-{contributor}"))
        detail(f"{contributor}: {path.name} — dashboard at "
               f"http://127.0.0.1:{plan['port']}/debug")

    say("   OPEN THESE NOW — the rest of the walkthrough is visible in them")
    for contributor, plan in FEED.items():
        detail(f"http://127.0.0.1:{plan['port']}/debug   {contributor}'s worker")
    detail(f"{SERVICE_URL}/debug   the service")
    time.sleep(3)

    say("6. Aditya works: a lint pass, then the timing discovery")
    aditya = DEMO / "transcripts" / f"{sessions['aditya']}.jsonl"
    for segment_id in FEED["aditya"]["segments"]:
        append_segment(aditya, segment_id, sessions["aditya"])
        detail(f"appended {segment_id} to {aditya.name}")
        time.sleep(args.interval + args.idle_flush + 4)
    landed = wait_for_findings(shared_id, 1, seconds=90)
    detail(f"{landed} finding(s) in the service log")
    # Read the worker's own triage record rather than asserting the outcome.
    # `seg-004`'s golden is empty, so a triage regression that KEPT it would
    # produce an identical finding count — the claim has to be checked where
    # the decision was actually made.
    for line in triage_record(FEED["aditya"]["port"]):
        detail(line)

    question = "why does the fec decode fail?"

    say("7. the same question, asked by two different sessions")
    outsider = (http("POST", f"{SERVICE_URL}/v1/sessions/{shared_id}/query",
                     {"query": question, "agent_session": "as-observer"})
                .get("findings") or [])
    mine = (http("POST", f"{SERVICE_URL}/v1/sessions/{shared_id}/query",
                 {"query": question, "agent_session": sessions["aditya"]})
            .get("findings") or [])
    detail(f"a session that contributed nothing sees {len(outsider)}")
    detail(f"the session that FOUND it sees {len(mine)}")
    detail("what Aditya already has in his own context window is not news to "
           "Aditya — invariant 3, suppression by attribution")
    if not args.live:
        # Say which half of retrieval this actually demonstrated. Suppression
        # is real code and runs before the model; the ranking that follows it
        # is the stand-in's identity function, so nothing here shows relevance.
        detail("(offline: WHICH findings are offered is real suppression; the "
               "ranking after it is the stand-in returning them in log order)")

    say("8. Sid hits the same wall, independently")
    sid = DEMO / "transcripts" / f"{sessions['sid']}.jsonl"
    append_segment(sid, "seg-005b", sessions["sid"])
    detail("appended seg-005b to " + sid.name)
    landed = wait_for_findings(shared_id, landed + 1, seconds=120)
    detail(f"{landed} finding(s) in the service log")

    say("9. what synthesis did with them")
    merged = wait_for_merge(shared_id, seconds=60)
    for entry in merged[-3:]:
        detail(f"MERGED  {entry.get('summary')}")
    if not merged:
        detail("no Merged entry — synthesis runs on every push; "
               f"the log tail is on {SERVICE_URL}/debug")
    stats = service_stats(shared_id)
    if working := stats.get("working_memory"):
        detail(f"working memory: {working[:110]}")
    view = stats.get("view") or {}
    detail(f"fold: {view.get('visible')} visible, {view.get('superseded')} superseded "
           f"— the originals are tombstoned, never deleted, and still carry their "
           f"attributions")

    say("10. the same question again, now that the two are one")
    after = (http("POST", f"{SERVICE_URL}/v1/sessions/{shared_id}/query",
                  {"query": question, "agent_session": sessions["aditya"]})
             .get("findings") or [])
    for finding in after[:5]:
        who = ", ".join(a["contributor"] for a in finding.get("attributions", []))
        detail(f"→ ({finding['type']}, from {who or 'synthesis'}) {finding['text'][:88]}")
    if not after:
        detail("nothing returned")
    detail("Aditya sees this one even though he half-wrote it: the merged Finding "
           "carries BOTH attributions, so it is no longer only his — which is the "
           "point, the team's version of what he knew is worth reading back")

    if args.exit_when_done:
        say("done — stopping everything")
        stop_all()
        return 0

    say("everything is still running. Ctrl-C to stop.")
    for contributor, plan in FEED.items():
        detail(f"http://127.0.0.1:{plan['port']}/debug   {contributor}'s worker")
    detail(f"{SERVICE_URL}/debug   the service")
    detail(f"logs in {DEMO / 'logs'}")
    detail(f"append more work with: python scripts/demo_local.py --help")
    try:
        # Not signal.pause(): that is Unix-only, and on Windows its
        # AttributeError fell straight through to the `finally` below, killing
        # all five processes one line after telling the operator they were
        # still running. Aditya's box is ARM64 Windows.
        while True:
            time.sleep(3600)
    except KeyboardInterrupt:
        pass
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except KeyboardInterrupt:
        print("\nstopping")
    finally:
        stop_all()
