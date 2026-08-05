"""Every committed fixture, against the committed expectation map.

This is the recall measurement Plan A.5b requires: the map records intent
(including two ACCEPTED FALSE POSITIVES), this test pins behaviour to it, and
any future triage improvement must flip the map entry in the same commit.
"""
import json

import pytest

from synapse_distiller.fixtures import available_fixtures, fixtures_root, load_segment
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
