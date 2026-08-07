# Rate-Limit Governance Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace the synthesis governor's guessed, systematically over-charged spend ledger with the provider's own reported rate-limit state, and give a real 429 a recovery path instead of an immediate raise.

**Architecture:** Four layers, in dependency order. `AIC100Provider` learns to read rate-limit headers off every response and to retry a 429 with bounded backoff, raising a typed `RateLimitedError` when it genuinely cannot proceed. `api.py`'s `_affordable()` stops double-charging requests, gains per-minute and per-day ceilings to match the provider's real limits, and defers to the provider's reported headroom whenever it has some. `debug.py`'s brain page surfaces the resulting state so a rate-limited service says so instead of failing silently. Finally, a background drain runs the merges that deferral postponed — without it, `deferred: true` is a promise the service has no way to keep.

**Tech Stack:** Python 3.14, `httpx` (with `MockTransport` in tests), Starlette, `pytest` + `pytest-asyncio`, `uv` for the workspace.

## Global Constraints

- **The measured ceilings are per key, per the Cirrascale dashboard (2026-08-06):** 5 requests/minute, 20 requests/hour, 250 requests/day, 25,000 tokens/hour.
- **Observed 429 cooldown is ~36s under load** — recorded in `packages/providers/src/synapse_providers/aic100.py:149`.
- **The synthesis output cap is 1600 tokens** (`SYNTHESIS_MAX_TOKENS` in `server_cli.py:42`), giving 500 working-memory words / 710 verdict tokens / 10 merges per round.
- **`INFERENCE_CLOUD_TIMEOUT` defaults to 180s.** Any backoff added inside a call must be bounded so retries plus generation cannot exceed it.
- **Never widen a budget without checking the pool can pay for it.** `_warn_if_the_key_pool_cannot_pay_for_the_budget` exists because `SYNAPSE_SYNTHESIS_KEYS` multiplies a ceiling with nothing verifying the keys exist. Preserve that check.
- **A deferral must always state its reason and its spender.** The whole point of the governor is that a silent 429 inside the provider reads as "findings landed, memory unchanged". Never trade a loud refusal for a silent one.
- **Ordering is load-bearing, but only for half of Task 3.** Tasks 1–2 make the 429 path visible and recoverable, which is what makes Task **3b** (trusting reported headroom) safe. Task **3a** (charging requests as made, plus the minute/day ceilings) only *corrects* the governor and can ship first with no dependency — see Self-Review.
- **A deferral must be a delay, not a stall.** Nothing may report `deferred: true` without something guaranteeing that merge eventually runs (Task 5).
- Existing tests must keep passing: `test_key_pool_headroom.py`, `test_merge_debounce.py`, `test_query_metering.py`, `test_aic100.py`, `test_synthesis_budget.py`.

---

## File Structure

| File | Responsibility |
|---|---|
| `packages/providers/src/synapse_providers/ratelimit.py` | **New.** `RateLimitSnapshot` dataclass + tolerant header parser. No httpx, no I/O — pure parsing so it is trivially testable. |
| `packages/providers/src/synapse_providers/aic100.py` | Records a snapshot off every response; retries 429 with bounded backoff; raises `RateLimitedError`. |
| `packages/providers/src/synapse_providers/__init__.py` | Exports `RateLimitSnapshot`, `RateLimitedError`. |
| `packages/service/src/synapse_service/api.py` | `_affordable()` — drop the ×2, add minute/day windows, prefer provider-reported headroom. |
| `packages/service/src/synapse_service/debug.py` | Brain-page payload gains a `rate_limit` block. |
| `packages/providers/tests/test_ratelimit.py` | **New.** Header-parsing table. |
| `packages/providers/tests/test_aic100_backoff.py` | **New.** Retry/backoff behaviour. |
| `packages/providers/src/synapse_providers/recording.py` | Carries `last_request_count` per component façade. |
| `packages/service/tests/test_governor_windows.py` | **New.** Minute/hour/day ceilings and the request-charge fix. |
| `packages/service/tests/test_pending_drain.py` | **New.** The deferred-merge drain — that a deferral is a delay, not a stall. |

---

### Task 1: Parse rate-limit headers into a snapshot

The provider currently inspects only `resp.status_code` and throws every header away. The dashboard proves the backend tracks per-minute, per-hour and per-day counters, so the cheapest possible source of truth costs zero extra requests: read it off responses we already make.

**We do not know Cirrascale's exact header spelling from here** (the key lives on the service host). So the parser accepts a table of common aliases and logs the full header set once when nothing matches, turning discovery into a one-line operator task rather than a guess.

**Files:**
- Create: `packages/providers/src/synapse_providers/ratelimit.py`
- Create: `packages/providers/tests/test_ratelimit.py`
- Modify: `packages/providers/src/synapse_providers/__init__.py`

**Interfaces:**
- Produces: `RateLimitSnapshot` (frozen dataclass, fields `requests_remaining: int | None`, `requests_limit: int | None`, `tokens_remaining: int | None`, `tokens_limit: int | None`, `reset_seconds: float | None`, `retry_after_seconds: float | None`, `raw: dict[str, str]`); `RateLimitSnapshot.from_headers(headers: Mapping[str, str]) -> RateLimitSnapshot`; property `is_empty: bool` (True when every parsed field is None).

- [ ] **Step 1: Write the failing test**

Create `packages/providers/tests/test_ratelimit.py`:

```python
"""Rate-limit headers are the only zero-cost source of truth about the key's
remaining headroom. The governor's alternative is ASSUMED_MERGE_TOKENS, which
over-charges in one direction and never reconciles."""

from synapse_providers.ratelimit import RateLimitSnapshot


def test_openai_style_headers_parse():
    snap = RateLimitSnapshot.from_headers({
        "x-ratelimit-limit-requests": "20",
        "x-ratelimit-remaining-requests": "13",
        "x-ratelimit-limit-tokens": "25000",
        "x-ratelimit-remaining-tokens": "18400",
        "x-ratelimit-reset-requests": "42",
    })
    assert snap.requests_limit == 20
    assert snap.requests_remaining == 13
    assert snap.tokens_limit == 25000
    assert snap.tokens_remaining == 18400
    assert snap.reset_seconds == 42.0
    assert not snap.is_empty


def test_header_lookup_is_case_insensitive():
    snap = RateLimitSnapshot.from_headers({"X-RateLimit-Remaining-Requests": "7"})
    assert snap.requests_remaining == 7


def test_retry_after_is_parsed_separately_from_reset():
    snap = RateLimitSnapshot.from_headers({"retry-after": "36"})
    assert snap.retry_after_seconds == 36.0
    assert snap.reset_seconds is None


def test_unknown_headers_yield_an_empty_snapshot_that_keeps_the_raw_set():
    snap = RateLimitSnapshot.from_headers({"x-quota-left": "3", "server": "nginx"})
    assert snap.is_empty
    assert snap.raw == {"x-quota-left": "3", "server": "nginx"}


def test_unparseable_values_are_ignored_not_raised():
    snap = RateLimitSnapshot.from_headers({"x-ratelimit-remaining-requests": "n/a"})
    assert snap.requests_remaining is None
    assert snap.is_empty
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest packages/providers/tests/test_ratelimit.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'synapse_providers.ratelimit'`

- [ ] **Step 3: Write minimal implementation**

Create `packages/providers/src/synapse_providers/ratelimit.py`:

```python
"""Rate-limit headers, parsed tolerantly.

The service's governor exists because a 429 inside the provider is invisible
from the API layer. Headers are the fix that costs nothing: every response
already carries them, and reading them replaces an assumption stack
(ASSUMED_MERGE_TOKENS, ASSUMED_QUERY_TOKENS, "2 requests per round") with a
number the gateway itself reported.

The alias tables exist because the exact spelling on
aisuite-indonesia.cirrascale.com has not been observed. A snapshot that
matches nothing is EMPTY rather than wrong, and `raw` keeps the full header
set so `AIC100Provider` can log it once and end the guessing.
"""
from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field

_REQUESTS_REMAINING = ("x-ratelimit-remaining-requests", "x-ratelimit-remaining",
                       "ratelimit-remaining", "x-rate-limit-remaining")
_REQUESTS_LIMIT = ("x-ratelimit-limit-requests", "x-ratelimit-limit",
                   "ratelimit-limit", "x-rate-limit-limit")
_TOKENS_REMAINING = ("x-ratelimit-remaining-tokens", "x-ratelimit-tokens-remaining")
_TOKENS_LIMIT = ("x-ratelimit-limit-tokens", "x-ratelimit-tokens-limit")
_RESET = ("x-ratelimit-reset-requests", "x-ratelimit-reset", "ratelimit-reset")
_RETRY_AFTER = ("retry-after",)


def _first_number(headers: Mapping[str, str], names: tuple[str, ...]) -> float | None:
    """The first alias that is present AND numeric. A header that exists with an
    unparseable value is treated as absent: a limiter that raises on a gateway's
    formatting choice is worse than one that falls back to the local ledger."""
    for name in names:
        raw = headers.get(name)
        if raw is None:
            continue
        try:
            return float(str(raw).strip())
        except (TypeError, ValueError):
            continue
    return None


@dataclass(frozen=True)
class RateLimitSnapshot:
    requests_remaining: int | None = None
    requests_limit: int | None = None
    tokens_remaining: int | None = None
    tokens_limit: int | None = None
    reset_seconds: float | None = None
    retry_after_seconds: float | None = None
    raw: dict[str, str] = field(default_factory=dict)

    @property
    def is_empty(self) -> bool:
        """True when the gateway told us nothing we understand. Callers must
        fall back to the local ledger rather than treating this as 'no limit'."""
        return all(v is None for v in (
            self.requests_remaining, self.requests_limit,
            self.tokens_remaining, self.tokens_limit,
            self.reset_seconds, self.retry_after_seconds))

    @classmethod
    def from_headers(cls, headers: Mapping[str, str]) -> RateLimitSnapshot:
        lower = {str(k).lower(): v for k, v in headers.items()}

        def as_int(value: float | None) -> int | None:
            return None if value is None else int(value)

        return cls(
            requests_remaining=as_int(_first_number(lower, _REQUESTS_REMAINING)),
            requests_limit=as_int(_first_number(lower, _REQUESTS_LIMIT)),
            tokens_remaining=as_int(_first_number(lower, _TOKENS_REMAINING)),
            tokens_limit=as_int(_first_number(lower, _TOKENS_LIMIT)),
            reset_seconds=_first_number(lower, _RESET),
            retry_after_seconds=_first_number(lower, _RETRY_AFTER),
            raw=dict(lower),
        )
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest packages/providers/tests/test_ratelimit.py -v`
Expected: 5 passed

- [ ] **Step 5: Export from the package**

In `packages/providers/src/synapse_providers/__init__.py`, add `RateLimitSnapshot` to the imports and to `__all__`, following the existing style of that file:

```python
from .ratelimit import RateLimitSnapshot
```

- [ ] **Step 6: Verify the export and commit**

Run: `uv run python -c "from synapse_providers import RateLimitSnapshot; print(RateLimitSnapshot.from_headers({'retry-after': '36'}))"`
Expected: prints a snapshot with `retry_after_seconds=36.0`

```bash
git add packages/providers/src/synapse_providers/ratelimit.py \
        packages/providers/src/synapse_providers/__init__.py \
        packages/providers/tests/test_ratelimit.py
git commit -m "feat(providers): parse rate-limit headers into a tolerant snapshot"
```

---

### Task 2: Retry a 429 with bounded backoff instead of raising immediately

`_post_rotating` with a one-key pool runs `for _ in range(1)`, gets its 429, rotates the index onto itself, returns the 429 response, and the caller's `raise_for_status()` raises. There is no sleep and no second attempt — `aic100.py:149` says so outright: *"429s with a ~36s cooldown under load, which this provider does not yet handle."*

**Files:**
- Modify: `packages/providers/src/synapse_providers/aic100.py` (constructor ~line 152, `_post_rotating` ~line 196)
- Create: `packages/providers/tests/test_aic100_backoff.py`
- Modify: `packages/providers/src/synapse_providers/__init__.py`

**Interfaces:**
- Consumes: `RateLimitSnapshot.from_headers` from Task 1.
- Produces: `RateLimitedError(Exception)` with attributes `snapshot: RateLimitSnapshot` and `attempts: int`; `AIC100Provider.last_rate_limit: RateLimitSnapshot | None`; module constants `RATE_LIMIT_MAX_ATTEMPTS = 3`, `RATE_LIMIT_DEFAULT_COOLDOWN_S = 36.0`, `RATE_LIMIT_SLEEP_BUDGET_S = 45.0`; module-level `_sleep` seam (defaults to `asyncio.sleep`) that tests monkeypatch.

