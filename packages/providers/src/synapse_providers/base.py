"""ModelProvider — the abstraction every component depends on.

A `mode` is a pair `{distiller, synthesizer}`, each a ModelProvider. Swapping
providers by config is what enables on-target / off-target execution and the
quality-vs-Claude benchmark.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Any

from synapse_contracts import ModelResult


@dataclass(frozen=True)
class ProviderCapabilities:
    """Static properties a provider advertises so callers can adapt.

    native_structured_output: True → the provider can *guarantee* schema-valid
    JSON output (via tool-use, JSON grammar, etc.). False → the provider is
    prompt-instructed and needs tolerant parsing + retry (typical for NPU
    ONNX-QNN paths).
    """

    native_structured_output: bool
    streaming: bool = False


class ModelProvider(ABC):
    """One method: complete(messages, response_schema?) → ModelResult.

    Implementations should populate ModelResult.usage and latency_ms for every
    call. When response_schema is supplied, ModelResult.data is the validated
    structured object; otherwise it is the raw text string. schema_valid
    reports whether the returned data satisfied the schema (only meaningful
    when response_schema is not None; True by convention when schema is None).
    """

    provider_id: str

    @property
    @abstractmethod
    def capabilities(self) -> ProviderCapabilities: ...

    @abstractmethod
    async def complete(
        self,
        messages: list[dict[str, Any]],
        response_schema: dict[str, Any] | None = None,
    ) -> ModelResult: ...
