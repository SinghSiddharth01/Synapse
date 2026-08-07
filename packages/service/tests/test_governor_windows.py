"""The governor must not refuse work the provider would have accepted.

2026-08-06: the Cirrascale dashboard read 1/20 requests for the hour and 5/250
for the day while `_affordable()` was deferring synthesis. Two causes, pinned
here: a x2 request charge that halved the ceiling, and a ledger that never
reconciled against the provider's own reported headroom.
"""
import time

from synapse_providers import RateLimitSnapshot

from synapse_service import api as api_mod


class _StubProvider:
    """Only the surface `affordable` reads."""
    max_tokens = 1600

    def __init__(self, snapshot=None):
        self.last_rate_limit = snapshot


def _ledger(rounds, *, spacing_s=150.0, tokens=100, component="synthesis",
            requests=1, now=None):
    """Entries spread BACKWARDS from `now`, one every `spacing_s`.

    Spacing is not cosmetic. Stamping every entry at `now` puts them all inside
    the per-minute window as well, so the 5/minute ceiling fires before the
    20/hour one and every test asserting on the hourly ceiling passes for the
    wrong reason -- or fails outright, for a 15-round ledger that is genuinely
    affordable by the hour. 150s spacing keeps at most one entry per minute
    while fitting 20 rounds inside the hour.
    """
    now = time.monotonic() if now is None else now
    return [(now - i * spacing_s, tokens, component, requests)
            for i in range(rounds - 1, -1, -1)]


def test_a_round_is_charged_one_request_not_two():
    """20 requests/hour must buy ~20 rounds, not 10. The internal retry is
    charged when it actually happens, not assumed on every round."""
    spend = _ledger(15)
    ok, why = api_mod.affordable(spend, provider=_StubProvider())
    assert ok, why


def test_the_hourly_request_ceiling_still_binds():
    spend = _ledger(20)
    ok, why = api_mod.affordable(spend, provider=_StubProvider())
    assert not ok
    assert "20 request(s) this hour" in why


def test_a_burst_inside_one_minute_is_left_to_backoff_not_pre_refused():
    """The 5/minute limit is real and deliberately NOT enforced here.

    A burst clears itself in seconds, and AIC100Provider now rotates keys and
    waits out the ~36s cooldown, so a burst is recoverable. A pre-emptive
    refusal is not. And `_spend` counts RETRIEVAL, so five queries in a minute
    -- an ordinary thing for a team reading the memory -- would otherwise stop
    the memory being written at all: the exact stall this change set exists to
    end. Measured: the demo rehearsal's pace (two pushes, three queries, one
    push) trips 5/minute and never comes near 20/hour.
    """
    spend = _ledger(6, spacing_s=5.0)
    ok, why = api_mod.affordable(spend, provider=_StubProvider())
    assert ok, why


def test_the_daily_ceiling_binds_when_hour_and_minute_are_clear():
    spend = _ledger(250, spacing_s=300.0)
    ok, why = api_mod.affordable(spend, provider=_StubProvider())
    assert not ok
    assert "this day" in why


def test_reported_headroom_overrides_a_pessimistic_ledger():
    """The ledger says spent; the gateway says 13 requests left. The gateway
    is the one holding the quota."""
    spend = _ledger(20)
    snapshot = RateLimitSnapshot(requests_remaining=13, tokens_remaining=18_000)
    ok, why = api_mod.affordable(spend, provider=_StubProvider(snapshot))
    assert ok, why


def test_reported_exhaustion_defers_even_when_the_ledger_is_clean():
    spend = _ledger(0)
    snapshot = RateLimitSnapshot(requests_remaining=0)
    ok, why = api_mod.affordable(spend, provider=_StubProvider(snapshot))
    assert not ok
    assert "reported" in why


def test_an_empty_snapshot_falls_back_to_the_ledger():
    """Unknown header spelling must not read as 'no limit'."""
    spend = _ledger(20)
    ok, why = api_mod.affordable(spend, provider=_StubProvider(RateLimitSnapshot()))
    assert not ok
    assert "this hour" in why


def test_a_partial_snapshot_still_consults_the_ledger_for_what_it_omits():
    """The gateway reported requests but said nothing about tokens. Answering
    True on the strength of the half it covered would skip the token ceiling
    entirely -- the dimension that actually binds at ~6 merges/hour."""
    spend = _ledger(6, tokens=4_000)          # 24,000 of the 25,000 token budget
    snapshot = RateLimitSnapshot(requests_remaining=13)   # no tokens_remaining
    ok, why = api_mod.affordable(spend, provider=_StubProvider(snapshot))
    assert not ok
    assert "token budget" in why
