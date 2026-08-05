"""Run the fixture corpus through the NPU distiller against live `geniex serve`.

    geniex serve                             # terminal 1
    uv run python scripts/run_npu_eval.py    # terminal 2

Everything is configurable from config/synapse.toml or the environment:

    $env:SYNAPSE_PROMPT_PACK = "v1-baseline"   # A/B a prompt
    $env:SYNAPSE_MODEL       = "..."           # A/B a model
    $env:SYNAPSE_SEGMENT_BUDGET = "2000"       # smaller segments

Order matters and is not cosmetic: the canary runs FIRST and gates the corpus.
A model that fails it is excluded rather than scored, because a dropped-prompt
model's corpus numbers are meaningless rather than merely bad.
"""

from __future__ import annotations

import asyncio
import sys
import time

from synapse_contracts import LocalBinding
from synapse_providers import NPUProvider

from synapse_distiller import Distiller, check_canary, load_config
from synapse_distiller.evaluation import score_fixture
from synapse_distiller.fixtures import available_fixtures, load_goldens, load_segment

BINDING = LocalBinding(
    agent_session_id="as-fixture-001",
    shared_id="shared-eval",
    contributor="aditya",
    agent="claude-code",
)


async def main() -> int:
    config = load_config()
    pack = config.prompt_pack

    print(config.describe())
    print(f"config           {config.source_path}\n")

    provider = NPUProvider(
        base_url=config.provider.base_url,
        model=config.model,
        max_tokens=config.provider.max_tokens,
        temperature=config.provider.temperature,
        timeout=config.provider.timeout_s,
    )

    print("── canary (gates the corpus) " + "─" * 40)
    started = time.perf_counter()
    canary = await check_canary(provider)
    print(f"  passed         {canary.passed}")
    print(f"  input_tokens   {canary.input_tokens}")
    print(f"  answer         {canary.answer.strip()[:90]!r}")
    print(f"  elapsed        {time.perf_counter() - started:.1f}s  (includes model load)")
    if not canary:
        print(f"\n  FAILED: {canary.detail}")
        print("  Corpus NOT run — a dropped-prompt model cannot be scored.")
        return 1

    print("\n── corpus " + "─" * 57)
    distiller = Distiller(
        provider, BINDING, pack, config.distil_kinds, config.render_style
    )
    scores = []
    voided: list[str] = []

    for fixture_id in available_fixtures():
        segment = load_segment(fixture_id)
        goldens = load_goldens(fixture_id)

        findings, stats = await distiller.distil(segment)
        score = score_fixture(
            fixture_id,
            segment,
            findings,
            goldens,
            attempts=stats.attempts,
            latency_ms=stats.latency_ms,
            input_tokens=stats.input_tokens,
            output_tokens=stats.output_tokens,
        )

        contaminated = pack.is_contaminated_for(fixture_id)
        if contaminated:
            voided.append(fixture_id)
        else:
            scores.append(score)

        decode = score.output_tokens / (score.latency_ms / 1000) if score.latency_ms else 0.0
        print(f"\n  {fixture_id}  (golden: {score.expected_count} findings)")
        if stats.skipped_empty:
            print(f"    SKIPPED — no events of kind {list(config.distil_kinds)}; "
                  "no NPU call made")
        if contaminated:
            print(f"    ⚠️  VOID — pack {pack.name!r} declares this fixture contaminated.")
            print("        Its few-shots duplicate this segment, so the score below")
            print("        measures pattern-matching, not generalization. Not counted.")
        print(f"    produced        {len(findings)}")
        print(f"    attempts        {score.attempts}")
        print(f"    prompt_tokens   {score.input_tokens}")
        print(f"    latency         {score.latency_ms/1000:.1f}s   ({decode:.1f} tok/s decode)")
        print(f"    types           {sorted(score.type_coverage) or '—'}")
        print(f"    verbatim max    {score.max_verbatim_overlap:.2f}  (0.0 = fully abstracted)")
        if score.leaked_identifiers:
            print(f"  LEAKED IDENTIFIERS: {', '.join(score.leaked_identifiers)}")
        if score.empty_discipline_ok is not None:
            if not pack.judges_durability:
                # This pack was never asked to filter. Reporting notes here as a
                # distiller failure would score it against a job it does not do.
                print("    empty discipline N/A — this pack compresses only; "
                      "filtering is triage's job upstream")
            else:
                verdict = "PASS" if score.empty_discipline_ok else "FAIL — invented findings"
                print(f"    empty discipline {verdict}")
        for finding in findings:
            print(f"      [{finding.type.value}] {finding.text}")

    print("\n── summary " + "─" * 56)
    print(f"  prompt pack     {pack.name}")
    print(f"  scored          {len(scores)} fixture(s)")
    if voided:
        print(f"  VOIDED          {', '.join(voided)} (contaminated by this pack)")

    all_leaks = sorted({t for s in scores for t in s.leaked_identifiers})
    if all_leaks:
        print(f"\n  identifier leaks across corpus: {', '.join(all_leaks)}")
        print("  The privacy claim does NOT hold for this run. Do not demo this table.")
    else:
        print(
            "\n  identifier leaks: none detected by this heuristic — evidence, not "
            "proof (allowlist and known gaps in evaluation.py: identifier_leaks docstring)"
        )

    if scores:
        total_out = sum(s.output_tokens for s in scores)
        total_ms = sum(s.latency_ms for s in scores)
        print(f"  prompt tokens   {sum(s.input_tokens for s in scores)}")
        print(f"  wall clock      {total_ms/1000:.1f}s")
        if total_ms:
            print(f"  decode          {total_out/(total_ms/1000):.1f} tok/s")
        print(f"  worst verbatim  {max(s.max_verbatim_overlap for s in scores):.2f}")

    print(
        f"\n  A {len(scores)}-fixture corpus is a directional signal, not a statistical claim."
    )
    if not pack.is_calibrated:
        print("  Prompt overhead is ESTIMATED — run scripts/calibrate_prompt.py.")
    print("  Power is unmeasured — do not claim efficiency from this run.")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
