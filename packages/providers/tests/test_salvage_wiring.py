"""The salvage DECISION, driven through `complete()` (c077a51, first half).

`test_openai_compat.py` covers `_salvage_partial` well — but it calls the
private helper directly, with hand-built `httpx.Response` objects. That leaves
the thing the commit actually changed untested:

    payload = _salvage_partial(response) if response.is_error else None
    if payload is None:
        response.raise_for_status()

Revert that one line to a bare `raise_for_status()` and `_salvage_partial`
stays defined, its two tests stay green, and the shipped behaviour returns
completely to the bug — a 400 carrying two good findings raises and the
findings are gone. A test that cannot fail when the fix is removed is worse
than no test (ADR 0005), so these drive real HTTP through the real client and
assert on what `complete()` returns or raises.

The decision has two directions and both are load-bearing. Salvaging an error
that carries output is the fix; NOT swallowing an error that carries nothing is
what keeps salvage from becoming a way to continue on a phantom response.
"""

from __future__ import annotations

import json
import logging

import httpx
import pytest
from pytest_httpserver import HTTPServer
from werkzeug.wrappers import Response

from synapse_providers import NPUProvider

SCHEMA = {"type": "object", "properties": {"findings": {"type": "array"}}}

# The body GenieX returned on 2026-08-06 when the prompt overran the 4096
# ceiling: an error code, and the completion the model had already produced —
# two whole findings and part of a third.
OVERFLOW_CONTENT = (
    '{"findings": [{"type": "learning", "text": "geniex counts prompt plus '
    'generation against one 4096 ceiling"}, {"type": "learning", "text": "the '
    'reserve is a promise the segmenter already kept"}, {"type": "open_q'
)
OVERFLOW_BODY = {
    "choices": [
        {"finish_reason": "length", "message": {"content": OVERFLOW_CONTENT}}
    ],
    "error": {"code": "context_length_exceeded"},
}


def _provider(httpserver: HTTPServer) -> NPUProvider:
    return NPUProvider(
        base_url=httpserver.url_for("/v1"), model="test-model", max_tokens=500
    )


def _serve(httpserver: HTTPServer, payload: object, status: int) -> None:
    body = payload if isinstance(payload, str) else json.dumps(payload)
    httpserver.expect_request("/v1/chat/completions").respond_with_handler(
        lambda request: Response(
            body, status=status, content_type="application/json"
        )
    )


def _salvage_logs(caplog) -> list[str]:
    return [r.getMessage() for r in caplog.records if "carries" in r.getMessage()]


# --- salvage: the error that carries output --------------------------------------


async def test_an_overflow_400_returns_its_findings_through_complete(
    httpserver: HTTPServer,
) -> None:
    """THE test this file exists for. Reverting the salvage line turns this into
    an `httpx.HTTPStatusError` and the two good findings are lost, which is
    exactly the shipped bug — and it is invisible to every test that calls
    `_salvage_partial` directly.
    """
    _serve(httpserver, OVERFLOW_BODY, 400)

    result = await _provider(httpserver).complete(
        messages=[{"role": "user", "content": "distil this"}],
        response_schema=SCHEMA,
    )

    assert [f["text"] for f in result.data["findings"] if "text" in f] == [
        "geniex counts prompt plus generation against one 4096 ceiling",
        "the reserve is a promise the segmenter already kept",
    ]
    # The repair ran, so the fragment parsed — `schema_valid` is the distiller's
    # signal that the payload is usable, and salvage must not lie about it.
    assert result.schema_valid is True


async def test_a_salvaged_response_is_not_reported_as_a_healthy_one(
    httpserver: HTTPServer, caplog
) -> None:
    """Salvage is a recovery, not a success. The warning naming the status and
    the error code is the only place an operator learns the request failed at
    all — the caller now gets a ModelResult and sees nothing else amiss.
    """
    _serve(httpserver, OVERFLOW_BODY, 400)

    with caplog.at_level(logging.WARNING, logger="synapse_providers.openai_compat"):
        await _provider(httpserver).complete(messages=[], response_schema=SCHEMA)

    [log] = _salvage_logs(caplog)
    assert "HTTP 400" in log
    assert "context_length_exceeded" in log
    assert str(len(OVERFLOW_CONTENT)) in log


