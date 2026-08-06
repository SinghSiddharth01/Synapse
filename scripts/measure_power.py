"""Measure energy per distillation on each compute unit — NPU vs GPU vs CPU.

    uv run python scripts/measure_power.py            # all three units
    uv run python scripts/measure_power.py --units npu cpu

docs/NPU-RUNBOOK.md Phase 4 asks for this number and calls it the one that
decides the argument: "Until this number exists, nobody claims efficiency on
stage." Every other harness in scripts/ measures latency or quality; this one
measures joules.

THE INSTRUMENT. Windows exposes whole-system power draw through the battery
controller: `root\\wmi BatteryStatus.DischargeRate`, in milliwatts. It is the
only power telemetry this box offers -- there is no per-component rail, and no
NPU performance counter exists on Windows for the Hexagon part.

Two consequences, both structural:

  1. It reports ONLY on battery. Plugged in, the battery is not discharging and
     the field reads 0. This script refuses to run on mains rather than
     recording a column of zeros.
  2. It is WHOLE SYSTEM -- display, radios, background work, and the sampler
     itself are all in it. So the workload figure alone is meaningless; what
     means something is the DELTA against an idle baseline captured in the same
     session, on the same machine, with the same sampler running. Every number
     this script reports is a delta.

WHAT IS AND IS NOT CONTROLLED. Screen brightness, radios and background
processes are not controlled by this script and will sit in both the baseline
and the workload. They cancel to first order. What does NOT cancel is anything
that changes DURING the run -- a sync, an indexer, a notification. The script
reports the standard deviation of every window so a disturbed run is visible
rather than silently averaged in.
"""
from __future__ import annotations

import argparse
import json
import statistics
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
GENIEX = Path.home() / "AppData/Local/GenieX CLI/geniex.exe"
BASE_URL = "http://127.0.0.1:18181/v1"

# Held constant across compute units. The production QAIRT bundle
# (qualcomm/Qwen3-4B-Instruct-2507:W4A16) is NPU-EXCLUSIVE and cannot run on
# CPU or GPU at all, so a three-way comparison has to use a GGUF model. This
# one is the closest in size to the production 4B.
MODEL = "google/gemma-4-E4B-it-qat-q4_0-gguf:Q4_0"

BASELINE_S = 45
SETTLE_S = 8

_PROMPT_BODY = "\n".join(
    f"assistant/text: Checked {part}. The call returned 200 and the binding was "
    f"written, so the path is wired end to end."
    for part in ("the orchestrator", "the producer endpoint", "the relay",
                 "the write-ahead log", "the segmenter", "the triage gate")
)
MESSAGES = [
    {"role": "system", "content":
        "You condense a developer session into durable findings. Reply with ONLY "
        'a JSON object: {"findings": [{"type": "learning", "text": "..."}]}. '
        "Types: learning, decision, dead_end, open_question."},
    {"role": "user", "content": f"SEGMENT:\n{_PROMPT_BODY}\n\nReturn the JSON object."},
]

_PS_SAMPLE = (
    "$b = Get-CimInstance -Namespace root\\wmi -ClassName BatteryStatus; "
    "Write-Output \"$($b.DischargeRate),$($b.PowerOnline)\""
)


def read_power() -> tuple[int, bool]:
    """(milliwatts, plugged_in). Returns (0, True) if the read fails."""
    try:
        done = subprocess.run(["powershell", "-NoProfile", "-Command", _PS_SAMPLE],
                              capture_output=True, text=True, timeout=15)
        rate, online = done.stdout.strip().split(",")
        return int(rate), online.strip().lower() == "true"
    except Exception:                                          # noqa: BLE001
        return 0, True


class Sampler(threading.Thread):
    """Background 1 Hz power trace. Runs for the whole session, baseline
    included, so its own cost is in every window and cancels in the delta."""

    def __init__(self) -> None:
        super().__init__(daemon=True)
        self.samples: list[tuple[float, int]] = []
        self._stop = threading.Event()

    def run(self) -> None:
        while not self._stop.is_set():
            milliwatts, _ = read_power()
            self.samples.append((time.time(), milliwatts))
            self._stop.wait(1.0)

    def stop(self) -> None:
        self._stop.set()

    def window(self, start: float, end: float) -> list[int]:
        return [mw for ts, mw in self.samples if start <= ts <= end and mw > 0]


