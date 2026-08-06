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
    assert outcome["recentCount"] == 3
    # Provenance drives the badge and the row's colour; "merged" and
    # "contributed" must not collapse into one another.
    assert outcome["provenances"] == ["synthesized", "contributed", "distilled"]


async def test_the_roster_renders_its_rows_and_says_what_it_does_not_know(tmp_path) -> None:
    outcome = await _outcome(tmp_path)
    roster = outcome["rosterHtml"]

    # Two windows of ONE human, a member who has contributed nothing, and a
    # contributor the member list has never heard of. Counted by the
    # contributor cell, so the header row is not one of them.
    assert roster.count('<td class="who"') == 4
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


async def test_a_contributor_the_member_list_never_saw_is_not_called_left(tmp_path) -> None:
    """`left` claims a human pressed leave. Absence from `members` does not:
    a raw `POST /findings` registers nobody, and a restarted service starts
    every session at `members=[]`. The roster renders the membership fact and
    stops there."""
    outcome = await _outcome(tmp_path)
    roster = outcome["rosterHtml"]

    assert "not a member" in roster
    assert ">left<" not in roster
    # The caveat travels with the cell, so the frame is self-explaining.
    assert "may never have registered" in roster


async def test_client_supplied_strings_cannot_break_out_of_an_attribute(tmp_path) -> None:
    """`Finding.id`, `Attribution.agent_session` and `shared_id` are bare
    `str` in contracts/schemas.py, written by whichever teammate's machine
    pushed them -- across the device boundary this system is built around.
    All three reach an HTML attribute on this page. The DOM text serializer
    escapes `& < >` and NOT quotes, so text-node escaping alone leaves them
    able to close the attribute they sit in."""
    outcome = await _outcome(tmp_path)

    for html in (outcome["rosterHtml"], outcome["recentHtml"], outcome["selectHtml"]):
        # A LIVE handler needs an unescaped quote to open its value. The
        # escaped rendering is `onmouseover=&quot;`, which is inert text.
        assert 'onmouseover="' not in html
        assert 'onfocus="' not in html
    # The payload really did carry the hostile strings -- otherwise the
    # assertions above would pass on an empty page -- and they came out as
    # escaped text inside the attribute they tried to close.
    assert "alert(1)" in outcome["recentHtml"]
    assert "alert(1)" in outcome["selectHtml"]
    assert "onmouseover=&quot;" in outcome["recentHtml"]
    assert "onfocus=&quot;" in outcome["selectHtml"]


async def test_losing_the_session_clears_every_region(tmp_path) -> None:
    """A service that comes back with no sessions must not leave the previous
    one's numbers on screen. Clearing only the two lists left the working
    memory at its `…` placeholder above a rail of stale counts, which reads as
    a live session whose memory happens to be empty."""
    outcome = await _outcome(tmp_path)
    empty = outcome["emptyState"]

    assert empty["purpose"] == "the service holds no shared session yet"
    assert empty["wmBody"] == ""
    # Em-dashes, not the previous session's 2 and 7, and not zeros either --
    # zero contributors is a claim about a session that no longer exists.
    assert empty["contributors"] == "—"
    assert empty["version"] == "—"
    assert "no session" in empty["revisions"]


async def test_the_js_fixture_has_not_drifted_from_the_real_payload(tmp_path) -> None:
    """The driver hand-writes its payload, so a server-side rename would leave
    every JS assertion above green while the real page rendered undefineds.
    This compares the fixture's key names against what `_brain_payload`
    actually produces for a session with one member, one contribution, one
    query and one observed rewrite."""
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(
            app=build_app(FakeProvider(scripts=[
                {"merges": [], "trivial_ids": [], "conflicts": [],
                 "working_memory": "the team is chasing a decode failure"},
                {"ranked": [0]},
            ]))),
        base_url="http://svc",
    ) as client:
        sid = (await client.post(
            "/v1/sessions",
            json={"purpose": "p", "created_by": "sid"})).json()["shared_id"]
        await client.post(f"/v1/sessions/{sid}/members",
                          json={"contributor": "sid"})
        await client.post(f"/v1/sessions/{sid}/findings", json={"findings": [{
            "id": "f-1", "type": "learning", "text": "a thing",
            "attributions": [{"contributor": "sid", "agent_session": "as-a",
                              "agent": "claude-code"}],
            "ts": "2026-08-04T12:00:00Z", "refs": [], "provenance": "distilled",
            "status": "kept", "merged_from": [], "merged_into": None}]})
        await client.post(f"/v1/sessions/{sid}/synthesize")
        await client.post(f"/v1/sessions/{sid}/query",
                          json={"query": "what do we know", "contributor": "sid",
                                "agent_session": "as-a"})
        payload = (await client.get(f"/debug/brain.json?session={sid}")).json()
        page = (await client.get("/debug")).text

    real = payload["session"]
    wm = real["working_memory"]
    assert wm["revisions"], "fixture comparison needs at least one revision"
    expected = {
        "session": sorted(real),
        "counts": sorted(real["counts"]),
        "working_memory": sorted(wm),
        "revision": sorted(wm["revisions"][0]),
        "participant": sorted(real["participants"][0]),
        "recent": sorted(real["recent"][0]),
        "attribution": sorted(real["recent"][0]["attributions"][0]),
        "sessions_row": sorted(payload["sessions"][0]),
    }
    outcome = _run_scenario(_extract_script(page), tmp_path)
    assert outcome["fixtureKeys"] == expected
