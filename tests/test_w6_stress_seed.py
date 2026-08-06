"""The W6 stress harness generates the same prose in every process.

`docs/overnight/w6-live-stress.md` section 4 presents its headroom numbers as
measurements and tells the reader to reproduce them with
`uv run python scripts/w6_live_stress.py --offline`. That was not true when it
was written: the harness seeded its generated prose with `hash(label) % 97`,
and `str.__hash__` is salted per interpreter, so every run produced different
prose from the same label -- different segment boundaries, different headroom,
a table nobody could reproduce including its author. Five runs of the published
command put `one-over-seam` at 38, 38, 45, 43 and 36.

The fix is `zlib.crc32`. The test for it has to cross a process boundary,
because that is the only place the bug was visible: a same-process assertion
(`seed_for(x) == seed_for(x)`) passes on `hash()` too. So this file forces
PYTHONHASHSEED to two different values in two child interpreters and requires
all three answers to agree.

Loaded by path because `scripts/` is not a package (same as
test_serve_local_synthesizer.py).
"""

from __future__ import annotations

import importlib.util
import os
import subprocess
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent

# The labels whose seeds actually reach the doc's table.
LABELS = ["small", "just-under-seam", "at-seam", "one-over-seam",
          "3x-budget", "12x-budget", "mixed-turn"]


def _load():
    spec = importlib.util.spec_from_file_location(
        "w6_live_stress", REPO / "scripts" / "w6_live_stress.py"
    )
    module = importlib.util.module_from_spec(spec)
    # Registered before exec: the harness is `from __future__ import
    # annotations`, so @dataclass resolves its string annotations through
    # sys.modules[cls.__module__] and gets None if we skip this.
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def harness():
    return _load()


def _seeds_in_a_child_interpreter(hash_seed: str) -> list[int]:
    """`seed_for` for every label, from a fresh interpreter with a chosen salt."""
    env = {**os.environ, "PYTHONHASHSEED": hash_seed}
    program = (
        "import importlib.util,json,sys;"
        f"spec=importlib.util.spec_from_file_location('w','{REPO}/scripts/w6_live_stress.py');"
        "m=importlib.util.module_from_spec(spec);sys.modules['w']=m;"
        "spec.loader.exec_module(m);"
        f"print(json.dumps([m.seed_for(l) for l in {LABELS!r}]))"
    )
    out = subprocess.run(
        [sys.executable, "-c", program],
        capture_output=True, text=True, env=env, cwd=REPO, check=True,
    )
    import json

    return json.loads(out.stdout)


def test_the_seed_survives_a_process_boundary(harness) -> None:
    """THE test, and the only shape that can fail on the bug.

    Two child interpreters salted differently, plus this one. `hash()` gives
    three different answers here; `zlib.crc32` gives one.
    """
    here = [harness.seed_for(label) for label in LABELS]

    assert _seeds_in_a_child_interpreter("1") == here
    assert _seeds_in_a_child_interpreter("42") == here


def test_the_generated_prose_is_the_same_text_not_just_the_same_seed(
    harness,
) -> None:
    """The seed is a means. What the doc's numbers actually depend on is the
    prose, because the segmenter splits on its newlines -- so assert the text,
    which is what would have to stay fixed even if `prose` were reworked to
    take the label directly.
    """
    first = harness.prose(4_000, seed=harness.seed_for("3x-budget"))
    again = _load().prose(4_000, seed=_load().seed_for("3x-budget"))

    assert first == again
    assert "\n" in first, "newline-free prose would exercise only the hard cut"


def test_seed_for_is_not_pythons_salted_hash(harness) -> None:
    """Stated directly, against the exact expression that was here. A future
    edit back to `hash(label) % 97` passes both tests above roughly one run in
    97; this one names the thing that must not come back.
    """
    import zlib

    for label in LABELS:
        assert harness.seed_for(label) == zlib.crc32(label.encode("utf-8")) % 97
