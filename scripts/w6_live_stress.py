"""W6 stress harness — drive the Anthropic/Haiku distiller arm through the real seams.

WHY THIS IS A SCRIPT AND NOT A TEST
-----------------------------------
Its live half needs a real API call, a real key, and real money. Putting that in
`packages/*/tests/` would either be skipped in CI (a test that never runs is the
ADR-0005 trap in its purest form) or would spend on every `uv run pytest -q`. So
it lives here, is run deliberately, and writes its numbers into
`docs/overnight/w6-live-stress.md`. The offline mirrors of these bounds are the
pytest suite's job and already exist; this script's job is the SHIPPED numbers
under load, and — with a key — under the real endpoint.

TWO PHASES, AND WHY THE OFFLINE ONE STILL RUNS WITHOUT A KEY
------------------------------------------------------------
`--offline` (also the automatic behaviour when no credential is found) runs
everything that does not need the network: the pin resolved through the
production construction path, and the segmenting/chunking bounds measured at the
EFFECTIVE segment budget on inputs at, over, and far past the split seam. Those
numbers are real measurements, not estimates, and they are what
`docs/overnight/w6-live-stress.md` reports. The live phases are additive: they
re-assert the same bounds against claude-haiku-4-5 and add the three things only
the endpoint can answer (does the cap BIND, does the override move it, is a
truncated response classified over-budget rather than degenerate).

WHY IN-PROCESS AND NOT `serve_local.py`
---------------------------------------
`serve_local.py` starts the service and the orchestrator only -- it never spawns
a worker (FLOW.md section 2), and the worker is where the segmenting/chunking
seam under test lives. Driving the arm through `serve_local` would exercise the
MCP `contribute()` path, which hand-builds a one-event Segment and deliberately
does NOT go through `synapse_worker.segmenter` -- i.e. it would skip the exact
code this run is meant to stress. So the harness imports the same two functions
the worker's `run` path calls (`synapse_worker.cli._build_distiller_provider` /
`build_distiller`, and `Segmenter`) and drives them in-process. No ports are
opened, so there is nothing to leave running.

WHAT IS ASSERTED (each able to fail; see ADR 0005)
--------------------------------------------------
  offline 1. The Haiku pin resolves to 4096 through the PRODUCTION construction
            path -- `_build_distiller_provider`, which passes no arguments at
            all -- and is strictly below the 16000 default.
  offline 2. The env override moves that resolved cap in BOTH directions.
  offline 3. Post-split, every Segment satisfies the token bound at the
            effective budget, every event satisfies the char bound, and the
            split is lossless. Asserted on inputs sized exactly AT the seam,
            one char OVER it, and several multiples past it.
  offline 4. The classifier separates a truncated findings object from a
            repetition loop -- measured as a GAP, not against the threshold.
  live    5. The pin BINDS at the endpoint: a prompt that wants far more than
            4096 output tokens returns exactly at the cap, not beyond it.
  live    6. The override binds live too.
  live    7. Real Haiku token counts stay under the estimator (i.e. the 3.5
            chars/token estimate over-counts -- the safe direction; if it
            under-counted, the whole bound would be decorative).
  live    8. A response starved to a small cap is classified `over-budget`,
            NOT `degenerate-repetition`.

RUN
    uv run python scripts/w6_live_stress.py             # live if a key exists
    uv run python scripts/w6_live_stress.py --offline   # never touches network

The key is read the same way `serve_local.py` reads it (env, then the
`anthropic` block of secrets.jsonc) and is never printed.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import re
import sys
import zlib
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

REPO = Path(__file__).resolve().parent.parent

# The hard ceiling on a live run. Not advisory: the counter raises rather than
# letting a retry loop quietly spend past it.
MAX_API_CALLS = 30

MODEL = "claude-haiku-4-5-20251001"
EXPECTED_PIN = 4096


# --------------------------------------------------------------------------
# credentials -- same source order as scripts/serve_local.py::_anthropic_key
# --------------------------------------------------------------------------
def anthropic_key() -> str | None:
    """The key, or None. Never returned to anything that prints it."""
    if key := os.environ.get("ANTHROPIC_API_KEY"):
        return key
    secrets = REPO / "secrets.jsonc"
    if not secrets.exists():
        return None
    try:
        data = json.loads(
            re.sub(r"^\s*//.*$", "", secrets.read_text(), flags=re.MULTILINE)
        )
    except json.JSONDecodeError:
        return None
    key = (data.get("anthropic") or {}).get("api_key")
    # An empty string is the shape secrets.example.jsonc ships and the shape a
    # checkout with no Anthropic credential carries. Treat it as absent rather
    # than handing "" to the SDK, which fails later and less legibly.
    return str(key) if key else None


# --------------------------------------------------------------------------
# the call budget
# --------------------------------------------------------------------------
class BudgetExceeded(RuntimeError):
    pass


@dataclass
class CallBudget:
    cap: int = MAX_API_CALLS
    used: int = 0

    def charge(self, note: str) -> None:
        if self.used >= self.cap:
            raise BudgetExceeded(
                f"call budget {self.cap} exhausted; refused call {self.used + 1} ({note})"
            )
        self.used += 1


BUDGET = CallBudget()


class CountedProvider:
    """Forwards to the real provider and charges the budget per call.

    `max_tokens` is forwarded as a real attribute, not swallowed: the Distiller
    reads `getattr(self.provider, "max_tokens", None)` to decide `over-budget`
    vs `degenerate-repetition`, so a proxy that hid it would silently turn the
    distinction under test into "not knowable from here" and the run would pass
    while proving nothing.
    """

    def __init__(self, inner: Any, note: str = "") -> None:
        self._inner = inner
        self._note = note
        self.calls = 0

    @property
    def provider_id(self) -> str:
        return self._inner.provider_id

    @property
    def max_tokens(self) -> int:
        return self._inner.max_tokens

    @property
    def capabilities(self) -> Any:
        return self._inner.capabilities

    async def complete(self, messages, response_schema=None):
        BUDGET.charge(self._note or "complete")
        self.calls += 1
        return await self._inner.complete(messages, response_schema)


# --------------------------------------------------------------------------
# assertions
# --------------------------------------------------------------------------
@dataclass
class Check:
    name: str
    ok: bool
    detail: str


CHECKS: list[Check] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    CHECKS.append(Check(name, bool(ok), detail))
    mark = "PASS" if ok else "FAIL"
    print(f"  [{mark}] {name}" + (f" -- {detail}" if detail else ""), flush=True)
    if not ok:
        raise AssertionError(f"{name}: {detail}")


# --------------------------------------------------------------------------
# input material
# --------------------------------------------------------------------------
_PARAGRAPHS = [
    "The GenieX server accepted the model but reported prompt_tokens=1 on every "
    "call, which is the signature of the VLM code path discarding a text prompt. "
    "We caught it because the usage block disagreed with the prompt we sent.",
    "Raising provider.max_tokens from 500 to 900 made the truncation worse, not "
    "better: the segment budget was derived from the 500-token reserve, so a "
    "full segment plus a 900-token ask overran the 4096 context outright.",
    "We tried pinning the segment budget in config rather than deriving it, and "
    "abandoned that: the pin happened to equal the derived value, so the "
    "validator accepted it silently and would have kept accepting it after the "
    "prompt pack's overhead changed.",
    "The HTTP sink defaulted to a 30 second timeout while the worker config "
    "declared 120; the worker passed its value explicitly on the run path, so "
    "the bug only appeared on the path that did not.",
    "Retrying a temperature-zero call with byte-identical messages reproduced "
    "the first failure exactly. The retry only became a retry once the second "
    "attempt appended a nudge that changed the prompt.",
    "A 400 from the endpoint carried the findings the model had already "
    "generated before the cut, so raising for status threw away recoverable "
    "work. Salvaging the body turned a hard failure into a partial result.",
    "finish_reason came back as 'stop' on a response that was cut off at "
    "exactly max_tokens, so any truncation check keyed on that field alone "
    "reported nothing. The usage numbers were the only honest signal.",
    "Compaction keeps fifteen head and fifteen tail lines of a tool result, but "
    "the oversized-event pre-split runs first, so a giant tool result is cut "
    "into parts before compaction ever sees it.",
    "The open question is whether the cloud arms should be clamped at all. They "
    "are protected by branch order today rather than by their own capability "
    "records, which is an invariant nobody wrote down.",
    "We decided the Haiku output cap belongs in the provider rather than in "
    "config, because synthesis reads provider.max_tokens and never reads a "
    "capability record; a config-only cap would have been inert on the very arm "
    "it was written for.",
]


def seed_for(label: str) -> int:
    """A per-label seed that is the same in every process, on every machine.

    `hash(label) % 97` was not, and that is what made section 4 of
    `docs/overnight/w6-live-stress.md` unreproducible. `str.__hash__` is salted
    per interpreter (PYTHONHASHSEED), so two runs of `--offline` generated
    DIFFERENT prose from the same label, and therefore different segment
    boundaries and different headroom -- while the doc said "reproduce with"
    and printed one run's numbers as measurements. Seven runs put
    `one-over-seam` worst headroom at 43, 45, 43, 46, 51, 51 and 43; the
    committed table said 36.

    `zlib.crc32` is a checksum rather than a hash-table hash: no salt, no
    per-process seed, specified output. The label is the only input, so the
    same label is the same prose forever. The `% 97` is unchanged and does
    nothing but pick a starting paragraph.

    Not `random.Random(label)` even though it is also deterministic: `prose`
    takes an integer offset into _PARAGRAPHS, and routing a string through a
    PRNG to get one would be a longer way to say crc32.
    """
    return zlib.crc32(label.encode("utf-8")) % 97


def prose(target_chars: int, seed: int = 0) -> str:
    """Realistic engineering-transcript prose of exactly `target_chars`.

    Deliberately NOT `"x" * n`: the estimator-vs-tokenizer comparison is only
    meaningful on text whose token density resembles a real transcript's, and a
    run of identical characters tokenizes nothing like English. Newline-
    separated so the splitter's newline preference has something to find --
    `"x"*n` would exercise only the hard-cut path and hide the other one.
    """
    out: list[str] = []
    total = 0
    i = seed
    while total < target_chars:
        para = f"[{i:04d}] {_PARAGRAPHS[i % len(_PARAGRAPHS)]}"
        out.append(para)
        total += len(para) + 1
        i += 1
    return "\n".join(out)[:target_chars]


def event(content: str, *, role: str = "assistant", kind: str = "text") -> Any:
    from synapse_contracts import AgentEvent

    return AgentEvent(
        role=role,
        kind=kind,
        content=content,
        ts=datetime.now(timezone.utc),
        agent_session_id="w6-live-stress",
    )


@dataclass
class SegmentRecord:
    label: str
    seg_id: str
    events: int
    chars: int
    estimated: int
    headroom: int
    widest_event: int
    actual_input: int | None = None
    output: int | None = None
    latency_ms: int | None = None
    findings: int | None = None
    attempts: int | None = None
    reasons: list[str] = field(default_factory=list)


RECORDS: list[SegmentRecord] = []


# --------------------------------------------------------------------------
async def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--offline",
        action="store_true",
        help="run only the phases that need no network (the default when no key is found)",
    )
    args = parser.parse_args(argv)

    key = None if args.offline else anthropic_key()
    live = key is not None
    if live:
        os.environ["ANTHROPIC_API_KEY"] = key
    os.environ["SYNAPSE_DISTILLER"] = "anthropic"
    os.environ["SYNAPSE_ANTHROPIC_MODEL"] = MODEL
    os.environ.pop("SYNAPSE_ANTHROPIC_MAX_TOKENS", None)

    from synapse_contracts import LocalBinding
    from synapse_distiller.config import load_config
    from synapse_distiller.distiller import (
        DROP_DEGENERATE,
        DROP_OVER_BUDGET,
        classify_drop,
        distinct_shingle_ratio,
        is_degenerate,
    )
    from synapse_providers import AnthropicProvider
    from synapse_worker.cli import _build_distiller_provider, build_distiller
    from synapse_worker.segmenter import Segmenter, estimate_tokens

    config = load_config()
    budget_tokens = config.segment_budget
    max_chars = int(budget_tokens * 3.5)

    binding = LocalBinding(
        agent_session_id="w6-live-stress",
        shared_id="w6-live",
        contributor="w6-stress",
        agent="claude-code",
    )

    print(f"\nmode: {'LIVE' if live else 'OFFLINE (no Anthropic credential found)'}")
    print(f"config: segment_budget={budget_tokens} max_chars={max_chars} "
          f"pack={config.prompt_pack_name} kinds={config.distil_kinds}\n", flush=True)

    # ---- phase 1: the pin, through the production construction path --------
    print("=== phase 1: the Haiku pin (0 API calls) ===", flush=True)
    provider = _build_distiller_provider(config)
    check("the anthropic arm builds an AnthropicProvider",
          isinstance(provider, AnthropicProvider))
    check("the Haiku pin resolves to 4096 with NO constructor arguments",
          provider.max_tokens == EXPECTED_PIN, f"max_tokens={provider.max_tokens}")
    check("the pin is strictly below the 16000 default (a pin edited to equal "
          "the default would pass every other check here)",
          provider.max_tokens < 16000, f"{provider.max_tokens} < 16000")

    os.environ["SYNAPSE_ANTHROPIC_MAX_TOKENS"] = "512"
    tightened = _build_distiller_provider(config)
    check("the env override tightens the resolved cap",
          tightened.max_tokens == 512, f"max_tokens={tightened.max_tokens}")
    os.environ["SYNAPSE_ANTHROPIC_MAX_TOKENS"] = "8000"
    loosened = _build_distiller_provider(config)
    check("and loosens it above the pin (a pin nobody can loosen is a ceiling)",
          loosened.max_tokens == 8000, f"max_tokens={loosened.max_tokens}")
    os.environ.pop("SYNAPSE_ANTHROPIC_MAX_TOKENS", None)

    # ---- phase 2: the segmenting/chunking seam -----------------------------
    print("\n=== phase 2: the segmenting seam at, over, and past the bound ===",
          flush=True)
    distiller = build_distiller(config, binding)
    distiller.provider = CountedProvider(distiller.provider, "distil")
    check("the distiller the worker builds carries the pinned cap",
          distiller.provider.max_tokens == EXPECTED_PIN,
          f"max_tokens={distiller.provider.max_tokens}")

    blobs: list[tuple[str, int | None]] = [
        ("small", 1_200),
        ("just-under-seam", max_chars - 1),
        ("at-seam", max_chars),
        ("one-over-seam", max_chars + 1),
        ("3x-budget", max_chars * 3),
        ("12x-budget", max_chars * 12),
        ("mixed-turn", None),
    ]

    for label, size in blobs:
        seg = Segmenter(budget_tokens=budget_tokens, agent_session_id="w6-live-stress")
        if label == "mixed-turn":
            seg.add([
                event("Why does the retry reproduce the first failure exactly?",
                      role="user"),
                event(prose(900, seed=21)),
                event(prose(max_chars + 4_000, seed=23)),
                event(prose(700, seed=29)),
            ])
        else:
            seg.add([event(prose(size, seed=seed_for(label)))])
        original = "".join(e.content for e in seg._pending)

        segments = seg.drain(flush_incomplete=True)

        rejoined = "".join(ev.content for s in segments for ev in s.events)
        check(f"[{label}] the split is lossless",
              rejoined == original,
              f"{len(rejoined)} chars back from {len(original)}")

        worst_headroom = budget_tokens
        for s in segments:
            est = estimate_tokens(s.events)
            widest = max(len(ev.content) for ev in s.events)
            worst_headroom = min(worst_headroom, budget_tokens - est)
            check(f"[{label}/{s.id}] token bound holds at the effective budget",
                  est <= budget_tokens,
                  f"estimate_tokens={est} <= {budget_tokens}")
            check(f"[{label}/{s.id}] no event exceeds the char bound",
                  widest <= max_chars, f"widest={widest} <= {max_chars}")
            RECORDS.append(SegmentRecord(
                label=label, seg_id=s.id, events=len(s.events),
                chars=sum(len(ev.content) for ev in s.events),
                estimated=est, headroom=budget_tokens - est, widest_event=widest,
            ))

        events_out = sum(len(s.events) for s in segments)
        if label == "at-seam":
            check("an event of exactly max_chars is NOT split",
                  len(segments) == 1 and events_out == 1,
                  f"segments={len(segments)} events={events_out}")
        if label == "one-over-seam":
            check("one char over max_chars IS split, into exactly two",
                  events_out == 2, f"events={events_out}")
        print(f"  {label}: {len(original)} chars -> {len(segments)} segment(s), "
              f"{events_out} event(s), worst headroom {worst_headroom} tok",
              flush=True)

    # ---- phase 3: the classifier separates the two failures ----------------
    print("\n=== phase 3: truncation vs repetition, as a GAP (0 API calls) ===",
          flush=True)
    # Both shapes below are synthetic HERE; phase 6 re-derives them from real
    # Haiku output when a key is present. The measure is asserted as a gap
    # between the two, never against the threshold -- a test that pins the
    # threshold passes even if the threshold is moved to where it separates
    # nothing.
    truncated_shape = '{"findings": [{"type": "learning", "text": "' + prose(1_500, 5)
    loop_shape = "retrying the segment now " * 200
    trunc_ratio = distinct_shingle_ratio(truncated_shape)
    loop_ratio = distinct_shingle_ratio(loop_shape)
    check("a cut-off findings object does not measure as a loop",
          not is_degenerate(truncated_shape), f"ratio={trunc_ratio:.3f}")
    check("a repetition loop does", is_degenerate(loop_shape),
          f"ratio={loop_ratio:.3f}")
    check("the two are separated by a wide gap, not by sitting either side of "
          "the threshold", trunc_ratio - loop_ratio > 0.5,
          f"{trunc_ratio:.3f} vs {loop_ratio:.3f}")
    check("a response cut at the cap is over-budget whatever its shape",
          classify_drop(loop_shape, 4096, 4096) == DROP_OVER_BUDGET)
    check("with no cap knowable, the same loop text is degenerate",
          classify_drop(loop_shape, 4096, None) == DROP_DEGENERATE)

    density_samples: list[tuple[int, int]] = []
    pin_binds_tokens = override_tokens = None
    live_truncated_ratio = live_loop_ratio = None

    if not live:
        print("\n=== live phases SKIPPED: no Anthropic credential ===", flush=True)
    else:
        # ---- phase 4: the pin BINDS at the endpoint ------------------------
        print("\n=== phase 4: does the 4K pin actually bind? ===", flush=True)
        counted = CountedProvider(provider, "pin-binds")
        overflow = [{
            "role": "user",
            "content": ("Count from 1 to 4000. Output one integer per line and "
                        "nothing else -- no preamble, no commentary. Start now."),
        }]
        result = await counted.complete(overflow)
        check("live output never exceeds the pinned cap",
              result.usage.output_tokens <= EXPECTED_PIN,
              f"output_tokens={result.usage.output_tokens}")
        check("the cap is what stopped it -- the bound BINDS, it is not merely "
              "unreached", result.usage.output_tokens == EXPECTED_PIN,
              f"output_tokens={result.usage.output_tokens} == {EXPECTED_PIN}")
        pin_binds_tokens = result.usage.output_tokens

        os.environ["SYNAPSE_ANTHROPIC_MAX_TOKENS"] = "512"
        counted_t = CountedProvider(_build_distiller_provider(config), "override")
        result = await counted_t.complete(overflow)
        check("the tightened cap binds live too",
              result.usage.output_tokens == 512,
              f"output_tokens={result.usage.output_tokens}")
        override_tokens = result.usage.output_tokens
        os.environ.pop("SYNAPSE_ANTHROPIC_MAX_TOKENS", None)

        # ---- phase 5: real segments through the real Distiller -------------
        print("\n=== phase 5: real segments, live ===", flush=True)
        for rec in RECORDS:
            if rec.label not in ("small", "at-seam", "one-over-seam", "3x-budget"):
                continue
            seg = Segmenter(budget_tokens=budget_tokens,
                            agent_session_id="w6-live-stress")
            seg.add([event(prose(rec.chars, seed=seed_for(rec.label)))])
            target = seg.drain(flush_incomplete=True)[0]
            findings, stats = await distiller.distil(target)
            rec.actual_input = stats.input_tokens
            rec.output = stats.output_tokens
            rec.latency_ms = stats.latency_ms
            rec.findings = len(findings)
            rec.attempts = stats.attempts
            rec.reasons = list(stats.drop_reasons)
            check(f"[{rec.label}] live output stayed under the pinned cap",
                  stats.output_tokens <= EXPECTED_PIN, f"{stats.output_tokens}")
            check(f"[{rec.label}] the prompt was not dropped",
                  stats.input_tokens > 1, f"input_tokens={stats.input_tokens}")
            if stats.attempts == 1:
                density_samples.append((rec.chars, stats.input_tokens))
            print(f"  {rec.label}: est {rec.estimated} tok, real in "
                  f"{stats.input_tokens} tok, out {stats.output_tokens} tok, "
                  f"{stats.latency_ms} ms, {len(findings)} findings", flush=True)

        # ---- phase 6: is the 3.5 chars/token estimate SAFE for Haiku? -------
        if len(density_samples) >= 2:
            ordered = sorted(density_samples)
            (c0, t0), (c1, t1) = ordered[0], ordered[-1]
            slope = (c1 - c0) / max(t1 - t0, 1)
            check("measured chars/token on real transcript prose is above the "
                  "3.5 estimator (the estimator over-counts -- the safe "
                  "direction; under-counting makes the bound decorative)",
                  slope >= 3.5,
                  f"measured {slope:.2f} chars/token over {c0}->{c1} chars")

        # ---- phase 7: truncation classified live ---------------------------
        print("\n=== phase 7: a live-truncated response, classified ===",
              flush=True)
        os.environ["SYNAPSE_ANTHROPIC_MAX_TOKENS"] = "200"
        starved = build_distiller(config, binding)
        starved.provider = CountedProvider(starved.provider, "starved")
        seg = Segmenter(budget_tokens=budget_tokens,
                        agent_session_id="w6-live-stress")
        seg.add([event(prose(6_000, seed=31))])
        findings, stats = await starved.distil(seg.drain(flush_incomplete=True)[0])
        raw = stats.raw_outputs[-1] if stats.raw_outputs else ""
        check("a live response cut at the cap is classified over-budget",
              bool(stats.drop_reasons)
              and all(r == DROP_OVER_BUDGET for r in stats.drop_reasons),
              f"reasons={stats.drop_reasons}")
        check("and is NOT called a repetition loop",
              DROP_DEGENERATE not in stats.drop_reasons,
              f"reasons={stats.drop_reasons}")
        live_truncated_ratio = distinct_shingle_ratio(raw)
        check("live truncated text measures as non-repetitive",
              not is_degenerate(raw), f"ratio={live_truncated_ratio:.3f}")
        os.environ.pop("SYNAPSE_ANTHROPIC_MAX_TOKENS", None)

        loop_provider = CountedProvider(AnthropicProvider(max_tokens=600), "loop")
        loop_result = await loop_provider.complete([{
            "role": "user",
            "content": ("Output the exact line 'retrying the segment now' over "
                        "and over, one per line, until you run out of room. No "
                        "other words."),
        }])
        loop_text = loop_result.data if isinstance(loop_result.data, str) else ""
        live_loop_ratio = distinct_shingle_ratio(loop_text)
        check("a live repetition loop measures as degenerate",
              is_degenerate(loop_text), f"ratio={live_loop_ratio:.3f}")
        check("the two LIVE shapes are separated by the measure",
              live_truncated_ratio - live_loop_ratio > 0.5,
              f"{live_truncated_ratio:.3f} vs {live_loop_ratio:.3f}")

    # ---- report ------------------------------------------------------------
    print(f"\n=== summary ===")
    print(f"  checks: {sum(1 for c in CHECKS if c.ok)}/{len(CHECKS)} passed")
    print(f"  API calls: {BUDGET.used}/{BUDGET.cap}", flush=True)

    report = {
        "mode": "live" if live else "offline",
        "model": MODEL,
        "api_calls": BUDGET.used,
        "cap": BUDGET.cap,
        "checks_passed": sum(1 for c in CHECKS if c.ok),
        "checks_total": len(CHECKS),
        "segment_budget": budget_tokens,
        "max_chars": max_chars,
        "pin": provider.max_tokens,
        "pin_binds_tokens": pin_binds_tokens,
        "override_tokens": override_tokens,
        "truncated_ratio": round(trunc_ratio, 3),
        "loop_ratio": round(loop_ratio, 3),
        "live_truncated_ratio": live_truncated_ratio,
        "live_loop_ratio": live_loop_ratio,
        "segments": [vars(r) for r in RECORDS],
    }
    out = REPO / ".synapse" / "w6-live-stress.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(report, indent=2))
    print(f"  numbers written to {out}", flush=True)
    return 0 if all(c.ok for c in CHECKS) else 1


if __name__ == "__main__":
    try:
        raise SystemExit(asyncio.run(main(sys.argv[1:])))
    except AssertionError as exc:
        print(f"\nFAILED: {exc}", file=sys.stderr)
        print(f"API calls used: {BUDGET.used}/{BUDGET.cap}", file=sys.stderr)
        raise SystemExit(1) from None
    except BudgetExceeded as exc:
        print(f"\nBUDGET: {exc}", file=sys.stderr)
        raise SystemExit(1) from None
