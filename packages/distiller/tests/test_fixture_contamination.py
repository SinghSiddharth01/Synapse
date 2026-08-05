# packages/distiller/tests/test_fixture_contamination.py
"""No fixture may overlap a prompt pack's few-shots — the v1-baseline lesson."""
import re
from pathlib import Path

import pytest

from synapse_distiller.fixtures import available_fixtures, fixtures_root, load_segment
from synapse_distiller.promptpack import load_pack_by_name


def _packs_dir() -> Path:
    for parent in Path(__file__).resolve().parents:
        candidate = parent / "config" / "prompts"
        if candidate.is_dir():
            return candidate
    raise FileNotFoundError("config/prompts not found above test file")


def _sixgrams(text: str) -> set[tuple[str, ...]]:
    words = re.findall(r"\w+", text.lower())
    return {tuple(words[i:i+6]) for i in range(len(words) - 5)}


@pytest.mark.parametrize("fixture_id", available_fixtures())
def test_fixture_shares_no_sixgram_with_any_pack(fixture_id):
    fixture_text = " ".join(e.content for e in load_segment(fixture_id).events)
    fixture_grams = _sixgrams(fixture_text)
    for pack_path in sorted(_packs_dir().glob("*.toml")):
        pack = load_pack_by_name(pack_path.stem)
        # The exemption is read from the pack's own declaration
        # (`contaminated_fixtures` in the TOML — frozen evidence for the
        # v1-baseline incident, see config/prompts/v1-baseline.toml) rather
        # than a literal duplicated in this test file. scripts/run_npu_eval.py
        # reads the exact same declaration to decide whether to VOID a score,
        # so deleting or emptying it disarms this test and the harness's VOID
        # banner together instead of letting them drift apart silently.
        if pack.is_contaminated_for(fixture_id):
            continue
        overlap = fixture_grams & _sixgrams(pack_path.read_text(encoding="utf-8"))
        assert not overlap, (
            f"{fixture_id} shares wording with {pack_path.name}: {sorted(overlap)[:3]} — "
            "a few-shot that duplicates a fixture measures pattern-matching, not generalization"
        )


def test_v1_baseline_declares_its_known_contamination():
    """Frozen evidence: v1-baseline's few-shots literally duplicate seg-004's
    tool_result wording (the incident that motivated this whole test file).
    The exemption above is only safe to grant because the pack declares it
    itself — this pins the declaration so it cannot silently disappear (which
    would make the exemption above a no-op, which is fine) without anyone
    reconsidering it, and pins that the overlap it excuses is still real
    rather than a stale blanket permission."""
    pack = load_pack_by_name("v1-baseline")
    assert pack.is_contaminated_for("seg-004"), (
        "v1-baseline must declare contaminated_fixtures = [\"seg-004\"] — "
        "this is the single source of truth for both this test's exemption "
        "and scripts/run_npu_eval.py's VOID banner"
    )
    fixture_text = " ".join(e.content for e in load_segment("seg-004").events)
    assert pack.source_path is not None
    overlap = _sixgrams(fixture_text) & _sixgrams(pack.source_path.read_text(encoding="utf-8"))
    assert overlap, (
        "v1-baseline no longer shares a six-gram with seg-004 — the "
        "contaminated_fixtures declaration is now stale evidence and should "
        "be removed from the pack rather than kept as a blanket exemption"
    )
