"""`/query` spends the synthesis key, so `/query` is charged — but no longer gates.

⟨REWRITTEN 2026-08-07⟩ Retrieval spend is still recorded against the same key
ledger a merge is, tagged `retrieval`. What changed is what that recording is
FOR. It used to let the ledger refuse the next merge IN ADVANCE, and that
prediction was wrong twice in one night in both directions — most expensively a
25,000 token/hour figure, read off a console once and never re-checked, that
stalled synthesis for 45 minutes while the same console read 1 of 20 requests
for the hour and 7 of 250 for the day. Probed live, this gateway sends no
rate-limit headers at all, so the estimate can never be corrected against
reality. `affordable()` now refuses only on something OBSERVED: headers a
provider actually sent, or a 429 it actually returned.

So every assertion here that read `deferred is True` because the LEDGER said so
is gone — deleted rather than weakened, because the behaviour it pinned was
removed on purpose. What remains is what is still true and still worth
defending: a query never blocks, and a query never silently stops the memory
being written.

Original note, still true of the recording itself:

The hole, as it stood (FLOW.md §1.5, confirmed at api.py:672-798 on
2026-08-06 after W1 rewrote the query path's failure contract): `build_app`
wraps ONE provider object twice — `synthesis_provider` and
`retrieval_provider` are two RecordingProvider façades over one instance, so
one API key and one hourly ceiling. `/query` makes a real model call through
it. `_record_spend` had exactly two call sites, both on the synthesis path.
So twenty queries and no pushes exhausted the key's 20 requests/hour while
`_affordable()` still answered `(True, "")`, and the next merge 429'd inside
AIC100Provider — where the only mitigation is key rotation a one-key pool
cannot perform. That surfaces as "findings landed, memory unchanged": the
exact symptom ADR 0005's governor was built to make visible, reached by the
one road it could not see.

Every assertion below is on behaviour through the real routes with a real
`build_app`. Nothing here asserts on a hand-built ledger entry — the ADR-0005
trap was a test that passed for four hours against a fixture the host never
sends.
"""

from datetime import datetime, timezone

from starlette.testclient import TestClient
from synapse_contracts import ModelUsage
from synapse_providers import FakeProvider

from synapse_service.api import build_app

TS = datetime(2026, 8, 6, tzinfo=timezone.utc)

VERDICT = {"working_memory": "wm", "merges": [], "trivial_ids": [], "conflicts": []}


def _finding(fid: str, text: str = "the pool trips under allocation pressure") -> dict:
    return {"id": fid, "type": "learning", "text": text,
            "attributions": [{"contributor": "sid", "agent_session": "as-1",
                              "agent": "claude-code"}],
            "ts": TS.isoformat()}


class SharedKeyProvider(FakeProvider):
    """ONE provider answering both components, which is the deployment shape
    the hole lives in — `build_app` wraps a single instance twice.

    Dispatches on the response schema exactly as the real one does (synthesis
    asks for `working_memory`, retrieval for `ranked`) and reports a
    per-component token bill, because FakeProvider's length-derived usage is a
    few dozen tokens and no hourly budget would ever bind against it.
    """

    def __init__(self, *, merge_tokens: int = 4_000, query_tokens: int = 1_200,
                 fail_queries: bool = False) -> None:
        super().__init__(scripts=[])
        self.merge_tokens = merge_tokens
        self.query_tokens = query_tokens
        self.fail_queries = fail_queries
        self.merges = 0
        self.queries = 0

    async def complete(self, messages, response_schema=None):
        ranking = bool(response_schema
                       and "ranked" in response_schema.get("properties", {}))
        if ranking:
            self.queries += 1
            if self.fail_queries:
                raise RuntimeError("retrieval backend is down")
            data, tokens = {"ranked": [0]}, self.query_tokens
        else:
            self.merges += 1
            data, tokens = VERDICT, self.merge_tokens
        self._scripts.append(data)
        result = await super().complete(messages, response_schema)
        return result.model_copy(update={"usage": ModelUsage(
            input_tokens=tokens - tokens // 4, output_tokens=tokens // 4)})


def _client(monkeypatch, *, tokens_per_hour=None, requests_per_hour=None,
            provider=None) -> tuple[TestClient, SharedKeyProvider]:
    if tokens_per_hour is not None:
        monkeypatch.setattr("synapse_service.api.SYNTHESIS_TOKENS_PER_HOUR",
                            tokens_per_hour)
    if requests_per_hour is not None:
        monkeypatch.setattr("synapse_service.api.SYNTHESIS_REQUESTS_PER_HOUR",
                            requests_per_hour)
    provider = provider or SharedKeyProvider()
    # interval 0 removes the LATENCY floor entirely, so every deferral below is
    # attributable to spend and to nothing else.
    return TestClient(build_app(provider, merge_min_interval_s=0)), provider


def _session(client) -> str:
    return client.post("/v1/sessions", json={"purpose": "p", "created_by": "sid"}
                       ).json()["shared_id"]


def _query(client, sid, text="what breaks the pool?"):
    return client.post(f"/v1/sessions/{sid}/query",
                       json={"query": text, "contributor": "someone-else",
                             "agent_session": "as-2"})


# --------------------------------------------------------------------------
# what is still true  ⟨2026-08-07⟩
# --------------------------------------------------------------------------

def test_query_spend_no_longer_defers_a_merge(monkeypatch):
    """THE demotion, observed through the real routes.

    Three queries then a push. Under the old ledger this deferred — twenty
    queries and no pushes could exhaust the key's hour and stop synthesis
    outright. Reading the memory must never be able to stop it being written.
    """
    client, _ = _client(monkeypatch, tokens_per_hour=10_000,
                        provider=SharedKeyProvider(query_tokens=3_000))
    sid = _session(client)
    client.post(f"/v1/sessions/{sid}/findings", json={"findings": [_finding("f-0")]})
    for _ in range(3):
        _query(client, sid)

    landed = client.post(f"/v1/sessions/{sid}/findings",
                         json={"findings": [_finding("f-1")]}).json()

    assert landed["deferred"] is False, (
        "query spend still refuses a merge in advance; the ledger is deciding again")
    assert landed["synthesized"] is True, "the merge did not actually run"


def test_a_query_still_never_blocks(monkeypatch):
    """Unchanged, and the reason the metering was scoped this way to begin
    with: whatever the budget is doing, retrieval answers."""
    client, _ = _client(monkeypatch, tokens_per_hour=1,
                        provider=SharedKeyProvider(query_tokens=3_000))
    sid = _session(client)
    client.post(f"/v1/sessions/{sid}/findings", json={"findings": [_finding("f-0")]})

    for _ in range(3):
        assert _query(client, sid).status_code == 200


def test_findings_stay_queryable_and_the_memory_still_moves(monkeypatch):
    """Nothing is lost by the demotion: the findings land, they are readable,
    and the working memory advances rather than waiting on a guess."""
    client, _ = _client(monkeypatch, tokens_per_hour=10_000,
                        provider=SharedKeyProvider(query_tokens=3_000))
    sid = _session(client)
    client.post(f"/v1/sessions/{sid}/findings", json={"findings": [_finding("f-0")]})
    for _ in range(3):
        _query(client, sid)
    before = client.post(f"/v1/sessions/{sid}/findings",
                         json={"findings": [_finding("f-1")]}).json()["memory_version"]

    mark = client.get(f"/v1/sessions/{sid}/watermark?contributor=nobody").json()
    assert sum(mark["by_type"].values()) == 2
    assert before >= 1