def serve(unit: str) -> None:
    subprocess.run(["powershell", "-NoProfile", "-Command",
                    "Get-Process geniex -ErrorAction SilentlyContinue | "
                    "Stop-Process -Force"], capture_output=True)
    time.sleep(4)
    subprocess.Popen([str(GENIEX), "serve", "--compute", unit],
                     stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    for _ in range(30):
        try:
            urllib.request.urlopen(f"{BASE_URL}/models", timeout=3).read()
            return
        except Exception:                                      # noqa: BLE001
            time.sleep(2)
    raise SystemExit(f"geniex serve did not come up for --compute {unit}")


def distil_once() -> tuple[int, bool]:
    payload = json.dumps({"model": MODEL, "messages": MESSAGES,
                          "temperature": 0.0, "max_tokens": 200}).encode()
    request = urllib.request.Request(f"{BASE_URL}/chat/completions", data=payload,
                                     headers={"Content-Type": "application/json"},
                                     method="POST")
    try:
        with urllib.request.urlopen(request, timeout=600) as response:
            data = json.loads(response.read())
        return int((data.get("usage") or {}).get("completion_tokens", 0)), True
    except Exception:                                          # noqa: BLE001
        return 0, False


def summarize(samples: list[int]) -> dict:
    if not samples:
        return {"n": 0, "mean_mw": 0, "stdev_mw": 0, "min_mw": 0, "max_mw": 0}
    return {
        "n": len(samples),
        "mean_mw": round(statistics.mean(samples), 1),
        "stdev_mw": round(statistics.stdev(samples), 1) if len(samples) > 1 else 0.0,
        "min_mw": min(samples), "max_mw": max(samples),
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--units", nargs="+", default=["npu", "gpu", "cpu"])
    parser.add_argument("--calls", type=int, default=8,
                        help="distillations per unit (default 8)")
    parser.add_argument("--out", default=".measurements/power.json")
    args = parser.parse_args(argv)

    milliwatts, plugged = read_power()
    if plugged or milliwatts <= 0:
        print("REFUSING TO RUN: the machine is on mains power.\n"
              "  DischargeRate reports 0 while plugged in, so every number this\n"
              "  script produces would be a zero. Unplug the charger, leave it\n"
              "  unplugged for the whole run, and start again.\n"
              f"  (read: DischargeRate={milliwatts} mW, PowerOnline={plugged})",
              file=sys.stderr)
        return 2

    print(f"on battery, idle draw ~{milliwatts} mW — starting\n")
    sampler = Sampler()
    sampler.start()

    print(f"idle baseline: {BASELINE_S}s, do not touch the machine")
    base_start = time.time()
    time.sleep(BASELINE_S)
    baseline = summarize(sampler.window(base_start, time.time()))
    print(f"  baseline {baseline['mean_mw']:.0f} mW "
          f"(sd {baseline['stdev_mw']:.0f}, n={baseline['n']})\n")

    results = {}
    for unit in args.units:
        print(f"=== {unit} ===")
        serve(unit)
        tokens, ok = distil_once()                     # warmup: model load
        print(f"  warmup ok={ok}")
        time.sleep(SETTLE_S)

        start = time.time()
        total_tokens, failures = 0, 0
        for i in range(args.calls):
            got, ok = distil_once()
            total_tokens += got
            failures += 0 if ok else 1
            print(f"  {i+1}/{args.calls} tokens={got} ok={ok}")
        end = time.time()

        power = summarize(sampler.window(start, end))
        elapsed = end - start
        delta_mw = power["mean_mw"] - baseline["mean_mw"]
        energy_j = delta_mw / 1000.0 * elapsed
        results[unit] = {
            "elapsed_s": round(elapsed, 1), "calls": args.calls,
            "failures": failures, "output_tokens": total_tokens,
            "tok_per_s": round(total_tokens / elapsed, 2) if elapsed else 0,
            "power": power, "delta_mw": round(delta_mw, 1),
            "energy_j_total": round(energy_j, 1),
            "energy_j_per_call": round(energy_j / args.calls, 2) if args.calls else 0,
            "energy_j_per_token": round(energy_j / total_tokens, 4) if total_tokens else 0,
        }
        print(f"  mean {power['mean_mw']:.0f} mW (sd {power['stdev_mw']:.0f}) "
              f"→ delta {delta_mw:.0f} mW over {elapsed:.0f}s "
              f"= {energy_j:.0f} J, {results[unit]['energy_j_per_token']} J/token\n")

    sampler.stop()
    out = REPO / args.out
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(
        {"model": MODEL, "baseline": baseline, "units": results,
         "trace": [{"ts": ts, "mw": mw} for ts, mw in sampler.samples]},
        indent=2), encoding="utf-8")

    print("=" * 62)
    print(f"{'unit':<6}{'J/call':>10}{'J/token':>10}{'tok/s':>9}{'delta mW':>11}")
    print("-" * 62)
    for unit, r in results.items():
        print(f"{unit:<6}{r['energy_j_per_call']:>10.1f}{r['energy_j_per_token']:>10.3f}"
              f"{r['tok_per_s']:>9.2f}{r['delta_mw']:>11.0f}")
    print("=" * 62)
    print(f"\nwritten to {out}")
    print("Whole-system deltas against an idle baseline, not per-component rails.")
    print("Report the standard deviations with the means -- a quiet run and a")
    print("disturbed one look identical in the mean alone.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
