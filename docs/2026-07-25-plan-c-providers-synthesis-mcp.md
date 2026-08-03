# Synapse — Plan C: Providers + Synthesis + MCP (Siddsing)

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Deliver the model-facing spine of Synapse — the `ClaudeProvider` baseline, the OpenAI-compatible HTTP adapter that unifies Ollama and AI-100 behind one class, the Synthesis service that merges findings into shared working memory, and the MCP server that agents pull from. Plus the AI-100 hardware spike.

**Architecture:** One `ClaudeProvider` for the Anthropic SDK; one `OpenAICompatibleProvider` (base) with `OllamaProvider` and `AIC100Provider` as thin subclasses differing only by `base_url`. `InMemorySynthesizer` performs incremental merge — each call takes `(current SessionContext, new findings)` → updated `SessionContext` with `Conflict[]`. `MCPServer` exposes three tools (`create_session`, `join_session`, `query`) over HTTP/SSE using the MCP Python SDK's `MCPServer` (formerly FastMCP). The ingest API and MCP live in one process on the AI-100 box.

**Tech Stack:** Python 3.12, Anthropic SDK (`anthropic>=0.40`), MCP Python SDK (`mcp>=1.0`), `httpx`, `pytest-httpserver`.

**Owner:** Siddsing.

**Prerequisites (from Plan 0):** all contracts frozen, `FakeProvider` shipped, fixture Segments + golden Findings committed.

**Handoff to other tracks:**
- `ClaudeProvider` (Task 1) unblocks Aditya's benchmark baseline.
- Ingest API implementation (Task 5) unblocks Akhil's real-service integration (until then he tests against `pytest-httpserver`).
- `Distiller.build_default_distiller()` reads `SYNAPSE_DISTILLER_MODE=ollama` and needs `OllamaProvider` (Task 2) present.

---

### Task 1: `ClaudeProvider` — the quality baseline and LLM judge

**Files:**
- Create: `packages/providers/src/synapse_providers/claude.py`
- Modify: `packages/providers/src/synapse_providers/__init__.py`
- Create: `packages/providers/tests/test_claude.py`

- [ ] **Step 1: Write the failing test suite**

`ANTHROPIC_API_KEY` is not required for these tests — we mock the SDK client so tests stay CI-safe.

Create `packages/providers/tests/test_claude.py`:

```python
import json
from unittest.mock import AsyncMock, MagicMock

import pytest

from synapse_providers import ClaudeProvider
from synapse_providers.base import ProviderCapabilities


def _mock_response(text: str, usage_in: int = 100, usage_out: int = 40):
    """Shape a fake anthropic.messages.create response.

    With output_config.format=json_schema the API guarantees the first text
    block is JSON matching the schema — we just serialize a dict for the mock.
    """
    resp = MagicMock()
    resp.content = [MagicMock(type="text", text=text)]
    resp.usage = MagicMock(input_tokens=usage_in, output_tokens=usage_out)
    return resp


def test_claude_provider_reports_native_structured_output() -> None:
    caps = ClaudeProvider.default_capabilities
    assert caps == ProviderCapabilities(native_structured_output=True, streaming=False)


@pytest.mark.asyncio
async def test_complete_with_schema_returns_structured_data() -> None:
    fake_client = MagicMock()
    fake_client.messages = MagicMock()
    fake_client.messages.create = AsyncMock(
        return_value=_mock_response(json.dumps({"findings": [{"type": "learning", "text": "x"}]}))
    )

    provider = ClaudeProvider.__new__(ClaudeProvider)
    provider._client = fake_client  # type: ignore[attr-defined]
    provider._model = "claude-opus-4-8"  # type: ignore[attr-defined]

    result = await provider.complete(
        messages=[{"role": "user", "content": "hi"}],
        response_schema={"type": "object", "properties": {"findings": {"type": "array"}}},
    )

    assert result.data == {"findings": [{"type": "learning", "text": "x"}]}
    assert result.provider_id == "claude"
    assert result.schema_valid is True
    assert result.usage.input_tokens == 100
    assert result.usage.output_tokens == 40
    assert result.latency_ms >= 0


@pytest.mark.asyncio
async def test_complete_with_schema_falls_back_when_json_parse_fails() -> None:
    """Belt-and-suspenders: even with output_config.format, if the returned
    text ever fails json.loads, schema_valid=False and the Distiller handles it."""
    fake_client = MagicMock()
    fake_client.messages = MagicMock()
    fake_client.messages.create = AsyncMock(return_value=_mock_response("not json"))

    provider = ClaudeProvider.__new__(ClaudeProvider)
    provider._client = fake_client  # type: ignore[attr-defined]
    provider._model = "claude-opus-4-8"  # type: ignore[attr-defined]

    result = await provider.complete(
        messages=[{"role": "user", "content": "hi"}],
        response_schema={"type": "object"},
    )
    assert result.data == "not json"
    assert result.schema_valid is False


@pytest.mark.asyncio
async def test_complete_without_schema_returns_text() -> None:
    fake_client = MagicMock()
    fake_client.messages = MagicMock()

    text_resp = MagicMock()
    text_resp.content = [MagicMock(type="text", text="hello world")]
    text_resp.usage = MagicMock(input_tokens=10, output_tokens=5)
    fake_client.messages.create = AsyncMock(return_value=text_resp)

    provider = ClaudeProvider.__new__(ClaudeProvider)
    provider._client = fake_client  # type: ignore[attr-defined]
    provider._model = "claude-opus-4-8"  # type: ignore[attr-defined]

    result = await provider.complete(messages=[{"role": "user", "content": "hi"}])
    assert result.data == "hello world"
    assert result.schema_valid is True  # trivially — no schema to violate
```

- [ ] **Step 2: Run — expect ImportError**

Run: `uv run pytest packages/providers/tests/test_claude.py -v`
Expected: FAIL — `ImportError: cannot import name 'ClaudeProvider'`.

- [ ] **Step 3: Implement `ClaudeProvider`**

Create `packages/providers/src/synapse_providers/claude.py`:

```python
"""ClaudeProvider — Anthropic SDK wrapper. The quality baseline and LLM judge.

Uses `client.messages.create(...)` with output_config.format = json_schema when
a schema is supplied — the API enforces JSON validity against the schema on
Claude Opus 4.8. We `json.loads` the returned text block; if that ever fails
(shouldn't happen with the strict format), we return the raw text and let the
Distiller's tolerant parse handle it.

Without a schema, plain `messages.create()` returns text and we extract the
first text block.

Model: claude-opus-4-8 by default (most capable Opus-tier at the time of the
hackathon; $5 input / $25 output per 1M tokens per shared/models.md). Override
via SYNAPSE_CLAUDE_MODEL env var.

Uses adaptive thinking (thinking={"type": "adaptive"}) — Claude decides depth
per call, no budget_tokens needed.
"""

from __future__ import annotations

import json
import os
import time
from typing import Any

from synapse_contracts import ModelResult, ModelUsage

from synapse_providers.base import ModelProvider, ProviderCapabilities


class ClaudeProvider(ModelProvider):
    provider_id = "claude"
    default_capabilities = ProviderCapabilities(native_structured_output=True, streaming=False)

    def __init__(
        self,
        *,
        model: str | None = None,
        api_key: str | None = None,
    ) -> None:
        try:
            from anthropic import AsyncAnthropic
        except ImportError as e:  # pragma: no cover
            raise ImportError("anthropic SDK not installed — check pyproject.toml") from e

        self._client = AsyncAnthropic(api_key=api_key) if api_key else AsyncAnthropic()
        self._model = model or os.environ.get("SYNAPSE_CLAUDE_MODEL", "claude-opus-4-8")

    @property
    def capabilities(self) -> ProviderCapabilities:
        return self.default_capabilities

    async def complete(
        self,
        messages: list[dict[str, Any]],
        response_schema: dict[str, Any] | None = None,
    ) -> ModelResult:
        # Split off system message(s) — Anthropic API takes them as a top-level
        # `system` string, not in the messages array.
        system, user_asst = _split_system(messages)

        kwargs: dict[str, Any] = {
            "model": self._model,
            "max_tokens": 16000,
            "system": system,
            "messages": user_asst,
            "thinking": {"type": "adaptive"},
        }
        if response_schema is not None:
            kwargs["output_config"] = {
                "format": {"type": "json_schema", "schema": response_schema}
            }

        t0 = time.perf_counter()
        resp = await self._client.messages.create(**kwargs)
        latency_ms = int((time.perf_counter() - t0) * 1000)

        text = _first_text(resp)
        if response_schema is not None:
            try:
                data: Any = json.loads(text)
                schema_valid = True
            except json.JSONDecodeError:
                # output_config.format shouldn't produce invalid JSON, but if it
                # ever does, hand the raw text back so the Distiller can tolerant-parse.
                data = text
                schema_valid = False
        else:
            data = text
            schema_valid = True

        return ModelResult(
            data=data,
            usage=ModelUsage(
                input_tokens=int(resp.usage.input_tokens),
                output_tokens=int(resp.usage.output_tokens),
            ),
            latency_ms=latency_ms,
            provider_id=self.provider_id,
            schema_valid=schema_valid,
        )


def _split_system(messages: list[dict[str, Any]]) -> tuple[str, list[dict[str, Any]]]:
    """Extract a leading system message(s) into a single string for the API top-level `system` param."""
    system_parts: list[str] = []
    user_asst: list[dict[str, Any]] = []
    for m in messages:
        if m["role"] == "system":
            system_parts.append(str(m["content"]))
        else:
            user_asst.append(m)
    return ("\n\n".join(system_parts), user_asst)


def _first_text(resp: Any) -> str:
    for block in resp.content:
        if getattr(block, "type", None) == "text":
            return getattr(block, "text", "")
    return ""
```

