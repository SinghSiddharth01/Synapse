"""Full-surface test over ASGITransport — no sockets, FakeProvider scripted."""
import httpx
import pytest
from synapse_providers import FakeProvider

from synapse_service.api import build_app

MERGE_NOOP = {"working_memory": "wm-1", "merges": [], "trivial_ids": [], "conflicts": []}


def _finding_json(fid: str, agent_session: str = "as-1") -> dict:
    return {"id": fid, "type": "learning", "text": f"insight {fid}",
            "attributions": [{"contributor": "aditya", "agent_session": agent_session,
                              "agent": "claude-code"}],
            "ts": "2026-08-04T12:00:00Z", "refs": [],
            "provenance": "distilled", "status": "kept",
            "merged_from": [], "merged_into": None}


def _client(provider) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=build_app(provider)),
                             base_url="http://svc")


async def test_full_flow_push_watermark_query():
    provider = FakeProvider(scripts=[MERGE_NOOP, {"ranked": [0]}])
    async with _client(provider) as client:
        r = await client.post("/v1/sessions",
                              json={"purpose": "fec decode", "created_by": "siddsing"})
        assert r.status_code == 201
        sid = r.json()["shared_id"]

        r = await client.post(f"/v1/sessions/{sid}/members", json={"contributor": "aditya"})
        assert r.json()["members"] == ["aditya"]

        r = await client.post(f"/v1/sessions/{sid}/findings",
                              json={"findings": [_finding_json("f-1")]})
        assert r.json() == {"accepted": 1, "memory_version": 1}

        r = await client.get(f"/v1/sessions/{sid}/watermark", params={"agent_session": "as-9"})
        assert r.json() == {"version": 1, "new_since": 1,
                            "by_type": {"learning": 1}, "conflicts": 0}

        r = await client.post(f"/v1/sessions/{sid}/query",
                              json={"query": "what do we know", "agent_session": "as-9"})
        assert [f["id"] for f in r.json()["findings"]] == ["f-1"]

        r = await client.get(f"/v1/sessions/{sid}/watermark", params={"agent_session": "as-9"})
        assert r.json()["new_since"] == 0            # query advanced last_seen


async def test_replayed_push_is_a_noop_and_skips_the_model():
    provider = FakeProvider(scripts=[MERGE_NOOP])     # exactly ONE merge scripted
    async with _client(provider) as client:
        sid = (await client.post("/v1/sessions", json={"purpose": "p", "created_by": "s"})
               ).json()["shared_id"]
        body = {"findings": [_finding_json("f-1")]}
        first = await client.post(f"/v1/sessions/{sid}/findings", json=body)
        replay = await client.post(f"/v1/sessions/{sid}/findings", json=body)
    assert first.json()["accepted"] == 1
    assert replay.json() == {"accepted": 0, "memory_version": 1}
    # scripts NOT exhausted-error'd: the replay never reached the provider.


async def test_unknown_session_404_and_bad_payload_422():
    async with _client(FakeProvider(scripts=[])) as client:
        assert (await client.get("/v1/sessions/nope/watermark")).status_code == 404
        sid = (await client.post("/v1/sessions", json={"purpose": "p", "created_by": "s"})
               ).json()["shared_id"]
        r = await client.post(f"/v1/sessions/{sid}/findings",
                              json={"findings": [{"not": "a finding"}]})
        assert r.status_code == 422
