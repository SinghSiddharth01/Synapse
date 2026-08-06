"""Tolerant JSON recovery — the fallback every prompt-instructed model needs."""

import httpx

from synapse_providers.openai_compat import _parse_json_tolerantly, _salvage_partial



def test_tolerant_parse_recovers_json_the_model_left_unterminated():
    """The live failure, 2026-08-05, Llama-3.1-8B through the distil path.

    Three well-formed findings arrived with exactly ONE closing brace missing.
    Both older recovery paths made it worse: the outermost-{...} span ends at
    the last `}`, which is INSIDE the final finding, and the outermost-[...]
    span parses to a bare LIST that `Distiller._to_findings` rejects because it
    requires an object with a "findings" key. So a one-character truncation
    discarded three good findings, twice (the retry saw the same shape), and
    `contribute` answered "Nothing durable extracted from that".
    """
    truncated = ('{"findings": [{"type": "learning", "text": "bind the orchestrator '
                 'to localhost"}, {"type": "learning", "text": "attribution comes '
                 'from the binding"}]')
    parsed = _parse_json_tolerantly(truncated)

    assert isinstance(parsed, dict), "a bare list is what the distiller rejects"
    assert [f["text"] for f in parsed["findings"]] == [
        "bind the orchestrator to localhost",
        "attribution comes from the binding",
    ]


def test_tolerant_parse_still_refuses_output_that_is_not_json_at_all():
    """The repair may only ever ADD terminators. Prose that merely contains a
    brace must not be coaxed into a finding — inventing structure here would
    put content nobody wrote into team memory."""
    assert _parse_json_tolerantly("I could not do that, sorry.") is None
    assert _parse_json_tolerantly("") is None
    assert _parse_json_tolerantly("the config uses { and } characters") is None


def test_an_overflow_400_still_yields_the_findings_it_generated_before_the_cut():
    """Measured against GenieX 2026-08-06. A prompt that overruns the context
    comes back 400 — but the body carries `finish_reason: length` and the
    completion the model had already produced, two whole findings and part of a
    third. `raise_for_status()` discarded every one of them, and because the
    text never reached `_parse_json_tolerantly`, the unterminated-JSON repair
    above could not run on the single case it was written for."""
    response = httpx.Response(400, json={
        "choices": [{"finish_reason": "length", "message": {"content": (
            '{"findings": [{"type": "learning", "text": "geniex counts prompt '
            'plus generation against one 4096 ceiling"}, {"type": "learning", '
            '"text": "the reserve is a promise the segmenter already kept"}, '
            '{"type": "open_q')}}],
        "error": {"code": "context_length_exceeded"},
    }, request=httpx.Request("POST", "http://model/v1/chat/completions"))

    payload = _salvage_partial(response)

    assert payload is not None, "a body with completion in it must not be thrown away"
    recovered = _parse_json_tolerantly(payload["choices"][0]["message"]["content"])
    assert [f["text"] for f in recovered["findings"] if "text" in f] == [
        "geniex counts prompt plus generation against one 4096 ceiling",
        "the reserve is a promise the segmenter already kept",
    ], "both COMPLETE findings survive a cut that used to discard the whole batch"
    # The half-written third comes back as `{"type": "open_q"}` — the repair
    # closes structure but never invents content, so it has no `text` at all
    # and `Distiller._to_findings` drops it as empty. Asserted rather than
    # trimmed here: this is the documented edge of the repair, not an oversight.
    assert [f for f in recovered["findings"] if "text" not in f] == [{"type": "open_q"}]


def test_an_error_with_no_completion_in_it_is_left_to_raise():
    """Salvage is not a way to swallow errors. A 500, a connection-level fault,
    or an error body with no `choices` has nothing to recover, and must return
    None so the caller still raises rather than continuing on a phantom."""
    request = httpx.Request("POST", "http://model/v1/chat/completions")
    assert _salvage_partial(httpx.Response(
        400, json={"error": {"code": "context_length_exceeded"}}, request=request)) is None
    assert _salvage_partial(httpx.Response(
        500, text="upstream exploded", request=request)) is None
    assert _salvage_partial(httpx.Response(
        400, json={"choices": [{"message": {"content": ""}}]}, request=request)) is None
