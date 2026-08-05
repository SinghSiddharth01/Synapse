"""Measure candidate recall — does a finding's true partner reach the model?

    uv run python scripts/measure_recall.py
    uv run python scripts/measure_recall.py --distractors 2000 --top-k 20

This is the first number the service track should produce, and the only quality
signal in it that needs no model, no key and no network. If candidate selection
does not put the partner in front of the synthesizer, the merge never happens,
nothing reports it, and the knowledge is lost silently — so no amount of prompt
tuning downstream can recover it.

Three readings, because one aggregate hides what you need:

    by band   which *kind* of duplicate is being missed
    by lane   which lane actually supplied the partner
    unique    partners only that lane found — the real case for keeping it

WHAT THESE NUMBERS ARE NOT
--------------------------
The corpus is synthetic and was written by the same author as the lanes it
measures, which is the failure recorded in the 2026-08-03 traps: do not let one
person write both the prompt and the eval target. And the offline embedder is a
token hash with no paraphrase signal at all, so the two lanes that exist to
catch paraphrase are measured here with the capability removed.

Treat this as a regression guard. It tells you a change made recall worse. It is
not evidence the lanes work on real distilled findings, and no number from it
belongs in a demo.
"""

from __future__ import annotations

import argparse
import sys

from synapse_service.corpus import Band, build
from synapse_service.lanes import DEFAULT_TOP_K, Lane
from synapse_service.recall import measure


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--distractors",
        type=int,
        default=400,
        help="unrelated findings to pad the corpus with (default: 400)",
    )
    parser.add_argument(
        "--top-k",
        type=int,
        default=DEFAULT_TOP_K,
        help=f"candidates shown to the model (default: {DEFAULT_TOP_K})",
    )
    args = parser.parse_args()

    report = measure(build(distractors=args.distractors), top_k=args.top_k)
    print()
    print(report.format())

    misses = [probe for probe in report.probes if not probe.found]
    if misses:
        print("\n  missed")
        for probe in misses:
            print(f"    {probe.band.value:<12} {probe.finding_id} -> {probe.partner_id}")

    print("\n  reading these")
    if report.by_band().get(Band.GOVERNING, 0.0) < 0.9:
        print(
            "    The governing band is the case the topic lane exists for, and it"
            "\n    has no signal under the offline embedder. A low score here is"
            "\n    expected; a high one is suspicious."
        )
    if report.by_lane()[Lane.TOPIC] == 0:
        print(
            "    The topic lane supplied NO partners. Either it is not working or"
            "\n    it is not needed — lane yield on a real corpus decides which,"
            "\n    and the design is built so that deleting it changes nothing else."
        )
    print(
        "    Synthetic corpus, authored alongside the lanes. Regression guard"
        "\n    only — not evidence, and not for a demo."
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
