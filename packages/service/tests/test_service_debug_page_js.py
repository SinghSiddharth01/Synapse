"""The service /debug page's client-side JS, actually executed.

See `packages/worker/tests/test_debug_page_js.py`'s module docstring for the
full rationale (duplicated per-package deliberately, matching this repo's
existing pattern of self-contained test files with no cross-package test
imports). Short version: every other test here only checks the served HTML
source, which cannot see that the page's 1s poll rebuilds `#feed` with
`innerHTML` and, pre-fix, never restored `.expanded` -- an entry a user just
clicked open collapsed again within a second. This test extracts the REAL
<script> body from a live `build_app` instance and runs it under a
hand-rolled DOM (support/minidom.js) via Node. Skipped, not failed, when
`node` isn't on PATH.
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
        (SUPPORT_DIR / "debug_page_driver.js").read_text(encoding="utf-8")
        .replace("/*__EXTRACTED_SCRIPT__*/", script),
    ])
    js_file = tmp_path / "scenario.js"
    js_file.write_text(combined, encoding="utf-8")
    result = subprocess.run(  # noqa: S603 - fixed argv, no shell, test-only
        ["node", str(js_file)], capture_output=True, text=True, timeout=15
    )
    assert result.returncode == 0, (
        f"driver failed:\nstdout={result.stdout}\nstderr={result.stderr}"
    )
    last_line = result.stdout.strip().splitlines()[-1]
    return json.loads(last_line)


async def test_an_expanded_llm_entry_survives_the_next_poll(tmp_path) -> None:
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=build_app(FakeProvider(scripts=[]))),
                                 base_url="http://svc") as client:
        r = await client.get("/debug")
        assert r.status_code == 200
        body = r.text

    outcome = _run_scenario(_extract_script(body), tmp_path)

    assert outcome["expandedAfterClick"] is True
    # The bug: a re-render (the page's 1s poll) rebuilds #feed with
    # innerHTML and, pre-fix, never restored .expanded -- a user got under a
    # second to read a prompt/output preview before it silently collapsed.
    assert outcome["expandedAfterPoll"] is True
    assert outcome["detailReparented"] is True
