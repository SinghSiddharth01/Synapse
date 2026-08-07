"""Which HTTP path the chosen synthesizer actually speaks (282fd07).

`test_cli_provider.py` asserts `isinstance(provider, NPUProvider)`. That is a
real discriminator — AIC100Provider is a direct ModelProvider subclass, not an
OpenAICompatibleProvider — but it is a stand-in for the invariant, and the
invariant is not a type name. The bug was an ENDPOINT:

    `geniex serve` exposes /chat/completions, /v1/models and /v1/models/{model}.
    It has no /completions route at all.
    AIC100Provider posts to /completions BY DESIGN (Cirrascale's
    /chat/completions eats emitted JSON into empty tool_calls).

So aic100-against-GenieX is a 410 Gone on every synthesis call, which
`retrieval.query_findings()` catches and fails closed — observed live
2026-08-06 as a host answering queries with an empty findings list and a 200
while its own /debug dashboard listed the findings. Nothing raised, nothing
logged as an error, and the demo would simply have had no memory.

These tests put a real HTTP server behind `_provider()`'s output and assert on
the path that arrives. A type assertion survives someone changing which
endpoint a provider posts to; this does not.

The other two arms — `aic100` and `anthropic` — had no coverage at all. The
commit message's own indictment ("`_provider()` had no test coverage at all,
which is how a synthesizer that could not work against the NPU shipped")
applied verbatim to half of it.
"""

from __future__ import annotations

import json

import pytest
from pytest_httpserver import HTTPServer
from werkzeug.wrappers import Response

from synapse_service.cli import _provider

SCHEMA = {"type": "object", "properties": {"verdict": {"type": "string"}}}
ANSWER = {"verdict": "merged"}


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    """Every environment variable either arm reads, cleared. A stray
    INFERENCE_CLOUD_BASE_URL in the runner's environment would send these
    requests to a real host."""
    for key in (
        "SYNAPSE_SYNTHESIZER", "SYNAPSE_BASE_URL", "SYNAPSE_MODEL",
        "INFERENCE_CLOUD_BASE_URL", "INFERENCE_CLOUD_MODEL",
        "INFERENCE_CLOUD_API_KEY", "INFERENCE_CLOUD_API_KEYS",
        "INFERENCE_CLOUD_MAX_TOKENS", "INFERENCE_CLOUD_TIMEOUT",
        "SYNAPSE_ANTHROPIC_MODEL",
    ):
        monkeypatch.delenv(key, raising=False)


def _serve_both_shapes(httpserver: HTTPServer) -> None:
    """Answer /chat/completions and /completions, each in its own response
    shape. Both are registered so the test cannot pass merely because one path
    404s — the server is willing to answer either, and which one the provider
    chooses is the finding.

    ⟨merge 2026-08-06, W1⟩ /v1/models is registered too, and is NOT one of the
    two shapes under test. `_provider()`'s `npu` arm now probes it once at boot
    (decision 005's preflight): a service pointed at a GenieX that is not there
    exits 2 instead of coming up and 503ing every call for as long as it runs.
    Without this route these tests would be asserting that a DEAD seam is
    refused, which is a different — already covered — fact. `_paths` drops the
    probe so every assertion below still reads as "which endpoint did the
    synthesis call go to".
    """
    httpserver.expect_request("/v1/models").respond_with_handler(
        lambda r: Response(json.dumps({"data": [{"id": "stand-in"}]}),
                           status=200, content_type="application/json"))
    httpserver.expect_request("/v1/chat/completions").respond_with_handler(
        lambda r: Response(
            json.dumps(
                {
                    "choices": [
                        {
                            "message": {"content": json.dumps(ANSWER)},
                            "finish_reason": "stop",
                        }
                    ],
                    "usage": {"prompt_tokens": 10, "completion_tokens": 5},
                }
            ),
            status=200,
            content_type="application/json",
        )
    )
    httpserver.expect_request("/v1/completions").respond_with_handler(
        lambda r: Response(
            json.dumps(
                {
                    "choices": [{"text": json.dumps(ANSWER), "finish_reason": "stop"}],
                    "usage": {"prompt_tokens": 10, "completion_tokens": 5},
                }
            ),
            status=200,
            content_type="application/json",
        )
    )


def _paths(httpserver: HTTPServer) -> list[str]:
    """The SYNTHESIS paths only. `/v1/models` is the boot preflight, not a
    completion, and counting it here would make every assertion in this file
    about the probe rather than about the endpoint the provider posts to."""
    return [request.path for request, _ in httpserver.log
            if request.path != "/v1/models"]


def _completion_requests(httpserver: HTTPServer) -> list:
    return [request for request, _ in httpserver.log
            if request.path != "/v1/models"]


# --- the invariant, as an endpoint -----------------------------------------------

