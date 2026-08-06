"""Truncation detection on the OpenAI-shaped path (c077a51, second half).

The warning at `openai_compat.complete()` existed with ZERO tests, and it keyed
on `finish_reason == "length"` alone — the exact anti-pattern
`aic100._was_truncated` was written to kill.

That docstring records the measurement (2026-08-06, aisuite-indonesia): a call
with max_tokens=3000 returned `completion_tokens=3000` and
`finish_reason: "stop"`. Cut off at exactly the cap; the endpoint said it
stopped naturally. And: *"The unit test covering the warning fabricated a
finish_reason='length' fixture the host never sends, so it passed throughout."*

So the load-bearing fixture in this file is the HONEST one — `finish_reason:
"stop"` with `completion_tokens == max_tokens`. A file that only tested
`finish_reason: "length"` would be the fabricated fixture written a second
time: green, and blind to the failure that actually happens.
"""

from __future__ import annotations

import json
import logging

from pytest_httpserver import HTTPServer
from werkzeug.wrappers import Response

from synapse_providers import NPUProvider

SCHEMA = {"type": "object", "properties": {"findings": {"type": "array"}}}
MAX_TOKENS = 500

TRUNCATED = '{"findings": [{"type": "learning", "text": "the reserve is a prom'


def _provider(httpserver: HTTPServer) -> NPUProvider:
    return NPUProvider(
        base_url=httpserver.url_for("/v1"), model="test-model", max_tokens=MAX_TOKENS
    )


def _serve(httpserver: HTTPServer, payload: dict) -> None:
    httpserver.expect_request("/v1/chat/completions").respond_with_handler(
        lambda request: Response(
            json.dumps(payload), status=200, content_type="application/json"
        )
    )


def _body(content: str, *, finish_reason: str, completion_tokens: int) -> dict:
    return {
        "choices": [{"message": {"content": content}, "finish_reason": finish_reason}],
        "usage": {"prompt_tokens": 3500, "completion_tokens": completion_tokens},
    }


def _warnings(caplog) -> list[str]:
    return [
        r.getMessage()
        for r in caplog.records
        if "TRUNCATED" in r.getMessage()
    ]


# --- the honest fixture: the host lies about finish_reason -----------------------


async def test_reaching_the_cap_is_reported_even_when_the_host_says_stop(
    httpserver: HTTPServer, caplog
) -> None:
    """THE test in this file. `finish_reason: "stop"` and
    `completion_tokens == max_tokens` is what a real host sends when it truncates,
    and the `finish_reason`-only check detected exactly none of it.

    Left undetected, every truncation takes the unparseable-garbage path with no
    warning: HTTP 200s in the log, a `dropped_malformed` counter going up, and
    nothing anywhere saying "the budget is too small". That is hours of silent
    failure, and it already happened once on the synthesis side.
    """
    _serve(
        httpserver,
        _body(TRUNCATED, finish_reason="stop", completion_tokens=MAX_TOKENS),
    )

    with caplog.at_level(logging.WARNING, logger="synapse_providers.openai_compat"):
        result = await _provider(httpserver).complete(
            messages=[], response_schema=SCHEMA
        )

    [warning] = _warnings(caplog)
    assert f"completion_tokens={MAX_TOKENS}" in warning
    assert "'stop'" in warning, (
        "the log has to name the disagreement — a host that reports 'stop' on a "
        "truncated response is the finding, not a detail"
    )
    assert str(MAX_TOKENS) in warning
    # The response is still returned, repaired where possible. Detection is a
    # diagnosis, never a discard.
    assert result.data == {
        "findings": [{"type": "learning", "text": "the reserve is a prom"}]
    }


async def test_overshooting_the_cap_counts_as_reaching_it(
    httpserver: HTTPServer, caplog
) -> None:
    """`>=`, not `==`. A host that counts tokens slightly differently from the
    cap it enforced must not fall through the check on an off-by-one."""
    _serve(
        httpserver,
        _body(TRUNCATED, finish_reason="stop", completion_tokens=MAX_TOKENS + 3),
    )

    with caplog.at_level(logging.WARNING, logger="synapse_providers.openai_compat"):
        await _provider(httpserver).complete(messages=[], response_schema=SCHEMA)

    assert len(_warnings(caplog)) == 1


# --- the documented signal still works -------------------------------------------


async def test_the_documented_finish_reason_is_still_honoured(
    httpserver: HTTPServer, caplog
) -> None:
    """A host that reports `length` honestly is detected on that alone, without
    needing usage at all — some bodies carry no `usage` block."""
    _serve(
        httpserver,
        {
            "choices": [
                {"message": {"content": TRUNCATED}, "finish_reason": "length"}
            ]
        },
    )

    with caplog.at_level(logging.WARNING, logger="synapse_providers.openai_compat"):
        await _provider(httpserver).complete(messages=[], response_schema=SCHEMA)

    [warning] = _warnings(caplog)
    assert "finish_reason=length" in warning


# --- and it must not cry wolf ----------------------------------------------------


async def test_a_short_healthy_response_is_not_called_truncated(
    httpserver: HTTPServer, caplog
) -> None:
    """The mirror, and the reason this is not just `if True`. A warning on every
    response is a warning on none — the operator stops reading it, and the real
    truncation goes past unnoticed with the rest."""
    _serve(
        httpserver,
        _body('{"findings": []}', finish_reason="stop", completion_tokens=7),
    )

    with caplog.at_level(logging.WARNING, logger="synapse_providers.openai_compat"):
        await _provider(httpserver).complete(messages=[], response_schema=SCHEMA)

    assert _warnings(caplog) == []


async def test_a_missing_usage_block_does_not_fabricate_a_truncation(
    httpserver: HTTPServer, caplog
) -> None:
    """`usage` absent, or `completion_tokens` absent — neither is evidence of
    anything, and each must leave the check silent rather than guessing."""
    for payload in (
        {"choices": [{"message": {"content": "x"}, "finish_reason": "stop"}]},
        {
            "choices": [{"message": {"content": "x"}, "finish_reason": "stop"}],
            "usage": {},
        },
    ):
        httpserver.clear()
        _serve(httpserver, payload)
        caplog.clear()

        with caplog.at_level(
            logging.WARNING, logger="synapse_providers.openai_compat"
        ):
            await _provider(httpserver).complete(messages=[])

        assert _warnings(caplog) == [], payload


def test_a_non_integer_completion_tokens_is_not_treated_as_a_count() -> None:
    """The `isinstance(emitted, int)` guard, at the method rather than over HTTP.

    `{"completion_tokens": null}` crashes `complete()` further down, in
    `ModelUsage(output_tokens=int(...))` — a separate, pre-existing fragility on
    the usage path that this commit does not touch. The guard here still has to
    hold on its own terms, because `None >= max_tokens` is a TypeError and a
    string `"500"` would compare as neither.
    """
    provider = NPUProvider(base_url="http://unused/v1", max_tokens=MAX_TOKENS)
    choice = {"message": {"content": "x"}, "finish_reason": "stop"}

    for usage in ({"completion_tokens": None}, {"completion_tokens": "500"}, {}):
        assert provider._truncation_signal(choice, {"usage": usage}) is None, usage

    assert provider._truncation_signal(choice, {"usage": None}) is None