- [ ] **Step 4: Re-export from package root**

Overwrite `packages/providers/src/synapse_providers/__init__.py`:

```python
"""ModelProvider implementations."""

from synapse_providers.base import ModelProvider, ProviderCapabilities
from synapse_providers.claude import ClaudeProvider
from synapse_providers.fake import FakeProvider

__all__ = ["ClaudeProvider", "FakeProvider", "ModelProvider", "ProviderCapabilities"]
```

- [ ] **Step 5: Run — verify PASS**

Run: `uv run pytest packages/providers/tests/test_claude.py -v`
Expected: All tests PASS.

- [ ] **Step 6: Commit**

```bash
git add packages/providers/src/synapse_providers/claude.py packages/providers/src/synapse_providers/__init__.py packages/providers/tests/test_claude.py
git commit -m "$(cat <<'EOF'
feat(providers): ClaudeProvider — Opus 4.8 with adaptive thinking (Plan C Task 1)

Uses messages.create() with output_config.format={type: json_schema} to
enforce schema-valid JSON on the returned text block; adaptive thinking
is on by default. Downgrades schema_valid to False and passes the raw text
back if json.loads ever fails, so the Distiller's tolerant parse absorbs
edge cases. This is the quality baseline and LLM-as-judge for the benchmark.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

### Task 2: OpenAI-compatible HTTP adapter — one class, three providers

`AIC100Provider`, `OllamaProvider`, and llama.cpp all speak OpenAI-compatible chat completions. That's ~90% shared code — one base class, three thin subclasses differing only by `base_url` and provider-id.

**Files:**
- Create: `packages/providers/src/synapse_providers/openai_compat.py`
- Modify: `packages/providers/src/synapse_providers/__init__.py`
- Create: `packages/providers/tests/test_openai_compat.py`

- [ ] **Step 1: Write the failing test suite**

Create `packages/providers/tests/test_openai_compat.py`:

```python
import json

import pytest
from pytest_httpserver import HTTPServer
from werkzeug.wrappers import Response

from synapse_providers import AIC100Provider, OllamaProvider


@pytest.mark.asyncio
async def test_ollama_provider_calls_correct_endpoint(httpserver: HTTPServer) -> None:
    httpserver.expect_request("/v1/chat/completions", method="POST").respond_with_json({
        "choices": [{"message": {"content": "hello from ollama"}}],
        "usage": {"prompt_tokens": 12, "completion_tokens": 5},
    })

    provider = OllamaProvider(base_url=httpserver.url_for("/v1"), model="llama3.2:3b")
    result = await provider.complete(messages=[{"role": "user", "content": "hi"}])
    assert result.data == "hello from ollama"
    assert result.usage.input_tokens == 12
    assert result.usage.output_tokens == 5
    assert result.provider_id == "ollama"


@pytest.mark.asyncio
async def test_aic100_provider_uses_json_schema_response_format(httpserver: HTTPServer) -> None:
    """When response_schema is provided, use OpenAI-style json_schema response_format."""
    def _handler(req):
        body = json.loads(req.data)
        assert body["response_format"]["type"] == "json_schema"
        return Response(
            json.dumps({
                "choices": [{"message": {"content": '{"findings": []}'}}],
                "usage": {"prompt_tokens": 20, "completion_tokens": 4},
            }),
            content_type="application/json",
        )

    httpserver.expect_request("/v1/chat/completions", method="POST").respond_with_handler(_handler)

    provider = AIC100Provider(
        base_url=httpserver.url_for("/v1"),
        model="Meta-Llama-3.1-8B-Instruct",
    )
    schema = {"type": "object", "properties": {"findings": {"type": "array"}}}
    result = await provider.complete(
        messages=[{"role": "user", "content": "extract findings"}],
        response_schema=schema,
    )
    assert result.data == {"findings": []}
    assert result.schema_valid is True
    assert result.provider_id == "aic100"


@pytest.mark.asyncio
async def test_response_without_schema_returns_text_verbatim(httpserver: HTTPServer) -> None:
    httpserver.expect_request("/v1/chat/completions", method="POST").respond_with_json({
        "choices": [{"message": {"content": "here's the answer, plain text"}}],
        "usage": {"prompt_tokens": 5, "completion_tokens": 6},
    })

    provider = OllamaProvider(base_url=httpserver.url_for("/v1"), model="llama3.2:3b")
    result = await provider.complete(messages=[{"role": "user", "content": "hi"}])
    assert result.data == "here's the answer, plain text"


@pytest.mark.asyncio
async def test_ollama_native_structured_output_is_advertised_true(httpserver: HTTPServer) -> None:
    """Ollama honors the JSON schema response_format on modern versions."""
    provider = OllamaProvider(base_url=httpserver.url_for("/v1"), model="llama3.2:3b")
    assert provider.capabilities.native_structured_output is True
```

- [ ] **Step 2: Run — expect ImportError**

Run: `uv run pytest packages/providers/tests/test_openai_compat.py -v`
Expected: FAIL.

- [ ] **Step 3: Implement the OpenAI-compat adapter**

Create `packages/providers/src/synapse_providers/openai_compat.py`:

```python
"""OpenAI-compatible chat-completions HTTP adapter.

AI-100 (vLLM `qaic` backend), Ollama, and llama.cpp-server all speak this
API. This one class handles all three; concrete subclasses differ only by
base_url, model, and provider_id.
"""

from __future__ import annotations

import json
import time
from typing import Any

import httpx

from synapse_contracts import ModelResult, ModelUsage

from synapse_providers.base import ModelProvider, ProviderCapabilities


class OpenAICompatibleProvider(ModelProvider):
    """Base: POST {base_url}/chat/completions, standard shape."""

    default_capabilities = ProviderCapabilities(native_structured_output=True, streaming=False)

    def __init__(
        self,
        *,
        base_url: str,
        model: str,
        provider_id: str,
        api_key: str | None = None,
        timeout: float = 60.0,
    ) -> None:
        self._base_url = base_url.rstrip("/")
        self._model = model
        self._provider_id = provider_id
        self._api_key = api_key
        self._timeout = timeout

    @property
    def provider_id(self) -> str:  # type: ignore[override]
        return self._provider_id

    @property
    def capabilities(self) -> ProviderCapabilities:
        return self.default_capabilities

    async def complete(
        self,
        messages: list[dict[str, Any]],
        response_schema: dict[str, Any] | None = None,
    ) -> ModelResult:
        body: dict[str, Any] = {"model": self._model, "messages": messages}
        if response_schema is not None:
            body["response_format"] = {
                "type": "json_schema",
                "json_schema": {"name": "response", "schema": response_schema, "strict": True},
            }

        headers = {"content-type": "application/json"}
        if self._api_key:
            headers["authorization"] = f"Bearer {self._api_key}"

        t0 = time.perf_counter()
        async with httpx.AsyncClient(timeout=self._timeout) as ac:
            resp = await ac.post(f"{self._base_url}/chat/completions", json=body, headers=headers)
        resp.raise_for_status()
        payload = resp.json()
        latency_ms = int((time.perf_counter() - t0) * 1000)

        content = payload["choices"][0]["message"]["content"]
        usage = payload.get("usage", {})
        input_tokens = int(usage.get("prompt_tokens", 0))
        output_tokens = int(usage.get("completion_tokens", 0))

        if response_schema is not None:
            try:
                data: Any = json.loads(content)
                schema_valid = True
            except json.JSONDecodeError:
                data = content  # let the caller tolerant-parse
                schema_valid = False
        else:
            data = content
            schema_valid = True

        return ModelResult(
            data=data,
            usage=ModelUsage(input_tokens=input_tokens, output_tokens=output_tokens),
            latency_ms=latency_ms,
            provider_id=self._provider_id,
            schema_valid=schema_valid,
        )


class OllamaProvider(OpenAICompatibleProvider):
    """Ollama's OpenAI-compatible endpoint (localhost:11434/v1 by default)."""

    def __init__(
        self,
        *,
        base_url: str = "http://localhost:11434/v1",
        model: str = "llama3.2:3b",
    ) -> None:
        super().__init__(base_url=base_url, model=model, provider_id="ollama")


class AIC100Provider(OpenAICompatibleProvider):
    """Cloud AI 100 served via vLLM's OpenAI-compatible endpoint (qaic backend).

    On the internal AI-100 box: `AIC100Provider(base_url="http://ai100-box:8000/v1", ...)`.
    The base URL is a config change; nothing else in this class is AI-100-specific.
    """

    def __init__(
        self,
        *,
        base_url: str,
        model: str,
        api_key: str | None = None,
    ) -> None:
        super().__init__(
            base_url=base_url,
            model=model,
            provider_id="aic100",
            api_key=api_key,
        )
