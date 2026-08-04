"""Measure a prompt pack's real token overhead against the model's own tokenizer.

    geniex serve
    uv run python scripts/calibrate_prompt.py [pack-name]

Why this exists: the segment budget is `usable_context - prompt_overhead -
response_reserve`. Until a pack is calibrated, `prompt_overhead` is a chars/4
estimate. The estimate is deliberately conservative — it over-counts, so the
budget comes out smaller rather than larger — but "conservative" means we are
leaving usable context on the table, and on a 4096-token QAIRT bundle that is
expensive.

Method: send the pack's fixed part (system + few-shots + suffix) with an empty
segment and read `prompt_tokens` back from the server. That is the model's own
tokenizer counting its own chat template, which no local approximation can match.

Prints the TOML block to paste into the pack. Deliberately does not write the
file — a measured number entering a tracked config should be a decision, not a
side effect.
"""

from __future__ import annotations

import asyncio
import sys
from datetime import datetime, timezone

from synapse_contracts import Segment
from synapse_providers import NPUProvider

from synapse_distiller import load_config, load_pack_by_name
from synapse_distiller.prompt import build_messages

EMPTY_SEGMENT = Segment(
    id="calibration-empty",
    agent_session_id="calibration",
    events=[],
    started_at=datetime.now(timezone.utc),
    ended_at=datetime.now(timezone.utc),
)


async def main() -> int:
    config = load_config()
    pack_name = sys.argv[1] if len(sys.argv) > 1 else config.prompt_pack_name
    pack = load_pack_by_name(pack_name)

    provider = NPUProvider(
        base_url=config.provider.base_url,
        model=config.model,
        max_tokens=1,
        temperature=config.provider.temperature,
        timeout=config.provider.timeout_s,
    )

    # max_tokens=1: we want the prompt counted, not a completion generated.
    result = await provider.complete(messages=build_messages(EMPTY_SEGMENT, pack))
    measured = result.usage.input_tokens

    estimate = pack.estimated_overhead_tokens
    drift = measured - estimate
    record = config.record
    budget_before = record.segment_budget(estimate)
    budget_after = record.segment_budget(measured)

    print(f"pack             {pack.name}")
    print(f"model            {config.model}\n")
    print(f"  estimated      {estimate} tokens")
    print(f"  MEASURED       {measured} tokens   ({drift:+d} vs estimate)")
    print(f"\n  segment budget {budget_before} -> {budget_after}  ({budget_after - budget_before:+d})")

    if measured > estimate:
        print(
            "\n  ⚠️  The estimate UNDER-counted. The budget in use was too large, "
            "\n      which truncates the end of each segment silently. Calibrate now."
        )

    print(f"\nPaste into {pack.source_path}:\n")
    print("[calibration]")
    print(f"overhead_tokens = {measured}")
    print(f'measured_against = "{config.model}"')
    print(f'measured_on = "{datetime.now(timezone.utc).date().isoformat()}"')
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