- [ ] **Step 1: Write the failing test**

Create `packages/providers/tests/test_aic100_backoff.py`:

```python
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
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest packages/providers/tests/test_aic100_backoff.py -v`
Expected: FAIL with `ImportError: cannot import name 'RateLimitedError'`

- [ ] **Step 3: Add the error type, constants and sleep seam**

Near the top of `packages/providers/src/synapse_providers/aic100.py`, after the existing imports and `DEFAULT_BASE_URL`:

```python
import asyncio

from .ratelimit import RateLimitSnapshot

# Retries for a 429, and the schedule they follow.
#
# The provider observed "~36s cooldown under load" and did not act on it: with
# a one-key pool `_post_rotating` looped once, returned the 429, and
# `raise_for_status()` raised it. The service's governor was then written to
# avoid ever reaching this path -- refusing work the dashboard showed was
# affordable (1/20 requests used in the hour it refused).
#
# RATE_LIMIT_SLEEP_BUDGET_S is the important one. INFERENCE_CLOUD_TIMEOUT
# defaults to 180s and covers GENERATION; a 70B synthesis was measured at 48.5s
# and 51.7s. Sleeping 3x36s inside that budget would leave a slow round one
# tick from a ReadTimeout -- which synthesis.py reports as the identical
# "findings landed, memory unchanged" symptom. So total sleep is capped
# independently of what the gateway asks for.
RATE_LIMIT_MAX_ATTEMPTS = 3
RATE_LIMIT_DEFAULT_COOLDOWN_S = 36.0
RATE_LIMIT_SLEEP_BUDGET_S = 45.0

# Module-level so tests can replace it without sleeping. Same seam shape as
# `transport=` on the constructor.
_sleep = asyncio.sleep


class RateLimitedError(Exception):
    """Every key 429'd and the retry budget is spent.

    Typed rather than an httpx.HTTPStatusError so `api.py` can tell a real
    exhausted quota from a 5xx and say so on the brain page. The distinction is
    the whole point: an untyped failure here is what "findings landed, memory
    unchanged" looked like from the outside.
    """

    def __init__(self, snapshot: RateLimitSnapshot, attempts: int) -> None:
        detail = ""
        if snapshot.requests_remaining is not None:
            detail = f", {snapshot.requests_remaining} request(s) remaining"
        super().__init__(
            f"rate limited after {attempts} attempt(s){detail}. "
            f"Raise the key pool (INFERENCE_CLOUD_API_KEYS) or wait for the "
            f"window to roll.")
        self.snapshot = snapshot
        self.attempts = attempts
```

- [ ] **Step 4: Initialise the snapshot attribute**

In `AIC100Provider.__init__`, immediately after `self._transport = transport`:

```python
        # The most recent rate-limit headers seen on ANY response, success or
        # 429. Read by the service's governor in preference to its own guessed
        # ledger, and by the brain page to show why synthesis is paused.
        self.last_rate_limit: RateLimitSnapshot | None = None
        self._logged_unknown_headers = False
        # HTTP requests made by the most recent `complete()`. Reset at the top
        # of `complete()`, incremented in `_post_rotating`. `api._record_spend`
        # charges this instead of assuming a constant.
        self.last_request_count = 0
```

And at the very top of `complete()`, before `started = time.perf_counter()`:

```python
        self.last_request_count = 0
```

- [ ] **Step 5: Rewrite `_post_rotating`**

Replace the whole existing `_post_rotating` method with:

```python
    async def _post_rotating(self, client: httpx.AsyncClient, url: str,
                             payload: dict[str, Any]) -> httpx.Response:
        """POST, rotating keys on 429, then waiting and retrying.

        Rotation first, sleeping second: another key is free and a sleep is
        not. Only when EVERY key in the pool has 429'd is the window genuinely
        shut, and only then is waiting the right move.
        """
        attempts = 0
        slept = 0.0
        snapshot = RateLimitSnapshot()
        while attempts < RATE_LIMIT_MAX_ATTEMPTS:
            attempts += 1
            for _ in range(len(self.api_keys)):
                key = self.api_keys[self._key_index]
                # Counted HERE, where requests are actually made. The governor
                # used to assume two per round (the internal schema retry) and
                # charge it unconditionally, which halved a 20/hour ceiling.
                # This counts rotations and backoff attempts too -- every one
                # of them is a real request against the key's quota.
                self.last_request_count += 1
                resp = await client.post(
                    url, json=payload, headers={"Authorization": f"Bearer {key}"})
                snapshot = self._record_rate_limit(resp)
                if resp.status_code != 429:
                    return resp
                self._key_index = (self._key_index + 1) % len(self.api_keys)

            # Every key is limited. Wait, unless waiting would cost more than
            # the budget allows or this was the last attempt.
            if attempts >= RATE_LIMIT_MAX_ATTEMPTS:
                break
            wait = snapshot.retry_after_seconds or RATE_LIMIT_DEFAULT_COOLDOWN_S
            wait = min(wait, RATE_LIMIT_SLEEP_BUDGET_S - slept)
            if wait <= 0:
                break
            logger.warning(
                "All %d key(s) rate limited; waiting %.0fs before attempt %d of %d.",
                len(self.api_keys), wait, attempts + 1, RATE_LIMIT_MAX_ATTEMPTS)
            await _sleep(wait)
            slept += wait

        raise RateLimitedError(snapshot, attempts)

    def _record_rate_limit(self, resp: httpx.Response) -> RateLimitSnapshot:
        """Keep the newest snapshot, and say once when the gateway's header
        spelling is one this parser does not know -- the alias table was
        written without a live response to read."""
        snapshot = RateLimitSnapshot.from_headers(resp.headers)
        self.last_rate_limit = snapshot
        if snapshot.is_empty and not self._logged_unknown_headers:
            self._logged_unknown_headers = True
            logger.info(
                "No recognised rate-limit headers on %s. Headers seen: %s. Add the "
                "right names to synapse_providers.ratelimit if any of these carry "
                "quota.", self.base_url, sorted(snapshot.raw))
        return snapshot
```

