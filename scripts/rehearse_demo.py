"""Automated §B rehearsal — every demo beat asserted, not hoped.

    uv run python scripts/rehearse_demo.py            # deterministic: scripted verdicts
    uv run python scripts/rehearse_demo.py --live     # the real 8B (spends shared credits)

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
"""

from __future__ import annotations

import argparse
import json
import os
import pathlib
import re
import shutil
import signal
import subprocess
import sys
import time
import urllib.error
import urllib.request

ROOT = pathlib.Path(__file__).resolve().parent.parent
FIX = ROOT / "fixtures" / "findings"
OUT = ROOT / ".measurements"
SVC = "http://127.0.0.1:8899"
ORCH = "http://127.0.0.1:8787"

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


def boot_service(live: bool, phase: str = "main") -> subprocess.Popen:
    env = dict(os.environ)
    env["REHEARSAL_PHASE"] = phase
    if live:
        env["SYNAPSE_SYNTHESIZER"] = "aic100"
        if "INFERENCE_CLOUD_API_KEY" not in env:
            for path, pattern in ((ROOT / "api-1.json", r'"INFERENCE_CLOUD_API_KEY"\s*:\s*"([^"]+)"'),
                                  (ROOT / "secrets.jsonc", r'"api_key"\s*:\s*"([^"]+)"')):
                if path.is_file():
                    m = re.search(pattern, path.read_text())
                    if m and m.group(1):
                        env["INFERENCE_CLOUD_API_KEY"] = m.group(1)
                        break
            if "INFERENCE_CLOUD_API_KEY" not in env:
                raise SystemExit("--live: no API key found in env, api-1.json, or secrets.jsonc")
        cmd = ["uv", "run", "synapse-service"]
    else:
        cmd = ["uv", "run", "python", "scripts/_rehearsal_service.py"]
    return subprocess.Popen(cmd, cwd=ROOT, env=env,
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--live", action="store_true",
                        help="real 8B via Cirrascale (spends shared credits)")
    args = parser.parse_args()
    mode = "live" if args.live else "fake"
    failures = 0
    procs: list[subprocess.Popen] = []

    def run_beat(name: str, ok: bool, detail: str = "") -> None:
        nonlocal failures
        if not beat(name, ok, detail):
            failures += 1

    try:
        log(f"== rehearsal ({mode}) ==")
        n1, n2, n3 = build_payloads()
        run_beat("beat 0: corpus properties", (n1, n2, n3) == (10, 14, 2),
                 f"push sizes {n1}/{n2}/{n3}, cumulative-before-push3 {n1 + n2}")

        svc = boot_service(args.live)
        procs.append(svc)
        orch = subprocess.Popen(
            ["uv", "run", "synapse-orchestrator", "--port", "8787",
             "--service-url", SVC, "--state-dir", ".synapse-rehearsal"],
            cwd=ROOT, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        procs.append(orch)
        run_beat("boot: service + orchestrator", wait_up(f"{SVC}/debug") and wait_up(f"{ORCH}/producer/findings" if False else f"{ORCH}/mcp"))

        code, body = http("POST", f"{SVC}/v1/sessions",
                          {"purpose": "fec decode on the NPU", "created_by": "siddsing"})
        sid = body.get("shared_id", "")
        run_beat("beat 1: create session", code == 201 and bool(sid), sid)

        code, body = http("POST", f"{SVC}/v1/sessions/{sid}/findings",
                          json.loads((OUT / "demo-push1.json").read_text()))
        run_beat("beat 2: push 1", code == 200 and body.get("accepted") == 10, str(body))

        code, wm = http("GET", f"{SVC}/v1/sessions/{sid}/watermark?agent_session=as-observer")
        run_beat("beat 3: arrival watermark", code == 200 and "by_type" in wm, str(wm)[:120])

        code, body = http("POST", f"{SVC}/v1/sessions/{sid}/findings",
                          json.loads((OUT / "demo-push2.json").read_text()))
        run_beat("beat 4a: push 2", code == 200 and body.get("accepted") == 14, str(body))
        code, wm = http("GET", f"{SVC}/v1/sessions/{sid}/watermark?agent_session=as-observer")
        topics = wm.get("topics", [])
        run_beat("beat 4b: topics in watermark", bool(topics),
                 ", ".join(t.get("label", "?") for t in topics[:4]) or "none")
        pre_answers = []
        for q in QUERIES:
            code, body = http("POST", f"{SVC}/v1/sessions/{sid}/query",
                              {"query": q, "agent_session": "as-observer"})
            pre_answers.append((q, body.get("findings", [])))
            run_beat(f"beat 4c: query '{q[:28]}…'", code == 200 and "findings" in body,
                     f"{len(body.get('findings', []))} findings")
        sum_before = sum(wm.get("by_type", {}).values())

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
                              {"query": q, "agent_session": "as-observer"})
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
        subprocess.run(
            ["uv", "run", "python", "-c",
             "from synapse_contracts.binding import write_binding, SessionBinding\n"
             "from datetime import datetime, timezone\n"
             "write_binding('.synapse-rehearsal/bindings/claude-code.json', SessionBinding("
             f"agent_session_id='as-demo-aditya', shared_id='{sid}', contributor='aditya',"
             "agent='claude-code', transcript_path='(rehearsal)',"
             "pinned_at=datetime.now(timezone.utc)))"],
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

        svc2 = boot_service(args.live, phase="recovery")
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
                           "agent_session": "as-observer"})
        recovered = any(f.get("id") == "f-demo-recovery-01" for f in body.get("findings", []))
        run_beat("beat 7d: recovery finding retrievable after restart",
                 code == 200 and (recovered or args.live),
                 "recovered" if recovered else "not ranked (live ranking is the 8B's call)" if args.live else str(body)[:140])

    finally:
        for p in procs:
            terminate_tree(p)
        OUT.mkdir(exist_ok=True)
        # encoding pinned: Path.write_text defaults to the locale codepage,
        # which on Windows is cp1252 and cannot encode arbitrary topic labels
        # echoed into beat details from service-supplied content.
        (OUT / f"rehearsal-{mode}.log").write_text(
            "\n".join(LOG_LINES) + "\n", encoding="utf-8")
        # shutil, not `rm -rf`: `rm` is not a Windows command, so the cleanup
        # raised FileNotFoundError out of the `finally` block and replaced the
        # real beat failures with a traceback.
        shutil.rmtree(ROOT / ".synapse-rehearsal", ignore_errors=True)

    log(f"== {('ALL BEATS PASS' if failures == 0 else f'{failures} BEAT(S) FAILED')} ({mode}) ==")
    return 0 if failures == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