```

- [ ] **Step 4: Re-export**

Overwrite `packages/providers/src/synapse_providers/__init__.py`:

```python
"""ModelProvider implementations."""

from synapse_providers.base import ModelProvider, ProviderCapabilities
from synapse_providers.claude import ClaudeProvider
from synapse_providers.fake import FakeProvider
from synapse_providers.openai_compat import (
    AIC100Provider,
    OllamaProvider,
    OpenAICompatibleProvider,
)

__all__ = [
    "AIC100Provider",
    "ClaudeProvider",
    "FakeProvider",
    "ModelProvider",
    "OllamaProvider",
    "OpenAICompatibleProvider",
    "ProviderCapabilities",
]
```

- [ ] **Step 5: Run — verify PASS**

Run: `uv run pytest packages/providers/tests/test_openai_compat.py -v`
Expected: All tests PASS.

- [ ] **Step 6: Commit**

```bash
git add packages/providers/src/synapse_providers/openai_compat.py packages/providers/src/synapse_providers/__init__.py packages/providers/tests/test_openai_compat.py
git commit -m "$(cat <<'EOF'
feat(providers): OpenAI-compatible adapter — one class, three providers (Plan C Task 2)

OpenAICompatibleProvider handles the /chat/completions shape; OllamaProvider
and AIC100Provider are thin subclasses. AI-100 (vLLM qaic), Ollama, and
llama.cpp all fit here; only the base_url and provider_id change.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

### Task 3: AI-100 hardware spike — stand up a model on the internal server

**This task is a spike, not a component build.** Output is a runbook + a go/no-go verdict.

**Kill time:** end of Day 2 of prep week. If red at kill time, the fallback for the demo is `synthesizer: claude` — the architecture handles it (Plan 0's `ModelProvider` abstraction is exactly what makes this fallback zero-code).

**Files:**
- Create: `docs/spikes/2026-07-26-ai100-spike.md`
- Create: `scripts/spike_ai100.sh`

- [ ] **Step 1: Get the model serving**

On the internal AI-100 Linux server, following the Qualcomm Cloud AI SDK docs:

```bash
# On the AI-100 server
git clone -b release/v1.19 --single-branch https://github.com/quic/efficient-transformers.git
cd efficient-transformers
pip install -e .

# Ensure custom-op compilation dependencies are met (CMake 3+).
# Reference: quic/cloud-ai-sdk-pages Getting-Started/Deployment/aws/index.html

# Start vLLM with the qaic backend, exposing an OpenAI-compatible API on :8000.
# Model choice at the start: Llama-3.2-1B-Instruct is the reference recipe; scale up
# once the pipeline is proven. AI-100 Standard SoC supports up to 8B models on a single
# SoC (sharding required for larger).
python -m QEfficient.cloud.infer \
    --model_name meta-llama/Llama-3.2-1B-Instruct \
    --batch_size 1 \
    --prompt_len 2048 \
    --ctx_len 4096 \
    --mxfp6 \
    --num_cores 14 \
    --device_group '[0]' \
    --serving_port 8000
```

Or (simpler if the box has the pre-built container) run vLLM directly with `qaic` backend and target the model.

- [ ] **Step 2: Curl the endpoint**

Create `scripts/spike_ai100.sh`:

```bash
#!/usr/bin/env bash
# AI-100 smoke test — curl the OpenAI-compatible endpoint and time it.
set -euo pipefail

: "${AIC100_BASE_URL:=http://ai100-box:8000/v1}"
: "${AIC100_MODEL:=meta-llama/Llama-3.2-1B-Instruct}"

curl -sS -X POST "${AIC100_BASE_URL}/chat/completions" \
    -H 'content-type: application/json' \
    -d '{
        "model": "'"${AIC100_MODEL}"'",
        "messages": [
            {"role": "system", "content": "You are a helpful assistant."},
            {"role": "user", "content": "Summarize in one sentence: the Snapdragon X Elite is a laptop chip with a Hexagon NPU."}
        ],
        "max_tokens": 100
    }'

echo
```

Make it executable and run:

```bash
chmod +x scripts/spike_ai100.sh
AIC100_BASE_URL=http://<ai100-host>:8000/v1 scripts/spike_ai100.sh
```

- [ ] **Step 3: Test structured output**

```bash
curl -sS -X POST "${AIC100_BASE_URL}/chat/completions" \
    -H 'content-type: application/json' \
    -d '{
        "model": "'"${AIC100_MODEL}"'",
        "messages": [{"role": "user", "content": "Return {\"answer\": 42}"}],
        "response_format": {
            "type": "json_schema",
            "json_schema": {
                "name": "r",
                "schema": {"type": "object", "properties": {"answer": {"type": "integer"}}, "required": ["answer"]},
                "strict": true
            }
        }
    }'
```

If vLLM's `qaic` backend supports `response_format: json_schema`, this returns valid JSON. If it doesn't yet, keep `native_structured_output=True` on `AIC100Provider` but be prepared for the demo pipeline to lean on the Distiller's tolerant parse.

- [ ] **Step 4: Run our provider against the real endpoint**

With the box running:

```bash
uv run python -c "
import asyncio
from synapse_providers import AIC100Provider

async def main():
    p = AIC100Provider(
        base_url='http://<ai100-host>:8000/v1',
        model='meta-llama/Llama-3.2-1B-Instruct',
    )
    r = await p.complete(messages=[{'role':'user','content':'Say hello in one sentence.'}])
    print(r)

asyncio.run(main())
"
```

Expected: a `ModelResult` prints with plausible `data`, `usage`, and `latency_ms`.

- [ ] **Step 5: Write the runbook**

Create `docs/spikes/2026-07-26-ai100-spike.md`:

```markdown
# AI-100 Spike — Serving a model on Cloud AI 100

**Owner:** Siddsing
**Prep window:** 2026-07-26 to 2026-07-31
**Kill time:** end of Day 2 (2026-07-27)

## Go / No-go verdict

- [ ] vLLM `qaic` OpenAI-compatible endpoint responds to POST /v1/chat/completions
- [ ] `AIC100Provider(...).complete()` returns a valid `ModelResult`
- [ ] `response_format: json_schema` produces valid JSON (or Distiller tolerant-parse compensates)
- [ ] Sustained tok/s at prompt_len=2048, ctx_len=4096: ______
- [ ] Latency (median, single request): ______ ms
- [ ] Model size fitting on-box: 1B / 3B / 8B (larger requires sharding — see model_sharding docs)

## Runbook

### On the AI-100 server
1. Install QEfficient (branch release/v1.19).
2. Start the model:
   ```
   python -m QEfficient.cloud.infer --model_name <hf-id> --num_cores 14 --device_group '[0]' --serving_port 8000
   ```
3. Verify: `bash scripts/spike_ai100.sh` from the dev machine.

### If provisioning stalls

AWS DL2q instances (`DL2q.24xlarge`) run the same SDK with the same model recipes.
Provisioning path:
1. Launch DL2q instance in a supported region.
2. Follow quic/cloud-ai-sdk-pages Deployment/aws.
3. Point `AIC100_BASE_URL` at the DL2q public IP + :8000.

## Numbers observed (fill in)

| Prompt shape | Model | Tokens in | Tokens out | Latency (s) | Structured OK? |
|---|---|---|---|---|---|
| Short chat | Llama-3.2-1B | | | | |
| Full segment prompt | Llama-3.2-1B | | | | |
| Full segment prompt + schema | Llama-3.2-1B | | | | |

## Fallback if red

If the spike is red by end of Day 2:
1. Set the synthesis config to `synthesizer: claude` — MCP and ingest still run on Siddsing's Mac (or on the AI-100 box; the process doesn't need the accelerator).
2. Present AI-100 as validated separately from a partial run — do NOT block the hackathon build.
3. The "one privacy boundary, three deployment targets" pitch still holds: only distilled Findings cross the boundary, and the endpoint is a config line.
```

- [ ] **Step 6: Commit spike artifacts**

```bash
git add docs/spikes/2026-07-26-ai100-spike.md scripts/spike_ai100.sh
git commit -m "$(cat <<'EOF'
docs(spike): AI-100 bring-up runbook + curl smoke test (Plan C Task 3)

QEfficient release/v1.19 + Llama-3.2-1B-Instruct as the starting recipe.
Fallback: AWS DL2q. If red by Day 2, synthesizer swaps to Claude with zero
code change — that's what the ModelProvider abstraction is for.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

### Task 4: `InMemorySynthesizer` — incremental merge of findings → SessionContext + Conflict[]

**Files:**
- Create: `packages/service/src/synapse_service/__init__.py` (overwrite from Plan 0 Task 1)
- Create: `packages/service/src/synapse_service/synthesis.py`
- Create: `packages/service/tests/__init__.py`
- Create: `packages/service/tests/test_synthesis.py`

- [ ] **Step 1: Write the failing test suite**

Create `packages/service/tests/__init__.py` as an empty file.

Create `packages/service/tests/test_synthesis.py`:

```python
from datetime import datetime, timezone

import pytest

from synapse_contracts import Finding, SessionContext
from synapse_providers import FakeProvider
from synapse_service.synthesis import InMemorySynthesizer


UTC = timezone.utc


def _finding(kind: str, text: str, contributor: str = "siddsing") -> Finding:
    return Finding(
        type=kind,  # type: ignore[arg-type]
        text=text,
        contributor=contributor,
        ts=datetime(2026, 7, 25, 12, 0, tzinfo=UTC),
        source_session="local-abc",
    )


@pytest.mark.asyncio
async def test_first_merge_writes_scripted_memory() -> None:
    scripted = {
        "working_memory": "Team is debugging flaky auth tests. Middleware clock-skew rejection is the current lead.",
        "conflicts": [],
    }
    provider = FakeProvider(scripts=[scripted])
    synth = InMemorySynthesizer(provider=provider)

    ctx = await synth.merge(
        shared_id="shared-1",
        purpose="Debug flaky auth tests",
        new_findings=[_finding("learning", "Middleware rejects clock-skew > 60s")],
    )
    assert isinstance(ctx, SessionContext)
    assert "clock-skew" in ctx.working_memory.lower()
    assert ctx.conflicts == []
    assert ctx.shared_id == "shared-1"


@pytest.mark.asyncio
async def test_second_merge_carries_previous_memory() -> None:
    """The merge is incremental — the model sees the current memory + new findings."""
    first = {"working_memory": "Round 1 memory.", "conflicts": []}
    second = {"working_memory": "Round 2 memory — includes Round 1 insight and new stuff.", "conflicts": []}

    provider = FakeProvider(scripts=[first, second])
    synth = InMemorySynthesizer(provider=provider)

    await synth.merge(shared_id="s1", purpose="p", new_findings=[_finding("learning", "one")])
    ctx = await synth.merge(shared_id="s1", purpose="p", new_findings=[_finding("learning", "two")])
    assert ctx.working_memory == "Round 2 memory — includes Round 1 insight and new stuff."


@pytest.mark.asyncio
async def test_conflict_between_two_contributors() -> None:
    f1 = _finding("decision", "Use JWT for auth", contributor="siddsing")
    f2 = _finding("decision", "Use session cookies for auth", contributor="akhil")

    scripted = {
        "working_memory": "Two contributors disagreed on auth strategy.",
        "conflicts": [
            {
                "finding_a_index": 0,
                "finding_b_index": 1,
                "description": "JWT vs session cookies for auth",
            }
        ],
    }
    provider = FakeProvider(scripts=[scripted])
    synth = InMemorySynthesizer(provider=provider)

    ctx = await synth.merge(shared_id="s1", purpose="p", new_findings=[f1, f2])
    assert len(ctx.conflicts) == 1
    assert "JWT" in ctx.conflicts[0].description
    assert ctx.conflicts[0].finding_a == f1
    assert ctx.conflicts[0].finding_b == f2


@pytest.mark.asyncio
async def test_isolated_shared_ids_do_not_bleed() -> None:
    provider = FakeProvider(scripts=[
        {"working_memory": "S1 memory", "conflicts": []},
        {"working_memory": "S2 memory", "conflicts": []},
    ])
    synth = InMemorySynthesizer(provider=provider)
    ctx1 = await synth.merge(shared_id="s1", purpose="p1", new_findings=[_finding("learning", "a")])
    ctx2 = await synth.merge(shared_id="s2", purpose="p2", new_findings=[_finding("learning", "b")])
    assert ctx1.working_memory != ctx2.working_memory
```

- [ ] **Step 2: Run — expect ImportError**

Run: `uv run pytest packages/service/tests/test_synthesis.py -v`
Expected: FAIL.

- [ ] **Step 3: Create the retrieval placeholder first (so the __init__.py import in Step 5 resolves)**

Real retrieval lands in Task 5; a placeholder module keeps package imports clean.

Create `packages/service/src/synapse_service/retrieval.py`:

```python
"""Retrieval — placeholder; real implementation in Task 5."""

from __future__ import annotations

from synapse_contracts import Finding, SessionContext
from synapse_providers import ModelProvider


async def query_findings(
    *,
    query: str,
    context: SessionContext,
    findings: list[Finding],
    provider: ModelProvider,
) -> list[Finding]:
    """LLM-as-retriever: ranks findings by relevance to the query. Filled in in Task 5."""
    raise NotImplementedError("Task 5")
```

- [ ] **Step 4: Implement the synthesizer**

Create `packages/service/src/synapse_service/synthesis.py`:

```python
"""InMemorySynthesizer — merge findings into a per-session SessionContext.

Per-shared_id state:
  - working_memory (str): a bounded organized doc the model maintains incrementally.
  - conflicts (list[Conflict]): pairs of findings the model judged to disagree.

Each merge() call:
  1. Loads the current SessionContext (or an empty one for shared_id).
  2. Sends (purpose, current working_memory, new findings) to the model, asking
     it to update the memory and flag any new conflicts with the new findings.
  3. Parses the model's structured response into an updated SessionContext.

Bounded by design: the memory grows by summarization, not by concatenation, so
the prompt stays a fixed size regardless of how many findings have been merged.
"""

from __future__ import annotations

import logging
from typing import Any

from synapse_contracts import Conflict, Finding, SessionContext
from synapse_providers import ModelProvider

log = logging.getLogger(__name__)


_SYSTEM_PROMPT = """You are the synthesizer for a team-intelligence system.

You maintain a bounded "working memory" — a short (~500 word) organized narrative
of what the team has learned about their shared session's purpose so far. Each
turn you receive:

  - The session's PURPOSE (a one-liner describing what the team is working on).
  - The CURRENT working memory (or empty on the first turn).
  - NEW FINDINGS from contributors, each tagged with type (learning/decision/
    dead_end/open_question), text, and contributor name.

Your job:
  1. Rewrite the working memory to incorporate the new findings. Keep it terse,
     organized by theme (not by contributor), and grounded in the purpose. Drop
     stale detail; do NOT just concatenate.
  2. Flag any pair of findings that DISAGREE (two contributors reaching different
     conclusions on the same question). Report each as an index pair into the
     new_findings list plus a one-line description. Only flag real disagreements —
     complementary findings are not conflicts.

Return exactly this JSON:
{
  "working_memory": "<updated narrative>",
  "conflicts": [{"finding_a_index": <int>, "finding_b_index": <int>, "description": "<one line>"}, ...]
}"""


_RESPONSE_SCHEMA = {
    "type": "object",
    "properties": {
        "working_memory": {"type": "string"},
        "conflicts": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "finding_a_index": {"type": "integer"},
                    "finding_b_index": {"type": "integer"},
                    "description": {"type": "string"},
                },
                "required": ["finding_a_index", "finding_b_index", "description"],
                "additionalProperties": False,
            },
        },
    },
    "required": ["working_memory", "conflicts"],
    "additionalProperties": False,
}


