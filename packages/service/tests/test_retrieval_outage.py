"""Decision 008 at the ROUTE: a dead retrieval backend is a 503, never an
empty 200.

`test_retrieval.py` pins the contract at the function; this pins what the wire
does with it, because the wire is where the lie was told. The defect being
closed: `query_findings` caught every exception and returned `[]`, the route
answered 200 with `{"findings": []}`, and the orchestrator rendered "Team
memory has nothing relevant to that. (Checked — not skipped.)" over a model
backend that was not answering at all. Observed live 2026-08-06 on a host
started with `--npu`, whose own /debug dashboard listed the findings its
queries claimed did not exist.

Helpers are duplicated from test_api.py rather than imported, matching that
file's own note on `_RecordingProvider`: these suites are black-box over
ASGITransport and reaching between them would couple two things that are
deliberately independent.
"""

import httpx
from synapse_providers import FakeProvider

from synapse_service.api import build_app

MERGE_NOOP = {"working_memory": "wm-1", "merges": [], "trivial_ids": [], "conflicts": []}


def _finding_json(fid: str, contributor: str = "aditya") -> dict:
    return {"id": fid, "type": "learning", "text": f"insight {fid}",
            "attributions": [{"contributor": contributor, "agent_session": "as-1",
                              "agent": "claude-code"}],
            "ts": "2026-08-04T12:00:00Z", "refs": [],
            "provenance": "distilled", "status": "kept",
            "merged_from": [], "merged_into": None}


def _client(provider) -> httpx.AsyncClient:
    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app=build_app(provider, merge_min_interval_s=0)),
        base_url="http://svc")


class _DiesOnRetrieval(FakeProvider):
    """Scripted for synthesis, dead for retrieval — the W1 shape.

    The GenieX idle death does not take the box down; it takes the HTTP server
    down while the process keeps running, and it does it BETWEEN calls. So a
    session can have merged perfectly well an hour ago and have a dead seam by
    the time somebody asks it a question. Scripting the merge and failing only
    the ranking call is what reproduces that, and it is why this cannot be
    written as `FakeProvider(scripts=[])`: that fails the merge too, so the
    test would go green having never reached `/query` at all.
    """

    provider_id = "npu"

    async def complete(self, messages, response_schema=None):
        if response_schema is not None and "ranked" in response_schema.get("properties", {}):
            raise TimeoutError("read timed out after 30s")
        return await super().complete(messages, response_schema)


async def _session_with_one_finding(client) -> str:
    sid = (await client.post("/v1/sessions",
                             json={"purpose": "fec decode", "created_by": "siddsing"})
           ).json()["shared_id"]
    await client.post(f"/v1/sessions/{sid}/findings",
                      json={"findings": [_finding_json("f-1")]})
    return sid


async def test_a_dead_retrieval_backend_answers_503_and_names_itself():
    async with _client(_DiesOnRetrieval(scripts=[MERGE_NOOP])) as client:
        sid = await _session_with_one_finding(client)
        r = await client.post(f"/v1/sessions/{sid}/query",
                              json={"query": "what do we know", "agent_session": "as-9"})

    # 503 and not 502: this service is healthy, its DEPENDENCY is not.
    assert r.status_code == 503
    body = r.json()
    assert body["error"] == "retrieval_unavailable"
    # The backend is NAMED. Without it the orchestrator's outage sentence
    # could only say "something", and an operator could not tell the local NPU
    # seam from the cloud one — which is the first question they will ask.
    assert body["provider"] == "npu"
    assert "TimeoutError" in body["detail"]
    # And the body is NOT the shape a working-but-empty memory returns. That
    # separation is the entire decision: an un-upgraded reader must be unable
    # to mistake one for the other.
    assert "findings" not in body


async def test_a_failed_query_does_not_advance_the_askers_watermark():
    """The watermark is "what this asker has already seen". A query that
    returned an outage showed them nothing, so advancing it would make the
    next arrival briefing report the whole Shared Memory as already-seen: the
    findings would exist, be visible, and never be mentioned to them again.

    The skip is achieved by control flow — the 503 returns before
    `mark_seen` — so this test is what stops a later refactor from hoisting
    `mark_seen` above the try block for tidiness."""
    async with _client(_DiesOnRetrieval(scripts=[MERGE_NOOP])) as client:
        sid = await _session_with_one_finding(client)

        before = (await client.get(f"/v1/sessions/{sid}/watermark",
                                   params={"agent_session": "as-9"})).json()
        assert before["new_since"] == 1

        failed = await client.post(f"/v1/sessions/{sid}/query",
                                   json={"query": "anything", "agent_session": "as-9"})
        assert failed.status_code == 503

        after = (await client.get(f"/v1/sessions/{sid}/watermark",
                                  params={"agent_session": "as-9"})).json()

    assert after["new_since"] == 1        # unchanged: they saw nothing


async def test_the_debug_feed_records_the_outage_as_its_own_event():
    """/debug is the view the demo puts on screen beside the agent. A failed
    query that logged nothing — or logged an ordinary `query` event with zero
    results — would show an operator a healthy-looking run of searches while
    every one of them 503'd. The tag is asserted because the dashboard filters
    and colours on it."""
    async with _client(_DiesOnRetrieval(scripts=[MERGE_NOOP])) as client:
        sid = await _session_with_one_finding(client)
        await client.post(f"/v1/sessions/{sid}/query",
                          json={"query": "anything", "contributor": "akhil"})
        stats = (await client.get("/debug/stats.json",
                                  params={"session": sid})).json()

    queries = stats["session"]["queries"]
    assert [e["tag"] for e in queries] == ["query_failed"]
    assert queries[0]["detail"]["provider"] == "npu"
    assert queries[0]["detail"]["cause"] == "TimeoutError"
    assert queries[0]["detail"]["asked_by"] == "akhil"
    assert "DOWN" in queries[0]["summary"]


async def test_a_healthy_query_that_ranks_nothing_is_still_a_plain_200():
    """The negative control for all of the above. If this goes red the change
    has over-reached: "nothing matched" is an ANSWER and keeps its 200 and its
    empty list, because that is the sentence the orchestrator is still allowed
    to say — and after 008 it is finally a true one."""
    async with _client(FakeProvider(scripts=[MERGE_NOOP, {"ranked": []}])) as client:
        sid = await _session_with_one_finding(client)
        r = await client.post(f"/v1/sessions/{sid}/query",
                              json={"query": "anything", "agent_session": "as-9"})

    assert r.status_code == 200
    assert r.json() == {"findings": []}
