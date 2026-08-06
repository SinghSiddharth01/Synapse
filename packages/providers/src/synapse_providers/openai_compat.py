"""OpenAI-shaped HTTP provider.

Deliberately a plain httpx client rather than the `openai` SDK: the only
surface we use is POST /v1/chat/completions, and a direct client keeps the
request body explicit. That matters here — see NPUProvider for why sending a
parameter is not evidence that it was honoured.
"""

from __future__ import annotations

import json
import time
from typing import Any

import httpx
from synapse_contracts import ModelResult, ModelUsage

from synapse_providers.base import ModelProvider, ProviderCapabilities


class OpenAICompatibleProvider(ModelProvider):
    """Any server speaking the OpenAI chat-completions shape."""

    provider_id = "openai-compat"

    def __init__(
        self,
        base_url: str,
        model: str,
        *,
        api_key: str | None = None,
        timeout: float = 300.0,
        temperature: float = 0.0,
        max_tokens: int = 1024,
        provider_id: str | None = None,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.model = model
        self.api_key = api_key
        self.timeout = timeout
        self.temperature = temperature
        self.max_tokens = max_tokens
        if provider_id is not None:
            self.provider_id = provider_id

    @property
    def capabilities(self) -> ProviderCapabilities:
        return ProviderCapabilities(native_structured_output=False, streaming=False)

    def _headers(self) -> dict[str, str]:
        headers = {"Content-Type": "application/json"}
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"
        return headers

    def _build_body(
        self,
        messages: list[dict[str, Any]],
        response_schema: dict[str, Any] | None,
    ) -> dict[str, Any]:
        body: dict[str, Any] = {
            "model": self.model,
            "messages": messages,
            "temperature": self.temperature,
            "max_tokens": self.max_tokens,
        }
        # Only send response_format when the provider actually guarantees it.
        # A provider that ignores unknown fields would otherwise let us believe
        # the schema was enforced when nothing enforced it.
        if response_schema is not None and self.capabilities.native_structured_output:
            body["response_format"] = {"type": "json_object"}
        return body

    async def complete(
        self,
        messages: list[dict[str, Any]],
        response_schema: dict[str, Any] | None = None,
    ) -> ModelResult:
        body = self._build_body(messages, response_schema)

        started = time.perf_counter()
        async with httpx.AsyncClient(timeout=self.timeout) as client:
            response = await client.post(
                f"{self.base_url}/chat/completions",
                headers=self._headers(),
                json=body,
            )
            response.raise_for_status()
            payload = response.json()
        latency_ms = int((time.perf_counter() - started) * 1000)

        text = payload["choices"][0]["message"]["content"] or ""
        usage = payload.get("usage") or {}

        data: Any = text
        schema_valid = True
        if response_schema is not None:
            parsed = _parse_json_tolerantly(text)
            schema_valid = parsed is not None
            data = parsed if parsed is not None else text

        return ModelResult(
            data=data,
            usage=ModelUsage(
                input_tokens=int(usage.get("prompt_tokens", 0)),
                output_tokens=int(usage.get("completion_tokens", 0)),
            ),
            latency_ms=latency_ms,
            provider_id=self.provider_id,
            schema_valid=schema_valid,
        )


def _parse_json_tolerantly(text: str) -> Any | None:
    """Recover JSON from a prompt-instructed model's output.

    Small models wrap JSON in prose or fenced code blocks even when told not to.
    Tolerant parsing is required precisely because the sampler is not constrained
    — this is the fallback path the NPU provider always takes.
    """
    candidate = text.strip()
    if not candidate:
        return None

    try:
        return json.loads(candidate)
    except json.JSONDecodeError:
        pass

    # Strip a fenced block, with or without a language tag.
    if "```" in candidate:
        fenced = candidate.split("```")
        for chunk in fenced:
            chunk = chunk.strip()
            if chunk.startswith("json"):
                chunk = chunk[4:].strip()
            if chunk.startswith(("{", "[")):
                try:
                    return json.loads(chunk)
                except json.JSONDecodeError:
                    continue

    # Close what the model left open, before falling back to a span. Observed
    # live 2026-08-05 against Llama-3.1-8B: a distil response arrived as
    #   {"findings": [{...}, {...}, {...}]
    # -- three well-formed findings, valid types, real text, and exactly ONE
    # missing closing brace. Every recovery below this point then made it
    # worse rather than better: the outermost-{...} span ends at the last `}`,
    # which sits INSIDE the final finding, and the outermost-[...] span parses
    # cleanly into a bare LIST, which `Distiller._to_findings` correctly
    # rejects because it requires an object with a "findings" key. So a
    # one-character truncation silently discarded three good findings, and the
    # retry produced the same shape a second time.
    #
    # Only ever appends terminators, never content: a truncated finding that
    # lost its "text" still fails validation downstream (empty text is
    # dropped) rather than being invented here.
    if (repaired := _close_unterminated(candidate)) is not None:
        return repaired

    # Fall back to the outermost brace/bracket span.
    for opener, closer in (("{", "}"), ("[", "]")):
        start = candidate.find(opener)
        end = candidate.rfind(closer)
        if start != -1 and end > start:
            try:
                return json.loads(candidate[start : end + 1])
            except json.JSONDecodeError:
                continue

    return None


def _close_unterminated(text: str) -> Any | None:
    """Parse JSON that was cut off mid-structure, by supplying the terminators
    the model never emitted. Returns None if that does not yield valid JSON.

    Scans for the first `{` or `[` and tracks nesting outside of string
    literals, so a brace inside a finding's text cannot confuse the depth
    count. An unterminated string is closed first, then a trailing comma
    dropped, then every open container closed innermost-first.
    """
    start = min((i for i in (text.find("{"), text.find("[")) if i != -1), default=-1)
    if start == -1:
        return None

    stack: list[str] = []
    in_string = False
    escaped = False
    for char in text[start:]:
        if in_string:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                in_string = False
            continue
        if char == '"':
            in_string = True
        elif char in "{[":
            stack.append("}" if char == "{" else "]")
        elif char in "}]" and stack:
            stack.pop()

    if not stack and not in_string:
        return None                      # nothing was left open; not our case

    body = text[start:]
    if in_string:
        body += '"'
    body = body.rstrip()
    if body.endswith(","):
        body = body[:-1]
    try:
        return json.loads(body + "".join(reversed(stack)))
    except json.JSONDecodeError:
        return None
