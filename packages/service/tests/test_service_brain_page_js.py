"""The brain page's client-side JS, actually executed.

Sibling of `test_service_debug_page_js.py`, same harness and same reason: every
other assertion in this package reads the served HTML SOURCE, which cannot see
what the page does once it has data. The brain page rebuilds TWO lists with
`innerHTML` on every 1s poll (revisions and the latest-into-memory rows), so
the "an entry the operator just clicked open collapses within a second" defect
has two places to reappear rather than one.

It also pins the property the whole page exists for: a participant about whom
nothing is known renders an em-dash, not a zero. That is invisible in the HTML
source, because the roster is built entirely at runtime.

Skipped, not failed, when `node` isn't on PATH.
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
from pathlib import Path

import httpx
import pytest
from synapse_providers import FakeProvider

from synapse_service.api import build_app

SUPPORT_DIR = Path(__file__).parent / "support"

pytestmark = pytest.mark.skipif(
    shutil.which("node") is None,
    reason="node not on PATH -- this test executes the page's real client-side JS",
)


def _extract_script(html: str) -> str:
    m = re.search(r"<script>(.*)</script>", html, re.S)
    assert m is not None, "served page has no <script> block"
    return m.group(1)


def _run_scenario(script: str, tmp_path: Path) -> dict:
    combined = "\n".join([
        (SUPPORT_DIR / "minidom.js").read_text(encoding="utf-8"),
        (SUPPORT_DIR / "brain_page_driver.js").read_text(encoding="utf-8")
        .replace("/*__EXTRACTED_SCRIPT__*/", script),
    ])
    js_file = tmp_path / "brain_scenario.js"
    js_file.write_text(combined, encoding="utf-8")
    result = subprocess.run(  # noqa: S603 - fixed argv, no shell, test-only
        ["node", str(js_file)], capture_output=True, text=True, timeout=15
    )
    assert result.returncode == 0, (
        f"driver failed:\nstdout={result.stdout}\nstderr={result.stderr}"
    )
    return json.loads(result.stdout.strip().splitlines()[-1])


async def _outcome(tmp_path) -> dict:
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=build_app(FakeProvider(scripts=[]))),
        base_url="http://svc",
    ) as client:
        r = await client.get("/debug")
        assert r.status_code == 200
    return _run_scenario(_extract_script(r.text), tmp_path)


async def test_an_expanded_revision_survives_the_next_poll(tmp_path) -> None:
    outcome = await _outcome(tmp_path)

    assert outcome["revisionCount"] == 2
    assert outcome["expandedAfterClick"] is True
    # The bug this guards: the 1s poll rebuilds #revisions with innerHTML and,
    # without the expandedKeys set, an open revision collapses again inside a
    # second -- never long enough to read the prose it was opened for. Keyed on
    # the revision VERSION, so the open row stays the same row.
    assert outcome["expandedAfterPoll"] is True
    assert outcome["expandedKey"].startswith("rev|7|")


async def test_the_page_renders_its_rows_and_its_working_memory(tmp_path) -> None:
    outcome = await _outcome(tmp_path)

    assert outcome["workingMemory"] == "the team is chasing a decode failure"
    assert "v7" in outcome["wmMeta"] and "6 words" in outcome["wmMeta"]
    assert outcome["recentCount"] == 2
    # Provenance drives the badge and the row's colour; "merged" and
    # "contributed" must not collapse into one another.
    assert outcome["provenances"] == ["synthesized", "contributed"]


async def test_the_roster_renders_three_rows_and_says_what_it_does_not_know(tmp_path) -> None:
    outcome = await _outcome(tmp_path)
    roster = outcome["rosterHtml"]

    # Two windows of ONE human, plus a member who has contributed nothing.
    # Counted by the contributor cell, so the header row is not one of them.
    assert roster.count('<td class="who"') == 3
    assert "as-window-1" in roster and "as-window-2" in roster
    assert roster.count("aditya") == 1

    # The honesty properties, in the rendered output rather than the payload:
    # a participant who has never read the memory gets an em-dash, and one who
    # has never asked gets one too. Never a zero, which would read as "at v0".
    assert "— never read" in roster
    assert "not yet seen" in roster
    assert "listening" in roster
    # And the word this page must never print for a state nobody measures.
    assert "connected" not in roster
