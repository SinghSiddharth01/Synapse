# packages/distiller/tests/test_fixture_contamination.py
"""No fixture may overlap a prompt pack's few-shots — the v1-baseline lesson."""
import re
from pathlib import Path

import pytest

from synapse_distiller.fixtures import available_fixtures, load_segment
from synapse_distiller.promptpack import available_packs, load_pack_by_name


def _packs_dir() -> Path:
    for parent in Path(__file__).resolve().parents:
        candidate = parent / "config" / "prompts"
        if candidate.is_dir():
            return candidate
    raise FileNotFoundError("config/prompts not found above test file")


def _sixgrams(text: str) -> set[tuple[str, ...]]:
    words = re.findall(r"\w+", text.lower())
    return {tuple(words[i:i+6]) for i in range(len(words) - 5)}


# Frozen evidence for the v1-baseline incident (see
# config/prompts/v1-baseline.toml), and the ONLY exemption this CI guard will
# ever grant. This literal is deliberately duplicated rather than read from
# the pack's own `contaminated_fixtures` declaration: letting a pack declare
# its own exemption from the guard that checks it means any pack can silently
# widen the exemption surface just by editing its TOML (add
# `contaminated_fixtures = ["seg-005a"]` to any pack and this test stops
# checking that pair, with no other test failing to flag it). Every NEW
# fixture must pass clean against every pack, no exceptions beyond this one.
#
# `contaminated_fixtures` still exists on PromptPack and is still read —
# by scripts/run_npu_eval.py, to decide whether to VOID a score in the
# harness's own report. That is a display decision the harness is allowed to
# make from pack-declared data; it is not the same thing as this test's pass/
# fail exemption surface, which must not be pack-controllable.
KNOWN_CONTAMINATED = {("seg-004", "v1-baseline.toml")}


@pytest.mark.parametrize("fixture_id", available_fixtures())
def test_fixture_shares_no_sixgram_with_any_pack(fixture_id):
    fixture_text = " ".join(e.content for e in load_segment(fixture_id).events)
    fixture_grams = _sixgrams(fixture_text)
    for pack_path in sorted(_packs_dir().glob("*.toml")):
        if (fixture_id, pack_path.name) in KNOWN_CONTAMINATED:
            continue
        overlap = fixture_grams & _sixgrams(pack_path.read_text(encoding="utf-8"))
        assert not overlap, (
            f"{fixture_id} shares wording with {pack_path.name}: {sorted(overlap)[:3]} — "
            "a few-shot that duplicates a fixture measures pattern-matching, not generalization"
        )


def test_v1_baseline_declares_its_known_contamination():
    """Frozen evidence: v1-baseline's few-shots literally duplicate seg-004's
    tool_result wording (the incident that motivated this whole test file).

    This does NOT feed the CI guard above — KNOWN_CONTAMINATED is the sole
    source of truth for that, precisely so the guard's exemption surface is
    not pack-controllable. What this pins is the *harness's* VOID display
    (scripts/run_npu_eval.py reads pack.is_contaminated_for(...) to decide
    whether to print the VOID banner): that the declaration in the pack's own
    TOML has not silently disappeared, and that the overlap it excuses is
    still real rather than a stale blanket permission."""
    pack = load_pack_by_name("v1-baseline")
    assert pack.is_contaminated_for("seg-004"), (
        "v1-baseline must declare contaminated_fixtures = [\"seg-004\"] — "
        "this is scripts/run_npu_eval.py's sole input for its VOID banner "
        "(the CI guard above uses its own frozen KNOWN_CONTAMINATED literal, "
        "not this declaration)"
    )
    fixture_text = " ".join(e.content for e in load_segment("seg-004").events)
    assert pack.source_path is not None
    overlap = _sixgrams(fixture_text) & _sixgrams(pack.source_path.read_text(encoding="utf-8"))
    assert overlap, (
        "v1-baseline no longer shares a six-gram with seg-004 — the "
        "contaminated_fixtures declaration is now stale evidence and should "
        "be removed from the pack rather than kept as a blanket exemption"
    )


def test_only_v1_baseline_declares_any_contamination():
    """The CI guard above is driven entirely by the frozen KNOWN_CONTAMINATED
    literal, so a pack cannot widen that guard's exemption surface by editing
    its own TOML — that risk is closed structurally, not by a test. What a
    pack CAN still do is mislead the harness's VOID display (which does read
    pack.is_contaminated_for), by declaring `contaminated_fixtures` for a
    fixture it does not actually share wording with, making the harness void
    a score that was not actually compromised, or vice versa. This test
    guards that surface: only v1-baseline (frozen evidence of a real
    incident) declares any contamination at all."""
    for pack_name in available_packs():
        pack = load_pack_by_name(pack_name)
        if pack_name == "v1-baseline":
            assert pack.contaminated_fixtures == ("seg-004",), (
                "v1-baseline's declared contamination changed — update this "
                "test deliberately if that is intentional"
            )
        else:
            assert pack.contaminated_fixtures == (), (
                f"{pack_name} declares contaminated_fixtures = "
                f"{list(pack.contaminated_fixtures)} — only v1-baseline/seg-004 "
                "is frozen evidence of a real incident. This does not affect "
                "the CI guard above (KNOWN_CONTAMINATED is pack-independent), "
                "but it would mislead scripts/run_npu_eval.py's VOID display; "
                "if this is deliberate, update this test in the same commit"
            )
