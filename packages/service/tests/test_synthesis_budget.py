"""The synthesis OUTPUT budget — the bug of 2026-08-06, pinned.

Root cause, measured live: `SYNTH_SYSTEM` asked for a working memory "under
500 words" while `AIC100Provider` capped output at `max_tokens=800`. The live
working memory reached 477 words / 3065 chars ~= 766 tokens, so the
working_memory field ALONE consumed the whole budget: ~806 tokens including the
JSON envelope, against a cap of 800, before a single merge verdict. Every
synthesis was cut off mid-object, `extract_first_json_object` returned None, and
`merge()` returned with memory_version untouched. Findings kept landing; the
Shared Memory silently stopped moving for 40 minutes across two rounds.

Neither number was wrong on its own. They were written in different files by
different concerns and nothing ever compared them. These tests make that
comparison mechanical: the word cap is DERIVED from the token cap, and a pair
that cannot work is refused at construction instead of at 2am.
"""

from datetime import datetime, timezone

import pytest
from starlette.testclient import TestClient
from synapse_providers import CallLog, FakeProvider, RecordingProvider

from synapse_service.api import build_app
from synapse_service.synthesis import (JSON_ENVELOPE_TOKENS, MAX_WM_WORDS,
                                       SynthesisBudget, SynthesisBudgetError,
                                       TOKENS_PER_WORD, synth_system)


def test_the_shipped_pair_of_500_words_and_800_tokens_is_refused():
    """The exact configuration that broke production. 500 words is ~850
    tokens at the measured ratio; with the JSON envelope there is negative
    room for verdicts. Deriving instead of typing makes this unrepresentable:
    at x=800 the cap comes out far below 500 and the prompt says so."""
    budget = SynthesisBudget.derive(800)
    assert budget.working_memory_words < 500
    # and the derived cap must actually FIT, envelope and verdicts included
    assert (budget.working_memory_words * TOKENS_PER_WORD
            + JSON_ENVELOPE_TOKENS + budget.verdict_tokens) <= 800


def test_the_recommended_budget_restores_the_intended_500_word_memory():
    """x=1600 is the number derived from the live rate limits (25,000
    tokens/hour against ~2,110 input). It is chosen so the ORIGINAL product
    intent -- a 500-word working memory -- becomes affordable again, with
    real verdict room rather than -6 tokens of it."""
    budget = SynthesisBudget.derive(1600)
    assert budget.working_memory_words == MAX_WM_WORDS
    assert budget.verdict_tokens >= 300


def test_a_budget_too_small_for_a_useful_memory_raises_rather_than_shipping():
    """capability.py's MIN_USABLE_SEGMENT_TOKENS rule, applied to the other
    end of the pipeline: a configuration that cannot produce a usable result
    is an error, not a tight budget to be silently honoured."""
    with pytest.raises(SynthesisBudgetError):
        SynthesisBudget.derive(200)


def test_the_word_cap_reaches_the_model_in_the_prompt():
    """"Encode it so we do not run into this" -- the derived number is
    useless if the model is still told a stale literal. The system prompt is
    generated from the budget, so editing max_tokens re-states the cap
    automatically, the way PromptPack derives overhead from its own text."""
    assert "400 words" in synth_system(400)
    assert "500 words" not in synth_system(400)


def test_the_merge_cap_scales_with_the_budget_and_reaches_the_model():
    """What the budget bounds is what the model REPORTS, not what it is shown.

    An earlier pass enforced this by trimming the candidate list instead, and
    test_synthesis.py caught it in one run: with 5 findings pushed at an 800-
    token budget the trim left ZERO established partners, starving exactly the
    merges the ranked union exists to find. A candidate is an input cost, and
    input is not the constraint (2110 tokens against a 128K window) -- it only
    becomes an output cost in the minority of cases where it actually merges.
    So the cap is an instruction, and candidates stay untouched."""
    small = SynthesisBudget.derive(800)
    large = SynthesisBudget.derive(1600)
    assert small.max_merges < large.max_merges
    assert small.max_merges >= 1
    assert f"at most {large.max_merges} merges" in synth_system(
        large.working_memory_words, large.max_merges)


def test_the_overflow_contract_names_dropping_the_oldest():
    """The sanctioned shed. Safe because the working memory is a projection
    over the Log, never the record: evicted material is still on disk and
    still reachable through /synthesize, whereas an omitted merge simply does
    not happen this round. So the instruction must push overflow toward
    eviction, never toward dropping keys or truncating mid-sentence."""
    prompt = synth_system(400, 6)
    assert "oldest" in prompt.lower()
    assert "truncating mid-sentence" in prompt


