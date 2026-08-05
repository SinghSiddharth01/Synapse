"""Hosted Cirrascale Cloud AI 100 — the synthesis target (amendment A + G).

Standalone ModelProvider on purpose, not an OpenAICompatibleProvider subclass:
both probed gotchas make its schema path structurally different —
  * response_format is silently IGNORED (a 200 proves nothing; control-probed)
  * /chat/completions EATS emitted JSON into empty tool_calls
so schema calls flatten the messages into one prompt and use POST /completions,
then tolerantly extract the first balanced JSON object. One retry, then honest
schema_valid=False. max_tokens is bounded on every call — shared credit pool.
"""

from __future__ import annotations

import json
import os
import time
from typing import Any

import httpx

from synapse_contracts import ModelResult, ModelUsage
from synapse_providers.base import ModelProvider, ProviderCapabilities

DEFAULT_BASE_URL = "https://aisuite.cirrascale.com/apis/v2"


def extract_first_json_object(text: str) -> dict[str, Any] | None:
    start = text.find("{")
    while start != -1:
        depth = 0
        for i in range(start, len(text)):
            if text[i] == "{":
                depth += 1
            elif text[i] == "}":
                depth -= 1
                if depth == 0:
                    try:
                        return json.loads(text[start:i + 1])
                    except json.JSONDecodeError:
                        break
        start = text.find("{", start + 1)
    return None


class AIC100Provider(ModelProvider):
    provider_id = "aic100"

    def __init__(self, base_url: str | None = None, model: str = "Llama-3.1-8B",
                 api_key: str | None = None, max_tokens: int = 800,
                 timeout: float = 60.0,
                 transport: httpx.AsyncBaseTransport | None = None) -> None:
        self.base_url = (base_url or os.environ.get("INFERENCE_CLOUD_BASE_URL")
                         or DEFAULT_BASE_URL).rstrip("/")
        self.model = model
        self.api_key = api_key or os.environ.get("INFERENCE_CLOUD_API_KEY", "")
        self.max_tokens = max_tokens
        self.timeout = timeout
        self._transport = transport

    @property
    def capabilities(self) -> ProviderCapabilities:
        return ProviderCapabilities(native_structured_output=False, streaming=False)

    def _client(self) -> httpx.AsyncClient:
        return httpx.AsyncClient(
            transport=self._transport, timeout=self.timeout,
            headers={"Authorization": f"Bearer {self.api_key}"})

    async def complete(self, messages: list[dict[str, Any]],
                       response_schema: dict[str, Any] | None = None) -> ModelResult:
        started = time.perf_counter()
        async with self._client() as client:
            if response_schema is None:
                resp = await client.post(f"{self.base_url}/chat/completions", json={
                    "model": self.model, "messages": messages,
                    "max_tokens": self.max_tokens, "temperature": 0.0})
                resp.raise_for_status()
                payload = resp.json()
                return self._result(payload["choices"][0]["message"]["content"],
                                    payload, started, schema_valid=True)

            prompt = "\n\n".join(f"{m['role'].upper()}:\n{m['content']}" for m in messages)
            prompt += ("\n\nReturn ONLY a JSON object of this shape:\n"
                       + json.dumps(response_schema) + "\nJSON:")
            payload: dict[str, Any] = {}
            for _attempt in range(2):                       # one retry, exactly
                resp = await client.post(f"{self.base_url}/completions", json={
                    "model": self.model, "prompt": prompt,
                    "max_tokens": self.max_tokens, "temperature": 0.0})
                resp.raise_for_status()
                payload = resp.json()
                parsed = extract_first_json_object(payload["choices"][0].get("text", ""))
                if parsed is not None:
                    return self._result(parsed, payload, started, schema_valid=True)
            return self._result(None, payload, started, schema_valid=False)

    def _result(self, data: Any, payload: dict[str, Any], started: float,
                *, schema_valid: bool) -> ModelResult:
        usage = payload.get("usage", {})
        return ModelResult(
            data=data,
            usage=ModelUsage(input_tokens=int(usage.get("prompt_tokens", 0)),
                             output_tokens=int(usage.get("completion_tokens", 0))),
            latency_ms=int((time.perf_counter() - started) * 1000),
            provider_id=self.provider_id,
            schema_valid=schema_valid,
        )