async def test_the_npu_arm_posts_to_chat_completions_and_never_to_completions(
    httpserver: HTTPServer, monkeypatch
) -> None:
    """THE assertion this file exists for. Reverting the `npu` arm drops through
    to `aic100`, which posts to /completions — the route GenieX does not serve.
    """
    monkeypatch.setenv("SYNAPSE_SYNTHESIZER", "npu")
    monkeypatch.setenv("SYNAPSE_BASE_URL", httpserver.url_for("/v1"))
    _serve_both_shapes(httpserver)

    result = await _provider().complete(
        messages=[{"role": "user", "content": "merge these"}], response_schema=SCHEMA
    )

    assert _paths(httpserver) == ["/v1/chat/completions"]
    assert "/v1/completions" not in _paths(httpserver), (
        "the route `geniex serve` does not have — a 410 on every synthesis call"
    )
    assert result.data == ANSWER


async def test_the_aic100_arm_posts_to_completions_which_is_why_npu_needed_its_own(
    httpserver: HTTPServer, monkeypatch
) -> None:
    """The other half of the same fact, and the first coverage this arm has had.

    Not an incidental difference: aic100 routes around /chat/completions
    deliberately, because Cirrascale's version eats emitted JSON into empty
    tool_calls. Both behaviours are correct for their own host, which is
    exactly why the service needs two arms rather than one configurable
    base_url — and why pointing either at the other's host fails silently.
    """
    monkeypatch.setenv("SYNAPSE_SYNTHESIZER", "aic100")
    monkeypatch.setenv("INFERENCE_CLOUD_BASE_URL", httpserver.url_for("/v1"))
    monkeypatch.setenv("INFERENCE_CLOUD_API_KEY", "not-a-real-key")
    _serve_both_shapes(httpserver)

    result = await _provider().complete(
        messages=[{"role": "user", "content": "merge these"}], response_schema=SCHEMA
    )

    assert _paths(httpserver) == ["/v1/completions"]
    assert result.data == ANSWER


async def test_the_two_arms_do_not_speak_the_same_endpoint(
    httpserver: HTTPServer, monkeypatch
) -> None:
    """Stated once, directly. The two tests above each assert their own branch,
    and both would pass if the arms ever converged on one path — at which point
    the whole reason 282fd07 exists would be gone and nothing would say so.
    """
    _serve_both_shapes(httpserver)

    monkeypatch.setenv("SYNAPSE_SYNTHESIZER", "npu")
    monkeypatch.setenv("SYNAPSE_BASE_URL", httpserver.url_for("/v1"))
    await _provider().complete(messages=[], response_schema=SCHEMA)
    npu_paths = _paths(httpserver)

    monkeypatch.setenv("SYNAPSE_SYNTHESIZER", "aic100")
    monkeypatch.setenv("INFERENCE_CLOUD_BASE_URL", httpserver.url_for("/v1"))
    monkeypatch.setenv("INFERENCE_CLOUD_API_KEY", "not-a-real-key")
    await _provider().complete(messages=[], response_schema=SCHEMA)

    assert set(_paths(httpserver)) - set(npu_paths) == {"/v1/completions"}


# --- the arms and their knobs ----------------------------------------------------


async def test_the_npu_arm_honours_synapse_model(
    httpserver: HTTPServer, monkeypatch
) -> None:
    """`if model := os.environ.get("SYNAPSE_MODEL")` — the branch neither
    existing test executes: one sets only SYNAPSE_BASE_URL, the other deletes
    both. Asserted on the request BODY, because the point of naming a model is
    that the host is told which one to load.

    The value must DIFFER from `npu.DEFAULT_MODEL`, and that is asserted rather
    than assumed. The first draft of this test used the shipped model id — which
    is the default — so deleting the SYNAPSE_MODEL branch entirely left it
    green. A test that passes on the provider's fallback is testing the
    fallback, not the branch (ADR 0005), and it looked completely correct.
    """
    from synapse_providers.npu import DEFAULT_MODEL

    other_model = "qualcomm/Llama-3.2-3B-Instruct:W4A16"
    assert other_model != DEFAULT_MODEL, (
        "pick a model the provider would NOT have chosen on its own, or this "
        "test passes with the branch deleted"
    )

    monkeypatch.setenv("SYNAPSE_SYNTHESIZER", "npu")
    monkeypatch.setenv("SYNAPSE_BASE_URL", httpserver.url_for("/v1"))
    monkeypatch.setenv("SYNAPSE_MODEL", other_model)
    _serve_both_shapes(httpserver)

    provider = _provider()
    await provider.complete(messages=[], response_schema=SCHEMA)

    assert provider.model == other_model
    [request] = _completion_requests(httpserver)
    assert request.get_json()["model"] == other_model


