"""Automated §B rehearsal — every demo beat asserted, not hoped.

    uv run python scripts/rehearse_demo.py            # deterministic: scripted verdicts
    uv run python scripts/rehearse_demo.py --live     # the real 8B (spends shared credits)
    uv run python scripts/rehearse_demo.py --service-port 13899 --orch-port 13787

Runs the demo script's §B beats end-to-end against real subprocesses
(service + orchestrator over real sockets, same commands the demo uses),
asserting each beat's *observable* — counts, topics, the by_type=25 merge
proof, the durable-recovery round trip — and writing a transcript to
.measurements/rehearsal-<mode>.log for STATE.md.

Default mode boots the service via scripts/_rehearsal_service.py, whose
FakeProvider verdict script merges seg-005a/b on push 3 — so every beat is
reproducible offline. --live boots `synapse-service` with
SYNAPSE_SYNTHESIZER=aic100 (key from the environment or secrets.jsonc):
HTTP codes and shapes are asserted, model-quality observations are
recorded verbatim instead of asserted. Nothing here is a test double:
if a beat fails in rehearsal it would have failed on stage.

The ports are the rehearsal's own (8899/8787 by default, --service-port /
--orch-port to move them), and the run REFUSES to start if either is already
listening. Until 2026-08-06 they were module constants and this file booted
its servers on top of whatever was already there: the second bind loses, the
beats then talk to the FIRST process, and a rehearsal that "passed" had
measured someone else's stack -- which is how the demo fixtures once reached
a real Shared Session. `--adopt-running` restores the old behaviour for the
one case that wants it (pointing the beats at a stack you booted yourself);
it is off by default, and every branch says out loud which one it took.
"""

from __future__ import annotations

import argparse
import json
import os
import pathlib
import re
import shutil
import signal
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request

# Windows writes piped stdout in the locale codepage, not UTF-8. Every beat
# line below carries U+2014 (`beat()`), the summary carries U+2026, and
# `argparse(description=__doc__)` puts U+00A7 from line 1 into `--help`. On the
# mainstream cp1252 box those three happen to be encodable; on cp437/cp850 --
# the codepage a fresh `cmd.exe` on a non-Western install still lands in -- they
# are not, and `rehearse_demo.py > rehearsal.log` dies inside the rehearsal
# instead of reporting on it. Every sibling on this path already has this
# guard: serve_local.py:50-53, demo_local.py:51-54, doctor.py:58-61,
# trace_one.py:27-30, run_npu_eval.py:37-40, verify_orchestrator.py:41-44.
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

ROOT = pathlib.Path(__file__).resolve().parent.parent
FIX = ROOT / "fixtures" / "findings"
OUT = ROOT / ".measurements"
DEFAULT_SERVICE_PORT = 8899
DEFAULT_ORCH_PORT = 8787
# Rebound from --service-port/--orch-port in main(), before anything binds or
# connects. Left as module globals so the ~40 beat lines below keep reading
# `f"{SVC}/v1/..."` unchanged -- the beats are the reviewed part of this file.
SVC = f"http://127.0.0.1:{DEFAULT_SERVICE_PORT}"
ORCH = f"http://127.0.0.1:{DEFAULT_ORCH_PORT}"

# WHO creates the Shared Session in beat 1, and what for. Named constants
# because beats 7e and 8e assert them BY VALUE after a restart+resync round
# trip: session identity is not durable service-side (the store is in-memory),
# so the only thing that can carry the creator across a restart is the
# orchestrator's locally retained `sessions.json`, and "the creator is still
# the real one" is a claim about these exact strings rather than about whatever
# the service happens to hand back. See beat 7e.
CREATOR = "siddsing"
PURPOSE = "fec decode on the NPU"

LOG_LINES: list[str] = []


def log(line: str) -> None:
    print(line, flush=True)
    LOG_LINES.append(line)


def terminate_tree(proc: subprocess.Popen, timeout: float = 10.0) -> None:
    """Kill `proc` AND its children. Required for correctness, not tidiness.

    Every server here is spawned as `uv run ...`, so `proc` is `uv.exe` and
    the actual listener is its CHILD. On POSIX a SIGTERM to the process
    group reaches both; on Windows there are no process groups in that
    sense and `Popen.send_signal(SIGTERM)` maps to `TerminateProcess`,
    which does NOT cascade. The child kept the port open, so
    `svc.wait()` returned promptly while the service was still serving --
    beat 7a ("contribute while service is DEAD") then measured a live
    service, reported `sent: True`, and the restart in 7b silently bound
    nothing because 8899 was still taken. Both failures, one cause.

    `taskkill /T` walks the tree; `/F` is needed because the intermediate
    `uv.exe` has no window to receive a graceful close.
    """
    if proc.poll() is not None:
        return
    if os.name == "nt":
        subprocess.run(["taskkill", "/PID", str(proc.pid), "/T", "/F"],
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                       check=False)
    else:
        proc.send_signal(signal.SIGTERM)
    try:
        proc.wait(timeout=timeout)
    except subprocess.TimeoutExpired:
        proc.kill()


