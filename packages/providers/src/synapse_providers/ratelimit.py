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
