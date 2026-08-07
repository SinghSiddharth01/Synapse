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
