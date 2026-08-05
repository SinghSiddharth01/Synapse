"""Every committed fixture, against the committed expectation map.

This is the recall measurement Plan A.5b requires: the map records intent
(including two ACCEPTED FALSE POSITIVES), this test pins behaviour to it, and
any future triage improvement must flip the map entry in the same commit.
"""
import json

import pytest

from synapse_distiller.fixtures import available_fixtures, fixtures_root, load_segment
from synapse_worker.compaction import compact
from synapse_worker.triage import triage

EXPECTATIONS = json.loads((fixtures_root() / "triage.json").read_text(encoding="utf-8"))


@pytest.mark.parametrize("fixture_id", available_fixtures())
def test_triage_matches_the_expectation_map(fixture_id):
    decision = triage(load_segment(fixture_id))
    expected_keep = EXPECTATIONS[fixture_id]["expected"] == "keep"
    assert decision.keep == expected_keep, (
        f"{fixture_id}: triage said {'keep' if decision.keep else 'skip'} "
        f"({decision.reason}), map says {EXPECTATIONS[fixture_id]['expected']} — "
        f"{EXPECTATIONS[fixture_id]['note']}"
    )


def test_no_fixture_is_skipped_without_a_named_reason():
    for fixture_id in available_fixtures():
        decision = triage(load_segment(fixture_id))
        if not decision.keep:
            assert decision.reason in {"lint-clean", "readonly-run"}


@pytest.mark.parametrize("fixture_id", available_fixtures())
def test_triage_still_matches_the_expectation_map_after_compaction(fixture_id):
    """`WorkerLoop.tick` runs triage BEFORE compaction now (compaction.py's
    module docstring, "WHY THIS RUNS AFTER TRIAGE" — an adjudicated fixer
    ruling that reversed this module's original ordering after review found
    compaction's truncation could flip a real triage `keep` to `skip`), so
    `triage(compact(segment))` is no longer what the real pipeline ever
    computes for a fixture that triage would keep. Kept anyway as a
    defense-in-depth corpus check on `compact()` itself: nothing about the
    reorder requires compaction's OUTPUT to newly disagree with triage's
    verdict on the RAW input, and a corpus-wide check catches a future
    compaction change that quietly would, even though production code no
    longer composes the two functions this way."""
    decision = triage(compact(load_segment(fixture_id)))
    expected_keep = EXPECTATIONS[fixture_id]["expected"] == "keep"
    assert decision.keep == expected_keep, (
        f"{fixture_id}: triage(compact(segment)) said "
        f"{'keep' if decision.keep else 'skip'} ({decision.reason}), map says "
        f"{EXPECTATIONS[fixture_id]['expected']} — compaction changed the verdict"
    )
