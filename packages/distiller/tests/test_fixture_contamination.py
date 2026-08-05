# packages/distiller/tests/test_fixture_contamination.py
"""No fixture may overlap a prompt pack's few-shots — the v1-baseline lesson."""
import re
from pathlib import Path

import pytest

from synapse_distiller.fixtures import available_fixtures, fixtures_root, load_segment

KNOWN_CONTAMINATED = {("seg-004", "v1-baseline.toml")}  # frozen evidence; declared in the pack itself


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
        if (fixture_id, pack_path.name) in KNOWN_CONTAMINATED:
            continue
        overlap = fixture_grams & _sixgrams(pack_path.read_text(encoding="utf-8"))
        assert not overlap, (
            f"{fixture_id} shares wording with {pack_path.name}: {sorted(overlap)[:3]} — "
            "a few-shot that duplicates a fixture measures pattern-matching, not generalization"
        )