- [ ] **Step 5b: Carry the count onto the RecordingProvider façade**

`synthesis_provider` and `retrieval_provider` are two `RecordingProvider`s over ONE provider object, so the inner counter belongs to whichever component called last. Snapshot it per façade. In `packages/providers/src/synapse_providers/recording.py`, in `RecordingProvider.__init__`:

```python
        self.last_request_count = 0
```

and in `complete()`, in BOTH the `except` branch (before `raise`) and immediately after the successful `inner.complete(...)` returns:

```python
        self.last_request_count = getattr(self.inner, "last_request_count", 1)
```

The failure path matters as much as the success path: a round that raised still spent its requests, and that is exactly the spend the governor must not lose track of.

- [ ] **Step 6: Export the error type**

In `packages/providers/src/synapse_providers/__init__.py`, add alongside the Task 1 export:

```python
from .aic100 import RateLimitedError
```

- [ ] **Step 7: Run the new and existing provider tests**

Run: `uv run pytest packages/providers/tests/test_aic100_backoff.py packages/providers/tests/test_aic100.py -v`
Expected: all pass. `test_key_pool_rotates_on_429` still passes — rotation is unchanged, only what happens *after* the pool is exhausted is new.

- [ ] **Step 8: Commit**

```bash
git add packages/providers/src/synapse_providers/aic100.py \
        packages/providers/src/synapse_providers/__init__.py \
        packages/providers/tests/test_aic100_backoff.py
git commit -m "feat(providers): retry 429 with bounded backoff and a typed RateLimitedError"
```

---

### Task 3: Stop double-charging requests, and prefer reported headroom

Two defects in `_affordable()`. First, `(len(_spend) + 1) * 2 > request_budget` charges every round two requests because `AIC100Provider` retries once internally — but that retry only fires **on failure**, so the happy path costs one. A 20/hour ceiling became an effective 10 rounds/hour. Second, the ledger is a local estimate that never reconciles: the dashboard read `1/20` for the hour in which the governor refused.

The fix is not to delete the ledger — it is the only estimate available before the first response, and Task 1's snapshot may be empty if the alias table misses. The fix is to charge honestly and to yield to the provider whenever the provider has spoken.

**Files:**
- Modify: `packages/service/src/synapse_service/api.py` (`_spend` ~286, `_record_spend` ~295, `_record_query_spend` ~327, `_affordable` ~378)
- Create: `packages/service/tests/test_governor_windows.py`

**Interfaces:**
- Consumes: `AIC100Provider.last_rate_limit` (Task 2), `RateLimitSnapshot.is_empty` (Task 1).
- Produces: `_spend` entries become 4-tuples `(monotonic_ts, tokens, component, requests)`; `_affordable() -> tuple[bool, str]` unchanged in signature.

- [ ] **Step 1: Write the failing test**

Create `packages/service/tests/test_governor_windows.py`:

```python
"""The governor must not refuse work the provider would have accepted.

2026-08-06: the Cirrascale dashboard read 1/20 requests for the hour and 5/250
for the day while `_affordable()` was deferring synthesis. Two causes, pinned
here: a x2 request charge that halved the ceiling, and a ledger that never
reconciled against the provider's own reported headroom.
"""
import time

import pytest

from synapse_providers import RateLimitSnapshot

from synapse_service import api as api_mod


class _StubProvider:
    """Only the surface `_affordable` reads."""
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
    charged when it actually happens (Step 4), not assumed on every round."""
    spend = _ledger(15)
    ok, why = api_mod.affordable(spend, provider=_StubProvider())
    assert ok, why


def test_the_hourly_request_ceiling_still_binds():
    spend = _ledger(20)
    ok, why = api_mod.affordable(spend, provider=_StubProvider())
    assert not ok
    assert "20 request(s) this hour" in why


def test_the_per_minute_ceiling_binds_independently_of_the_hour():
    """Six rounds in one minute is nothing against 20/hour and over the 5/min
    limit the governor never modelled."""
    spend = _ledger(6, spacing_s=5.0)
    ok, why = api_mod.affordable(spend, provider=_StubProvider())
    assert not ok
    assert "this minute" in why


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
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest packages/service/tests/test_governor_windows.py -v`
Expected: FAIL with `AttributeError: module 'synapse_service.api' has no attribute 'affordable'`

- [ ] **Step 3: Extract `affordable` as a module-level pure function**

`_affordable` is currently a closure over `_spend`. Extracting it makes the ceilings testable without booting an app, and leaves the closure as a thin caller.

**First add the missing import** — `api.py` imports `Counter` and `Mapping` but not `Any`, which the signature below needs:

```python
from typing import Any
```

Then add at module scope in `api.py`, after `_warn_if_the_key_pool_cannot_pay_for_the_budget`:

```python
def affordable(spend: list[tuple[float, int, str, int]], *,
               provider: Any, now: float | None = None) -> tuple[bool, str]:
    """Whether one more synthesis round fits.

    Two sources, in priority order.

    THE PROVIDER'S OWN HEADERS win when present. The local ledger is an
    estimate built from ASSUMED_MERGE_TOKENS and ASSUMED_QUERY_TOKENS, and it
    over-charges in one direction only -- a failed call is billed the assumed
    cost, and nothing ever credits it back. On 2026-08-06 that drift refused
    synthesis for an hour in which the dashboard recorded ONE request of
    twenty. A number the gateway reported is not an estimate.

    THE LEDGER is the fallback, and it is not optional: `RateLimitSnapshot`
    is empty until the first response, and stays empty if the gateway spells
    its headers in a way `synapse_providers.ratelimit` does not yet know.
    Empty must read as "no information", never as "no limit".
    """
    snapshot = getattr(provider, "last_rate_limit", None)
    if snapshot is None:
        return _affordable_from_ledger(spend, now=now)

    # Reported exhaustion is decisive in the REFUSING direction, per dimension.
    if snapshot.requests_remaining is not None and snapshot.requests_remaining < 1:
        return False, ("provider reported 0 request(s) remaining"
                       + (f", resets in {snapshot.reset_seconds:.0f}s"
                          if snapshot.reset_seconds else ""))
    if (snapshot.tokens_remaining is not None
            and snapshot.tokens_remaining < ASSUMED_MERGE_TOKENS):
        return False, (f"provider reported {snapshot.tokens_remaining} token(s) "
                       f"remaining, below the {ASSUMED_MERGE_TOKENS} one round costs")

    # Reported HEADROOM is decisive only for the dimensions actually reported.
    # A gateway that sends request counts and no token counts must not buy a
    # free pass on the token ceiling -- that is the dimension which binds first
    # at ~6 merges/hour, and skipping it would put us straight back to
    # discovering the limit as a 429.
    if snapshot.requests_remaining is not None and snapshot.tokens_remaining is not None:
        return True, ""

    ok, why = _affordable_from_ledger(spend, now=now,
                                      skip_requests=snapshot.requests_remaining is not None,
                                      skip_tokens=snapshot.tokens_remaining is not None)
    return ok, why


def _affordable_from_ledger(spend: list[tuple[float, int, str, int]], *,
                            now: float | None = None,
                            skip_requests: bool = False,
                            skip_tokens: bool = False) -> tuple[bool, str]:
    """The estimate, used for every dimension the provider did not report.

    Requests are charged AS MADE (the fourth tuple element) rather than at two
    per round. The old `* 2` assumed AIC100Provider's internal retry always
    fires; it fires on failure, and pricing the happy path as a failure halved
    a 20/hour ceiling to 10.

    `skip_requests` / `skip_tokens` let `affordable()` hand back only the half
    it could not answer from live headers, so a partial snapshot neither
    overrides the ledger wholesale nor is ignored.
    """
    now = time.monotonic() if now is None else now
    while spend and spend[0][0] < now - DAY_S:
        spend.pop(0)

    def window(seconds: float) -> list[tuple[float, int, str, int]]:
        return [e for e in spend if e[0] >= now - seconds]

    hour = window(HOUR_S)
    if not skip_tokens:
        tokens_spent = sum(t for _, t, _c, _r in hour)
        merges = [t for _, t, c, _r in hour if c == "synthesis"]
        next_cost = max(merges[-5:] or [ASSUMED_MERGE_TOKENS])
        if tokens_spent + next_cost > SYNTHESIS_TOKENS_PER_HOUR * SYNTHESIS_KEYS:
            return False, (f"token budget: {tokens_spent}+{next_cost} would exceed "
                           f"{SYNTHESIS_TOKENS_PER_HOUR * SYNTHESIS_KEYS}/hour across "
                           f"{SYNTHESIS_KEYS} key(s) ({_spenders(hour)})")

    if skip_requests:
        return True, ""

    for label, seconds, per_key_cap in (
            ("minute", MINUTE_S, SYNTHESIS_REQUESTS_PER_MINUTE),
            ("hour", HOUR_S, SYNTHESIS_REQUESTS_PER_HOUR),
            ("day", DAY_S, SYNTHESIS_REQUESTS_PER_DAY)):
        entries = window(seconds)
        made = sum(r for _, _t, _c, r in entries)
        cap = per_key_cap * SYNTHESIS_KEYS
        if made + 1 > cap:
            return False, (f"request budget: {made} request(s) this {label} against "
                           f"{cap}/{label} ({_spenders(entries)})")
    return True, ""


def _spenders(entries: list[tuple[float, int, str, int]]) -> str:
    counts = Counter(c for _, _t, c, _r in entries)
    return ", ".join(f"{n} {c}" for c, n in sorted(counts.items()))
```

- [ ] **Step 4: Update the constants and the spend recorders**

Replace the `SYNTHESIS_TOKENS_PER_HOUR` / `SYNTHESIS_REQUESTS_PER_HOUR` block's neighbours by adding, just after `SYNTHESIS_REQUESTS_PER_HOUR = 20`:

```python
# The dashboard enforces THREE request ceilings; the governor modelled one.
# Read off the Cirrascale console for Llama-3.3-70B on 2026-08-06.
# MERGE_MIN_INTERVAL_S=60 happens to keep ONE session under the per-minute
# limit, but it is per-session state -- several sessions merging at once could
# breach 5/min while `_affordable` saw nothing wrong.
SYNTHESIS_REQUESTS_PER_MINUTE = 5
SYNTHESIS_REQUESTS_PER_DAY = 250
MINUTE_S = 60.0
HOUR_S = 3600.0
DAY_S = 86_400.0
```

In `_record_spend`, replace the final append with a request-counted one:

```python
        # `requests` is what the round actually cost the key, counted by the
        # provider as it made them (Task 2) rather than assumed. The old `* 2`
        # priced every round as a failure and halved the ceiling.
        #
        # Read off the SYNTHESIS façade, not the shared inner provider:
        # `synthesis_provider` and `retrieval_provider` are two
        # RecordingProviders over one object, and the inner counter is
        # whichever component called most recently.
        requests = max(1, getattr(synthesis_provider, "last_request_count", 1))
        _spend.append((time.monotonic(), tokens, "synthesis", requests))
```

In `_record_query_spend`, replace its append with:

```python
        _spend.append((time.monotonic(), tokens, "retrieval", 1))
```

And replace the body of the `_affordable` closure with a delegation:

```python
    def _affordable() -> tuple[bool, str]:
        """Thin wrapper: the policy lives in the module-level `affordable` so
        it is testable without booting an app."""
        return affordable(_spend, provider=provider)
```

Update the `_spend` declaration's type annotation at line ~286 to `list[tuple[float, int, str, int]]` and extend its comment to say the fourth element is requests actually made.

- [ ] **Step 5: Run the new tests**

Run: `uv run pytest packages/service/tests/test_governor_windows.py -v`
Expected: 5 passed

- [ ] **Step 6: Run the existing governor tests, which pin the old behaviour**

Run: `uv run pytest packages/service/tests/test_merge_debounce.py packages/service/tests/test_query_metering.py packages/service/tests/test_key_pool_headroom.py -v`

`test_merge_debounce.py::test_more_keys_buy_more_rounds` and the metering tests assert against the old 3-tuple and the `* 2` charge. Update them to the 4-tuple and the one-request charge — the behaviour change is the point of this task, so the assertions move with it. Do **not** weaken `test_key_pool_headroom.py`: the pool-vs-`SYNAPSE_SYNTHESIS_KEYS` warning is unaffected and must still fire.

- [ ] **Step 7: Commit**

