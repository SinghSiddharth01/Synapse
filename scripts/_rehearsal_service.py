"""The rehearsal's deterministic service — real app, scripted verdicts.

Boots the REAL `build_app` over a `FakeProvider` whose script list matches
the §B beat sequence exactly, so `rehearse_demo.py`'s default mode exercises
every beat offline with a guaranteed seg-005 merge on push 3. The script
list is generous with no-op verdicts at the tail so an extra call (a retry,
a resync round) exhausts gracefully into synthesis's designed
fail-open rather than crashing the rehearsal.

Not a test double of the service — the service is real; only the model is
scripted. `--live` in rehearse_demo.py swaps this file out for the real 8B.
"""

from __future__ import annotations

import os
import sys

import uvicorn

sys.path.insert(0, "packages/service/src")
sys.path.insert(0, "packages/providers/src")
sys.path.insert(0, "packages/contracts/src")

from synapse_providers import FakeProvider  # noqa: E402
from synapse_service.api import build_app  # noqa: E402

NOOP = {"working_memory": "wm", "merges": [], "trivial_ids": [], "conflicts": []}
RANK_FIRST = {"ranked": [0, 1, 2]}

SCRIPTS = [
    # push 1 verdict: nothing merges yet
    {"working_memory": "Ten findings in: the fec decode session is warming up.",
     "merges": [], "trivial_ids": [], "conflicts": []},
    # push 2 verdict: lint chatter marked trivial
    {"working_memory": "Two contributors in. The decode-failure thread and a lint pass.",
     "merges": [], "trivial_ids": ["f-006-01"], "conflicts": []},
    # three pre-merge queries
    RANK_FIRST, RANK_FIRST, RANK_FIRST,
    # push 3 verdict: THE merge — seg-005a + seg-005b, 24 findings apart
    {"working_memory": "The decode failure is a ~40 ms timing window that only "
                       "manifests under load. Buffer tuning is a dead end.",
     "merges": [{"source_ids": ["f-005a-01", "f-005b-01"],
                 "text": "The decode failure is a ~40 ms timing window between the two "
                         "DMA writes, and it only manifests under load.",
                 "type": "learning"}],
     "trivial_ids": [], "conflicts": []},
    # three post-merge queries
    RANK_FIRST, RANK_FIRST, RANK_FIRST,
    # tail: resync replay verdict + recovery query + slack for retries
    NOOP, RANK_FIRST, NOOP, RANK_FIRST, NOOP, RANK_FIRST,
]

# The post-restart boot: the relay log holds ONLY what went through the
# orchestrator (the one contributed finding — beats 1-6 pushed straight at
# the service by the demo's own design, so those 26 die with the restart:
# the known, documented loss). Call order on this boot is push-merge, then
# cmd_resync's /synthesize round, THEN the recovery query — three script
# slots, in that order.
RECOVERY_SCRIPTS = [
    NOOP,                     # merge for the re-pushed finding(s)
    NOOP,                     # cmd_resync's /synthesize self-heal round
    {"ranked": [0, 1, 2]},    # the recovery query — recovery finding is index 0
    NOOP, {"ranked": [0, 1]}, NOOP, {"ranked": [0, 1]},
]

if __name__ == "__main__":
    scripts = RECOVERY_SCRIPTS if os.environ.get("REHEARSAL_PHASE") == "recovery" else SCRIPTS
    uvicorn.run(build_app(FakeProvider(scripts=scripts)),
                host="127.0.0.1", port=8899, log_level="warning")
