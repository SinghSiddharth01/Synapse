"""A 429 must cost a wait, not the round.

Before this, `_post_rotating` returned the 429 and `raise_for_status()` raised
it — with one key the loop body ran exactly once. The provider's own comment
(aic100.py) recorded a ~36s cooldown it did not handle.

Sleeps are asserted, never slept: `_sleep` is monkeypatched so the suite stays
fast and the backoff schedule itself is what gets pinned.
"""
import httpx
import pytest

from synapse_providers import AIC100Provider, RateLimitedError
from synapse_providers import aic100 as aic100_mod

OK = {"choices": [{"message": {"content": "Pong"}}],
      "usage": {"prompt_tokens": 5, "completion_tokens": 1}}


@pytest.fixture
def slept(monkeypatch):
    recorded: list[float] = []

    async def fake_sleep(seconds: float) -> None:
        recorded.append(seconds)

    monkeypatch.setattr(aic100_mod, "_sleep", fake_sleep)
    return recorded


def _provider(handler, **kw):
    return AIC100Provider(base_url="https://x/apis/v2", api_key="k",
                          transport=httpx.MockTransport(handler), **kw)


async def test_a_429_is_retried_and_can_succeed(slept):
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        if calls["n"] == 1:
            return httpx.Response(429, headers={"retry-after": "12"}, json={})
        return httpx.Response(200, json=OK)

    result = await _provider(handler).complete([{"role": "user", "content": "ping"}])
    assert result.data == "Pong"
    assert calls["n"] == 2
    assert slept == [12.0]          # honoured Retry-After, did not invent one


async def test_backoff_falls_back_to_the_measured_cooldown(slept):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(429, json={})           # no Retry-After header

    with pytest.raises(RateLimitedError):
        await _provider(handler).complete([{"role": "user", "content": "ping"}])
    assert slept and slept[0] == aic100_mod.RATE_LIMIT_DEFAULT_COOLDOWN_S


async def test_sleeping_is_bounded_by_the_budget(slept):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(429, headers={"retry-after": "600"}, json={})

    with pytest.raises(RateLimitedError):
        await _provider(handler).complete([{"role": "user", "content": "ping"}])
    # A gateway asking for 600s must not park us past INFERENCE_CLOUD_TIMEOUT.
    assert sum(slept) <= aic100_mod.RATE_LIMIT_SLEEP_BUDGET_S


async def test_exhausted_retries_raise_a_typed_error_carrying_the_snapshot(slept):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(429, headers={"retry-after": "1",
                                            "x-ratelimit-remaining-requests": "0"},
                              json={})

    with pytest.raises(RateLimitedError) as exc:
        await _provider(handler).complete([{"role": "user", "content": "ping"}])
    assert exc.value.attempts == aic100_mod.RATE_LIMIT_MAX_ATTEMPTS
    assert exc.value.snapshot.requests_remaining == 0


async def test_a_successful_response_still_records_the_snapshot(slept):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, headers={"x-ratelimit-remaining-requests": "13"},
                              json=OK)

    provider = _provider(handler)
    await provider.complete([{"role": "user", "content": "ping"}])
    assert provider.last_rate_limit.requests_remaining == 13


async def test_a_multi_key_pool_rotates_before_it_sleeps(slept, monkeypatch):
    """Rotation is free and a sleep is not, so every key is tried first."""
    monkeypatch.setenv("INFERENCE_CLOUD_API_KEYS", "a,b")
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request.headers["authorization"])
        return httpx.Response(200, json=OK) if len(seen) == 2 else httpx.Response(429, json={})

    await _provider(handler).complete([{"role": "user", "content": "ping"}])
    assert seen == ["Bearer a", "Bearer b"]
    assert slept == []


async def test_requests_are_counted_as_they_are_made(slept):
    """The governor charged 2 requests per round unconditionally, halving a
    20/hour ceiling. Count them where they happen instead -- including the
    rotations and backoff attempts a boolean would have missed."""
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        if calls["n"] < 3:
            return httpx.Response(429, headers={"retry-after": "1"}, json={})
        return httpx.Response(200, json=OK)

    provider = _provider(handler)
    await provider.complete([{"role": "user", "content": "ping"}])
    assert provider.last_request_count == 3


async def test_the_request_count_resets_between_calls(slept):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=OK)

    provider = _provider(handler)
    await provider.complete([{"role": "user", "content": "ping"}])
    await provider.complete([{"role": "user", "content": "ping"}])
    assert provider.last_request_count == 1
