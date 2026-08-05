"""Hosted Cirrascale Cloud AI 100 — the synthesis target (amendment A + G).

Standalone ModelProvider on purpose, not an OpenAICompatibleProvider subclass:
both probed gotchas make its schema path structurally different —
  * response_format is silently IGNORED (a 200 proves nothing; control-probed)
  * /chat/completions EATS emitted JSON into empty tool_calls
so schema calls flatten the messages into one prompt and use POST /completions,
then tolerantly extract and structurally validate a JSON object (reusing
openai_compat's _parse_json_tolerantly — routing and response_format are the
standalone-class justification, not the parser). One retry that changes the
prompt and temperature so a deterministic decode isn't just resent, then
honest schema_valid=False. max_tokens is bounded on every call — shared
credit pool.
"""

from __future__ import annotations

import json
import os
import time
from typing import Any

import httpx

from synapse_contracts import ModelResult, ModelUsage
from synapse_providers.base import ModelProvider, ProviderCapabilities
from synapse_providers.openai_compat import _parse_json_tolerantly

DEFAULT_BASE_URL = "https://aisuite.cirrascale.com/apis/v2"

# extract_first_json_object used to be a standalone brace-counter with no
# string awareness: it mis-closed on the first `}` inside a *string* value
# (routine for working_memory, free prose an 8B writes about code), costing
# a full retry for nothing. _parse_json_tolerantly (openai_compat) already
# solves this -- it tries json.loads on the whole text first, which respects
# string escaping, before falling back to a fenced-block or outer-span
# heuristic. Reusing it is strictly stronger; kept as a module-level name
# here since existing callers/tests reference `extract_first_json_object`.
extract_first_json_object = _parse_json_tolerantly


def _satisfies_schema(data: Any, schema: dict[str, Any]) -> bool:
    """Structural check, not full JSON Schema: confirms the parsed object
    plausibly matches response_schema rather than merely being *some*
    parseable JSON. 'It parsed' proves nothing was satisfied -- the same
    false-confidence trap this module exists to catch one level up (a 200
    proves nothing was enforced)."""
    if schema.get("type") == "object" and not isinstance(data, dict):
        return False
    if isinstance(data, dict):
        for key in schema.get("required", []):
            if key not in data:
                return False
    return True


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

            base_prompt = "\n\n".join(f"{m['role'].upper()}:\n{m['content']}" for m in messages)
            base_prompt += ("\n\nReturn ONLY a JSON object of this shape:\n"
                            + json.dumps(response_schema) + "\nJSON:")
            prompt = base_prompt
            payload: dict[str, Any] = {}
            for attempt in range(2):                        # one retry, exactly
                # temperature 0.0 is a deterministic decode: an unconditional
                # identical resend of attempt 0's request would be a
                # guaranteed-identical second failure, doubling consumption
                # of the shared credit pool for nothing. The retry (attempt
                # 1) echoes the bad response back and asks for a repair, and
                # nudges temperature off zero so it isn't a no-op resend.
                temperature = 0.0 if attempt == 0 else 0.2
                resp = await client.post(f"{self.base_url}/completions", json={
                    "model": self.model, "prompt": prompt,
                    "max_tokens": self.max_tokens, "temperature": temperature})
                resp.raise_for_status()
                payload = resp.json()
                text = payload["choices"][0].get("text", "")
                parsed = extract_first_json_object(text)
                if parsed is not None and _satisfies_schema(parsed, response_schema):
                    return self._result(parsed, payload, started, schema_valid=True)
                prompt = (base_prompt
                         + "\n\nYour previous response did not match the schema:\n"
                         + text.strip()[:500]
                         + "\n\nReturn ONLY the corrected JSON object, nothing else.\nJSON:")
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