```bash
git add packages/service/src/synapse_service/api.py \
        packages/service/tests/test_governor_windows.py \
        packages/service/tests/test_merge_debounce.py \
        packages/service/tests/test_query_metering.py
git commit -m "fix(service): charge requests as made and prefer provider-reported headroom"
```

---

### Task 4: Surface rate-limit state on the brain page

The user-visible complaint that started this: *"there is no indication that this is happening, so it is such a silent failure that it is kind of hard to debug."* The brain page is already the state view (`debug.py`: "the top level shows the state of the brain, not the log"), and `_brain_payload` is its only data source.

**Files:**
- Modify: `packages/service/src/synapse_service/debug.py` (`_brain_payload` ~510, `debug_routes` ~543)
- Modify: `packages/service/tests/test_debug_brain.py`

**Interfaces:**
- Consumes: `AIC100Provider.last_rate_limit` (Task 2), `affordable()` (Task 3).
- Produces: `_brain_payload(...)` gains a top-level `"rate_limit"` key: `{"state": "ok" | "throttled" | "unknown", "requests_remaining": int | None, "tokens_remaining": int | None, "reset_seconds": float | None, "reason": str}`.

- [ ] **Step 1: Write the failing test**

Append to `packages/service/tests/test_debug_brain.py`:

```python
def test_brain_payload_reports_throttled_when_the_provider_says_zero():
    """A rate-limited service must SAY so. The 2026-08-06 failure was
    invisible from every surface: findings landed, memory did not move, and
    nothing anywhere said why."""
    from synapse_providers import RateLimitSnapshot
    from synapse_service.debug import _rate_limit_panel

    panel = _rate_limit_panel(_StubProvider(
        RateLimitSnapshot(requests_remaining=0, reset_seconds=42)))
    assert panel["state"] == "throttled"
    assert panel["requests_remaining"] == 0
    assert panel["reset_seconds"] == 42
    assert "0 request" in panel["reason"]


def test_brain_payload_reports_ok_with_headroom():
    from synapse_providers import RateLimitSnapshot
    from synapse_service.debug import _rate_limit_panel

    panel = _rate_limit_panel(_StubProvider(
        RateLimitSnapshot(requests_remaining=13, tokens_remaining=18_000)))
    assert panel["state"] == "ok"


def test_brain_payload_reports_unknown_when_nothing_was_reported():
    """Absence of headers is not evidence of headroom."""
    from synapse_service.debug import _rate_limit_panel

    assert _rate_limit_panel(_StubProvider(None))["state"] == "unknown"
```

Add near the top of that file, if no equivalent stub exists:

```python
class _StubProvider:
    def __init__(self, snapshot):
        self.last_rate_limit = snapshot
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest packages/service/tests/test_debug_brain.py -k rate_limit -v`
Expected: FAIL with `ImportError: cannot import name '_rate_limit_panel'`

- [ ] **Step 3: Implement the panel**

Add to `packages/service/src/synapse_service/debug.py`, in the derivations section near `_working_memory`:

```python
def _rate_limit_panel(provider: Any) -> dict[str, Any]:
    """What the synthesis key has left, as the gateway last reported it.

    Three states, and "unknown" is not "ok": the snapshot is empty until the
    first response and stays empty if the gateway's header spelling is one
    `synapse_providers.ratelimit` does not know. Showing headroom we cannot
    see would recreate the silent failure this panel exists to end.
    """
    snapshot = getattr(provider, "last_rate_limit", None)
    if snapshot is None or snapshot.is_empty:
        return {"state": "unknown", "requests_remaining": None,
                "tokens_remaining": None, "reset_seconds": None,
                "reason": "no rate-limit headers seen from the provider yet"}

    throttled = (snapshot.requests_remaining is not None
                 and snapshot.requests_remaining < 1)
    if throttled:
        reset = (f", resets in {snapshot.reset_seconds:.0f}s"
                 if snapshot.reset_seconds else "")
        reason = f"provider reported 0 request(s) remaining{reset}"
    else:
        reason = "headroom reported by the provider"
    return {"state": "throttled" if throttled else "ok",
            "requests_remaining": snapshot.requests_remaining,
            "tokens_remaining": snapshot.tokens_remaining,
            "reset_seconds": snapshot.reset_seconds,
            "reason": reason}
```

- [ ] **Step 4: Wire it into the payload**

`_brain_payload` and `debug_routes` must receive the provider. Add a `provider: Any = None` keyword parameter to both, thread it from `build_app`'s `debug_routes(...)` call site in `api.py`, and add to the dict `_brain_payload` returns:

```python
        "rate_limit": _rate_limit_panel(provider),
```

- [ ] **Step 5: Render it on the page**

In the brain page's HTML/JS template, add a row beside the working-memory block, following the existing markup style in that file:

```html
<div class="row" id="rate-limit">
  <span class="label">synthesis key</span>
  <span class="value" data-state=""></span>
</div>
```

and in the page's JS, where the other fields are populated from `brain_json`:

```javascript
const rl = data.rate_limit || {state: "unknown", reason: ""};
const el = document.querySelector("#rate-limit .value");
el.dataset.state = rl.state;
el.textContent = rl.state === "ok"
  ? `ok — ${rl.requests_remaining ?? "?"} request(s) left`
  : rl.state === "throttled" ? `RATE LIMITED — ${rl.reason}` : `unknown — ${rl.reason}`;
```

- [ ] **Step 6: Run the debug tests**

Run: `uv run pytest packages/service/tests/test_debug_brain.py packages/service/tests/test_service_brain_page_js.py -v`
Expected: all pass. `test_service_brain_page_js.py` asserts on the page's JS — extend it if it enumerates fields.

- [ ] **Step 7: Commit**

```bash
git add packages/service/src/synapse_service/debug.py \
        packages/service/src/synapse_service/api.py \
        packages/service/tests/test_debug_brain.py \
        packages/service/tests/test_service_brain_page_js.py
git commit -m "feat(service): surface synthesis-key rate-limit state on the brain page"
```

---

### Task 5: Drain deferred merges on a timer — the actual recovery mechanism

**This is the task that fixes the reported symptom.** Everything above makes deferral *correct* and *visible*; none of it makes a deferred merge ever run.

`api.py` contains no `create_task`, no `BackgroundTask`, no scheduler. A deferred merge is retried **only by the next push to that session**. A session that goes quiet immediately after a deferral keeps its stale working memory indefinitely — which is exactly the observed failure: findings pushed at 23:03 and 23:10, memory frozen at 22:57. "Deferred" currently means "waiting for a push that may never come".

