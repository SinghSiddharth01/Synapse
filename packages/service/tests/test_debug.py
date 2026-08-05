"""Service /debug routes — over ASGI, no sockets, matching test_api.py's style."""

from __future__ import annotations

import httpx
from synapse_providers import FakeProvider

from synapse_service.api import build_app

# The seg-005 golden pair, byte-equivalent to test_synthesis.py's `_pair()`
# and MERGE_SCRIPT, serialized to the wire shape test_api.py's `_finding_json`
# uses -- this suite pushes over HTTP, not through the store directly.
MERGE_SCRIPT = {
    "working_memory": "Team is chasing a decode failure: a ~40 ms timing window, load-dependent.",
    "merges": [{
        "source_ids": ["f-005a-01", "f-005b-01"],
        "text": "The decode failure is a ~40 ms timing window between the two DMA "
                "writes, and it only manifests under load.",
        "type": "learning",
    }],
    "trivial_ids": [],
    "conflicts": [],
}


def _finding(fid: str, contributor: str, agent_session: str, agent: str, text: str) -> dict:
    return {"id": fid, "type": "learning", "text": text,
            "attributions": [{"contributor": contributor, "agent_session": agent_session,
                              "agent": agent}],
            "ts": "2026-08-04T12:00:00Z", "refs": [],
            "provenance": "distilled", "status": "kept",
            "merged_from": [], "merged_into": None}


def _pair() -> list[dict]:
    a = _finding("f-005a-01", "aditya", "as-fixture-005a", "claude-code",
                 "The decode failure is a timing window: it occurs only when the gap "
                 "between the two DMA writes exceeds roughly 40 ms.")
    b = _finding("f-005b-01", "akhil", "as-fixture-005b", "codex",
                 "The decode failure reproduces only under load, when background "
                 "traffic pushes the delay past about 40 ms.")
    return [a, b]


def _client(provider) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=build_app(provider)),
                             base_url="http://svc")


async def test_debug_stats_reflect_a_merge_and_a_query() -> None:
    provider = FakeProvider(scripts=[MERGE_SCRIPT, {"ranked": [0]}])
    async with _client(provider) as client:
        sid = (await client.post("/v1/sessions",
                                 json={"purpose": "fec decode", "created_by": "sid"})
              ).json()["shared_id"]

        push = await client.post(f"/v1/sessions/{sid}/findings", json={"findings": _pair()})
        assert push.json()["synthesized"] is True

        r = await client.get("/debug/stats.json", params={"session": sid})
        assert r.status_code == 200
        session = r.json()["session"]
        assert session["sid"] == sid
        assert any(e["kind"] == "Merged" for e in session["log_tail"])
        assert session["view"]["visible"] == 1
        assert session["view"]["superseded"] == 2
        assert any(c["component"] == "synthesis" for c in session["llm"])

        await client.post(f"/v1/sessions/{sid}/query",
                          json={"query": "what do we know", "agent_session": "as-9"})

        r2 = await client.get("/debug/stats.json", params={"session": sid})
        session2 = r2.json()["session"]
        assert any(c["component"] == "retrieval" for c in session2["llm"])
        assert len(session2["queries"]) == 1


async def test_debug_stats_json_is_read_only_and_touches_no_provider() -> None:
    # scripts=[] -> an exhausted FakeProvider raises on ANY call. A 200 here is
    # what proves the debug endpoint never reaches the provider.
    provider = FakeProvider(scripts=[])
    async with _client(provider) as client:
        sid = (await client.post("/v1/sessions", json={"purpose": "p", "created_by": "s"})
              ).json()["shared_id"]
        before = (await client.get(f"/v1/sessions/{sid}/watermark",
                                   params={"agent_session": "as-1"})).json()["version"]

        r = await client.get("/debug/stats.json", params={"session": sid})
        assert r.status_code == 200

        after = (await client.get(f"/v1/sessions/{sid}/watermark",
                                  params={"agent_session": "as-1"})).json()["version"]
        assert after == before == 0


async def test_debug_page_has_required_ids() -> None:
    async with _client(FakeProvider(scripts=[])) as client:
        r = await client.get("/debug")
        assert r.status_code == 200
        assert "text/html" in r.headers["content-type"]
        assert 'id="log-tail"' in r.text
        assert 'id="feed"' in r.text


async def test_debug_rejects_writes() -> None:
    async with _client(FakeProvider(scripts=[])) as client:
        r = await client.post("/debug/stats.json", json={})
        assert r.status_code == 405