async def test_an_unset_synapse_model_leaves_the_provider_on_its_own_default(
    httpserver: HTTPServer, monkeypatch
) -> None:
    """The other side of the walrus. The branch must not fire on an unset (or
    empty) variable and hand `model=""` to a host that would then load nothing
    — `if model :=` is a truthiness check, and an exported-but-empty
    SYNAPSE_MODEL is a normal shell accident."""
    from synapse_providers.npu import DEFAULT_MODEL

    monkeypatch.setenv("SYNAPSE_SYNTHESIZER", "npu")
    monkeypatch.setenv("SYNAPSE_BASE_URL", httpserver.url_for("/v1"))
    monkeypatch.setenv("SYNAPSE_MODEL", "")
    _serve_both_shapes(httpserver)

    provider = _provider()
    await provider.complete(messages=[], response_schema=SCHEMA)

    assert provider.model == DEFAULT_MODEL
    [request] = _completion_requests(httpserver)
    assert request.get_json()["model"] == DEFAULT_MODEL


def test_the_anthropic_arm_builds_the_anthropic_provider(monkeypatch) -> None:
    """The fourth arm, previously uncovered. Asserted by provider_id rather
    than by isinstance — it speaks the Messages API, not an OpenAI-shaped HTTP
    path at all, so there is no endpoint to compare."""
    from synapse_providers import AnthropicProvider

    monkeypatch.setenv("SYNAPSE_SYNTHESIZER", "anthropic")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key-not-used")

    provider = _provider()

    assert isinstance(provider, AnthropicProvider)
    assert provider.provider_id == "anthropic"


@pytest.mark.parametrize(
    "mode", ["", "FAKE", "npu ", "aic-100", "claude", "true", "1"]
)
def test_an_unrecognised_synthesizer_exits_rather_than_guessing(
    monkeypatch, mode: str
) -> None:
    """Swept over the near-misses someone would plausibly type. None of them
    may be GUESSED at — "npu " must not become "npu" and point synthesis at a
    host that may not be there.

    ⟨AMENDED at the W1 merge, 2026-08-06⟩ This test asserted that every one of
    these fell through to FakeProvider, on the reasoning that "a service that
    refuses to boot on a typo cannot be brought up on a laptop with nothing
    installed". That reasoning holds for UNSET and does not survive contact
    with a typo: the laptop-with-nothing-installed case is `SYNAPSE_SYNTHESIZER`
    absent, which still boots (`test_unset_still_boots_on_the_fake`, and
    `test_the_fake_fails_loudly...` below). A SET-but-wrong value is a
    deliberate act by someone who believed they were choosing a synthesizer,
    and silently handing them the stub is the exact silent-seam failure W1
    exists to close — observed as `SYNAPSE_SYNTHESIZER=aic-100` running the
    FakeProvider while the startup line read "synthesizer: aic-100". Exit 2,
    naming the value and the valid set (decision 005's preflight section); the
    one-key fix is to unset the variable, which the message says.

    Empty string is on this list on purpose: an exported-but-empty variable is
    a shell accident, not an intent to run the stub, and it is indistinguishable
    from a typo from where this code stands.
    """
    monkeypatch.setenv("SYNAPSE_SYNTHESIZER", mode)

    with pytest.raises(SystemExit) as exit_info:
        _provider()

    assert exit_info.value.code == 2


def test_fake_asked_for_by_name_is_still_the_fake(monkeypatch) -> None:
    """`fake` is a VALID mode, not a near-miss — the other half of the test
    above, kept separate so the parametrised sweep is unambiguously about
    values the service does not know."""
    from synapse_providers import FakeProvider

    monkeypatch.setenv("SYNAPSE_SYNTHESIZER", "fake")

    assert isinstance(_provider(), FakeProvider)


async def test_the_fake_fails_loudly_rather_than_answering_with_nothing(
    monkeypatch,
) -> None:
    """The default boots, but it must never look like a working synthesizer.

    An empty script that answered blandly would reproduce the 282fd07 symptom
    exactly — queries returning nothing, with a 200, and no error anywhere.

    Narrow on purpose. `pytest.raises(Exception)` was passing on a TypeError
    from a signature change: renaming `response_schema` on `FakeProvider.
    complete` kept this test green while it never reached the exhaustion path
    it exists to assert. The point of the test is that the fake fails for the
    RIGHT reason, so the reason is what gets asserted — the type and the
    message, not merely that something was raised.
    """
    from synapse_providers import FakeProvider

    monkeypatch.delenv("SYNAPSE_SYNTHESIZER", raising=False)
    provider = _provider()

    assert isinstance(provider, FakeProvider)
    with pytest.raises(RuntimeError, match=r"scripts exhausted after 0 call\(s\)"):
        await provider.complete(messages=[], response_schema=SCHEMA)
