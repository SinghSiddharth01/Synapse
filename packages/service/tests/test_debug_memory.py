"""The memory page and its JSON -- every finding, projected through the fold.

The brain page's `recent` list is a bounded ring; `/debug/memory.json` is the
whole memory. The property under test: after a merge, the two sources are
still present, `superseded`, pointing at the synthesized result -- nothing is
deleted, and `display_status` is the fold's projection, not a producer flag.
"""

from __future__ import annotations

import httpx
from synapse_providers import FakeProvider

from synapse_service.api import build_app

MERGE_SCRIPT = {
    "working_memory": "Team is chasing a decode failure: a ~40 ms timing window.",
    "merges": [{
        "source_ids": ["f-005a-01", "f-005b-01"],
        "text": "The decode failure is a ~40 ms timing window between the two "
                "DMA writes, and it only manifests under load.",
        "type": "learning",
    }],
    "trivial_ids": [],
    "conflicts": [],
}


def _finding(fid: str, contributor: str, agent_session: str, text: str) -> dict:
    return {"id": fid, "type": "learning", "text": text,
            "attributions": [{"contributor": contributor,
                              "agent_session": agent_session,
                              "agent": "claude-code"}],
            "ts": "2026-08-04T12:00:00Z", "refs": [],
            "provenance": "distilled", "status": "kept",
            "merged_from": [], "merged_into": None}


def _client(provider, **kw) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=build_app(provider, **kw)),
                             base_url="http://svc")


async def test_memory_json_holds_every_finding_after_a_merge() -> None:
    provider = FakeProvider(scripts=[MERGE_SCRIPT])
    async with _client(provider) as client:
        sid = (await client.post("/v1/sessions",
                                 json={"purpose": "fec decode", "created_by": "sid"})
              ).json()["shared_id"]
        push = await client.post(f"/v1/sessions/{sid}/findings", json={"findings": [
            _finding("f-005a-01", "aditya", "as-a", "timing window, ~40 ms"),
            _finding("f-005b-01", "akhil", "as-b", "only reproduces under load"),
        ]})
        assert push.json()["synthesized"] is True

        r = await client.get("/debug/memory.json", params={"session": sid})
        assert r.status_code == 200
        session = r.json()["session"]
        rows = {row["id"]: row for row in session["findings"]}

        # All three: the two sources AND the synthesized result.
        assert len(rows) == 3
        for source_id in ("f-005a-01", "f-005b-01"):
            assert rows[source_id]["display_status"] == "superseded"
            assert rows[source_id]["merged_into"] is not None
        synthesized = [row for row in rows.values()
                       if row["provenance"] == "synthesized"]
        assert len(synthesized) == 1
        assert synthesized[0]["display_status"] == "visible"
        assert set(synthesized[0]["merged_from"]) == {"f-005a-01", "f-005b-01"}
        # The merge result carries every source's authors.
        assert set(synthesized[0]["authors"]) == {"aditya", "akhil"}

        assert session["counts"]["total"] == 3
        assert session["counts"]["visible"] == 1
        assert session["counts"]["superseded"] == 2


async def test_memory_page_has_required_ids() -> None:
    async with _client(FakeProvider(scripts=[])) as client:
        r = await client.get("/debug/memory")
        assert r.status_code == 200
        assert "text/html" in r.headers["content-type"]
        for element_id in ("session-list", "mem-body",
                           "mem-search", "status-chips", "prov-chips"):
            assert f'id="{element_id}"' in r.text


async def test_memory_routes_reject_writes() -> None:
    async with _client(FakeProvider(scripts=[])) as client:
        assert (await client.post("/debug/memory.json", json={})).status_code == 405
        assert (await client.post("/debug/memory", json={})).status_code == 405


async def test_memory_routes_are_absent_when_debug_is_disabled() -> None:
    async with _client(FakeProvider(scripts=[]), debug=False) as client:
        assert (await client.get("/debug/memory")).status_code == 404
        assert (await client.get("/debug/memory.json")).status_code == 404


async def test_memory_page_makes_zero_external_requests() -> None:
    async with _client(FakeProvider(scripts=[])) as client:
        body = (await client.get("/debug/memory")).text
    assert "<script src" not in body
    assert "http://" not in body
    assert "https://" not in body
    assert 'href="data:,"' in body
