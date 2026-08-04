"""NPUProvider — the distiller running on the Hexagon NPU via GenieX.

A thin OpenAICompatibleProvider subclass. No custom client, no chat-template
rendering, no optional native dependency: `geniex serve` is OpenAI-shaped and
the standard body works unchanged.

    geniex serve            # 127.0.0.1:18181, ready ~1 s after launch

Why native_structured_output is False
-------------------------------------
Probed live against GenieX CLI v0.3.18 on X1E80100, 2026-08-04. Three requests
were sent with an identical prompt:

    A. no structured-output field at all
    B. response_format: {"type": "json_object"}
    C. enable_json: true          <- not a real OpenAI field; sent as a control

All three returned HTTP 200 with byte-identical output. The server accepts
unknown top-level parameters silently rather than rejecting them, so a
successful response is not evidence that anything was enforced. The control
request (C) is the proof: a server that honoured B would still have had no
reason to accept C.

This reverses the optimistic reading in
`docs/brainstorming/2026-07-30-npu-llm-benchmarks-and-geniex-findings.md`
Part 5, which hoped GBNF grammars reached the HTTP API and that the edge path
might therefore get *better* structured-output guarantees than the cloud
synthesizer. The CLI does expose --enable-json / --grammar-path /
--grammar-string, but they are not reachable over `serve`.

Consequence: the distiller must use prompt-instructed JSON, tolerant parsing,
and exactly one retry. Do not set this flag True without a control request
that the server *rejects*.
"""

from __future__ import annotations

from synapse_providers.base import ProviderCapabilities
from synapse_providers.openai_compat import OpenAICompatibleProvider

# The QAIRT (Qualcomm AI Engine Direct) bundle, NPU-exclusive, W4A16.
# Cached locally via `geniex pull`; confirm with `geniex list`.
DEFAULT_MODEL = "qualcomm/Qwen3-4B-Instruct-2507:W4A16"
DEFAULT_BASE_URL = "http://127.0.0.1:18181/v1"


class NPUProvider(OpenAICompatibleProvider):
    """GenieX `serve` on the Hexagon NPU."""

    provider_id = "npu"

    def __init__(
        self,
        base_url: str = DEFAULT_BASE_URL,
        model: str = DEFAULT_MODEL,
        **kwargs: object,
    ) -> None:
        super().__init__(base_url=base_url, model=model, **kwargs)  # type: ignore[arg-type]

    @property
    def capabilities(self) -> ProviderCapabilities:
        # See the module docstring. False is a measured result, not a default.
        return ProviderCapabilities(native_structured_output=False, streaming=False)
