"""RecordingProvider — transparent instrumentation for any ModelProvider.

Wraps, records, re-raises. The debug dashboards read CallLog.snapshot();
nothing else in the system may depend on it. Previews are truncated hard so
a dashboard never becomes a second place raw content accumulates.
"""

from __future__ import annotations

import time
from collections import deque
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from synapse_contracts import ModelResult

from synapse_providers.base import ModelProvider, ProviderCapabilities

PREVIEW_CHARS = 200


@dataclass(frozen=True)
class LLMCall:
    """Documents the shape CallLog entries take. CallLog itself stores plain
    JSON-safe dicts (not instances of this class) so `snapshot()` needs no
    serialization step for the debug endpoints; this dataclass exists as the
    typed reference for that shape.
    """

    ts_iso: str
    component: str
    provider_id: str
    input_tokens: int
    output_tokens: int
    latency_ms: int
    schema_valid: bool
    ok: bool
    prompt_preview: str
    output_preview: str


class CallLog:
    def __init__(self, maxlen: int = 200) -> None:
        self._calls: deque[dict[str, Any]] = deque(maxlen=maxlen)

    def append_raw(self, call: dict[str, Any]) -> None:
        self._calls.append(call)

    def snapshot(self) -> list[dict[str, Any]]:
        return list(self._calls)


def _preview(text: str) -> str:
    return text[:PREVIEW_CHARS] + ("…" if len(text) > PREVIEW_CHARS else "")


class RecordingProvider(ModelProvider):
    def __init__(self, inner: ModelProvider, component: str, log: CallLog) -> None:
        self.inner = inner
        self.component = component
        self.log = log

    @property
    def provider_id(self) -> str:  # type: ignore[override]
        return self.inner.provider_id

    @property
    def capabilities(self) -> ProviderCapabilities:
        return self.inner.capabilities

    async def complete(
        self,
        messages: list[dict[str, Any]],
        response_schema: dict[str, Any] | None = None,
    ) -> ModelResult:
        started = time.perf_counter()
        base = {
            "ts_iso": datetime.now(timezone.utc).isoformat(),
            "component": self.component,
            "provider_id": self.inner.provider_id,
            "prompt_preview": _preview(" | ".join(str(m.get("content", "")) for m in messages)),
        }
        try:
            result = await self.inner.complete(messages, response_schema)
        except Exception:
            self.log.append_raw(
                {
                    **base,
                    "ok": False,
                    "input_tokens": 0,
                    "output_tokens": 0,
                    "latency_ms": int((time.perf_counter() - started) * 1000),
                    "schema_valid": False,
                    "output_preview": "",
                }
            )
            raise
        self.log.append_raw(
            {
                **base,
                "ok": True,
                "input_tokens": result.usage.input_tokens,
                "output_tokens": result.usage.output_tokens,
                "latency_ms": result.latency_ms,
                "schema_valid": result.schema_valid,
                "output_preview": _preview(str(result.data)),
            }
        )
        return result
