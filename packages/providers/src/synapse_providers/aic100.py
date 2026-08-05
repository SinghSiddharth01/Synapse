"""Hosted Cirrascale Cloud AI 100 — the synthesis target (amendment A + G).

Standalone ModelProvider on purpose, not an OpenAICompatibleProvider subclass:
both probed gotchas make its schema path structurally different —
  * response_format is silently IGNORED (a 200 proves nothing; control-probed)
  * /chat/completions EATS emitted JSON into empty tool_calls
so schema calls flatten the messages into one prompt and use POST /completions,
then tolerantly extract and structurally validate a JSON object. One retry
that changes the prompt and temperature so a deterministic decode isn't just
resent, then honest schema_valid=False. max_tokens is bounded on every call
— shared credit pool.
"""

from __future__ import annotations

import json
import logging
import os
import time
from typing import Any

import httpx

from synapse_contracts import ModelResult, ModelUsage
from synapse_providers.base import ModelProvider, ProviderCapabilities

logger = logging.getLogger(__name__)

DEFAULT_BASE_URL = "https://aisuite.cirrascale.com/apis/v2"


def extract_first_json_object(text: str) -> dict[str, Any] | None:
    """Find the FIRST balanced, string-aware JSON object in free-form text.

    A Llama-3.1-8B answering a /completions prompt that ends in "JSON:"
    routinely narrates around the object, shows a code snippet with its own
    braces, or appends a trailing example -- any of which puts a second,
    unrelated brace pair in the response. Reaching for the OUTERMOST
    `{`..`}` span (as openai_compat._parse_json_tolerantly's fallback does)
    is poisoned by that second pair; a naive brace-counter is poisoned by an
    unbalanced `}` inside a *string* value (routine free prose about code).
    `json.JSONDecoder.raw_decode` is used instead: it is string-aware (a `}`
    inside a quoted value never closes the object early) and, tried at every
    `{` in the text, stops at the FIRST index that parses -- matching what a
    balanced-brace scanner intends without either failure mode.
    """
    stripped = text.strip()
    if not stripped:
        return None

    # The common, cheap case: the whole response IS the JSON object.
    try:
        whole = json.loads(stripped)
        if isinstance(whole, dict):
            return whole
    except json.JSONDecodeError:
        pass

    # A fenced ```json block, with or without the language tag.
    if "```" in stripped:
        for chunk in stripped.split("```"):
            chunk = chunk.strip()
            if chunk.startswith("json"):
                chunk = chunk[4:].strip()
            if chunk.startswith("{"):
                try:
                    fenced = json.loads(chunk)
                except json.JSONDecodeError:
                    continue
                if isinstance(fenced, dict):
                    return fenced

    # Scan for the first index where a balanced object decodes, string
    # escaping respected by the stdlib tokenizer rather than hand-rolled.
    decoder = json.JSONDecoder()
    idx = stripped.find("{")
    while idx != -1:
        try:
            candidate, _end = decoder.raw_decode(stripped, idx)
            if isinstance(candidate, dict):
                return candidate
        except json.JSONDecodeError:
            pass
        idx = stripped.find("{", idx + 1)
    return None


def _satisfies_schema(data: Any, schema: dict[str, Any]) -> bool:
    """Structural check, not full JSON Schema: confirms the parsed object
    plausibly matches response_schema rather than merely being *some*
    parseable JSON. 'It parsed' proves nothing was satisfied -- the same
    false-confidence trap this module exists to catch one level up (a 200
    proves nothing was enforced).

    Deliberately requires only ONE of the schema's declared `required` keys
    to be present, not all of them. synapse_service.synthesis validates the
    verdict via its _SynthesisVerdicts model with every field defaulted, so an
    8B dropping one key (e.g. omitting "conflicts" on a round with nothing
    to report) doesn't cost the rest of an otherwise-usable verdict; an
    all-required gate here would silently discard that whole round before
    synthesis's own tolerance ever got a chance to run. A response matching
    NONE of the declared keys is the real false-confidence case this check
    exists to catch -- some parseable JSON that has nothing to do with the
    schema it was asked for."""
    if schema.get("type") == "object" and not isinstance(data, dict):
        return False
    if isinstance(data, dict):
        required = schema.get("required", [])
        if required and not any(key in data for key in required):
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
                choice = payload["choices"][0]
                text = choice.get("text", "")
                parsed = extract_first_json_object(text)
                if parsed is not None and _satisfies_schema(parsed, response_schema):
                    return self._result(parsed, payload, started, schema_valid=True)
                if choice.get("finish_reason") == "length":
                    # A response cut off by max_tokens takes the IDENTICAL
                    # path as unparseable garbage from here on (retry, then
                    # honest schema_valid=False) -- nothing in the
                    # ModelResult distinguishes "the model was wrong" from
                    # "we cut it off mid-answer". Log it distinctly so an
                    # operator watching logs (not just HTTP 200s) can tell
                    # the two apart and knows to raise max_tokens rather
                    # than blame the model or the prompt.
                    logger.warning("AIC100 response truncated at max_tokens=%d "
                                   "(finish_reason=length) on attempt %d; a truncated "
                                   "response is indistinguishable from garbage downstream "
                                   "without this log line", self.max_tokens, attempt)
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