def write_transcript(mode: str) -> None:
    OUT.mkdir(exist_ok=True)
    # encoding pinned: Path.write_text defaults to the locale codepage, which on
    # Windows is cp1252 and cannot encode arbitrary topic labels echoed into
    # beat details from service-supplied content.
    (OUT / f"rehearsal-{mode}.log").write_text(
        "\n".join(LOG_LINES) + "\n", encoding="utf-8")


def port_is_listening(port: int, host: str = "127.0.0.1", timeout: float = 0.75) -> bool:
    """True if something ALREADY accepts connections on host:port.

    A connect probe rather than a trial bind, deliberately: a trial bind tells
    you about binding, and the condition that makes this script measure a
    stranger is that a TCP connect COMPLETES -- which is exactly what `wait_up`
    below accepts as proof the boot worked, after which every beat is asserted
    against a store this run does not own.
    """
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


def preflight_ports(ports: dict[str, int], adopt_running: bool,
                    probe=port_is_listening) -> tuple[bool, list[str]]:
    """Decide whether it is safe to boot our own servers on `ports`.

    Returns `(may_proceed, lines)` rather than logging or exiting itself, so the
    decision is testable without opening a socket: `probe` is injected.

    LOUD IN EVERY BRANCH. The failure this prevents was silent -- ports free,
    ports occupied-and-adopted, and ports occupied-and-refused all used to look
    identical in the transcript, so a green rehearsal could not be told apart
    from a green rehearsal of somebody else's process.
    """
    busy = [(name, port) for name, port in ports.items() if probe(port)]
    listed = ", ".join(f"{name} {port}" for name, port in ports.items())
    if adopt_running:
        return True, ["!! --adopt-running: PORT GUARD OFF. " + (
            "ADOPTING the process(es) already listening on "
            + ", ".join(f"{name} {port}" for name, port in busy)
            + " -- the beats below measure a stack this run did not boot."
            if busy else
            f"nothing is listening on {listed}; this run boots its own servers.")]
    if busy:
        return False, [
            "!! REFUSING to rehearse: "
            + ", ".join(f"{name} port {port} is ALREADY LISTENING"
                        for name, port in busy),
            "!! This script boots its own service and orchestrator. The second "
            "bind loses, the beats below talk to whatever was already there, "
            "and the demo fixtures get pushed into ITS Shared Session.",
            "!! Free the port(s), move this run with --service-port/--orch-port, "
            "or pass --adopt-running if measuring the running stack is the point.",
        ]
    return True, [f"port guard: {listed} -- all free, booting our own."]


def beat(name: str, ok: bool, detail: str = "") -> bool:
    log(f"  {'PASS' if ok else 'FAIL'}  {name}" + (f" — {detail}" if detail else ""))
    return ok


