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


class _LimitedProvider(_StubProvider):
    """A provider that has actually been 429'd and is cooling down."""

    def __init__(self, seconds_remaining: float, snapshot=None):
        super().__init__(snapshot)
        self.rate_limited_until = time.monotonic() + seconds_remaining


# ---------------------------------------------------------------------------
# what may refuse: only things OBSERVED  ⟨2026-08-07⟩
# ---------------------------------------------------------------------------

def test_a_heavy_ledger_no_longer_refuses_anything():
    """The demotion. The ledger predicted exhaustion and was wrong twice in one
    night, in both directions: a x2 request charge halved a 20/hour ceiling, and
    a 25,000 token/hour guess stalled synthesis for 45 minutes while the console
    read 1 of 20 requests for the hour. It is an estimate that cannot be
    corrected -- this gateway sends no rate-limit headers at all -- so it is no
    longer allowed to stop work."""
    spend = _ledger(500, spacing_s=5.0, tokens=10_000)   # absurd by any old rule
    ok, why = api_mod.affordable(spend, provider=_StubProvider())
    assert ok, f"the ledger still refused: {why}"


def test_a_real_429_refuses_until_its_cooldown_elapses():
    """The reactive half. Not a prediction -- a 429 that actually happened,
    recorded by the provider where it was received."""
    ok, why = api_mod.affordable([], provider=_LimitedProvider(30))
    assert not ok
    assert "cooling down" in why


def test_the_cooldown_releases_by_itself():
    """Self-healing: once the window passes, the drain re-runs the merge with
    no operator action."""
    ok, why = api_mod.affordable([], provider=_LimitedProvider(-1))
    assert ok, why


def test_reported_exhaustion_still_refuses():
    """Other gateways DO send headers. When one says zero, believe it."""
    ok, why = api_mod.affordable(
        [], provider=_StubProvider(RateLimitSnapshot(requests_remaining=0)))
    assert not ok
    assert "reported" in why


def test_an_empty_snapshot_is_not_a_refusal():
    """Absence of headers is not evidence of exhaustion -- and against this
    provider the snapshot is ALWAYS empty, so treating it as a refusal would
    stop synthesis permanently."""
    ok, why = api_mod.affordable(
        _ledger(200, spacing_s=10.0, tokens=9_000),
        provider=_StubProvider(RateLimitSnapshot()))
    assert ok, why


