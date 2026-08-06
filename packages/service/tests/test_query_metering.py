"""`/query` spends the synthesis key, so `/query` is charged to the governor.

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
# The hole itself
# --------------------------------------------------------------------------

def test_queries_alone_exhaust_the_key_and_the_governor_now_says_so(monkeypatch):
    """THE REPRODUCTION. Queries and no pushes; the token ceiling is reached
    by retrieval alone and the next merge is deferred rather than 429'd.

    Before the fix this push came back `deferred: false, synthesized: true` —
    the governor authorising a round from a budget retrieval had already
    spent. Budget 12,000; four queries at 2,000 each is 8,000, and the next
    merge is priced at ASSUMED_MERGE_TOKENS (4,000, nothing measured yet), so
    the fifth thing the key is asked for does not fit."""
    client, provider = _client(monkeypatch, tokens_per_hour=12_000,
                               provider=SharedKeyProvider(query_tokens=2_000))
    sid = _session(client)
    client.post(f"/v1/sessions/{sid}/findings", json={"findings": [_finding("f-0")]})
    provider.merge_tokens = 0                      # the merge above is not the point

    for _ in range(4):
        assert _query(client, sid).status_code == 200
    assert provider.queries == 4

    landed = client.post(f"/v1/sessions/{sid}/findings",
                         json={"findings": [_finding("f-1")]}).json()
    assert landed["deferred"] is True
    assert landed["accepted"] == 1                 # queryable regardless
    assert landed["pending"] == 1


def test_the_deferral_reason_names_retrieval_as_the_spender(monkeypatch, caplog):
    """The two deferral reasons are logged distinctly because their fixes
    differ (ADR 0005 §7). A third spender needs the same treatment: an
    operator reading "budget" while pushes are rare must be able to see that
    the queries are what drained it, or the only available conclusion is that
    the governor is broken."""
    client, _ = _client(monkeypatch, tokens_per_hour=10_000,
                        provider=SharedKeyProvider(query_tokens=3_000))
    sid = _session(client)
    client.post(f"/v1/sessions/{sid}/findings", json={"findings": [_finding("f-0")]})
    for _ in range(3):
        _query(client, sid)

    with caplog.at_level("WARNING", logger="synapse_service.api"):
        client.post(f"/v1/sessions/{sid}/findings",
                    json={"findings": [_finding("f-1")]})

    budget = [r for r in caplog.records if "deferred on BUDGET" in r.getMessage()]
    assert budget, "the push was deferred with nothing said about why"
    assert "retrieval" in budget[0].getMessage()


def test_queries_count_against_the_request_ceiling_too(monkeypatch):
    """Tokens are not the only thing a key meters. The hosted key allows 20
    REQUESTS/hour and retrieval passes a response_schema, so it takes the same
    internal retry a merge does and is priced at 2 the same way."""
    client, _ = _client(monkeypatch, tokens_per_hour=10_000_000,
                        requests_per_hour=8)
    sid = _session(client)
    client.post(f"/v1/sessions/{sid}/findings", json={"findings": [_finding("f-0")]})

    for _ in range(3):
        _query(client, sid)                        # 4 rounds on the ledger now

    landed = client.post(f"/v1/sessions/{sid}/findings",
                         json={"findings": [_finding("f-1")]}).json()
    assert landed["deferred"] is True


# --------------------------------------------------------------------------
# What the fix must NOT do
# --------------------------------------------------------------------------

def test_cheap_queries_do_not_make_the_next_merge_look_cheap(monkeypatch):
    """`_affordable` prices the next round at the dearest of the last five,
    because merge cost trends upward and an average lags the trend. Retrieval
    rounds are 3-4x cheaper: folding them into that window would price the
    next 70B merge as if it were a ranking call, and the governor would
    authorise a round the key cannot pay for — re-opening the failure from
    inside the fix.

    Budget 20,000. One real merge at 9,000, then five 500-token queries
    (11,500 spent). Pricing the next merge off the merges says 9,000 →
    20,500 > 20,000, deferred. Pricing it off the last five entries says 500
    → 12,000, authorised. The assertion is which one happens."""
    client, provider = _client(monkeypatch, tokens_per_hour=20_000,
                               provider=SharedKeyProvider(merge_tokens=9_000,
                                                          query_tokens=500))
    sid = _session(client)
    first = client.post(f"/v1/sessions/{sid}/findings",
                        json={"findings": [_finding("f-1")]}).json()
    assert first["deferred"] is False
    for _ in range(5):
        _query(client, sid)
    assert provider.queries == 5

    nxt = client.post(f"/v1/sessions/{sid}/findings",
                      json={"findings": [_finding("f-2")]}).json()
    assert nxt["deferred"] is True


def test_a_query_that_reaches_no_model_is_not_charged(monkeypatch):
    """The same discipline `_record_spend`'s ⟨CORRECTED⟩ note bought for
    synthesis. `query_findings` returns before `provider.complete` when
    nothing is visible to the asker, so there is no request and no charge —
    otherwise a session nobody has pushed to could book the hour."""
    client, provider = _client(monkeypatch, tokens_per_hour=5_000,
                               provider=SharedKeyProvider(query_tokens=4_000))
    sid = _session(client)

    for _ in range(10):                            # 40,000 tokens, if charged
        assert _query(client, sid).status_code == 200
    assert provider.queries == 0                   # nothing to rank, no call

    landed = client.post(f"/v1/sessions/{sid}/findings",
                         json={"findings": [_finding("f-1")]}).json()
    assert landed["deferred"] is False
    assert landed["synthesized"] is True


def test_a_query_that_raised_is_still_charged(monkeypatch):
    """A retrieval that burned the key and then failed is the most expensive
    round there is, and W1's typed 503 makes it the most visible — charging it
    is what stops an outage from looking free. That this can defer synthesis
    during a retrieval outage is not a cost: the provider is SHARED, so a
    ranking call that cannot complete is a merge that could not have
    completed either.

    Budget 10,000. One real merge at 4,000, then two failed queries at
    ASSUMED_QUERY_TOKENS (1,500) each — 7,000 spent, next merge priced at
    4,000, so 11,000 does not fit. Uncharged, the two outages are free and
    8,000 fits comfortably: the arithmetic is chosen so this test can only
    pass if the failure path charges."""
    client, provider = _client(monkeypatch, tokens_per_hour=10_000,
                               provider=SharedKeyProvider(fail_queries=True))
    sid = _session(client)
    assert client.post(f"/v1/sessions/{sid}/findings",
                       json={"findings": [_finding("f-0")]}).json()["deferred"] is False

    for _ in range(2):
        assert _query(client, sid).status_code == 503
    assert provider.queries == 2                   # the calls really went out

    landed = client.post(f"/v1/sessions/{sid}/findings",
                         json={"findings": [_finding("f-1")]}).json()
    assert landed["deferred"] is True              # 2 x ASSUMED_QUERY_TOKENS


def test_an_unparseable_ranking_is_charged_exactly_once(monkeypatch):
    """⟨W3b review⟩ THE DOUBLE-CHARGE. `AIC100Provider` returns
    `data=None, schema_valid=False` (aic100.py:291) after a successful HTTP
    round whose output failed the schema twice. `result.data.get("ranked")`
    used to sit INSIDE the same `try` as `on_usage(result.usage)`, so `.get`
    on None raised, the handler fired `on_usage(None)` a second time, and one
    retrieval booked TWO ledger entries — its real cost plus the assumed cost
    of a failure that never happened.

    Both ceilings are asserted, because the two count different things and the
    bug hit both. `_affordable`'s request check counts ENTRIES
    (`(len(_spend) + 1) * 2 > request_budget`), so a doubled entry burned 4 of
    the key's 20 requests/hour for one query.

    Arithmetic, tokens: budget 10,000, merge 4,000, two schema-failing queries
    at 1,000 each. Charged once: 4,000 + 2,000 = 6,000, next merge priced at
    4,000 → exactly 10,000, which fits. Charged twice: 4,000 + 2,000 + 2x1,500
    assumed = 9,000 → 13,000, deferred. The test can only pass at one charge.
    """
    class SchemaFailProvider(SharedKeyProvider):
        """A round that COMPLETED and reported usage, but whose output could
        not be parsed. Not an exception — that is the other failure class, and
        the one this bug was hiding behind."""

        async def complete(self, messages, response_schema=None):
            ranking = bool(response_schema
                           and "ranked" in response_schema.get("properties", {}))
            result = await super().complete(messages, response_schema)
            if ranking:
                return result.model_copy(update={"data": None,
                                                 "schema_valid": False})
            return result

    client, provider = _client(monkeypatch, tokens_per_hour=10_000,
                               requests_per_hour=8,
                               provider=SchemaFailProvider(merge_tokens=4_000,
                                                           query_tokens=1_000))
    sid = _session(client)
    assert client.post(f"/v1/sessions/{sid}/findings",
                       json={"findings": [_finding("f-0")]}).json()["deferred"] is False

    # Still a 503: an unusable ranking is the ABSENCE of an answer, never an
    # empty one (decision 008). The fix changed what it costs, not what it means.
    for _ in range(2):
        assert _query(client, sid).status_code == 503
    assert provider.queries == 2

    landed = client.post(f"/v1/sessions/{sid}/findings",
                         json={"findings": [_finding("f-1")]}).json()
    assert landed["deferred"] is False, (
        "the schema-failing query was charged more than once — 3 rounds on the "
        "ledger for 3 real requests is the bound here")


def test_metering_retrieval_never_blocks_a_query_only_the_merge(monkeypatch):
    """The product decision, pinned. Charging `/query` deliberately lets query
    traffic defer SYNTHESIS; it must not gate `/query` itself. A query is the
    read path — the agent asking a question in front of an audience — and
    turning a spent budget into "shared memory is unreachable" would trade a
    lagging working memory for a broken one, and collide with W1's 503, which
    means something specific and different."""
    client, _ = _client(monkeypatch, tokens_per_hour=1)   # nothing is affordable
    sid = _session(client)
    client.post(f"/v1/sessions/{sid}/findings", json={"findings": [_finding("f-1")]})

    for _ in range(5):
        response = _query(client, sid)
        assert response.status_code == 200
        assert "findings" in response.json()


def test_findings_stay_queryable_while_the_merge_is_deferred_on_query_spend(
        monkeypatch):
    """ADR 0005's promise, held under the new spender. Deferral must cost
    freshness of the SYNTHESIZED memory and nothing else; if a query-drained
    budget also hid findings this change would be trading one silent loss for
    another."""
    client, _ = _client(monkeypatch, tokens_per_hour=9_000,
                        provider=SharedKeyProvider(query_tokens=3_000))
    sid = _session(client)
    client.post(f"/v1/sessions/{sid}/findings", json={"findings": [_finding("f-0")]})
    for _ in range(3):
        _query(client, sid)

    held = client.post(
        f"/v1/sessions/{sid}/findings",
        json={"findings": [_finding("f-9", "the decoder stalls on retry")]}).json()
    assert held["deferred"] is True

    mark = client.get(f"/v1/sessions/{sid}/watermark?contributor=nobody").json()
    assert sum(mark["by_type"].values()) == 2
