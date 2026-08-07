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


def _last_message_preview(messages: list[dict[str, Any]]) -> str:
    """Preview the call's actual payload, not its boilerplate.

    Every `build_messages`-style caller (distiller, synthesis, retrieval) ends
    the list with one user message holding the real content -- the rendered
    segment, or the purpose/working-memory/query/findings block. Everything
    before it is the system prompt and few-shot pairs, which are identical
    across every call from a given component and would otherwise fill the
    entire PREVIEW_CHARS window (see recording.py module docstring: the
    dashboards exist to show what each call actually asked, not the pack).
    """
    if not messages:
        return ""
    return str(messages[-1].get("content", ""))


class RecordingProvider(ModelProvider):
    def __init__(self, inner: ModelProvider, component: str, log: CallLog) -> None:
        self.inner = inner
        self.component = component
        self.log = log
        # HTTP requests the inner provider made on THIS façade's last call.
        # Per-façade because synthesis and retrieval are two RecordingProviders
        # over ONE provider object: reading the inner counter directly would
        # return whichever component called most recently, and the governor
        # would charge one component for the other's spend.
        self.last_request_count = 0

    @property
    def provider_id(self) -> str:  # type: ignore[override]
        return self.inner.provider_id

    @property
    def capabilities(self) -> ProviderCapabilities:
        return self.inner.capabilities

    @property
    def max_tokens(self) -> int:
        """The wrapped provider's output cap, forwarded.

        Added 2026-08-06 from an observed defect, and the reason this file's
        first line says "transparent": it was transparent for the two
        attributes someone had needed so far and opaque for everything else.
        `SynthesisBudget.for_provider` reads `max_tokens` off whatever provider
        the `Synthesizer` holds, and in the DEFAULT deployment
        (`build_app(..., debug=True)`, which is what `synapse-service` runs)
        that is this wrapper -- so the `getattr(..., DEFAULT_OUTPUT_TOKENS)`
        fallback fired on every merge and the whole budget was derived from
        800 regardless of `INFERENCE_CLOUD_MAX_TOKENS`. Measured with the env
        var at 1600: the raw provider derives 500 words / 10 merges, the
        wrapped one derived 270 words / 4 merges, so the prompt asked for HALF
        the memory the operator had paid for. Worse in the other direction: a
        cap LOWERED to 600 still produced a 270-word ask, i.e. the exact
        mid-object truncation the budget exists to prevent, and
        `SynthesisBudgetError` could never fire because `derive` only ever saw
        800.

        AttributeError propagates on purpose when `inner` has no cap of its own
        (FakeProvider, ClaudeCLIProvider's `None`-able one is still an
        attribute): `getattr(provider, "max_tokens", DEFAULT_OUTPUT_TOKENS)`
        catches it and every such caller keeps the documented default.

        Rejected alternative: a blanket `__getattr__` delegating everything to
        `inner`. It would fix this instance and every future one, but it also
        makes the wrapper silently satisfy `hasattr` for attributes it does not
        proxy behaviourally, which is how a recorder starts being mistaken for
        the thing it records. Explicit properties, one per configuration
        attribute that matters, match the two already here.
        """
        return self.inner.max_tokens  # type: ignore[attr-defined]

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
            "prompt_preview": _preview(_last_message_preview(messages)),
        }
        try:
            result = await self.inner.complete(messages, response_schema)
        except Exception:
            # The failure path matters as much as the success path: a round
            # that raised still spent its requests, and that is exactly the
            # spend the governor must not lose track of.
            self._capture_request_count()
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
        self._capture_request_count()
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

    def _capture_request_count(self) -> None:
        """Snapshot the inner provider's per-call request count onto this
        façade. Providers without one (FakeProvider, NPUProvider) report a
        single request, which is what they cost."""
        self.last_request_count = getattr(self.inner, "last_request_count", 1)