`POST /synthesize` already does the work; nothing calls it automatically.

**Files:**
- Modify: `packages/service/src/synapse_service/api.py` (`build_app`, near `_pending` ~264)
- Create: `packages/service/tests/test_pending_drain.py`

**Interfaces:**
- Consumes: `_pending`, `_last_merge`, `_affordable()`, `synthesizer.merge`.
- Produces: `drain_pending(store, synthesizer, pending, last_merge, affordable, interval_s, now) -> list[str]` — module-level, returns the session ids merged. Pure enough to test without a running loop; the app owns the loop that calls it.

- [ ] **Step 1: Write the failing test**

Create `packages/service/tests/test_pending_drain.py`:

```python
"""A deferral must be a delay, not a stall.

2026-08-06: findings pushed at 23:03 and 23:10 against a working memory last
written at 22:57. Both pushes deferred, and nothing retried them -- `_pending`
is drained only by the NEXT push, so a session that goes quiet stays stale
forever. `POST /synthesize` already does the work; nothing called it.
"""
import pytest

from synapse_service.api import drain_pending


class _Synth:
    def __init__(self):
        self.merged: list[str] = []

    async def merge(self, store, sid, findings):
        self.merged.append(sid)


async def test_a_session_past_the_interval_is_drained():
    synth = _Synth()
    pending = {"sh-1": ["f"]}
    merged = await drain_pending(
        store=None, synthesizer=synth, pending=pending,
        last_merge={"sh-1": 0.0}, affordable=lambda: (True, ""),
        interval_s=60.0, now=120.0)
    assert merged == ["sh-1"]
    assert synth.merged == ["sh-1"]
    assert pending == {}                      # drained, not re-queued


async def test_a_session_inside_the_interval_is_left_alone():
    synth = _Synth()
    merged = await drain_pending(
        store=None, synthesizer=_Synth(), pending={"sh-1": ["f"]},
        last_merge={"sh-1": 100.0}, affordable=lambda: (True, ""),
        interval_s=60.0, now=120.0)
    assert merged == []


async def test_an_unaffordable_drain_keeps_the_findings_pending():
    """Budget refusal must not consume the backlog -- the whole point of
    `_pending` is that nothing is lost by waiting."""
    pending = {"sh-1": ["f"]}
    merged = await drain_pending(
        store=None, synthesizer=_Synth(), pending=pending,
        last_merge={"sh-1": 0.0}, affordable=lambda: (False, "token budget"),
        interval_s=60.0, now=120.0)
    assert merged == []
    assert pending == {"sh-1": ["f"]}


async def test_a_session_with_nothing_pending_is_skipped():
    merged = await drain_pending(
        store=None, synthesizer=_Synth(), pending={"sh-1": []},
        last_merge={}, affordable=lambda: (True, ""),
        interval_s=60.0, now=120.0)
    assert merged == []


async def test_one_failing_session_does_not_block_the_others():
    class _Boom(_Synth):
        async def merge(self, store, sid, findings):
            if sid == "sh-1":
                raise RuntimeError("provider down")
            self.merged.append(sid)

    synth = _Boom()
    merged = await drain_pending(
        store=None, synthesizer=synth, pending={"sh-1": ["f"], "sh-2": ["g"]},
        last_merge={"sh-1": 0.0, "sh-2": 0.0}, affordable=lambda: (True, ""),
        interval_s=60.0, now=120.0)
    assert merged == ["sh-2"]
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest packages/service/tests/test_pending_drain.py -v`
Expected: FAIL with `ImportError: cannot import name 'drain_pending'`

- [ ] **Step 3: Implement the drain**

Add at module scope in `api.py`:

```python
async def drain_pending(*, store, synthesizer, pending: dict[str, list],
                        last_merge: dict[str, float],
                        affordable, interval_s: float, now: float) -> list[str]:
    """Run the merges that deferral postponed.

    Without this, `deferred: true` is a promise the service cannot keep: the
    only thing that drains `_pending` is the next push to the SAME session, so
    a burst that ends in a deferral leaves the working memory stale until
    someone happens to push again. Findings stay queryable throughout -- the
    stall is confined to the synthesized memory -- which is why this went
    unnoticed for 40 minutes twice.

    A failure here is logged and skipped, never fatal: this runs on a
    background loop, and one sick session must not stop every other session's
    memory from catching up. The findings stay in `pending` and the next tick
    retries them.
    """
    merged: list[str] = []
    for sid in list(pending):
        findings = pending.get(sid) or []
        if not findings:
            continue
        last = last_merge.get(sid)
        if last is not None and (now - last) < interval_s:
            continue
        ok, why = affordable()
        if not ok:
            logger.info("Drain for %s held on budget (%s); %d finding(s) still "
                        "pending.", sid, why, len(findings))
            continue
        try:
            await synthesizer.merge(store, sid, findings)
        except Exception:
            logger.exception("Drain for %s failed; %d finding(s) stay pending "
                             "for the next tick.", sid, len(findings))
            continue
        pending.pop(sid, None)
        last_merge[sid] = now
        merged.append(sid)
    return merged
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest packages/service/tests/test_pending_drain.py -v`
Expected: 5 passed

- [ ] **Step 5: Start the loop with the app**

In `build_app`, after the `_pending` declaration, add the background task and wire it to Starlette's lifespan so it stops cleanly:

```python
    # The drain interval is the debounce floor: a deferral is answered no later
    # than one interval after it becomes affordable. Opt out with
    # SYNAPSE_DRAIN_DISABLED=1 -- tests that assert on exact merge counts want
    # a service that only merges when pushed.
    drain_enabled = os.environ.get("SYNAPSE_DRAIN_DISABLED") != "1"

    async def _drain_loop() -> None:
        while True:
            await asyncio.sleep(interval_s)
            try:
                await drain_pending(
                    store=store, synthesizer=synthesizer, pending=_pending,
                    last_merge=_last_merge, affordable=_affordable,
                    interval_s=interval_s, now=time.monotonic())
            except Exception:
                logger.exception("Pending drain tick failed; continuing.")

    @asynccontextmanager
    async def _lifespan(app):
        task = asyncio.create_task(_drain_loop()) if drain_enabled else None
        try:
            yield
        finally:
            if task is not None:
                task.cancel()
                with suppress(asyncio.CancelledError):
                    await task
```