class InMemorySynthesizer:
    def __init__(self, *, provider: ModelProvider) -> None:
        self._provider = provider
        self._state: dict[str, SessionContext] = {}

    async def merge(
        self,
        *,
        shared_id: str,
        purpose: str,
        new_findings: list[Finding],
    ) -> SessionContext:
        current = self._state.get(shared_id)
        current_memory = current.working_memory if current else ""

        user_content = _format_prompt(purpose, current_memory, new_findings)
        messages = [
            {"role": "system", "content": _SYSTEM_PROMPT},
            {"role": "user", "content": user_content},
        ]

        result = await self._provider.complete(messages=messages, response_schema=_RESPONSE_SCHEMA)
        data = result.data if isinstance(result.data, dict) else {}

        working_memory = str(data.get("working_memory", current_memory))
        conflicts = _parse_conflicts(data.get("conflicts", []), new_findings)

        ctx = SessionContext(
            shared_id=shared_id,
            purpose=purpose,
            working_memory=working_memory,
            conflicts=conflicts,
        )
        self._state[shared_id] = ctx
        return ctx

    def get(self, shared_id: str) -> SessionContext | None:
        return self._state.get(shared_id)


def _format_prompt(purpose: str, current_memory: str, new_findings: list[Finding]) -> str:
    findings_block = "\n".join(
        f"[{i}] ({f.contributor}, {f.type}) {f.text}" for i, f in enumerate(new_findings)
    ) or "(none)"
    return (
        f"PURPOSE:\n{purpose}\n\n"
        f"CURRENT WORKING MEMORY:\n{current_memory or '(empty)'}\n\n"
        f"NEW FINDINGS:\n{findings_block}"
    )


def _parse_conflicts(raw: Any, new_findings: list[Finding]) -> list[Conflict]:
    if not isinstance(raw, list):
        return []
    conflicts: list[Conflict] = []
    for item in raw:
        try:
            ia = int(item["finding_a_index"])
            ib = int(item["finding_b_index"])
            description = str(item["description"])
        except (KeyError, TypeError, ValueError):
            log.debug("skipping malformed conflict item: %r", item)
            continue
        if not (0 <= ia < len(new_findings) and 0 <= ib < len(new_findings)) or ia == ib:
            continue
        conflicts.append(
            Conflict(
                finding_a=new_findings[ia],
                finding_b=new_findings[ib],
                description=description,
            )
        )
    return conflicts
