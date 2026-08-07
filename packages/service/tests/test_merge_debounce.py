"""Synthesis is rate-limited, and an identical resend is not a log entry.

Both land 2026-08-06, from the same incident.

The debounce exists because `merge()` ran on EVERY accepted push. That was
affordable while pushes were occasional; a `synapse-worker run` following a
live conversation pushes every couple of minutes, and the hosted synthesis key
allows 20 requests / 25,000 tokens per hour -- ~7 merges. Without a floor
between rounds the truncation fix just trades one silent "memory unchanged"
for another with a different cause.

The dedup exists because the dashboard's log tail showed every finding twice.
`push_findings` upserts and then `merge()` upserts the SAME list again, and
`append` recorded each resend deliberately, so one push of N cost 3N entries --
worse with a write-ahead log that retries by design.
"""

from datetime import datetime, timezone

from starlette.testclient import TestClient
from synapse_contracts import ModelUsage
from synapse_providers import FakeProvider

from synapse_service.api import build_app
from synapse_service.store import InMemoryStore

TS = datetime(2026, 8, 6, tzinfo=timezone.utc)

VERDICT = {"working_memory": "wm", "merges": [], "trivial_ids": [], "conflicts": []}


def _finding(fid: str, text: str = "the pool trips under allocation pressure") -> dict:
    return {"id": fid, "type": "learning", "text": text,
            "attributions": [{"contributor": "sid", "agent_session": "as-1",
                              "agent": "claude-code"}],
            "ts": TS.isoformat()}


class _CostlyProvider(FakeProvider):
    """A FakeProvider that reports a REALISTIC token bill.

    FakeProvider derives usage from prompt/response length, so a scripted
    verdict costs a few dozen tokens -- against which no budget in this test
    would ever bind. The governor charges what the provider reports, which is
    right (an unspent round must not be billed) and does mean a test about
    running OUT of budget has to spend some.
    """

    def __init__(self, scripts, tokens_per_call: int = 4_000):
        super().__init__(scripts=scripts)
        self._tokens_per_call = tokens_per_call

    async def complete(self, messages, response_schema=None):
        result = await super().complete(messages, response_schema)
        return result.model_copy(update={"usage": ModelUsage(
            input_tokens=self._tokens_per_call - 1_600, output_tokens=1_600)})


def _client(monkeypatch, interval: float, provider=None):
    monkeypatch.setattr("synapse_service.api.MERGE_MIN_INTERVAL_S", interval)
    app = build_app(provider or FakeProvider(scripts=[VERDICT] * 20),
                    merge_min_interval_s=interval)
    return TestClient(app)


def _session(client) -> str:
    return client.post("/v1/sessions", json={"purpose": "p", "created_by": "sid"}
                       ).json()["shared_id"]


def test_a_second_push_inside_the_interval_defers_synthesis(monkeypatch):
    """The rate limit, enforced. The first push merges immediately -- an empty
    memory that stays empty until a timer expires is a worse first impression
    than one model call -- and the second is held."""
    client = _client(monkeypatch, interval=3600)
    sid = _session(client)

    first = client.post(f"/v1/sessions/{sid}/findings",
                        json={"findings": [_finding("f-1")]}).json()
    assert first["deferred"] is False

    second = client.post(f"/v1/sessions/{sid}/findings",
                         json={"findings": [_finding("f-2")]}).json()
    assert second["deferred"] is True
    assert second["pending"] == 1


def test_deferred_findings_are_queryable_immediately(monkeypatch):
    """Nothing is lost by waiting, and this is why: the upsert happens before
    the debounce decision, so a deferred push is in the log, in the watermark
    and in /query the moment it is accepted. Only the SYNTHESIZED memory lags.
    Were this false the debounce would be trading a rate-limit failure for a
    data-visibility one."""
    client = _client(monkeypatch, interval=3600)
    sid = _session(client)
    client.post(f"/v1/sessions/{sid}/findings", json={"findings": [_finding("f-1")]})
    client.post(f"/v1/sessions/{sid}/findings",
                json={"findings": [_finding("f-2", "the decoder stalls on retry")]})

    mark = client.get(f"/v1/sessions/{sid}/watermark?contributor=someone-else").json()
    assert sum(mark["by_type"].values()) == 2