Pass `lifespan=_lifespan` to the `Starlette(...)` constructor. Add `import asyncio`, `from contextlib import asynccontextmanager, suppress` at the top of the file if absent.

- [ ] **Step 6: Verify the whole service suite still passes**

Run: `uv run pytest packages/service/tests/ -q`
Expected: green. If any test now sees an extra merge it did not expect, set `SYNAPSE_DRAIN_DISABLED=1` in that test's environment rather than weakening the drain.

- [ ] **Step 7: Commit**

```bash
git add packages/service/src/synapse_service/api.py \
        packages/service/tests/test_pending_drain.py
git commit -m "fix(service): drain deferred merges on a timer so a deferral is a delay, not a stall"
```

---

### Task 6: Full-suite verification and the operator note

**Files:**
- Modify: `docs/plans/2026-08-06-rate-limit-governance.md` (tick the boxes)
- Modify: `README.md` (operator-facing note)

- [ ] **Step 1: Run the entire suite**

Run: `uv run pytest -q`
Expected: green. The baseline before this plan was 1272 tests; this plan adds roughly 19.

- [ ] **Step 2: Verify the header alias table against the live gateway**

The one thing no test can settle: whether `synapse_providers.ratelimit`'s aliases match what Cirrascale actually sends. After deploying, check the service log for the Task 2 discovery line:

```
No recognised rate-limit headers on https://aisuite-indonesia.cirrascale.com/apis/v2.
Headers seen: [...]
```

If it appears, add the real header names to the alias tuples in `ratelimit.py` and re-run `test_ratelimit.py` with a case for them. If it does not appear, the tables are right and the governor is now reading live quota.

- [ ] **Step 3: Document the knobs in README.md**

Add to the configuration section:

```markdown
### Synthesis rate limits

The synthesis key is rate limited per key: 5 requests/minute, 20/hour,
250/day, 25,000 tokens/hour (Cirrascale, Llama-3.3-70B). The service reads the
gateway's own `X-RateLimit-*` headers and only falls back to its internal
estimate when the gateway reports nothing. A 429 is retried up to 3 times with
bounded backoff (~36s, capped at 45s total) before synthesis defers.

Current headroom is shown on the brain page as `synthesis key`. A `RATE
LIMITED` reading there is the answer to "findings are landing but the working
memory is not moving".

- `INFERENCE_CLOUD_API_KEYS` — comma-separated pool; rotation happens on 429
  before any waiting, so more keys means less waiting.
- `SYNAPSE_SYNTHESIS_KEYS` — must match the pool size. Setting it higher
  authorises budget the pool cannot pay for; the service warns at boot.
```

- [ ] **Step 4: Commit**

```bash
git add docs/plans/2026-08-06-rate-limit-governance.md README.md
git commit -m "docs: record the rate-limit governance plan and its operator knobs"
```

---

## Self-Review

**Spec coverage.** All four requested changes plus the GUI ask are covered: header-based reconciliation (Tasks 1, 3), retry with backoff (Task 2), per-minute/day ceilings (Task 3 Step 4), governor demoted to a fallback behind reported headroom (Task 3 Step 3), and the silent-failure complaint answered by Task 4.

**Ordering.** Tasks 1–2 land the visibility and recovery before Task 3 relaxes the governor, per the Global Constraints. Reversing them would regress to the silent 429.

**Type consistency.** `RateLimitSnapshot` fields are used identically in `ratelimit.py`, `aic100.py`, `api.py` and `debug.py`. The `_spend` tuple is 4 elements everywhere after Task 3 Step 4 — `_record_spend`, `_record_query_spend`, `_affordable_from_ledger` and `_spenders` all agree.

**Splitting Task 3 if you want value sooner.** Task 3 contains two independent changes with different risk profiles:

- **3a — the arithmetic (`* 2` → charge as made) and the minute/day ceilings — has no dependency on Tasks 1–2 and can ship first.** It *corrects* the governor rather than removing it: every call still defers before reaching a 429, so there is no silent-failure exposure. It alone takes the effective ceiling from 10 to 20 rounds/hour.
- **3b — preferring provider-reported headroom — genuinely allows calls to reach a 429**, and so requires Task 2's retry and Task 4's visibility underneath it.

If only one thing ships, make it Task 5. Tasks 1–4 make deferral correct and visible; Task 5 is the only one that makes a deferred merge ever run.

**Second review pass — three defects found and fixed in the plan itself:**

1. **A test that would have failed on first run.** Task 3's `_ledger` helper stamped every entry at `now`, putting all of them inside the per-minute window too. Since the ceilings are checked minute → hour → day, a 15-round ledger tripped the 5/minute limit and `test_a_round_is_charged_one_request_not_two` asserted `ok` against a `False`. Worse, the two tests that *passed* passed on the minute ceiling while claiming to test the hourly one. The helper now spreads entries backwards at a configurable spacing, and there are explicit per-minute and per-day cases.
2. **`affordable()` short-circuited on a partial snapshot.** A gateway reporting `requests_remaining` but no `tokens_remaining` returned `True` without ever consulting the token ledger — the dimension that binds first, at ~6 merges/hour. Reported exhaustion is now decisive per dimension, but reported *headroom* only short-circuits the dimensions actually reported; the rest falls through to the ledger via `skip_requests` / `skip_tokens`.
3. **`Any` was not imported** in `api.py` (it has `Counter` and `Mapping` only). Added to Task 3 Step 3.

**Verified against the source, not assumed:** `Starlette(routes=routes)` at api.py:1051 passes no `lifespan`, so Task 5's is a clean addition; `Counter` is already imported for `_spenders`; `RecordingProvider` is already imported at api.py:25, so Task 3's `synthesis_provider` reference resolves.

**Resolved during review:** an earlier draft charged retries via a `last_call_retried` boolean on `Synthesizer`, which does not exist (it exposes `last_call_made` and `last_usage`). That was the wrong layer — the retry happens inside `AIC100Provider`, which `Synthesizer` cannot see. Requests are now counted where they are made (Task 2 Step 5) and carried per-component by `RecordingProvider` (Task 2 Step 5b), which also captures key rotations and backoff attempts that a boolean would have missed entirely.