async def test_an_error_body_with_no_error_code_still_salvages(
    httpserver: HTTPServer, caplog
) -> None:
    """The `(payload.get("error") or {}).get(...)` fallback. A host that returns
    a bare status with output and no error object must not crash the recovery —
    losing the findings to a KeyError is the same outcome as not salvaging."""
    _serve(httpserver, {"choices": OVERFLOW_BODY["choices"]}, 400)

    with caplog.at_level(logging.WARNING, logger="synapse_providers.openai_compat"):
        result = await _provider(httpserver).complete(
            messages=[], response_schema=SCHEMA
        )

    assert len(result.data["findings"]) == 3
    assert "no error code" in _salvage_logs(caplog)[0]


@pytest.mark.parametrize("status", [400, 413, 422, 500, 503])
async def test_output_is_recovered_at_every_status_a_host_might_use(
    httpserver: HTTPServer, status: int
) -> None:
    """`response.is_error` covers 4xx AND 5xx, and the guard is the status class
    rather than a list of codes. Swept because which code a host picks for
    "your prompt was too long" is not something we control — GenieX said 400,
    and a 413 or a 422 carrying the same body must not lose the findings.
    """
    _serve(httpserver, OVERFLOW_BODY, status)

    result = await _provider(httpserver).complete(messages=[], response_schema=SCHEMA)

    assert len(result.data["findings"]) == 3


# --- raise: the error that carries nothing ---------------------------------------


@pytest.mark.parametrize(
    ("label", "body", "status"),
    [
        ("bare error object", {"error": {"code": "context_length_exceeded"}}, 400),
        ("non-JSON 500", "upstream exploded", 500),
        ("empty completion", {"choices": [{"message": {"content": ""}}]}, 400),
        ("null completion", {"choices": [{"message": {"content": None}}]}, 400),
        ("no choices at all", {"choices": []}, 502),
    ],
)
async def test_an_error_with_nothing_to_recover_still_raises_through_complete(
    httpserver: HTTPServer, label: str, body: object, status: int
) -> None:
    """The other half of the decision, and the one that keeps salvage honest.

    If `_salvage_partial` returning None did not fall through to
    `raise_for_status()`, a 500 would proceed into `payload["choices"][0]` and
    surface as a KeyError, or worse, as an empty finding list indistinguishable
    from "the model found nothing durable". The caller must still see the
    upstream failure as an upstream failure.
    """
    _serve(httpserver, body, status)

    with pytest.raises(httpx.HTTPStatusError) as caught:
        await _provider(httpserver).complete(messages=[], response_schema=SCHEMA)

    assert caught.value.response.status_code == status, label


# --- and a healthy response never goes near salvage ------------------------------


async def test_a_200_is_never_routed_through_salvage(
    httpserver: HTTPServer, caplog
) -> None:
    """`if response.is_error` — the ordinary path must not pay for the recovery
    path, and must not log a warning about an error that did not happen. A
    salvage notice on every healthy call is a notice nobody reads."""
    _serve(
        httpserver,
        {
            "choices": [
                {
                    "finish_reason": "stop",
                    "message": {"content": '{"findings": []}'},
                }
            ],
            "usage": {"prompt_tokens": 3500, "completion_tokens": 7},
        },
        200,
    )

    with caplog.at_level(logging.WARNING, logger="synapse_providers.openai_compat"):
        result = await _provider(httpserver).complete(
            messages=[], response_schema=SCHEMA
        )

    assert result.data == {"findings": []}
    assert _salvage_logs(caplog) == []


async def test_a_200_whose_body_is_unparseable_is_not_rescued_by_salvage(
    httpserver: HTTPServer,
) -> None:
    """The boundary in the other direction. Salvage keys on the STATUS, so a
    healthy-status response with a broken body still fails as it always did —
    salvage widened what survives an error, not what counts as a response."""
    _serve(httpserver, "not json at all", 200)

    with pytest.raises(json.JSONDecodeError):
        await _provider(httpserver).complete(messages=[], response_schema=SCHEMA)