```

- [ ] **Step 5: Overwrite the package __init__.py to re-export the new symbols**

Both `retrieval.py` (placeholder) and `synthesis.py` now exist, so this import resolves.

Overwrite `packages/service/src/synapse_service/__init__.py`:

```python
"""Synapse service: ingest API, synthesis, MCP retrieval server."""

from synapse_service.retrieval import query_findings
from synapse_service.synthesis import InMemorySynthesizer

__all__ = ["InMemorySynthesizer", "query_findings"]
```

- [ ] **Step 6: Run — verify PASS**

Run: `uv run pytest packages/service/tests/test_synthesis.py -v`
Expected: All tests PASS.

- [ ] **Step 7: Commit**

```bash
git add packages/service/src/synapse_service packages/service/tests
git commit -m "$(cat <<'EOF'
feat(service): InMemorySynthesizer — incremental merge (Plan C Task 4)

Per-shared_id working memory + conflicts. Each merge() rewrites the memory
(bounded by summarization) and flags disagreements between new findings.
Uses ModelProvider — swappable between Claude (off-target) and AI-100
(on-target) by config.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

### Task 5: LLM-as-retriever — `query_findings(...)` ranks by relevance

The MCP server's `query` tool needs to answer natural-language questions about the shared memory. At hackathon scale (tens of findings per session), we can feed all findings plus the query straight to the model and ask it to rank — no embeddings needed. Vector RAG is a documented stretch goal.

**Files:**
- Overwrite: `packages/service/src/synapse_service/retrieval.py`
- Create: `packages/service/tests/test_retrieval.py`

- [ ] **Step 1: Write the failing test suite**

Create `packages/service/tests/test_retrieval.py`:

```python
from datetime import datetime, timezone

import pytest

from synapse_contracts import Finding, SessionContext
from synapse_providers import FakeProvider
from synapse_service.retrieval import query_findings


UTC = timezone.utc


def _f(text: str, i: int) -> Finding:
    return Finding(
        type="learning",
        text=text,
        contributor="c",
        ts=datetime(2026, 7, 25, 12, i, tzinfo=UTC),
        source_session="local-abc",
    )


def _ctx() -> SessionContext:
    return SessionContext(
        shared_id="s1",
        purpose="Debug flaky auth tests",
        working_memory="Team investigated auth clock skew.",
        conflicts=[],
    )


@pytest.mark.asyncio
async def test_returns_ranked_findings_in_provider_order() -> None:
    all_findings = [_f("A", 0), _f("B", 1), _f("C", 2)]
    scripted = {"ranked": [{"index": 2}, {"index": 0}]}
    provider = FakeProvider(scripts=[scripted])

    ranked = await query_findings(
        query="What do we know about the auth issue?",
        context=_ctx(),
        findings=all_findings,
        provider=provider,
    )
    assert [f.text for f in ranked] == ["C", "A"]


@pytest.mark.asyncio
async def test_out_of_range_and_duplicate_indices_are_dropped() -> None:
    all_findings = [_f("A", 0), _f("B", 1)]
    scripted = {"ranked": [{"index": 99}, {"index": 0}, {"index": 0}]}
    provider = FakeProvider(scripts=[scripted])
    ranked = await query_findings(
        query="?",
        context=_ctx(),
        findings=all_findings,
        provider=provider,
    )
    assert [f.text for f in ranked] == ["A"]


@pytest.mark.asyncio
async def test_empty_findings_returns_empty_without_calling_provider() -> None:
    provider = FakeProvider(scripts=[])  # would raise if called
    ranked = await query_findings(
        query="anything",
        context=_ctx(),
        findings=[],
        provider=provider,
    )
    assert ranked == []
```

- [ ] **Step 2: Run — expect FAIL** (retrieval is a NotImplementedError placeholder)

Run: `uv run pytest packages/service/tests/test_retrieval.py -v`
Expected: FAIL — `NotImplementedError`.

- [ ] **Step 3: Implement retrieval**

Overwrite `packages/service/src/synapse_service/retrieval.py`:

```python
"""LLM-as-retriever — no embeddings, no vector DB.

At hackathon scale (tens of findings per session), sending the full findings
list and asking the model to rank them is fine. The model sees:
  - The query
  - The session purpose + current working memory (context)
  - All findings, indexed
and returns a list of indices in ranked order (best first).

Vector RAG is a scaling story we punt to a stretch goal.
"""

from __future__ import annotations

import logging
from typing import Any

from synapse_contracts import Finding, SessionContext
from synapse_providers import ModelProvider

log = logging.getLogger(__name__)


_SYSTEM_PROMPT = """You are a retrieval system for a team-intelligence store.

You are given:
  - QUERY: a natural-language question from an AI coding agent.
  - PURPOSE: the shared session's declared goal.
  - WORKING MEMORY: the team's current synthesized narrative.
  - FINDINGS: a numbered list of individual findings.

Return the indices of findings that are relevant to the query, in order of
relevance (best first). Skip irrelevant findings entirely — don't pad. If none
are relevant, return an empty ranked list.

Return exactly this JSON: {"ranked": [{"index": <int>}, ...]}"""

_RESPONSE_SCHEMA = {
    "type": "object",
    "properties": {
        "ranked": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {"index": {"type": "integer"}},
                "required": ["index"],
                "additionalProperties": False,
            },
        },
    },
    "required": ["ranked"],
    "additionalProperties": False,
}


async def query_findings(
    *,
    query: str,
    context: SessionContext,
    findings: list[Finding],
    provider: ModelProvider,
) -> list[Finding]:
    if not findings:
        return []

    findings_block = "\n".join(
        f"[{i}] ({f.contributor}, {f.type}) {f.text}" for i, f in enumerate(findings)
    )
    user_content = (
        f"QUERY:\n{query}\n\n"
        f"PURPOSE:\n{context.purpose}\n\n"
        f"WORKING MEMORY:\n{context.working_memory or '(empty)'}\n\n"
        f"FINDINGS:\n{findings_block}"
    )
    messages = [
        {"role": "system", "content": _SYSTEM_PROMPT},
        {"role": "user", "content": user_content},
    ]

    result = await provider.complete(messages=messages, response_schema=_RESPONSE_SCHEMA)
    data = result.data if isinstance(result.data, dict) else {}
    return _pick(data.get("ranked", []), findings)


def _pick(raw: Any, findings: list[Finding]) -> list[Finding]:
    if not isinstance(raw, list):
        return []
    picked: list[Finding] = []
    seen: set[int] = set()
    for item in raw:
        try:
            i = int(item["index"])
        except (KeyError, TypeError, ValueError):
            continue
        if i in seen or not (0 <= i < len(findings)):
            continue
        seen.add(i)
        picked.append(findings[i])
    return picked
```

- [ ] **Step 4: Run — verify PASS**

Run: `uv run pytest packages/service/tests/test_retrieval.py -v`
Expected: All tests PASS.

- [ ] **Step 5: Commit**

```bash
git add packages/service/src/synapse_service/retrieval.py packages/service/tests/test_retrieval.py
git commit -m "$(cat <<'EOF'
feat(service): LLM-as-retriever ranks findings by relevance (Plan C Task 5)

No embeddings, no vector DB — at hackathon scale, the model sees all
findings and returns indices in ranked order. Out-of-range / duplicate
indices are dropped silently.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

### Task 6: Ingest API — HTTP server implementation

Akhil's sync client already speaks this API. We build the server side that matches, storing sessions in-memory and delegating merges to `InMemorySynthesizer`.

**Files:**
- Create: `packages/service/src/synapse_service/ingest.py`
- Modify: `packages/service/pyproject.toml` (add starlette + uvicorn)
- Create: `packages/service/tests/test_ingest.py`

- [ ] **Step 1: Add web deps to the service package**

Edit `packages/service/pyproject.toml` — replace the dependencies block:

```toml
dependencies = [
    "synapse-contracts",
    "synapse-providers",
    "mcp>=1.0",
    "httpx>=0.27",
    "starlette>=0.40",
    "uvicorn>=0.30",
]
```

Run: `uv sync`

- [ ] **Step 2: Write the failing test suite using Starlette's TestClient**

Create `packages/service/tests/test_ingest.py`:

```python
from datetime import datetime, timezone

import pytest
from starlette.testclient import TestClient

from synapse_providers import FakeProvider
from synapse_service.ingest import build_app
from synapse_service.synthesis import InMemorySynthesizer


UTC = timezone.utc


@pytest.fixture
def app_and_state():
    """Build an app with a scripted synthesizer.

    Two scripts scheduled: the first response is for the initial merge triggered
    by push_findings; extend the list if tests trigger more merges.
    """
    provider = FakeProvider(scripts=[
        {"working_memory": "First merge memory", "conflicts": []},
        {"working_memory": "Second merge memory", "conflicts": []},
    ])
    synth = InMemorySynthesizer(provider=provider)
    app = build_app(synthesizer=synth)
    return app, synth


def test_create_session_returns_shared_id(app_and_state) -> None:
    app, _ = app_and_state
    client = TestClient(app)
    resp = client.post("/v1/sessions", json={"purpose": "Debug auth", "created_by": "sid"})
    assert resp.status_code == 200
    data = resp.json()
    assert "shared_id" in data
    assert data["shared_id"].startswith("shared-")