# --- the budget the SHIPPED service actually derives ---------------------------
#
# Everything above calls `SynthesisBudget.derive()` directly. That is the same
# fabricated-fixture shape this change set criticises in test_aic100.py ("the
# unit test covering the warning fabricated a finish_reason the host never
# sends, which is exactly why it passed while synthesis silently stopped bumping
# memory_version"): it pins the arithmetic and proves nothing about the number
# the running service puts in front of the model. `build_app(debug=True)` -- the
# default, and what `synapse-service` runs -- wraps the provider in
# RecordingProvider, and the wrapper did not forward `max_tokens`, so every
# merge in every real deployment was budgeted at DEFAULT_OUTPUT_TOKENS=800
# whatever INFERENCE_CLOUD_MAX_TOKENS said. Observed 2026-08-06 with the env var
# at 1600: 270 words / 4 merges asked for, 500 / 10 paid for.
#
# So these two go through the seams the defect hid behind: the wrapper, and
# then the whole app down to the messages that reach the provider.

class _BudgetedProvider(FakeProvider):
    """A FakeProvider that declares an output cap, like every real provider.

    Plain FakeProvider has no `max_tokens` at all -- deliberately, so the
    existing suite stays on the shipped 800-token default -- which is precisely
    why it cannot catch a wrapper that swallows the attribute. This one carries
    a cap AND keeps the messages it was called with, so a test can assert on
    the prompt that actually went out rather than on a budget object.
    """

    def __init__(self, scripts, max_tokens: int):
        super().__init__(scripts=scripts)
        self.max_tokens = max_tokens
        self.seen: list[list[dict]] = []

    async def complete(self, messages, response_schema=None):
        self.seen.append(messages)
        return await super().complete(messages, response_schema)


def test_the_budget_survives_the_recording_wrapper():
    """RecordingProvider is instrumentation and must be transparent to
    CONFIGURATION, not only to results. It forwarded `provider_id` and
    `capabilities` and dropped `max_tokens`, so `for_provider` fell to its
    default on the one code path that ships."""
    inner = _BudgetedProvider(scripts=[], max_tokens=1600)
    wrapped = RecordingProvider(inner, "synthesis", CallLog())

    assert SynthesisBudget.for_provider(wrapped).output_tokens == 1600
    assert SynthesisBudget.for_provider(wrapped) == SynthesisBudget.for_provider(inner)


def test_a_provider_with_no_cap_still_gets_the_documented_default():
    """The other half of the forwarding rule. `max_tokens` is optional on a
    ModelProvider (FakeProvider has none), so the wrapper must let the
    attribute be ABSENT rather than answering None or raising -- otherwise
    fixing the wrapper would break every test that relies on the default."""
    wrapped = RecordingProvider(FakeProvider(scripts=[]), "synthesis", CallLog())

    from synapse_service.synthesis import DEFAULT_OUTPUT_TOKENS
    assert SynthesisBudget.for_provider(wrapped).output_tokens == DEFAULT_OUTPUT_TOKENS


def test_the_word_cap_in_the_shipped_prompt_matches_the_providers_max_tokens():
    """End to end, through `build_app` exactly as `synapse-service` builds it.

    This is the assertion the direct `derive(1600)` test cannot make: it asks
    what the model was actually TOLD. With the wrapper opaque it read "under
    270 words" while the operator was paying for 1600 output tokens; the
    dangerous direction is the same defect mirrored -- a cap LOWERED to save
    credits still asked for 270 words and re-introduced the mid-object
    truncation of 2026-08-06, with SynthesisBudgetError unable to fire because
    `derive` only ever saw 800.
    """
    provider = _BudgetedProvider(
        scripts=[{"working_memory": "wm", "merges": [], "trivial_ids": [],
                  "conflicts": []}] * 4,
        max_tokens=1600)
    client = TestClient(build_app(provider, merge_min_interval_s=0))
    sid = client.post("/v1/sessions",
                      json={"purpose": "p", "created_by": "sid"}).json()["shared_id"]
    client.post(f"/v1/sessions/{sid}/findings", json={"findings": [{
        "id": "f-1", "type": "learning",
        "text": "the pool trips under allocation pressure",
        "attributions": [{"contributor": "sid", "agent_session": "as-1",
                          "agent": "claude-code"}],
        "ts": datetime(2026, 8, 6, tzinfo=timezone.utc).isoformat()}]})

    expected = SynthesisBudget.derive(1600)
    assert expected.working_memory_words == MAX_WM_WORDS      # the point of 1600
    system = provider.seen[0][0]["content"]
    assert f"under {MAX_WM_WORDS} words" in system
    assert f"at most {expected.max_merges} merges" in system
