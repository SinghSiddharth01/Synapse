"""Full-surface test over ASGITransport — no sockets, FakeProvider scripted."""
import re

import httpx
import pytest
from synapse_providers import FakeProvider

from synapse_service.api import build_app

MERGE_NOOP = {"working_memory": "wm-1", "merges": [], "trivial_ids": [], "conflicts": []}


class _RecordingProvider(FakeProvider):
    """Records the finding ids listed in each merge prompt, so a test can
    assert on WHICH candidates synthesis was offered -- not just on the
    verdict it returned. Mirrors test_synthesis.py's helper of the same
    name; duplicated rather than imported so this suite stays a pure
    over-ASGI black-box test with no reach into synthesis's own test
    module."""

    def __init__(self, scripts):
        super().__init__(scripts=scripts)
        self.seen: list[list[str]] = []

    async def complete(self, messages, response_schema=None):
        listing = messages[-1]["content"]
        self.seen.append(re.findall(r"\[([^\]]+)\]", listing))
        return await super().complete(messages, response_schema)


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


async def test_push_larger_than_candidate_window_reaches_the_merge_prompt_via_the_route():
    """API-level pin for the CANDIDATE_WINDOW starvation fix (E3 residual,
    Finding #10). synthesis.py's own unit tests already pin this at the
    Synthesizer level (test_a_push_larger_than_the_candidate_window_is_not_
    starved); this test pins it at the ROUTE api.py actually calls.
    api.py's `await synthesizer.merge(store, sid, findings)` -- findings,
    not [] -- is the fix; before this test, nothing at the API level
    failed if that call regressed back to `merge(store, sid, [])` (the
    verifier confirmed the full service+synthesis suite passed either
    way)."""
    from synapse_service.synthesis import CANDIDATE_WINDOW

    provider = _RecordingProvider(scripts=[MERGE_NOOP])
    async with _client(provider) as client:
        sid = (await client.post("/v1/sessions", json={"purpose": "p", "created_by": "s"})
               ).json()["shared_id"]
        batch_size = CANDIDATE_WINDOW + 5
        findings = [_finding_json(f"f-{i:02d}") for i in range(batch_size)]
        r = await client.post(f"/v1/sessions/{sid}/findings", json={"findings": findings})
        assert r.json()["accepted"] == batch_size

    assert set(provider.seen[0]) == {f["id"] for f in findings}   # every pushed id offered, none starved


def _normalize(findings: list[dict]) -> set[tuple]:
    """Content only -- deliberately excludes `id` (synthesis mints a fresh
    `syn-<uuid4>` per run, so two independent runs never share one) and
    `ts`. What must match across an incremental stream and a replay is
    the type/text/lineage, not the accidental identifiers of the run that
    produced it."""
    return {(f["type"], f["text"], frozenset(f["merged_from"])) for f in findings}


async def test_synthesize_self_heals_a_session_whose_last_push_failed():
    """POST /v1/sessions/{sid}/synthesize re-runs merge over stored
    findings with no new push required -- the self-heal path for the E3
    residual (Finding #11's sibling gap): 'a session whose last push
    failed synthesis cannot re-run it without new findings.'
    `FakeProvider(scripts=[None])` reproduces the documented non-dict-data
    failure mode (test_non_dict_verdicts_do_not_crash_merge in
    test_synthesis.py): the finding lands, but memory_version stays put
    and the push honestly reports synthesized: false. A second, working
    script via /synthesize is what recovers it -- without a NEW finding
    to push."""
    provider = FakeProvider(scripts=[None, MERGE_NOOP])
    async with _client(provider) as client:
        sid = (await client.post("/v1/sessions", json={"purpose": "p", "created_by": "s"})
               ).json()["shared_id"]

        r = await client.post(f"/v1/sessions/{sid}/findings",
                              json={"findings": [_finding_json("f-1")]})
        assert r.json() == {"accepted": 1, "memory_version": 0, "synthesized": False}

        r = await client.post(f"/v1/sessions/{sid}/synthesize")
        assert r.json() == {"memory_version": 1, "synthesized": True}

        r = await client.get(f"/v1/sessions/{sid}/watermark", params={"agent_session": "as-x"})
        assert r.json()["version"] == 1


async def test_synthesize_unknown_session_404():
    async with _client(FakeProvider(scripts=[])) as client:
        r = await client.post("/v1/sessions/nope/synthesize")
        assert r.status_code == 404


async def test_full_log_replay_into_a_fresh_store_converges_with_the_original_stream():
    """Plan C.3's own first-failing-test, never built until now
    (docs/plans/2026-08-03-plan-c-service.md:55 -- 'a full resync of a
    machine's entire log after a service restart converges to the same
    state as the original stream'; flagged as a gap by the E3 verifier).

    Two Findings pushed INCREMENTALLY -- one merge() call per push -- are
    compared against the SAME two Findings replayed into a FRESH store as
    one batch push, followed by exactly one /synthesize call. Two merge()
    calls either way, so `memory_version` converges too, not merely
    content. Synthesized ids are randomly generated per run (uuid4 in
    synthesis.py) and are therefore EXPECTED to differ between the two
    runs; `_normalize` strips them so the comparison is over the
    type/text/lineage synthesis actually decided, which must match."""
    def _findings() -> list[dict]:
        return [_finding_json("f-1"), _finding_json("f-2")]

    noop1 = {"working_memory": "interim", "merges": [], "trivial_ids": [], "conflicts": []}
    merge2 = {"working_memory": "final-wm",
              "merges": [{"source_ids": ["f-1", "f-2"], "text": "merged: f-1 and f-2",
                         "type": "learning"}],
              "trivial_ids": [], "conflicts": []}
    rank_all = {"ranked": [0]}

    # --- the original stream: two incremental pushes ---
    async with _client(FakeProvider(scripts=[noop1, merge2, rank_all])) as client:
        sid = (await client.post("/v1/sessions", json={"purpose": "p", "created_by": "s"})
               ).json()["shared_id"]
        f1, f2 = _findings()

        r = await client.post(f"/v1/sessions/{sid}/findings", json={"findings": [f1]})
        assert r.json()["memory_version"] == 1
        r = await client.post(f"/v1/sessions/{sid}/findings", json={"findings": [f2]})
        assert r.json()["memory_version"] == 2
        original_version = r.json()["memory_version"]

        r = await client.post(f"/v1/sessions/{sid}/query",
                              json={"query": "anything", "agent_session": "as-other"})
        original_state = _normalize(r.json()["findings"])

    # --- the replay: the SAME log, pushed as one batch into a FRESH
    #     store, then one explicit /synthesize call ---
    async with _client(FakeProvider(scripts=[noop1, merge2, rank_all])) as client:
        sid = (await client.post("/v1/sessions", json={"purpose": "p", "created_by": "s"})
               ).json()["shared_id"]

        r = await client.post(f"/v1/sessions/{sid}/findings",
                              json={"findings": _findings()})
        assert r.json()["memory_version"] == 1

        r = await client.post(f"/v1/sessions/{sid}/synthesize")
        assert r.json() == {"memory_version": 2, "synthesized": True}
        replay_version = r.json()["memory_version"]

        r = await client.post(f"/v1/sessions/{sid}/query",
                              json={"query": "anything", "agent_session": "as-other"})
        replay_state = _normalize(r.json()["findings"])

    assert replay_state == original_state and len(replay_state) == 1
    assert replay_version == original_version
