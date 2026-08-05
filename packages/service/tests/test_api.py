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
        assert r.json() == {"accepted": 1, "memory_version": 1, "synthesized": True}

        r = await client.get(f"/v1/sessions/{sid}/watermark", params={"agent_session": "as-9"})
        assert r.json() == {"version": 1, "new_since": 1,
                            "by_type": {"learning": 1}, "conflicts": 0}

        r = await client.post(f"/v1/sessions/{sid}/query",
                              json={"query": "what do we know", "agent_session": "as-9"})
        assert [f["id"] for f in r.json()["findings"]] == ["f-1"]

        r = await client.get(f"/v1/sessions/{sid}/watermark", params={"agent_session": "as-9"})
        assert r.json()["new_since"] == 0            # query advanced last_seen


async def test_replayed_push_is_a_noop_and_skips_the_model():
    # A second script is deliberately scripted: if the replay wrongly reached
    # the model it would succeed and be indistinguishable via the JSON
    # response alone (a swallowed-exception replay also reports
    # accepted=0/memory_version=1). provider.calls is the only way to prove
    # the model was never invoked a second time.
    provider = FakeProvider(scripts=[MERGE_NOOP, MERGE_NOOP])
    async with _client(provider) as client:
        sid = (await client.post("/v1/sessions", json={"purpose": "p", "created_by": "s"})
               ).json()["shared_id"]
        body = {"findings": [_finding_json("f-1")]}
        first = await client.post(f"/v1/sessions/{sid}/findings", json=body)
        replay = await client.post(f"/v1/sessions/{sid}/findings", json=body)
    assert first.json()["accepted"] == 1
    assert replay.json() == {"accepted": 0, "memory_version": 1, "synthesized": False}
    assert provider.calls == 1     # the replay must never reach the provider a 2nd time


async def test_push_response_reports_whether_synthesis_actually_ran():
    """A push can be accepted (findings durably landed) while synthesis
    silently fails or is skipped -- a provider outage, an exhausted retry,
    a verdict that fails structural validation. Before this, the response
    shape made "merged" and "landed but not yet synthesized" look
    identical: {"accepted": N, "memory_version": <unchanged>} either way.
    `synthesized` reports whether memory_version actually moved THIS
    round, so a producer watching pushes -- not just HTTP status codes --
    can tell the two outcomes apart instead of treating every 200 as a
    completed merge."""
    async with _client(FakeProvider(scripts=[])) as client:   # scripts exhausted -> raises
        sid = (await client.post("/v1/sessions", json={"purpose": "p", "created_by": "s"})
              ).json()["shared_id"]
        r = await client.post(f"/v1/sessions/{sid}/findings",
                              json={"findings": [_finding_json("f-1")]})
        assert r.json() == {"accepted": 1, "memory_version": 0, "synthesized": False}


async def test_watermark_applies_the_same_suppression_rule_as_query():
    """Round-2 adjudication on watermark suppression: by_type (a CONTENT
    field) runs through the same all-attributions suppression rule as
    /query (invariant 3) -- an asker whose own Agent Session produced
    every attribution on a Finding must not see it counted there. version
    and new_since (CHANGE fields) stay global and unfiltered -- they
    measure how much the memory moved, not whether that movement is
    visible to this particular asker. So new_since legitimately stays 1
    for as-me even though by_type == {}: the memory DID change (a merge
    round ran), it's just that none of what changed is new information
    to as-me specifically. That is the intended split, not a bug: a
    caller that wants "how much is new FOR ME" reads by_type/conflicts;
    new_since answers "did anything happen at all"."""
    provider = FakeProvider(scripts=[MERGE_NOOP])
    async with _client(provider) as client:
        sid = (await client.post("/v1/sessions", json={"purpose": "p", "created_by": "s"})
               ).json()["shared_id"]
        findings = [_finding_json(f"f-{i}", agent_session="as-me") for i in range(3)]
        r = await client.post(f"/v1/sessions/{sid}/findings", json={"findings": findings})
        assert r.json()["accepted"] == 3

        r = await client.get(f"/v1/sessions/{sid}/watermark", params={"agent_session": "as-me"})
        body = r.json()
        assert body["by_type"] == {}                      # all three are the asker's own
        assert body["new_since"] == 1                      # but the memory DID move (global)

        r = await client.get(f"/v1/sessions/{sid}/watermark", params={"agent_session": "as-other"})
        body = r.json()
        assert body["by_type"] == {"learning": 3}          # a teammate sees all three
        assert body["new_since"] == 1


async def test_watermark_conflicts_count_only_conflicts_touching_a_visible_finding():
    """conflicts is the other CONTENT field of the round-2 split: a
    Conflict entirely between two of the asker's own (suppressed)
    Findings is not new information to them -- it's already in that
    Agent Session's context window. A Conflict where at least one side is
    a teammate's IS new information, even though the OTHER side won't be
    returned by /query either."""
    provider = FakeProvider(scripts=[{
        "working_memory": "wm", "merges": [], "trivial_ids": [],
        "conflicts": [
            {"a": "f-1", "b": "f-2", "description": "both mine, not news to me"},
            {"a": "f-1", "b": "f-3", "description": "mine vs a teammate's"},
        ],
    }])
    async with _client(provider) as client:
        sid = (await client.post("/v1/sessions", json={"purpose": "p", "created_by": "s"})
               ).json()["shared_id"]
        findings = [_finding_json("f-1", agent_session="as-me"),
                   _finding_json("f-2", agent_session="as-me"),
                   _finding_json("f-3", agent_session="as-teammate")]
        r = await client.post(f"/v1/sessions/{sid}/findings", json={"findings": findings})
        assert r.json()["accepted"] == 3

        r = await client.get(f"/v1/sessions/{sid}/watermark", params={"agent_session": "as-me"})
        assert r.json()["conflicts"] == 1                 # only the mine-vs-teammate one

        r = await client.get(f"/v1/sessions/{sid}/watermark", params={"agent_session": "as-unrelated"})
        assert r.json()["conflicts"] == 2                 # both sides visible to a third party


async def test_unknown_session_404_and_bad_payload_422():
    async with _client(FakeProvider(scripts=[])) as client:
        assert (await client.get("/v1/sessions/nope/watermark")).status_code == 404
        sid = (await client.post("/v1/sessions", json={"purpose": "p", "created_by": "s"})
               ).json()["shared_id"]
        r = await client.post(f"/v1/sessions/{sid}/findings",
                              json={"findings": [{"not": "a finding"}]})
        assert r.status_code == 422


async def test_every_route_returns_422_on_a_missing_required_field_not_500():
    """Only push_findings validated its body; create_session, add_member,
    and query indexed body["..."] directly and raised KeyError -- a 500,
    not the plan's documented `422 {error}` for a malformed payload. E4's
    Relay treats 5xx as retryable, so a client bug becomes an infinite
    retry loop against a request that will never succeed instead of a
    reported, terminal 422."""
    async with _client(FakeProvider(scripts=[])) as client:
        r = await client.post("/v1/sessions", json={"purpose": "p"})   # missing created_by
        assert r.status_code == 422 and "error" in r.json()

        sid = (await client.post("/v1/sessions", json={"purpose": "p", "created_by": "s"})
               ).json()["shared_id"]

        r = await client.post(f"/v1/sessions/{sid}/members", json={})  # missing contributor
        assert r.status_code == 422 and "error" in r.json()

        r = await client.post(f"/v1/sessions/{sid}/query",
                              json={"agent_session": "as-x"})           # missing query
        assert r.status_code == 422 and "error" in r.json()