def test_join_and_push_findings_flow(app_and_state) -> None:
    app, synth = app_and_state
    client = TestClient(app)

    # Create
    shared_id = client.post("/v1/sessions", json={"purpose": "Debug", "created_by": "sid"}).json()["shared_id"]

    # Join
    r = client.post(
        f"/v1/sessions/{shared_id}/join",
        json={
            "shared_id": shared_id,
            "local_agent_session_id": "local-abc",
            "contributor": "akhil",
        },
    )
    assert r.status_code == 200
    assert r.json() == {"ok": True}

    # Push findings
    r = client.post(
        f"/v1/sessions/{shared_id}/findings",
        json={
            "shared_id": shared_id,
            "findings": [
                {
                    "type": "learning",
                    "text": "auth middleware rejects clock-skew > 60s",
                    "contributor": "akhil",
                    "ts": datetime(2026, 7, 25, 12, 0, tzinfo=UTC).isoformat(),
                    "source_session": "local-abc",
                }
            ],
        },
    )
    assert r.status_code == 200
    assert r.json() == {"accepted": 1}

    ctx = synth.get(shared_id)
    assert ctx is not None
    assert "First merge memory" in ctx.working_memory


def test_push_findings_on_unknown_session_400s(app_and_state) -> None:
    app, _ = app_and_state
    client = TestClient(app)
    r = client.post(
        "/v1/sessions/does-not-exist/findings",
        json={"shared_id": "does-not-exist", "findings": []},
    )
    assert r.status_code == 404


def test_shared_id_in_url_must_match_body(app_and_state) -> None:
    app, _ = app_and_state
    client = TestClient(app)
    shared_id = client.post("/v1/sessions", json={"purpose": "p", "created_by": "u"}).json()["shared_id"]
    r = client.post(
        f"/v1/sessions/{shared_id}/findings",
        json={"shared_id": "different", "findings": []},
    )
    assert r.status_code == 400
```

- [ ] **Step 3: Run — expect ImportError**

Run: `uv run pytest packages/service/tests/test_ingest.py -v`
Expected: FAIL.

- [ ] **Step 4: Implement the ingest server**

Create `packages/service/src/synapse_service/ingest.py`:

```python
"""Ingest API — Starlette app serving the Plan 0 ingest contracts.

Routes:
  POST /v1/sessions                         → create_session
  POST /v1/sessions/{shared_id}/join        → join_session
  POST /v1/sessions/{shared_id}/findings    → push_findings

State is in-memory: sessions, bindings, and findings-per-session live in a
single ServiceState instance. This is the hackathon-scale answer. Every push
triggers a synthesis merge; the resulting SessionContext is stored on the
InMemorySynthesizer.
"""

from __future__ import annotations

import logging
import secrets
from dataclasses import dataclass, field

from starlette.applications import Starlette
from starlette.exceptions import HTTPException
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route

from synapse_contracts import (
    CreateSessionRequest,
    CreateSessionResponse,
    Finding,
    JoinSessionRequest,
    JoinSessionResponse,
    PushFindingsRequest,
    PushFindingsResponse,
    SynapseSession,
)
from synapse_service.synthesis import InMemorySynthesizer

log = logging.getLogger(__name__)


@dataclass
class _State:
    synthesizer: InMemorySynthesizer
    sessions: dict[str, SynapseSession] = field(default_factory=dict)
    findings_by_session: dict[str, list[Finding]] = field(default_factory=dict)


def build_app(*, synthesizer: InMemorySynthesizer) -> Starlette:
    state = _State(synthesizer=synthesizer)

    async def create_session(request: Request) -> JSONResponse:
        req = CreateSessionRequest.model_validate(await request.json())
        shared_id = f"shared-{secrets.token_urlsafe(6)}"
        state.sessions[shared_id] = SynapseSession(
            shared_id=shared_id,
            purpose=req.purpose,
            members=[req.created_by],
            created_by=req.created_by,
        )
        state.findings_by_session[shared_id] = []
        resp = CreateSessionResponse(shared_id=shared_id)
        return JSONResponse(resp.model_dump())

    async def join_session(request: Request) -> JSONResponse:
        shared_id = request.path_params["shared_id"]
        req = JoinSessionRequest.model_validate(await request.json())
        if shared_id != req.shared_id:
            raise HTTPException(400, "URL shared_id and body shared_id mismatch")
        if shared_id not in state.sessions:
            raise HTTPException(404, f"session {shared_id} not found")
        session = state.sessions[shared_id]
        if req.contributor not in session.members:
            session.members.append(req.contributor)
        return JSONResponse(JoinSessionResponse(ok=True).model_dump())

    async def push_findings(request: Request) -> JSONResponse:
        shared_id = request.path_params["shared_id"]
        req = PushFindingsRequest.model_validate(await request.json())
        if shared_id != req.shared_id:
            raise HTTPException(400, "URL shared_id and body shared_id mismatch")
        if shared_id not in state.sessions:
            raise HTTPException(404, f"session {shared_id} not found")

        state.findings_by_session[shared_id].extend(req.findings)
        # Trigger a merge — this is the incremental synthesis step.
        session = state.sessions[shared_id]
        await synthesizer.merge(
            shared_id=shared_id, purpose=session.purpose, new_findings=req.findings
        )
        return JSONResponse(PushFindingsResponse(accepted=len(req.findings)).model_dump())

    app = Starlette(
        debug=False,
        routes=[
            Route("/v1/sessions", create_session, methods=["POST"]),
            Route("/v1/sessions/{shared_id}/join", join_session, methods=["POST"]),
            Route("/v1/sessions/{shared_id}/findings", push_findings, methods=["POST"]),
        ],
    )
    # Attach state so the MCP layer (Task 7) can reach the same store.
    app.state.synapse_state = state
    return app
```

- [ ] **Step 5: Run — verify PASS**

Run: `uv run pytest packages/service/tests/test_ingest.py -v`
Expected: All tests PASS. If Starlette's `TestClient` complains about not finding `httpx`, note it's already a dep from Plan A.

- [ ] **Step 6: Commit**

```bash
git add packages/service/pyproject.toml packages/service/src/synapse_service/ingest.py packages/service/tests/test_ingest.py uv.lock
git commit -m "$(cat <<'EOF'
feat(service): ingest API server (Plan C Task 6)

Starlette app serving create_session, join_session, push_findings against
the frozen ingest contracts. Every push triggers an incremental synthesis
merge. State is in-memory (hackathon scale); a session lookup key drives
per-shared_id isolation.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

### Task 7: MCP server — `create_session`, `join_session`, `query` over HTTP/SSE

**MCP Python SDK API note:** the SDK renamed `FastMCP` to `MCPServer`. Import from `mcp.server.mcpserver`. Tools are `@mcp.tool()` decorators; run with `mcp.run(transport="streamable-http")`.

**Files:**
- Create: `packages/service/src/synapse_service/mcp_server.py`
- Create: `packages/service/tests/test_mcp_server.py`

- [ ] **Step 1: Write the failing test suite using the MCP in-memory client**

Create `packages/service/tests/test_mcp_server.py`:

```python
"""MCP server tests using the in-memory client (no subprocess, no network)."""

from datetime import datetime, timezone

import pytest

from synapse_contracts import Finding, SessionContext
from synapse_providers import FakeProvider
from synapse_service.mcp_server import build_mcp_server


UTC = timezone.utc


@pytest.fixture
def mcp_and_state():
    """Build an MCP server with a scripted retrieval provider."""
    # Retrieval provider returns a ranking pointing at the single finding.
    retrieval_provider = FakeProvider(scripts=[
        {"ranked": [{"index": 0}]},
    ])
    # Synthesis provider not used in these tests (we seed findings directly).
    synth_provider = FakeProvider(scripts=[
        {"working_memory": "seeded", "conflicts": []},
    ])
    mcp, state = build_mcp_server(
        synthesis_provider=synth_provider,
        retrieval_provider=retrieval_provider,
    )
    return mcp, state


@pytest.mark.asyncio
async def test_create_session_tool_returns_shared_id(mcp_and_state) -> None:
    from mcp import Client

    mcp, _ = mcp_and_state
    async with Client(mcp) as client:
        result = await client.call_tool("create_session", {"purpose": "Debug", "created_by": "sid"})
        payload = result.structured_content
        assert "shared_id" in payload


@pytest.mark.asyncio
async def test_query_returns_ranked_findings(mcp_and_state) -> None:
    from mcp import Client

    mcp, state = mcp_and_state
    async with Client(mcp) as client:
        shared_id = (await client.call_tool(
            "create_session", {"purpose": "Debug", "created_by": "sid"}
        )).structured_content["shared_id"]

        # Seed a finding directly into state
        state.findings_by_session[shared_id] = [
            Finding(
                type="learning",
                text="auth middleware rejects clock-skew > 60s",
                contributor="siddsing",
                ts=datetime(2026, 7, 25, 12, 0, tzinfo=UTC),
                source_session="local-abc",
            )
        ]
        # Seed synthesis state so the retriever has context to work with
        state.synthesizer._state[shared_id] = state.synthesizer._state.get(
            shared_id
        ) or SessionContext(
            shared_id=shared_id, purpose="Debug", working_memory="", conflicts=[]
        )

        r = await client.call_tool("query", {"shared_id": shared_id, "query": "auth"})
        results = r.structured_content["results"]
        assert len(results) == 1
        assert "clock-skew" in results[0]["text"]
```