def http(method: str, url: str, body: dict | None = None, timeout: float = 120.0) -> tuple[int, dict]:
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, method=method,
                                 headers={"content-type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status, json.loads(resp.read() or b"{}")
    except urllib.error.HTTPError as exc:
        return exc.code, json.loads(exc.read() or b"{}")


def wait_up(url: str, seconds: float = 15.0) -> bool:
    deadline = time.time() + seconds
    while time.time() < deadline:
        try:
            urllib.request.urlopen(url, timeout=2)
            return True
        except urllib.error.HTTPError:
            return True  # any HTTP answer means the socket is up
        except Exception:
            time.sleep(0.3)
    return False


# ── corpus (the demo script's builder, repo-rooted) ──────────────────────────

def _inference_cloud_key() -> str | None:
    """The Cirrascale key for --live, or None. Never printed, never logged.

    Parses. The previous version ran `"api_key"\\s*:\\s*"([^"]+)"` over the raw
    text of secrets.jsonc and took the first hit, which is wrong three ways and
    all three are silent:

      * `anthropic.api_key` matches that pattern too. A file with an empty
        `inference_cloud.api_key` and a real Anthropic key handed the Anthropic
        key to `INFERENCE_CLOUD_API_KEY`, and the rehearsal then reported an
        upstream auth failure that named the wrong credential.
      * A commented-out `// "api_key": "..."` is not a credential. Every other
        reader in the tree strips `^\\s*//.*$` before parsing for exactly this
        reason (scripts/local_model_server.py:462, scripts/doctor.py:260,
        scripts/serve_local.py:100); a regex over raw text resurrects a block
        the team deliberately disabled.
      * It ignored the `inference_cloud` block entirely, so
        secrets.example.jsonc:3's claim that this file reads that block was
        false.

    The precedence below is `local_model_server._credentials`' (see
    scripts/local_model_server.py:447-470), so the two agree about which key
    the --live path uses.
    """
    api1 = ROOT / "api-1.json"
    if api1.is_file():
        m = re.search(r'"INFERENCE_CLOUD_API_KEY"\s*:\s*"([^"]+)"',
                      api1.read_text(encoding="utf-8"))
        if m and m.group(1):
            return m.group(1)

    secrets = ROOT / "secrets.jsonc"
    if secrets.is_file():
        stripped = re.sub(r"^\s*//.*$", "", secrets.read_text(encoding="utf-8"),
                          flags=re.MULTILINE)
        try:
            data = json.loads(stripped)
        except json.JSONDecodeError:
            data = {}
        if isinstance(data, dict):
            block = data.get("inference_cloud") or {}
            key = (block.get("api_key") if isinstance(block, dict) else None) \
                or data.get("api_key")
            if key:
                return str(key)
    return None


def load(stem: str) -> list[dict]:
    return json.loads((FIX / f"{stem}.findings.json").read_text())


def filler(prefix: str, n: int, contributor: str, agent_session: str, text: str) -> list[dict]:
    return [{"id": f"f-{prefix}-{i:02d}", "type": "learning", "text": text.format(i=i),
             "attributions": [{"contributor": contributor, "agent_session": agent_session,
                               "agent": "claude-code"}],
             "ts": "2026-08-05T09:00:00Z", "refs": [], "provenance": "distilled",
             "status": "kept", "merged_from": [], "merged_into": None} for i in range(n)]


def build_payloads() -> tuple[int, int, int]:
    OUT.mkdir(exist_ok=True)
    push1 = load("seg-005a") + load("seg-001") + filler(
        "a", 5, "aditya", "as-demo-aditya", "The build script re-exports flag {i} on every run.")
    push2 = (load("seg-002") + load("seg-003") + load("seg-006") + load("seg-007")
             + filler("b", 8, "akhil", "as-demo-akhil",
                      "Allocation attempt {i} for the context binary trips the pool ceiling."))
    push3 = load("seg-005b") + filler(
        "c", 1, "aditya", "as-demo-aditya", "The tokenizer cache is rebuilt on cold start ({i}).")
    for name, batch in (("push1", push1), ("push2", push2), ("push3", push3)):
        (OUT / f"demo-{name}.json").write_text(json.dumps({"findings": batch}, indent=1))
    return len(push1), len(push2), len(push3)


QUERIES = ["what do we know about timing", "why does the decode fail",
           "what should I avoid touching"]

# The seat that does the asking in beats 3-7. TWO ids, because since 2026-08-06
# the wire carries both: `contributor` is the identity the service keys
# suppression (retrieval.visible_to) and the watermark (store.last_seen) on, and
# `agent_session` is the conversation it arrived from, now only ever a fallback
# for an un-upgraded client. The real orchestrator sends both on every query and
# every watermark (server.py), so this is the shape the demo actually puts on
# the wire.
#
# ⟨CORRECTED 2026-08-06⟩ Every request below used to send `agent_session` alone.
# The re-key is additive, so they all still passed -- and that was the problem:
# they were exercising api._asking_contributor's FALLBACK arm on every beat,
# leaving the primary path with no coverage at all. A service that had dropped
# `contributor` handling outright would have rehearsed green.
OBSERVER = "observer"
OBSERVER_SESSION = "as-observer"


def boot_service(live: bool, port: int = DEFAULT_SERVICE_PORT,
                 phase: str = "main") -> subprocess.Popen:
    env = dict(os.environ)
    env["REHEARSAL_PHASE"] = phase
    # NO DEBOUNCE IN THE REHEARSAL (2026-08-06, with the debounce itself).
    # api.MERGE_MIN_INTERVAL_S defaults to 60s and this file pushes beats 2, 4a
    # and 5a back to back, so pushes 2 and 3 would be DEFERRED and the flagship
    # merge beat (5b, the seg-005a/seg-005b merge the whole fixture corpus is
    # built around) would simply never run. Worse than a red beat: the deferred
    # rounds do not consume their scripted verdict slots either, so every later
    # query pops the wrong script and beats 4c and 6 assert against another
    # beat's response. Set on the ENV rather than passed to `build_app`, because
    # it has to reach BOTH branches below -- the scripted `_rehearsal_service.py`
    # and the real `synapse-service` that `--live` runs, which has no such
    # parameter to pass. test_api.py sets `merge_min_interval_s=0` for exactly
    # this reason; the rehearsal is the demo-readiness gate and was missed.
    #
    # The debounce itself still has coverage: test_merge_debounce.py owns it.
    # What this file rehearses is the demo, and the demo does not wait 60s
    # between pushes.
    env["SYNAPSE_MERGE_MIN_INTERVAL_S"] = "0"
    if live:
        env["SYNAPSE_SYNTHESIZER"] = "aic100"
        if "INFERENCE_CLOUD_API_KEY" not in env:
            key = _inference_cloud_key()
            if not key:
                raise SystemExit("--live: no API key found in env, api-1.json, or secrets.jsonc")
            env["INFERENCE_CLOUD_API_KEY"] = key
        cmd = ["uv", "run", "synapse-service", "--port", str(port)]
    else:
        cmd = ["uv", "run", "python", "scripts/_rehearsal_service.py",
               "--port", str(port)]
    return subprocess.Popen(cmd, cwd=ROOT, env=env,
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--live", action="store_true",
                        help="real 8B via Cirrascale (spends shared credits)")
    parser.add_argument("--service-port", type=int, default=DEFAULT_SERVICE_PORT,
                        help=f"port for THIS run's service (default {DEFAULT_SERVICE_PORT})")
    parser.add_argument("--orch-port", type=int, default=DEFAULT_ORCH_PORT,
                        help=f"port for THIS run's orchestrator (default {DEFAULT_ORCH_PORT})")
    parser.add_argument("--adopt-running", action="store_true",
                        help="measure whatever is already listening on those ports "
                             "instead of refusing to start (off by default)")
    args = parser.parse_args()
    mode = "live" if args.live else "fake"
    failures = 0
    procs: list[subprocess.Popen] = []

    global SVC, ORCH
    SVC = f"http://127.0.0.1:{args.service_port}"
    ORCH = f"http://127.0.0.1:{args.orch_port}"

    log(f"== rehearsal ({mode}) ==")
    log(f"ports: service {args.service_port}, orchestrator {args.orch_port}")
    may_proceed, guard_lines = preflight_ports(
        {"service": args.service_port, "orchestrator": args.orch_port},
        adopt_running=args.adopt_running)
    for line in guard_lines:
        log(line)
    if not may_proceed:
        # The transcript is written on the refusal path too, so a stale green
        # rehearsal-<mode>.log cannot sit there answering for a run that never
        # happened: STATE.md reads that file, not this stdout.
        write_transcript(mode)
        return 2

    def run_beat(name: str, ok: bool, detail: str = "") -> None:
        nonlocal failures
        if not beat(name, ok, detail):
            failures += 1

    try:
        n1, n2, n3 = build_payloads()
        run_beat("beat 0: corpus properties", (n1, n2, n3) == (10, 14, 2),
                 f"push sizes {n1}/{n2}/{n3}, cumulative-before-push3 {n1 + n2}")

        svc = boot_service(args.live, args.service_port)
        procs.append(svc)
        orch = subprocess.Popen(
            ["uv", "run", "synapse-orchestrator", "--port", str(args.orch_port),
             "--service-url", SVC, "--state-dir", ".synapse-rehearsal"],
            cwd=ROOT, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        procs.append(orch)
        run_beat("boot: service + orchestrator", wait_up(f"{SVC}/debug") and wait_up(f"{ORCH}/producer/findings" if False else f"{ORCH}/mcp"))

        code, body = http("POST", f"{SVC}/v1/sessions",
                          {"purpose": PURPOSE, "created_by": CREATOR})
        sid = body.get("shared_id", "")
        run_beat("beat 1: create session", code == 201 and bool(sid), sid)

        code, body = http("POST", f"{SVC}/v1/sessions/{sid}/findings",
                          json.loads((OUT / "demo-push1.json").read_text()))
        run_beat("beat 2: push 1", code == 200 and body.get("accepted") == 10, str(body))

        # THE ONE DELIBERATE LEGACY-SHAPED REQUEST in this file: `agent_session`
        # and no `contributor`, which is exactly what an un-upgraded orchestrator
        # on a teammate's laptop still sends. The 2026-08-06 re-key is additive on
        # the wire so that the service and the orchestrator can be deployed in
        # either order -- and "additive" is a claim about a request nobody in this
        # repo sends any more, so one has to be sent here on purpose or nothing
        # exercises it. Every other beat sends `contributor`, the primary path.
        #
        # Pinned on the WATERMARK rather than on a query deliberately: the
        # watermark is a pure read, while /query calls store.mark_seen, so pinning
        # it there would leave a second watermark filed under a second identity for
        # the same asker -- an artefact of the pin, not of the system.
        code, wm = http("GET", f"{SVC}/v1/sessions/{sid}/watermark?agent_session=as-observer")
        run_beat("beat 3: arrival watermark (legacy agent_session-only shape, on purpose)",
                 code == 200 and "by_type" in wm, str(wm)[:120])

        code, body = http("POST", f"{SVC}/v1/sessions/{sid}/findings",
                          json.loads((OUT / "demo-push2.json").read_text()))
        run_beat("beat 4a: push 2", code == 200 and body.get("accepted") == 14, str(body))
        code, wm = http("GET", f"{SVC}/v1/sessions/{sid}/watermark"
                               f"?contributor={OBSERVER}&agent_session={OBSERVER_SESSION}")
        topics = wm.get("topics", [])
        run_beat("beat 4b: topics in watermark", bool(topics),
                 ", ".join(t.get("label", "?") for t in topics[:4]) or "none")
        pre_answers = []
        for q in QUERIES:
            code, body = http("POST", f"{SVC}/v1/sessions/{sid}/query",
                              {"query": q, "contributor": OBSERVER,
                               "agent_session": OBSERVER_SESSION})
            pre_answers.append((q, body.get("findings", [])))
            run_beat(f"beat 4c: query '{q[:28]}…'", code == 200 and "findings" in body,
                     f"{len(body.get('findings', []))} findings")
        code, body = http("POST", f"{SVC}/v1/sessions/{sid}/findings",
                          json.loads((OUT / "demo-push3.json").read_text()))
        run_beat("beat 5a: push 3", code == 200 and body.get("accepted") == 2, str(body))
        # The merge evidence is the append-only log's Merged entries — the sum
        # arithmetic the demo script drafted is only valid if EXACTLY the
        # flagship pair merges, and the live 8B merges more than that (observed
        # 2026-08-05: 24 pushed -> 8 retrievable before push 3).
        code, dbg = http("GET", f"{SVC}/debug/stats.json?session={sid}")
        tail = (dbg.get("session") or {}).get("log_tail", [])
        merged_entries = [e for e in tail if e.get("kind") == "Merged"]
        flagship = [e for e in merged_entries
                    if "f-005a-01" in e.get("summary", "") and "f-005b-01" in e.get("summary", "")]
        detail = f"{len(merged_entries)} Merged entr(y/ies); " + (
            f"flagship pair merged: {flagship[0]['summary'][:80]}" if flagship
            else "flagship pair NOT directly merged — inspect: " +
                 " | ".join(e["summary"][:60] for e in merged_entries[-3:]))
        if args.live:
            run_beat("beat 5b: merges (OBSERVED via log tail, live 8B)",
                     bool(merged_entries), detail)
        else:
            run_beat("beat 5b: the flagship merge (log tail)", bool(flagship), detail)

        for q in QUERIES:
            code, body = http("POST", f"{SVC}/v1/sessions/{sid}/query",
                              {"query": q, "contributor": OBSERVER,
                               "agent_session": OBSERVER_SESSION})
            run_beat(f"beat 6: re-query '{q[:28]}…'", code == 200 and "findings" in body,
                     f"{len(body.get('findings', []))} findings")

        # The instrumentation beat: our own dashboard's CallLog is the proof the
        # model layer was actually reached — a rehearsal that passes shapes while
        # every model call fails must FAIL here, not smile.
        code, dbg = http("GET", f"{SVC}/debug/stats.json")
        llm = (dbg.get("session") or {}).get("llm", [])
        ok_calls = [c for c in llm if c.get("ok")]
        valid = [c for c in ok_calls if c.get("schema_valid")]
        run_beat("beat 6b: model layer actually reached (CallLog)",
                 bool(ok_calls) and bool(valid),
                 f"{len(llm)} calls, {len(ok_calls)} ok, {len(valid)} schema-valid")

        # beat 7 — durable recovery through the orchestrator
        #
        # Two files stand in for what the lifecycle MCP tools write when a human
        # drives them: the BINDING (`synapse_worker.discovery.join_session`) and
        # the retained SESSION METADATA (`session_meta.record_session`, added
        # 2026-08-06). Both are written through the production writers rather
        # than as hand-built JSON, so a format change breaks this line rather
        # than silently rehearsing a shape nothing produces. That the real
        # `create_session`/`join_session` actually call them is asserted, by
        # mutation, in packages/orchestrator/tests/test_lifecycle_tools.py --
        # what is being rehearsed here is what happens NEXT, over real sockets:
        # whether the creator survives the restart and the resync.
        subprocess.run(
            ["uv", "run", "python", "-c",
             "from synapse_contracts.binding import write_binding, SessionBinding\n"
             "from synapse_orchestrator.session_meta import record_session\n"
             "from datetime import datetime, timezone\n"
             "write_binding('.synapse-rehearsal/bindings/claude-code.json', SessionBinding("
             f"agent_session_id='as-demo-aditya', shared_id='{sid}', contributor='aditya',"
             "agent='claude-code', transcript_path='(rehearsal)',"
             "pinned_at=datetime.now(timezone.utc)))\n"
             f"record_session('.synapse-rehearsal', {sid!r}, created_by={CREATOR!r},"
             f" purpose={PURPOSE!r})"],
            cwd=ROOT, check=True)
        terminate_tree(svc)
        time.sleep(1)
        code, body = http("POST", f"{ORCH}/producer/findings", {"findings": [{
            "id": "f-demo-recovery-01", "type": "learning",
            "text": "The resync recreate pass must run before the retry loop, or queued findings never flush.",
            "attributions": [{"contributor": "aditya", "agent_session": "as-demo-aditya",
                              "agent": "claude-code"}],
            "ts": "2026-08-05T09:05:00Z", "refs": [], "provenance": "contributed",
            "status": "kept", "merged_from": [], "merged_into": None}]})
        run_beat("beat 7a: contribute while service is dead",
                 code == 200 and body.get("accepted") == 1 and body.get("sent") is False, str(body))

        svc2 = boot_service(args.live, args.service_port, phase="recovery")
        procs.append(svc2)
        run_beat("beat 7b: service restarted", wait_up(f"{SVC}/debug"))
        resync = subprocess.run(
            ["uv", "run", "synapse-orchestrator", "resync",
             "--service-url", SVC, "--state-dir", ".synapse-rehearsal"],
            cwd=ROOT, capture_output=True, text=True, timeout=180)
        run_beat("beat 7c: resync", resync.returncode == 0,
                 (resync.stdout or resync.stderr).strip()[:140])
        code, body = http("POST", f"{SVC}/v1/sessions/{sid}/query",
                          {"query": "what must run before the retry loop?",
                           "contributor": OBSERVER, "agent_session": OBSERVER_SESSION})
        recovered = any(f.get("id") == "f-demo-recovery-01" for f in body.get("findings", []))
        run_beat("beat 7d: recovery finding retrievable after restart",
                 code == 200 and (recovered or args.live),
                 "recovered" if recovered else "not ranked (live ranking is the 8B's call)" if args.live else str(body)[:140])

        # beat 7e — WHO OWNS THE SESSION THAT JUST CAME BACK.
        #
        # The restart above wiped the service's store, so the sh-... beats 1-7d
        # used no longer existed and resync RE-CREATED it. Until 2026-08-06 it
        # re-created it as `{"purpose": "(recovered by resync)", "created_by":
        # "resync"}` -- the orchestrator had no record of the session, only of
        # its findings. Against a live service that is inert (create-or-return
        # hands back the existing session unchanged), so it only ever bit in
        # exactly this arc: the recreated session's creator BECAME the string
        # "resync", after which `api.end_session`'s creator-only gate refused
        # siddsing ("only resync can end this session") and accepted anyone who
        # sent `{"ended_by": "resync"}`.
        #
        # Asserted BY VALUE against the beat 1 constants, not "whatever comes
        # back": reading the creator back and trusting it is precisely how the
        # defect stayed invisible for a week (see beat 8e, which used to do
        # that). The read is `POST /v1/sessions` with a known id --
        # create-or-return -- so it doubles as the pin that recreating an
        # existing session hands back the ORIGINAL identity unchanged.
        code, session = http("POST", f"{SVC}/v1/sessions",
                             {"purpose": "(rehearsal: who owns this session?)",
                              "created_by": "not-the-creator", "shared_id": sid})
        run_beat("beat 7e: the real creator and purpose survive restart + resync",
                 code == 200 and session.get("created_by") == CREATOR
                 and session.get("purpose") == PURPOSE,
                 f"created_by={session.get('created_by')!r} "
                 f"purpose={session.get('purpose')!r}")

        # ── beat 8 — the lifecycle arc: join, leave, re-join, end ────────────
        # Added 2026-08-06 with the lifecycle routes themselves. Everything
        # below runs against the SAME service + orchestrator subprocesses and the
        # same real localhost sockets as beats 0-7 -- packages/service/tests
        # already covers these routes through an in-process ASGI transport, and
        # that is a different claim from "the routes answer over a socket, in the
        # order a human drives them, on the session this rehearsal has been
        # filling for the last seven beats".
        #
        # It runs on the POST-RESTART service on purpose rather than earlier:
        # ending a session is terminal, so any beat placed after it would be
        # asserting against a closed session, and the recovery arc is the one
        # part of the demo that must still work when the memory has just been
        # rebuilt from a retained log.
        LEAVER, TEAMMATE = "aditya", "akhil"
        LEAVER_SESSION, LEAVER_SESSION_2 = "as-demo-aditya", "as-demo-aditya-window-2"

        code, body = http("POST", f"{SVC}/v1/sessions/{sid}/members",
                          {"contributor": LEAVER, "agent_session": LEAVER_SESSION})
        joined_one = code == 200 and LEAVER in body.get("members", [])
        code, body = http("POST", f"{SVC}/v1/sessions/{sid}/members",
                          {"contributor": TEAMMATE, "agent_session": "as-demo-akhil"})
        run_beat("beat 8a: two contributors join",
                 joined_one and code == 200 and TEAMMATE in body.get("members", []),
                 str(body.get("members")))

        # aditya READS, which is the only thing that moves a watermark:
        # store.mark_seen is on the /query path and nowhere else. Without this
        # the re-join beat below could not tell "kept my place" apart from
        # "never had one", because both read new_since == version.
        http("POST", f"{SVC}/v1/sessions/{sid}/query",
             {"query": "what must run before the retry loop?",
              "contributor": LEAVER, "agent_session": LEAVER_SESSION})
        code, wm = http("GET", f"{SVC}/v1/sessions/{sid}/watermark"
                               f"?contributor={LEAVER}&agent_session={LEAVER_SESSION}")
        caught_up, version = wm.get("new_since"), wm.get("version")
        run_beat("beat 8b: reading marks the watermark",
                 code == 200 and caught_up == 0 and (version or 0) > 0,
                 f"new_since {caught_up} at version {version}")

        code, body = http("DELETE", f"{SVC}/v1/sessions/{sid}/members/{LEAVER}")
        members = body.get("members", [])
        run_beat("beat 8c: leave detaches one member and only that member",
                 code == 200 and LEAVER not in members and TEAMMATE in members,
                 str(members))

        # THE beat this whole arc exists for, and the reason the identity re-key
        # was worth doing. Re-join as the SAME Contributor from a DIFFERENT Agent
        # Session id -- which is not an exotic case, it is closing one Claude Code
        # window and opening another, because an Agent Session id IS the
        # transcript filename stem (worker/discovery.py:112).
        #
        # Keyed on the Agent Session, as it was before 2026-08-06, the new stem is
        # an unknown key: last_seen falls back to 0 and new_since jumps to the
        # whole memory version. Nothing raises, no status code changes, and the
        # only symptom is a briefing telling someone who read everything five
        # minutes ago that all of it is new to them. A regression here is
        # SILENT, which is exactly why it gets an assertion of its own rather
        # than riding along inside the join beat.
        code, body = http("POST", f"{SVC}/v1/sessions/{sid}/members",
                          {"contributor": LEAVER, "agent_session": LEAVER_SESSION_2})
        rejoined = code == 200 and LEAVER in body.get("members", [])
        code, wm = http("GET", f"{SVC}/v1/sessions/{sid}/watermark"
                               f"?contributor={LEAVER}&agent_session={LEAVER_SESSION_2}")
        after, version_after = wm.get("new_since"), wm.get("version")
        run_beat("beat 8d: re-joining from a NEW agent session keeps your place",
                 rejoined and code == 200 and after == caught_up
                 and after != version_after and (version_after or 0) > 0,
                 f"new_since {caught_up} -> {after} at version {version_after} "
                 f"(a reset to the Agent Session key would read {version_after})")

        # THE CREATOR the gate below is about, re-read here because everything
        # between 7e and now (two joins, a leave, a re-join) touches membership,
        # and the creator-only gate must be unmoved by any of it.
        #
        # ⟨TIGHTENED 2026-08-06⟩ This beat used to read the creator back and
        # assert only that it was non-empty and not the name just sent, with a
        # comment explaining that the creator here is NOT beat 1's because
        # resync's recreate had overwritten it with "resync". That comment was
        # describing the defect, and the loose assertion was the reason it could
        # be described rather than caught: with the creator taken from the wire,
        # the 403 in 8f and the 200 in 8g pass for ANY creator, including one no
        # human can be. Now that resync restores the real one (beat 7e), the
        # assertion is the real one -- `creator` is the beat 1 constant, so 8f
        # proves a teammate is refused and 8g proves the ACTUAL OWNER is
        # accepted, which is the property the gate exists for.
        code, session = http("POST", f"{SVC}/v1/sessions",
                             {"purpose": "(rehearsal: who owns this session?)",
                              "created_by": "not-the-creator", "shared_id": sid})
        # `or ""`, not `get(..., "")`: `created_by` is `str | None` since
        # 2026-08-06 and the service serialises the unknown case as JSON null,
        # so the key is PRESENT and the default never applies. `creator` is used
        # as the left operand of an `in` against the 403's error text in 8f,
        # where a None raises TypeError -- and run_beat exists precisely so a
        # failing beat is RECORDED and the rehearsal continues, so an uncaught
        # crash here would take beats 8g/8h/8i with it and report nothing. The
        # `== CREATOR` at 394 is null-safe already and left alone.
        creator = session.get("created_by") or ""
        run_beat("beat 8e: the session is still owned by the human who created it",
                 code == 200 and creator == CREATOR, f"{creator!r} (expected {CREATOR!r})")

        # BEFORE the successful end, deliberately: once the session is closed the
        # creator-only check would still fire, but it would be indistinguishable
        # from a session that simply refuses everything, and the layer being
        # asserted here is the service-side gate (spec's layer 2 of three) -- the
        # one an agent cannot talk its way past.
        code, body = http("POST", f"{SVC}/v1/sessions/{sid}/end", {"ended_by": TEAMMATE})
        # `creator and creator in ...` rather than the bare `in`: with `creator`
        # normalised to "" above, an empty left operand is `in` EVERY string, so
        # a lost creator would turn this beat into "any 403 will do" — the beat
        # asserting the gate names the owner would pass hardest exactly when
        # the owner is unknown. When it IS unknown the honest assertion is the
        # other arm of the gate (api.end_session: membership required, and why),
        # so that is what is checked instead.
        error_text = body.get("error", "")
        named_the_gate = (creator in error_text if creator
                          else "member" in error_text)
        run_beat("beat 8f: a non-creator cannot end the session",
                 code == 403 and named_the_gate, f"{code} {body}")

        # The gate ACCEPTS the real owner -- the half that a sentinel creator
        # silently broke, and the half no status code would have flagged: with
        # created_by="resync" this same call returned 403 to siddsing, and the
        # session could only be closed by someone quoting a string out of
        # cli.py. `CREATOR`, not `creator`, so the beat cannot pass by agreeing
        # with whatever the previous read happened to return.
        code, body = http("POST", f"{SVC}/v1/sessions/{sid}/end", {"ended_by": CREATOR})
        run_beat("beat 8g: the creator-only gate accepts the real creator",
                 code == 200 and body.get("status") == "ended"
                 and body.get("ended_by") == CREATOR, str(body))

        # ALL FOUR routes that serve the memory or extend it, in one beat,
        # because the gate is one helper (api._unavailable) and the failure mode
        # it exists to prevent is a route that quietly does not go through it --
        # which only shows up if every route is asked. A write accepted into a
        # session the team has closed returns 200 and disappears into a log
        # nothing will ever read again.
        closed = {}
        code, body = http("POST", f"{SVC}/v1/sessions/{sid}/query",
                          {"query": "is anyone still there", "contributor": OBSERVER,
                           "agent_session": OBSERVER_SESSION})
        closed["query"] = (code, body)
        code, body = http("POST", f"{SVC}/v1/sessions/{sid}/findings",
                          json.loads((OUT / "demo-push3.json").read_text()))
        closed["push_findings"] = (code, body)
        code, body = http("POST", f"{SVC}/v1/sessions/{sid}/synthesize", {})
        closed["synthesize"] = (code, body)
        code, body = http("GET", f"{SVC}/v1/sessions/{sid}/watermark"
                                 f"?contributor={OBSERVER}&agent_session={OBSERVER_SESSION}")
        closed["watermark"] = (code, body)
        run_beat("beat 8h: query/push/synthesize/watermark all 409 session_ended",
                 all(c == 409 and b.get("error") == "session_ended"
                     for c, b in closed.values()),
                 "; ".join(f"{route} {c} {b.get('error')}"
                           for route, (c, b) in closed.items()))

        # The durability caveat, asserted rather than trusted. `cmd_resync`'s
        # step 1 is create-or-return against every session id in the retained
        # log, and that call is made unconditionally -- so "resync does not
        # bring back a session the team closed" is a property of
        # store.create_session returning an existing session UNCHANGED (leaving
        # the SessionEnded entry in the log, from which get_context folds the
        # status) and of the orchestrator's locally retained ended.json. Both are
        # exercised here; what is asserted is the observable they exist for.
        resurrect = subprocess.run(
            ["uv", "run", "synapse-orchestrator", "resync",
             "--service-url", SVC, "--state-dir", ".synapse-rehearsal"],
            cwd=ROOT, capture_output=True, text=True, timeout=180)
        code, body = http("GET", f"{SVC}/v1/sessions/{sid}/watermark"
                                 f"?contributor={OBSERVER}&agent_session={OBSERVER_SESSION}")
        run_beat("beat 8i: resync does not resurrect an ended session",
                 code == 409 and body.get("error") == "session_ended",
                 f"resync exit {resurrect.returncode}: "
                 f"{(resurrect.stdout or resurrect.stderr).strip()[:110]}")

    finally:
        for p in procs:
            terminate_tree(p)
        write_transcript(mode)
        # shutil, not `rm -rf`: `rm` is not a Windows command, so the cleanup
        # raised FileNotFoundError out of the `finally` block and replaced the
        # real beat failures with a traceback.
        shutil.rmtree(ROOT / ".synapse-rehearsal", ignore_errors=True)

    log(f"== {('ALL BEATS PASS' if failures == 0 else f'{failures} BEAT(S) FAILED')} ({mode}) ==")
    return 0 if failures == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
