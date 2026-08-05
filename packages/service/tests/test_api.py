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
        # The RAW prompt body, kept alongside `seen`. `seen` parses the
        # bracketed tokens, which are finding ids in a SYNTHESIS prompt but
        # enumeration indices in a RETRIEVAL one -- so absence-of-a-finding
        # can only be asserted against the text, never against `seen`.
        self.prompts: list[str] = []

    async def complete(self, messages, response_schema=None):
        listing = messages[-1]["content"]
        self.seen.append(re.findall(r"\[([^\]]+)\]", listing))
        self.prompts.append(listing)
        return await super().complete(messages, response_schema)


def _finding_json(fid: str, agent_session: str = "as-1", text: str | None = None) -> dict:
    return {"id": fid, "type": "learning", "text": text or f"insight {fid}",
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
        # Exact equality on purpose: this is the ONLY thing pinning the route
        # contract that briefing.py consumes. `topics`/`purpose`/`members`
        # joined it in Plan E Task E.8. One finding lands in one topic, and
        # `_finding_json` gives it the text "insight f-1", which is therefore
        # its own medoid label.
        assert r.json() == {
            "version": 1, "new_since": 1, "by_type": {"learning": 1}, "conflicts": 0,
            "topics": [{"id": "t0001", "size": 1, "label": "insight f-1"}],
            "purpose": "fec decode",          # whatever this test created the session with
            "members": ["aditya"],
        }

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


async def test_query_sends_at_most_top_k_findings_into_one_prompt():
    """api.py used to pass the ENTIRE visible log into one model prompt,
    uncapped and growing linearly. Fourteen findings instead of a hundred is
    what keeps an 8B usable as the session grows."""
    from synapse_service.api import TOP_K

    provider = _RecordingProvider(scripts=[MERGE_NOOP, {"ranked": [0]}])
    async with _client(provider) as client:
        sid = (await client.post("/v1/sessions", json={"purpose": "p", "created_by": "s"})
               ).json()["shared_id"]
        findings = [_finding_json(f"f-{i:03d}") for i in range(100)]
        await client.post(f"/v1/sessions/{sid}/findings", json={"findings": findings})

        await client.post(f"/v1/sessions/{sid}/query",
                          json={"query": "insight", "agent_session": "as-OTHER"})

    # Bracketed tokens in a RETRIEVAL prompt are indices, so this counts
    # candidates -- which is exactly what is being bounded.
    assert 0 < len(provider.seen[-1]) <= TOP_K


async def test_a_small_session_sends_every_visible_finding_exactly_as_main_did():
    """THE HEDGE. At or below TOP_K allowed findings the route skips select()
    entirely and passes the visible log through in arrival order -- byte-
    identical to what main does today, which is what makes this task a no-op
    at demo scale and a scaling property above it.

    Without this branch, a five-finding session's answer would depend on BM25
    + symbol overlap + a HashingEmbedder with no paraphrase signal, for no
    gain: everything fits in the prompt anyway."""
    from synapse_service.api import TOP_K

    provider = _RecordingProvider(scripts=[MERGE_NOOP, {"ranked": [0]}])
    async with _client(provider) as client:
        sid = (await client.post("/v1/sessions", json={"purpose": "p", "created_by": "s"})
               ).json()["shared_id"]
        findings = [_finding_json(f"f-{i}") for i in range(5)]
        await client.post(f"/v1/sessions/{sid}/findings", json={"findings": findings})

        await client.post(f"/v1/sessions/{sid}/query",
                          json={"query": "totally unrelated words", "agent_session": "as-X"})

    assert 5 <= TOP_K                                   # the branch under test is live
    prompt = provider.prompts[-1]
    for i in range(5):
        assert f"insight f-{i}" in prompt               # every one of them, not 14 of 5
    assert prompt.index("insight f-0") < prompt.index("insight f-4")   # arrival order


async def test_the_bypass_boundary_is_where_the_guarantee_stops():
    """TOP_K and TOP_K+1, because that is where a finding that WAS returned
    stops being returned -- and the plan should document that cliff rather than
    meet it on stage.

    Revision 1's tests sat at 5 findings (bypass) and 100 (lanes), so nothing
    exercised the one visible finding's worth of difference that flips the
    route from "the model reads everything" to "the model reads a selection".

    Two contracts, both stated:
      AT TOP_K      -- every allowed finding reaches the prompt. Guaranteed by
                       the bypass, not by the selectors.
      AT TOP_K + 1  -- exactly TOP_K reach it, and the guarantee that survives
                       is the RECENT reserved floor: `max(1, top_k //
                       RESERVE_DIVISOR)` == `max(1, 14 // 5)` == 2, so the two
                       most recently arrived allowed findings are ALWAYS in the
                       prompt (via the fusion, or via the reservation, or via
                       the back-fill Task 7 added). Which of the remaining
                       findings is dropped is decided by fused rank, and a
                       lexically disjoint one that is not among the two most
                       recent has NO guarantee. That is the cliff. The
                       pre-agreed response, if it bites at demo scale, is Task
                       13 Step 2's: raise the bypass threshold, do not revert
                       this task."""
    from synapse_service.api import TOP_K

    provider = _RecordingProvider(scripts=[MERGE_NOOP, {"ranked": [0]},
                                           MERGE_NOOP, {"ranked": [0]}])
    async with _client(provider) as client:
        sid = (await client.post("/v1/sessions", json={"purpose": "p", "created_by": "s"})
               ).json()["shared_id"]
        # exactly TOP_K, one of them lexically disjoint from the query
        batch = [_finding_json(f"f-{i:03d}") for i in range(TOP_K - 1)]
        batch.append(_finding_json("f-odd", text="qairt refuses the context binary"))
        await client.post(f"/v1/sessions/{sid}/findings", json={"findings": batch})

        await client.post(f"/v1/sessions/{sid}/query",
                          json={"query": "insight", "agent_session": "as-X"})
        at_top_k = provider.prompts[-1]

        # one more unrelated finding: now TOP_K + 1 are visible
        await client.post(f"/v1/sessions/{sid}/findings",
                          json={"findings": [_finding_json("f-extra")]})
        await client.post(f"/v1/sessions/{sid}/query",
                          json={"query": "insight", "agent_session": "as-X"})
        above = provider.prompts[-1]

    # AT the boundary: the bypass is live and nothing is selected away.
    assert "qairt refuses the context binary" in at_top_k
    assert at_top_k.count("(learning)") == TOP_K

    # ONE ABOVE it: the prompt is bounded, one finding was dropped, and the
    # RECENT floor is the guarantee that survives. Arrival order is
    # f-000 … f-012, f-odd, f-extra -- so the two reserved slots are f-extra
    # and f-odd, and f-odd is the lexically disjoint one. It reaches the prompt
    # HERE because it is second-most-recent, not because any lane matched it.
    # Had it arrived first, nothing would guarantee it.
    assert above.count("(learning)") == TOP_K             # 14 of 15: one dropped
    assert "insight f-extra" in above                     # most recent, reserved
    assert "qairt refuses the context binary" in above    # 2nd most recent, reserved


async def test_a_relevant_finding_the_lanes_cannot_match_still_reaches_a_small_prompt():
    """The paraphrase case, at the one scale where this system can promise it.
    `HashingEmbedder` has NO paraphrase signal, so above the bypass a
    lexically-disjoint but relevant finding is NOT guaranteed to be selected --
    that is a known, recorded limitation (docs/STATE.md; the Embedder protocol
    is the seam for a real embedding model). Below the bypass it is guaranteed,
    because nothing is selected at all. If someone deletes the bypass, this
    test is what says so."""
    provider = _RecordingProvider(scripts=[MERGE_NOOP, {"ranked": [0]}])
    async with _client(provider) as client:
        sid = (await client.post("/v1/sessions", json={"purpose": "p", "created_by": "s"})
               ).json()["shared_id"]
        await client.post(f"/v1/sessions/{sid}/findings", json={"findings": [
            _finding_json("f-para", text="the accelerator refuses work past 40 ms"),
            _finding_json("f-other", text="the build script sets a stale flag"),
        ]})

        await client.post(f"/v1/sessions/{sid}/query",
                          json={"query": "why is the NPU dropping requests under load",
                                "agent_session": "as-X"})

    assert "the accelerator refuses work past 40 ms" in provider.prompts[-1]


async def test_a_finding_only_the_asker_produced_never_reaches_the_candidate_set():
    """Invariant 3 at the NEW seam. The branch has no suppression anywhere and
    `exclude=` is the seam with nothing populating it; `visible_to` stays the
    ONE definition and now feeds both the exclusion and query_findings.

    Sized ABOVE the bypass (20 findings > TOP_K) on purpose -- below it the
    route never calls select(), so a small-session version of this test would
    pin query_findings' suppression and NOT the exclude= seam, which is the
    thing this integration puts at risk.

    Asserted against the PROMPT TEXT, not against `provider.seen` -- see the
    helper's comment. `seen` holds indices for a retrieval prompt, so an id
    membership check there is vacuous."""
    provider = _RecordingProvider(scripts=[MERGE_NOOP, {"ranked": [0]}])
    async with _client(provider) as client:
        sid = (await client.post("/v1/sessions", json={"purpose": "p", "created_by": "s"})
               ).json()["shared_id"]
        others = [_finding_json(f"f-them-{i:02d}", agent_session="as-them")
                  for i in range(19)]
        await client.post(f"/v1/sessions/{sid}/findings", json={"findings": [
            _finding_json("f-mine", agent_session="as-me"), *others]})

        r = await client.post(f"/v1/sessions/{sid}/query",
                              json={"query": "insight", "agent_session": "as-me"})

    prompt = provider.prompts[-1]
    assert "insight f-mine" not in prompt          # never offered to the model
    assert "insight f-them" in prompt              # ...and the teammates' were
    assert "f-mine" not in [f["id"] for f in r.json()["findings"]]


async def test_watermark_returns_topics_sorted_by_size_with_no_provider_call():
    provider = _RecordingProvider(scripts=[MERGE_NOOP])
    async with _client(provider) as client:
        sid = (await client.post("/v1/sessions",
                                 json={"purpose": "fec decode", "created_by": "s"})
               ).json()["shared_id"]
        await client.post(f"/v1/sessions/{sid}/members", json={"contributor": "aditya"})
        await client.post(f"/v1/sessions/{sid}/findings", json={"findings": [
            _finding_json("f-1"), _finding_json("f-2"), _finding_json("f-3")]})
        calls_before = len(provider.seen)

        body = (await client.get(f"/v1/sessions/{sid}/watermark",
                                 params={"agent_session": "as-OTHER"})).json()

    assert len(provider.seen) == calls_before          # NO provider call, ever
    assert body["purpose"] == "fec decode"
    assert body["members"] == ["aditya"]
    sizes = [t["size"] for t in body["topics"]]
    assert sizes == sorted(sizes, reverse=True)
    assert all(t["label"] for t in body["topics"])


async def test_a_topic_whose_members_are_all_merged_away_reports_size_from_the_view():
    """Topic membership is never pruned on merge, so TopicIndex still reports
    the pre-merge size -- 'collapse looks like working'. Reading View.members_of
    is what makes the number honest without fixing the underlying bug."""
    merge = {"working_memory": "wm",
             "merges": [{"source_ids": ["f-1", "f-2"], "text": "merged", "type": "learning"}],
             "trivial_ids": [], "conflicts": []}
    async with _client(FakeProvider(scripts=[merge])) as client:
        sid = (await client.post("/v1/sessions", json={"purpose": "p", "created_by": "s"})
               ).json()["shared_id"]
        await client.post(f"/v1/sessions/{sid}/findings", json={"findings": [
            _finding_json("f-1"), _finding_json("f-2")]})

        body = (await client.get(f"/v1/sessions/{sid}/watermark",
                                 params={"agent_session": "as-OTHER"})).json()

    assert sum(t["size"] for t in body["topics"]) == 1     # not 3, and not 2


async def test_topic_labels_are_stable_across_a_rebuild():
    """`rebuild()` discards every index and recomputes from the log alone. A
    label that moves across it is derived from something that is not in the
    log -- a second source of truth hiding as a cache (adr/0004).

    Read what this DOES and does not prove. `rebuild()` re-derives TopicAssigned
    rather than replaying it (branch store.py:191-192), so what holds this green
    is that the replay ORDER is identical and assignment is deterministic under
    HashingEmbedder. It is not proof that the recorded entry is authoritative.
    Swap in a real Embedder, or start pruning membership, and this becomes a
    real test of a property the code does not yet have."""
    # Built explicitly rather than through _client(), because this test needs
    # the store the app is actually using, and `client._transport.app` is an
    # httpx internal.
    app = build_app(FakeProvider(scripts=[MERGE_NOOP]))
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),
                                 base_url="http://svc") as client:
        sid = (await client.post("/v1/sessions", json={"purpose": "p", "created_by": "s"})
               ).json()["shared_id"]
        await client.post(f"/v1/sessions/{sid}/findings", json={"findings": [
            _finding_json(f"f-{i}") for i in range(5)]})
        before = (await client.get(f"/v1/sessions/{sid}/watermark",
                                   params={"agent_session": "as-O"})).json()["topics"]

        app.state.store._memories[sid].rebuild()            # the seam added in Step 4

        after = (await client.get(f"/v1/sessions/{sid}/watermark",
                                  params={"agent_session": "as-O"})).json()["topics"]

    assert before == after
    assert before != []                                     # not vacuously equal


async def test_a_topic_of_only_the_askers_own_findings_contributes_nothing():
    """`topics` is a CONTENT field and runs through the same all-attributions
    suppression rule as by_type and /query (invariant 3)."""
    async with _client(FakeProvider(scripts=[MERGE_NOOP])) as client:
        sid = (await client.post("/v1/sessions", json={"purpose": "p", "created_by": "s"})
               ).json()["shared_id"]
        await client.post(f"/v1/sessions/{sid}/findings", json={"findings": [
            _finding_json("f-mine", agent_session="as-me")]})

        body = (await client.get(f"/v1/sessions/{sid}/watermark",
                                 params={"agent_session": "as-me"})).json()

    assert body["topics"] == []