- [ ] **Step 2: Run — expect ImportError**

Run: `uv run pytest packages/service/tests/test_mcp_server.py -v`
Expected: FAIL.

- [ ] **Step 3: Implement the MCP server**

Create `packages/service/src/synapse_service/mcp_server.py`:

```python
"""MCP server — exposes three tools over HTTP/SSE.

Tools:
  - create_session(purpose, created_by)         → {shared_id}
  - join_session(shared_id, local_session_id, contributor) → {ok}
  - query(shared_id, query)                     → {results: [Finding, ...]}

Uses the MCP Python SDK's MCPServer (formerly FastMCP). In-process state is
shared with the ingest server via the returned _State object so a single
process can host both.
"""

from __future__ import annotations

import logging
import secrets
from dataclasses import dataclass, field
from typing import Any

from mcp.server.mcpserver import MCPServer

from synapse_contracts import Finding, SynapseSession
from synapse_providers import ModelProvider
from synapse_service.retrieval import query_findings
from synapse_service.synthesis import InMemorySynthesizer

log = logging.getLogger(__name__)


@dataclass
class MCPState:
    synthesizer: InMemorySynthesizer
    retrieval_provider: ModelProvider
    sessions: dict[str, SynapseSession] = field(default_factory=dict)
    findings_by_session: dict[str, list[Finding]] = field(default_factory=dict)


def build_mcp_server(
    *,
    synthesis_provider: ModelProvider,
    retrieval_provider: ModelProvider,
    port: int = 3333,
) -> tuple[MCPServer, MCPState]:
    state = MCPState(
        synthesizer=InMemorySynthesizer(provider=synthesis_provider),
        retrieval_provider=retrieval_provider,
    )
    # Port is a construction-time setting on MCPServer, not a run() kwarg.
    mcp = MCPServer("synapse", port=port)

    @mcp.tool()
    def create_session(purpose: str, created_by: str) -> dict[str, str]:
        """Start a new shared team-intelligence session and return its shared_id."""
        shared_id = f"shared-{secrets.token_urlsafe(6)}"
        state.sessions[shared_id] = SynapseSession(
            shared_id=shared_id,
            purpose=purpose,
            members=[created_by],
            created_by=created_by,
        )
        state.findings_by_session[shared_id] = []
        return {"shared_id": shared_id}

    @mcp.tool()
    def join_session(shared_id: str, local_session_id: str, contributor: str) -> dict[str, bool]:
        """Join an existing session by shared_id, binding a local agent session to it."""
        if shared_id not in state.sessions:
            return {"ok": False}
        session = state.sessions[shared_id]
        if contributor not in session.members:
            session.members.append(contributor)
        return {"ok": True}

    @mcp.tool()
    async def query(shared_id: str, query: str) -> dict[str, Any]:
        """Ask the shared team memory a question. Returns ranked findings, most relevant first."""
        if shared_id not in state.sessions:
            return {"results": []}
        findings = state.findings_by_session.get(shared_id, [])
        context = state.synthesizer.get(shared_id)
        if context is None:
            # No merge has happened yet.
            return {"results": []}
        ranked = await query_findings(
            query=query,
            context=context,
            findings=findings,
            provider=state.retrieval_provider,
        )
        return {"results": [f.model_dump(mode="json") for f in ranked]}

    return mcp, state


def run(
    *,
    synthesis_provider: ModelProvider,
    retrieval_provider: ModelProvider,
    port: int = 3333,
) -> None:  # pragma: no cover
    """Serve the MCP tools over Streamable HTTP for real agent clients.

    Called by the CLI entry point; not exercised in unit tests (which use the
    in-memory Client instead). Port is a construction-time setting.
    """
    mcp, _ = build_mcp_server(
        synthesis_provider=synthesis_provider,
        retrieval_provider=retrieval_provider,
        port=port,
    )
    mcp.run(transport="streamable-http", json_response=True)
```

- [ ] **Step 4: Run — verify PASS**

Run: `uv run pytest packages/service/tests/test_mcp_server.py -v`
Expected: All tests PASS.

- [ ] **Step 5: Commit**

```bash
git add packages/service/src/synapse_service/mcp_server.py packages/service/tests/test_mcp_server.py
git commit -m "$(cat <<'EOF'
feat(service): MCP server — create/join/query over HTTP/SSE (Plan C Task 7)

Uses the MCP Python SDK's MCPServer (formerly FastMCP). Three tools:
create_session, join_session, query. In-process state shared with the
ingest API so a single process hosts both. Tested via the in-memory
mcp.Client — no subprocess, no network in CI.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

### Task 8: One-process runner + demo config

Wire ingest + MCP into a single ASGI app or process. This is what runs on the AI-100 box.

**Files:**
- Create: `packages/service/src/synapse_service/app.py`
- Create: `packages/service/src/synapse_service/cli.py`
- Modify: `packages/service/pyproject.toml` (add CLI entry point)
- Create: `packages/service/tests/test_app_wiring.py`

- [ ] **Step 1: Write the failing wiring test**

Create `packages/service/tests/test_app_wiring.py`:

```python
"""Smoke test that both surfaces share the same state instance.

We don't spin up an HTTP server — we just prove the wiring composes right.
"""

import pytest
from starlette.testclient import TestClient

from synapse_providers import FakeProvider
from synapse_service.app import build_full_service


@pytest.mark.asyncio
async def test_ingest_push_is_visible_to_mcp_query() -> None:
    """After push_findings via HTTP, MCP's query tool sees the finding."""
    synth_provider = FakeProvider(scripts=[{"working_memory": "m", "conflicts": []}])
    retrieval_provider = FakeProvider(scripts=[{"ranked": [{"index": 0}]}])

    service = build_full_service(
        synthesis_provider=synth_provider,
        retrieval_provider=retrieval_provider,
    )
    client = TestClient(service.http_app)

    # Create + push
    shared_id = client.post("/v1/sessions", json={"purpose": "p", "created_by": "u"}).json()["shared_id"]
    from datetime import datetime, timezone
    UTC = timezone.utc
    push = client.post(
        f"/v1/sessions/{shared_id}/findings",
        json={
            "shared_id": shared_id,
            "findings": [{
                "type": "learning",
                "text": "shared with MCP",
                "contributor": "sid",
                "ts": datetime(2026, 7, 25, 12, 0, tzinfo=UTC).isoformat(),
                "source_session": "local-abc",
            }],
        },
    )
    assert push.status_code == 200

    # Same shared_id visible to MCP-side state
    from mcp import Client
    async with Client(service.mcp_server) as mcp_client:
        r = await mcp_client.call_tool("query", {"shared_id": shared_id, "query": "MCP"})
        assert r.structured_content["results"], (
            "MCP query returned no results — ingest and MCP are not sharing state"
        )
```

- [ ] **Step 2: Run — expect ImportError**

Run: `uv run pytest packages/service/tests/test_app_wiring.py -v`
Expected: FAIL.

- [ ] **Step 3: Implement the composed service**

Create `packages/service/src/synapse_service/app.py`:

```python
"""Compose ingest + MCP so both surfaces share one state.

Design note: the ingest server has its own _State (used by Starlette handlers)
and the MCP server has its own MCPState. To share them, we build the MCP
server first (which owns the synthesizer + findings map) and then hand that
state into a modified ingest app.

For the hackathon, we run them as two ASGI paths — MCP is mounted at /mcp,
ingest under /v1 — with a small shim that redirects ingest writes into the
MCP state. This is intentionally the simplest thing that works.
"""

from __future__ import annotations

import secrets
from dataclasses import dataclass

from starlette.applications import Starlette
from starlette.exceptions import HTTPException
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route

from synapse_contracts import (
    CreateSessionRequest,
    CreateSessionResponse,
    JoinSessionRequest,
    JoinSessionResponse,
    PushFindingsRequest,
    PushFindingsResponse,
    SynapseSession,
)
from synapse_providers import ModelProvider
from synapse_service.mcp_server import build_mcp_server


@dataclass
class Service:
    http_app: Starlette
    mcp_server: object  # MCPServer type from the MCP SDK