def test_the_deferred_batch_reaches_the_next_round_as_new_findings(monkeypatch):
    """A deferred push must still arrive as `new_findings` when the round
    finally runs, not merely be sitting in the log. synthesis.merge forces
    every id in `new_findings` into its candidate window regardless of
    CANDIDATE_WINDOW; a finding that reaches synthesis only as "one more old
    row in the log" can age out of that window and never be considered for
    merging at all."""
    client = _client(monkeypatch, interval=3600)
    sid = _session(client)
    client.post(f"/v1/sessions/{sid}/findings", json={"findings": [_finding("f-1")]})
    held = client.post(f"/v1/sessions/{sid}/findings",
                       json={"findings": [_finding("f-2")]}).json()
    assert held["pending"] == 1

    flushed = client.post(f"/v1/sessions/{sid}/synthesize").json()
    assert flushed["flushed"] == 1
    # and the queue is empty afterwards, so the next push does not re-offer it
    again = client.post(f"/v1/sessions/{sid}/findings",
                        json={"findings": [_finding("f-3")]}).json()
    assert again["pending"] == 1


def test_synthesize_forces_a_round_regardless_of_the_interval(monkeypatch):
    """The operator override. A rate limit nobody can bypass is one people
    work around by restarting the service."""
    client = _client(monkeypatch, interval=3600)
    sid = _session(client)
    client.post(f"/v1/sessions/{sid}/findings", json={"findings": [_finding("f-1")]})
    before = client.get(f"/v1/sessions/{sid}/watermark").json()["version"]
    forced = client.post(f"/v1/sessions/{sid}/synthesize").json()
    assert forced["memory_version"] > before


def test_an_identical_resend_writes_no_log_entry():
    """The duplicate the dashboard showed. Re-upserting the same finding
    unchanged must not grow the log: `accepted` already reports 0, and an
    append-only log that records every retry of a write-ahead queue is noise
    that scales with flakiness."""
    store = InMemoryStore()
    sid = store.create_session("p", "sid").shared_id
    from synapse_contracts import Finding
    finding = Finding.model_validate(_finding("f-1"))

    assert store.upsert(sid, [finding]) == 1
    after_first = len(store.log_entries(sid))

    assert store.upsert(sid, [finding]) == 0
    assert len(store.log_entries(sid)) == after_first


def test_the_budget_governor_defers_once_the_hourly_tokens_are_spent(monkeypatch):
    """Latency and spend are separate limits, and this is the second one.

    MERGE_MIN_INTERVAL_S is 60s because that is the latency the team wants.
    One key affords ~6 rounds/hour at ~4,000 tokens each, so 60 rounds/hour is
    ~10x over budget. Without a governor the floor alone would authorise every
    one of them and the key would 429 -- which surfaces as "findings landed,
    memory unchanged", the exact symptom this whole change set exists to
    remove, reached by a different road."""
    monkeypatch.setattr("synapse_service.api.SYNTHESIS_TOKENS_PER_HOUR", 5_000)
    client = _client(monkeypatch, interval=0,      # latency floor removed entirely
                     provider=_CostlyProvider([VERDICT] * 20))
    sid = _session(client)

    first = client.post(f"/v1/sessions/{sid}/findings",
                        json={"findings": [_finding("f-1")]}).json()
    assert first["deferred"] is False               # 4,000 of 5,000 spent

    second = client.post(f"/v1/sessions/{sid}/findings",
                         json={"findings": [_finding("f-2")]}).json()
    assert second["deferred"] is True               # 8,000 would exceed 5,000
    assert second["pending"] == 1


def test_more_keys_buy_more_rounds(monkeypatch):
    """The governor scales with the pool, which is what makes "activate more
    keys" an actual answer to "we want 60s latency" rather than advice. Same
    spend, same floor, more budget -> the round that was refused above runs."""
    monkeypatch.setattr("synapse_service.api.SYNTHESIS_TOKENS_PER_HOUR", 5_000)
    monkeypatch.setattr("synapse_service.api.SYNTHESIS_KEYS", 4)
    client = _client(monkeypatch, interval=0,
                     provider=_CostlyProvider([VERDICT] * 20))
    sid = _session(client)

    client.post(f"/v1/sessions/{sid}/findings", json={"findings": [_finding("f-1")]})
    second = client.post(f"/v1/sessions/{sid}/findings",
                         json={"findings": [_finding("f-2")]}).json()
    assert second["deferred"] is False


