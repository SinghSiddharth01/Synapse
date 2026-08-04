"""Safety guards against silent knowledge poisoning.

This module exists because of a measured failure, not a hypothetical one. On
2026-07-30, `google/gemma-4-E4B-it-qat-q4_0-gguf` was observed loading cleanly,
reporting NPU residency, streaming at 14 tok/s, and emitting fluent, confident,
well-formatted English that answered a completely different question every time.
Its manifest declared `ModelType: "vlm"` (it ships an mmproj), and the VLM code
path discards plain-text prompts. The server's own usage block gave it away:

    "usage": { "prompt_tokens": 1, "completion_tokens": 434 }

A 13-token prompt became 1 token.

For most projects that is a bad demo. For Synapse it is the highest-severity
failure in the system: the distiller's entire job is to read an Agent Session
and emit structured findings, so under this bug it emits schema-plausible
findings *invented from nothing* — straight into the Shared Memory that other
engineers' agents then build on. No error, no exception, output that passes
casual review, and teammates reasoning on top of it.

Two guards, both cheap, both mandatory:

  1. assert_prompt_conditioned  — per call. Nothing reaches a Finding from a
     dropped prompt.
  2. check_canary               — per model, before scoring. A model that fails
     it is excluded from the benchmark rather than measured.

See docs/brainstorming/2026-07-30-npu-llm-benchmarks-and-geniex-findings.md
Part 2, and Plan B Task B.3.
"""

from __future__ import annotations

from dataclasses import dataclass

from synapse_contracts import ModelResult
from synapse_providers import ModelProvider

# A prompt of length 1 is the signature of the dropped-prompt path. Any real
# rendered Segment is hundreds of tokens; the threshold is deliberately not
# tuned upward, because the point is to catch the pathological case with zero
# false positives rather than to police prompt size.
MIN_PROMPT_TOKENS = 1


class PromptDropError(RuntimeError):
    """The model did not condition on its prompt. Any output is invented."""


def assert_prompt_conditioned(result: ModelResult, *, context: str = "") -> None:
    """Hard-fail a distiller call whose prompt never reached the model.

    Raises rather than returning a flag: a caller that forgets to check a
    boolean is exactly the silent path this guard exists to close.
    """
    if result.usage.input_tokens > MIN_PROMPT_TOKENS:
        return

    where = f" ({context})" if context else ""
    raise PromptDropError(
        f"Provider {result.provider_id!r} reported "
        f"input_tokens={result.usage.input_tokens}{where}, which means the prompt "
        f"was dropped and the {result.usage.output_tokens} output tokens were "
        f"invented. Findings from this call must not be produced. "
        f"Most likely cause: the model is typed 'vlm' and the VLM path discards "
        f"text prompts — check `geniex list` and run "
        f"`geniex model set-type <model> llm`."
    )


# The canary fact is unambiguous, single-token-ish, and cannot be produced by a
# plausible-sounding guess: a model that ignores the prompt has no route to it.
CANARY_PROMPT = (
    "The deployment failed because the TLS certificate for the host "
    "api.internal expired. Which hostname's certificate expired? "
    "Answer with the hostname and nothing else."
)
CANARY_EXPECTED = "api.internal"


@dataclass(frozen=True)
class CanaryResult:
    passed: bool
    answer: str
    input_tokens: int
    detail: str

    def __bool__(self) -> bool:
        return self.passed


async def check_canary(provider: ModelProvider) -> CanaryResult:
    """Verify a model conditions on its prompt at all, before it is scored.

    Call this before running the corpus. A model that fails is excluded from
    the benchmark — not measured and reported as low-quality, because a
    dropped-prompt model's corpus scores are meaningless rather than merely bad.
    """
    result = await provider.complete(
        messages=[{"role": "user", "content": CANARY_PROMPT}]
    )
    answer = str(result.data)
    input_tokens = result.usage.input_tokens

    if input_tokens <= MIN_PROMPT_TOKENS:
        return CanaryResult(
            passed=False,
            answer=answer,
            input_tokens=input_tokens,
            detail=(
                f"prompt dropped: input_tokens={input_tokens}. "
                f"The model is not reading its prompt."
            ),
        )

    if CANARY_EXPECTED not in answer.lower():
        return CanaryResult(
            passed=False,
            answer=answer,
            input_tokens=input_tokens,
            detail=(
                f"answer did not contain {CANARY_EXPECTED!r}; the model reads its "
                f"prompt but did not extract an unambiguous fact from it."
            ),
        )

    return CanaryResult(
        passed=True, answer=answer, input_tokens=input_tokens, detail="ok"
    )
