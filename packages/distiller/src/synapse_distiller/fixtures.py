"""Loader for the committed fixture corpus.

Fixtures are the eval target and the segmentation boundary. They are data, not
code, so they live at the repo root rather than inside a package — Plan A's
segmenter must reproduce the same files.

⚠️ The current fixtures are PROVISIONAL and solo-authored. See fixtures/README.md.
"""

from __future__ import annotations

import json
from functools import lru_cache
from pathlib import Path

from synapse_contracts import Finding, Segment


@lru_cache(maxsize=1)
def fixtures_root() -> Path:
    """Walk up to the repo root and find fixtures/.

    Searching rather than hard-coding a relative depth: the loader is imported
    from tests, from the eval harness, and from ad-hoc scripts at different
    depths, and a wrong ../.. is a confusing failure.
    """
    for parent in Path(__file__).resolve().parents:
        candidate = parent / "fixtures"
        if (candidate / "segments").is_dir():
            return candidate
    raise FileNotFoundError(
        "Could not locate the fixtures/ directory above "
        f"{Path(__file__).resolve()}. Expected fixtures/segments/ at the repo root."
    )


def load_segment(fixture_id: str) -> Segment:
    path = fixtures_root() / "segments" / f"{fixture_id}.json"
    return Segment.model_validate_json(path.read_text(encoding="utf-8"))


def load_goldens(fixture_id: str) -> list[Finding]:
    path = fixtures_root() / "findings" / f"{fixture_id}.findings.json"
    raw = json.loads(path.read_text(encoding="utf-8"))
    return [Finding.model_validate(item) for item in raw]


def available_fixtures() -> list[str]:
    segments = (fixtures_root() / "segments").glob("*.json")
    return sorted(path.stem for path in segments)