def test_a_resend_whose_content_changed_is_still_recorded():
    """The line the dedup must not cross. Only EXACT duplicates are dropped:
    the log is the record, and discarding a changed finding because its id was
    seen before would lose data rather than noise."""
    store = InMemoryStore()
    sid = store.create_session("p", "sid").shared_id
    from synapse_contracts import Finding
    original = Finding.model_validate(_finding("f-1"))
    store.upsert(sid, [original])
    before = len(store.log_entries(sid))

    edited = Finding.model_validate(_finding("f-1", "a materially different claim"))
    assert store.upsert(sid, [edited]) == 0        # not a NEW id
    assert len(store.log_entries(sid)) > before    # but still an event


def test_a_round_that_never_reaches_the_provider_is_not_charged(monkeypatch):
    """The governor may only spend what was actually spent.

    `Synthesizer.merge` returns at `if not candidates` -- a session with
    nothing retrievable -- without making a request, and `_record_spend` used
    to charge it ASSUMED_MERGE_TOKENS anyway because `last_usage` was None.
    `POST /synthesize` is public and ungated, so a handful of calls against an
    EMPTY session booked the whole hour's budget with zero model calls; the
    ledger is app-scoped, so the next genuine push -- on ANOTHER session --
    came back deferred and that team's working memory was frozen for an hour.
    Deferring real synthesis on the strength of rounds that cost nothing is
    the same "landed, memory unchanged" symptom the governor exists to
    prevent."""
    monkeypatch.setattr("synapse_service.api.SYNTHESIS_TOKENS_PER_HOUR", 5_000)
    client = _client(monkeypatch, interval=0,
                     provider=_CostlyProvider([VERDICT] * 20))
    empty = _session(client)
    for _ in range(10):                            # 40,000 tokens, if charged
        assert client.post(f"/v1/sessions/{empty}/synthesize").json()["flushed"] == 0

    working = _session(client)
    landed = client.post(f"/v1/sessions/{working}/findings",
                         json={"findings": [_finding("f-1")]}).json()
    assert landed["deferred"] is False
    assert landed["synthesized"] is True


def test_a_failed_round_is_charged_at_the_assumed_cost_not_the_last_ones(monkeypatch):
    """A round that RAISES still spent its tokens -- AIC100Provider has burned
    its internal retry by the time it raises -- so it IS charged, at
    ASSUMED_MERGE_TOKENS because nothing came back to measure. What it must not
    be charged is the PREVIOUS round's usage, which is what `last_usage` still
    held when merge() raised: an expensive successful round made every
    subsequent failure cost that much again, and the ledger ran away from the
    real spend fastest exactly when the provider was struggling.

    The arithmetic is the assertion. Budget 25,000; one real round at 9,000;
    one failed round at 4,000 assumed (13,000 spent) or at 9,000 re-charged
    (18,000 spent). `_affordable` then prices the next round at the dearest of
    the last five, 9,000 -- so 22,000 fits and 27,000 does not, and the third
    push runs or is deferred accordingly."""
    monkeypatch.setattr("synapse_service.api.SYNTHESIS_TOKENS_PER_HOUR", 25_000)

    class _Flaky(_CostlyProvider):
        """Succeeds once, at 9,000 tokens, then fails."""

        def __init__(self, scripts):
            super().__init__(scripts, tokens_per_call=9_000)
            self._n = 0

        async def complete(self, messages, response_schema=None):
            self._n += 1
            if self._n > 1:
                raise RuntimeError("provider is down")
            return await super().complete(messages, response_schema)

    client = _client(monkeypatch, interval=0, provider=_Flaky([VERDICT] * 20))
    sid = _session(client)
    first = client.post(f"/v1/sessions/{sid}/findings",
                        json={"findings": [_finding("f-1")]}).json()
    assert first["synthesized"] is True             # 9,000 really spent

    second = client.post(f"/v1/sessions/{sid}/findings",
                         json={"findings": [_finding("f-2")]}).json()
    assert second["deferred"] is False              # it ran; the provider failed
    assert second["synthesized"] is False           # landed, memory unchanged

    third = client.post(f"/v1/sessions/{sid}/findings",
                        json={"findings": [_finding("f-3")]}).json()
    assert third["deferred"] is False               # 13,000 + 9,000 fits in 25,000
