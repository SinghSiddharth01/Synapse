"""Tolerant JSON recovery — the fallback every prompt-instructed model needs."""

from synapse_providers.openai_compat import _parse_json_tolerantly



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
