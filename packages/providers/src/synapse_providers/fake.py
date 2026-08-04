"""FakeProvider — scripted, deterministic, offline. The TDD backbone.

Every unit and contract test in Synapse runs against FakeProvider so tests
stay CI-safe (no API keys, no network, no GPU). LLM quality is a *measurement*
made by the eval harness, not a pass/fail gate in unit tests.
"""

from __future__ import annotations

from typing import Any

from synapse_contracts import ModelResult, ModelUsage

from synapse_providers.base import ModelProvider, ProviderCapabilities


class FakeProvider(ModelProvider):
    """Returns a pre-scripted sequence of outputs, one per complete() call.

    Usage:
        fake = FakeProvider(scripts=["response 1", {"key": "structured"}])
        result = await fake.complete(messages=[...])  # → "response 1"
        result = await fake.complete(messages=[...])  # → {"key": "structured"}
    """

    provider_id = "fake"

    def __init__(self, scripts: list[Any], *, input_tokens: int | None = None) -> None:
        self._scripts = list(scripts)
        self._index = 0
        # Override the derived input_tokens. Exists so tests can reproduce the
        # prompt-drop failure mode (prompt_tokens == 1) that the distiller guard
        # exists to catch. See the vlm prompt-drop bug in the 2026-07-30 findings.
        self._input_tokens_override = input_tokens

    @property
    def capabilities(self) -> ProviderCapabilities:
        return ProviderCapabilities(native_structured_output=True, streaming=False)

    @property
    def calls(self) -> int:
        """How many times complete() has been called. Used to assert retry counts."""
        return self._index

    async def complete(
        self,
        messages: list[dict[str, Any]],
        response_schema: dict[str, Any] | None = None,
    ) -> ModelResult:
        if self._index >= len(self._scripts):
            raise RuntimeError(
                f"FakeProvider scripts exhausted after {self._index} call(s); "
                f"add more scripts or reset the provider."
            )
        data = self._scripts[self._index]
        self._index += 1

        # Deterministic pseudo-usage: token count proxies from character length.
        # These are stable across runs so tests can assert on them if needed.
        input_chars = sum(len(str(m.get("content", ""))) for m in messages)
        output_chars = len(str(data))
        input_tokens = (
            self._input_tokens_override
            if self._input_tokens_override is not None
            else max(1, input_chars // 4)
        )
        return ModelResult(
            data=data,
            usage=ModelUsage(
                input_tokens=input_tokens,
                output_tokens=max(1, output_chars // 4),
            ),
            latency_ms=0,
            provider_id=self.provider_id,
            schema_valid=True,
        )