def build_full_service(
    *,
    synthesis_provider: ModelProvider,
    retrieval_provider: ModelProvider,
    mcp_port: int = 3333,
) -> Service:
    mcp, mcp_state = build_mcp_server(
        synthesis_provider=synthesis_provider,
        retrieval_provider=retrieval_provider,
        port=mcp_port,
    )

    async def create_session(request: Request) -> JSONResponse:
        req = CreateSessionRequest.model_validate(await request.json())
        shared_id = f"shared-{secrets.token_urlsafe(6)}"
        mcp_state.sessions[shared_id] = SynapseSession(
            shared_id=shared_id, purpose=req.purpose,
            members=[req.created_by], created_by=req.created_by,
        )
        mcp_state.findings_by_session[shared_id] = []
        return JSONResponse(CreateSessionResponse(shared_id=shared_id).model_dump())

    async def join_session(request: Request) -> JSONResponse:
        shared_id = request.path_params["shared_id"]
        req = JoinSessionRequest.model_validate(await request.json())
        if shared_id != req.shared_id:
            raise HTTPException(400, "shared_id mismatch")
        if shared_id not in mcp_state.sessions:
            raise HTTPException(404, "session not found")
        session = mcp_state.sessions[shared_id]
        if req.contributor not in session.members:
            session.members.append(req.contributor)
        return JSONResponse(JoinSessionResponse(ok=True).model_dump())

    async def push_findings(request: Request) -> JSONResponse:
        shared_id = request.path_params["shared_id"]
        req = PushFindingsRequest.model_validate(await request.json())
        if shared_id != req.shared_id:
            raise HTTPException(400, "shared_id mismatch")
        if shared_id not in mcp_state.sessions:
            raise HTTPException(404, "session not found")

        mcp_state.findings_by_session[shared_id].extend(req.findings)
        session = mcp_state.sessions[shared_id]
        await mcp_state.synthesizer.merge(
            shared_id=shared_id, purpose=session.purpose, new_findings=req.findings
        )
        return JSONResponse(PushFindingsResponse(accepted=len(req.findings)).model_dump())

    http_app = Starlette(
        routes=[
            Route("/v1/sessions", create_session, methods=["POST"]),
            Route("/v1/sessions/{shared_id}/join", join_session, methods=["POST"]),
            Route("/v1/sessions/{shared_id}/findings", push_findings, methods=["POST"]),
        ],
    )
    return Service(http_app=http_app, mcp_server=mcp)
```

Create `packages/service/src/synapse_service/cli.py`:

```python
"""synapse-service CLI — run ingest + MCP in one process.

Config via env vars:
  SYNAPSE_SYNTHESIZER = "aic100" | "claude" (default: "claude")
  SYNAPSE_RETRIEVAL   = "aic100" | "claude" (default: "claude")
  SYNAPSE_AIC100_BASE_URL = e.g. "http://ai100-box:8000/v1"
  SYNAPSE_AIC100_MODEL    = e.g. "meta-llama/Llama-3.2-1B-Instruct"
  SYNAPSE_INGEST_PORT   (default 8080)
  SYNAPSE_MCP_PORT      (default 3333)
"""

from __future__ import annotations

import logging
import os
import threading

import uvicorn

from synapse_providers import AIC100Provider, ClaudeProvider, ModelProvider
from synapse_service.app import build_full_service


def _make_provider(mode: str) -> ModelProvider:
    if mode == "aic100":
        base = os.environ["SYNAPSE_AIC100_BASE_URL"]
        model = os.environ.get("SYNAPSE_AIC100_MODEL", "meta-llama/Llama-3.2-1B-Instruct")
        return AIC100Provider(base_url=base, model=model)
    return ClaudeProvider()


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    synth = _make_provider(os.environ.get("SYNAPSE_SYNTHESIZER", "claude"))
    retrieval = _make_provider(os.environ.get("SYNAPSE_RETRIEVAL", "claude"))

    mcp_port = int(os.environ.get("SYNAPSE_MCP_PORT", "3333"))
    service = build_full_service(
        synthesis_provider=synth,
        retrieval_provider=retrieval,
        mcp_port=mcp_port,
    )

    ingest_port = int(os.environ.get("SYNAPSE_INGEST_PORT", "8080"))

    # MCP server runs its own event loop; run it in a thread so the process
    # can serve both surfaces concurrently. Port was set at construction time.
    def _run_mcp() -> None:
        service.mcp_server.run(transport="streamable-http", json_response=True)

    threading.Thread(target=_run_mcp, daemon=True).start()

    uvicorn.run(service.http_app, host="0.0.0.0", port=ingest_port)


if __name__ == "__main__":
    main()
```

Edit `packages/service/pyproject.toml` to expose the CLI. Replace the whole file with:

```toml
[project]
name = "synapse-service"
version = "0.1.0"
requires-python = ">=3.12"
dependencies = [
    "synapse-contracts",
    "synapse-providers",
    "mcp>=1.0",
    "httpx>=0.27",
    "starlette>=0.40",
    "uvicorn>=0.30",
]

[project.scripts]
synapse-service = "synapse_service.cli:main"

[build-system]
requires = ["hatchling"]
build-backend = "hatchling.build"

[tool.hatch.build.targets.wheel]
packages = ["src/synapse_service"]

[tool.uv.sources]
synapse-contracts = { workspace = true }
synapse-providers = { workspace = true }
```

Run: `uv sync`

- [ ] **Step 4: Run — verify PASS**

Run: `uv run pytest packages/service/tests/test_app_wiring.py -v`
Expected: All tests PASS.

- [ ] **Step 5: Turn the walking-skeleton test fully green**

Run: `uv run pytest tests/test_walking_skeleton.py -v`
Expected: The `InMemorySynthesizer` and `query_findings` imports now succeed. The XFAILing test passes → XPASS (which pytest treats as passing since `strict=False`).

- [ ] **Step 6: Commit**

```bash
git add packages/service uv.lock
git commit -m "$(cat <<'EOF'
feat(service): one-process service composing ingest + MCP (Plan C Task 8)

MCP server + Starlette ingest share the same MCPState instance so an ingest
push is immediately visible to an MCP query. CLI reads SYNAPSE_SYNTHESIZER
and SYNAPSE_RETRIEVAL from env to swap between Claude (off-target) and
AI-100 (on-target). The walking-skeleton test is now green end-to-end.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

### Task 9: Exit criteria

- [ ] **Step 1: Full suite green**

Run: `uv run pytest -v`
Expected: Every test passes, including the walking skeleton.

- [ ] **Step 2: Lint**

Run: `uv run ruff check . && uv run ruff format --check .`
Expected: Clean.

- [ ] **Step 3: End-to-end smoke test**

With `ANTHROPIC_API_KEY` set, run the service:

```bash
uv run synapse-service &
SERVICE_PID=$!
sleep 2

# Create a session
SHARED_ID=$(curl -sS -X POST http://localhost:8080/v1/sessions \
    -H 'content-type: application/json' \
    -d '{"purpose":"Demo","created_by":"sid"}' | jq -r .shared_id)

# Push a finding
curl -sS -X POST http://localhost:8080/v1/sessions/$SHARED_ID/findings \
    -H 'content-type: application/json' \
    -d '{
        "shared_id": "'"$SHARED_ID"'",
        "findings": [
            {
                "type": "learning",
                "text": "The synthesis service is working end-to-end.",
                "contributor": "sid",
                "ts": "2026-07-25T12:00:00Z",
                "source_session": "local-demo"
            }
        ]
    }'

kill $SERVICE_PID
```

Expected: 200 responses; a real Claude synthesis merge happens for the pushed finding.

- [ ] **Step 4: AI-100 spike verdict recorded**

Fill in `docs/spikes/2026-07-26-ai100-spike.md` § Go/No-go verdict with real numbers from Task 3.

- [ ] **Step 5: Confirm hand-off**

- Akhil's `SyncClient` can now talk to a live service (previously mocked).
- Aditya's `run_benchmark.py --providers claude ollama` can point at the running service if we ever want to end-to-end benchmark the full loop (not required for the demo).
- Config-flip demo works: set `SYNAPSE_SYNTHESIZER=aic100` + AI-100 env vars and restart the service; no code changes.

---

## Scope / YAGNI

**In (Plan C):** ClaudeProvider, OpenAI-compatible base + Ollama/AIC100 subclasses, AI-100 spike + runbook, InMemorySynthesizer with per-shared_id state and Conflict detection, LLM-as-retriever, ingest server, MCP server (create/join/query), one-process runner.

**Out (stretch):**
- Vector-embedding retrieval (`embed()` method on ModelProvider, external vector DB) — noted as a scaling story in the design; not in the demo path.
- Cross-session persistence (findings live only in memory; a service restart wipes them) — hackathon-scale acceptable.
- Auth beyond opt-in join (anyone with the URL can push/query) — noted in the design as intentional.
- Vault credentials, deployments, multiagent MCP — these are Managed Agents features; our MCP surface is intentionally small.
- Streaming synthesis (progress events as merges happen) — not on the demo path.

## Known Risks

| Risk | Mitigation |
|---|---|
| AI-100 doesn't come up by Day 2 | `SYNAPSE_SYNTHESIZER=claude` fallback works with zero code changes; the "one privacy boundary, three targets" pitch still holds |
| vLLM `qaic` backend doesn't honor `response_format: json_schema` | Distiller's tolerant parse absorbs it; report per-provider schema-valid rate in the benchmark |
| MCP SDK version drift renames `MCPServer` back to `FastMCP` (or another rename) | Pinned in `pyproject.toml` (`mcp>=1.0`); if the import breaks, pin more tightly |
| Threading MCP+ASGI in one process introduces race on `mcp_state` | State mutations happen inside async handlers on either surface; Python's GIL + async single-threaded execution keeps this safe at hackathon scale. Not a distributed system — no need for locks. |
| Synthesizer prompt drifts from what Claude / Llama actually produce | Test coverage uses `FakeProvider` with scripted outputs; real behavior falls out of the demo. Fine-tune the prompt if the demo memory quality is weak — the prompt is not a hard contract |
