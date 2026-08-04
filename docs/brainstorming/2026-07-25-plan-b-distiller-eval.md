# Synapse — Plan B: Distiller + Eval Harness (Aditya)

> # ⚠️ SUPERSEDED 2026-08-03
> **Do not execute this plan.** It was written against the pre-revision architecture (remote MCP server, worker→service egress, pre-Attribution contracts) and carries inline code snippets whose shapes no longer exist.
>
> Current plans live in [`docs/plans/`](../plans/README.md). Vocabulary in `/CONTEXT.md`.
> Kept for history — the contract block in Plan 0 Task 2 remains the source the new Plan 0 copies from.

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Turn a `Segment` into a validated `Finding[]` by calling the model behind `ModelProvider`, produce an on-target NPU implementation, and build the eval harness that measures quality-vs-Claude, cost, and latency — the numbers that answer *"why not just call Claude?"*.

**Architecture:** `Distiller(provider: ModelProvider, contributor: str)` renders a Segment into a prompt + `Finding[]` JSON schema, calls `provider.complete()`, validates the result, and returns `Finding[]`. Providers with `native_structured_output=True` (Claude, vLLM) get schema-validated output directly; those without (likely NPU) fall back to prompt-instructed JSON + tolerant parse + one retry. The eval harness runs the fixture corpus through any provider and produces a `(quality, cost, latency)` table.

**Tech Stack:** Python 3.12, pytest, Pydantic v2, ONNX Runtime + QNN Execution Provider (X Elite laptop), Ollama (Mac fallback), Anthropic SDK (baseline).

**Owner:** Aditya (owns the X Elite laptop for the NPU spike).

> **⟨CONTRACT REVISION 2026-08-03⟩** — the frozen contracts changed. Inline snippets below still show the old shapes; see the revision table at the top of `2026-07-25-plan-0-foundation.md` before writing code. Affecting this plan most: the distiller stamps `Finding.id` and builds `attributions: list[Attribution]` rather than a bare `contributor`; `provenance` defaults to `distilled`; `status`/`merged_from`/`merged_into` are service-written and producers leave them at defaults. Vocabulary: `/CONTEXT.md`.

> **⟨AMENDED 2026-08-03 D⟩** — see `2026-08-03-agent-detection-demo-storage-amendment.md`. (1) Spike/eval measurements (usable compiled context, prefill tok/s) populate **per-model capability rows** that the segmenter's budget derivation reads — one row per candidate model, not one global number. (2) The distiller prompt is **explicitly first-pass** (Claude-suggested); the eval loop + failure-analysis → prompt-revision loop is the tuning mechanism, and the prompt is not a contract. (3) The eval harness's usage/latency numbers also feed the **pre-recorded A/B demo** (baseline vs Synapse deltas: tokens, wall-clock, duplicated exploration).

> **⟨AMENDED 2026-08-03 E — measured evidence⟩** — see `2026-07-30-npu-llm-benchmarks-and-geniex-findings.md`; supersedes the ⟨A⟩ spike notes where they disagree. (1) Task 4 step 1 (`qairt` from AI Hub) is **blocked** (AI Hub 503) — start at step 2, `llama_cpp` GGUF, already validated live; pull models from Docker Hub/HF and cache now. (2) **Add a GPU arm** to the bake-off (Adreno = 2.9× NPU at 1.7B) and note the NPU is the slowest unit for decode — the NPU case is contention + power, and **power is still unmeasured**. (3) Task 5 is **done in spirit**: `serve` verified OpenAI-shaped live. (4) **Mandatory guards** from the Gemma-4-E4B `vlm` prompt-drop bug: assert `usage.prompt_tokens > 1` on every distiller call, add a canary fixture that fails a model before it is scored, verify `ModelType` after every pull (fix: `geniex model set-type <model> llm`). (5) Probe whether `--enable-json` / GBNF reaches `serve`'s HTTP API — if yes, `native_structured_output=True` on the edge.

**Prerequisites (from Plan 0):** `Segment` and `Finding` schemas frozen, `ModelProvider` + `FakeProvider` shipped, fixture Segments + golden Findings committed. From Plan C: `ClaudeProvider` (Aditya can build against a mocked adapter until C1–C3 land, but the eval harness needs a real Claude call to establish the baseline).

**Handoff to other tracks:** `Distiller` (Task 3) plugs into `EdgeWorker` from Plan A Task 5 (matches the `DistillerLike` protocol). `build_default_distiller()` (Task 3) is what the worker CLI imports.

---

### Task 1: Design the distillation prompt + response schema

**Files:**
- Create: `packages/worker/src/synapse_worker/distiller/__init__.py`
- Create: `packages/worker/src/synapse_worker/distiller/prompt.py`
- Create: `packages/worker/src/synapse_worker/distiller/schema.py`
- Create: `packages/worker/tests/test_prompt.py`
- Create: `packages/worker/tests/test_schema.py`

- [ ] **Step 1: Write the failing test for the response schema**

Create `packages/worker/tests/test_schema.py`:

```python
from synapse_worker.distiller.schema import FINDINGS_RESPONSE_SCHEMA


def test_schema_is_json_schema_object() -> None:
    assert FINDINGS_RESPONSE_SCHEMA["type"] == "object"
    assert "findings" in FINDINGS_RESPONSE_SCHEMA["properties"]


def test_findings_are_a_typed_array() -> None:
    fs = FINDINGS_RESPONSE_SCHEMA["properties"]["findings"]
    assert fs["type"] == "array"
    item = fs["items"]
    assert item["type"] == "object"
    assert set(item["required"]) >= {"type", "text"}
    # Enumerated type
    assert set(item["properties"]["type"]["enum"]) == {
        "learning",
        "decision",
        "dead_end",
        "open_question",
    }


def test_schema_is_strict() -> None:
    """additionalProperties: false on every object — required for strict mode."""
    item = FINDINGS_RESPONSE_SCHEMA["properties"]["findings"]["items"]
    assert item.get("additionalProperties") is False
    assert FINDINGS_RESPONSE_SCHEMA.get("additionalProperties") is False
```

- [ ] **Step 2: Write the failing test for prompt rendering**

Create `packages/worker/tests/test_prompt.py`:

```python
import json
from datetime import datetime, timezone
from pathlib import Path

from synapse_contracts import AgentEvent, Segment
from synapse_worker.distiller.prompt import render_prompt


UTC = timezone.utc
FIXTURES = Path(__file__).parent.parent.parent.parent / "fixtures"


def test_prompt_includes_all_content_blocks() -> None:
    events = [
        AgentEvent(role="user", kind="text", content="Debug flaky tests",
                   ts=datetime(2026, 7, 25, 12, 0, tzinfo=UTC), session_id="s"),
        AgentEvent(role="assistant", kind="tool_use", content='{"pattern":"iat"}',
                   tool_name="Grep", ts=datetime(2026, 7, 25, 12, 1, tzinfo=UTC), session_id="s"),
        AgentEvent(role="user", kind="tool_result", content="line 88: iat check",
                   ts=datetime(2026, 7, 25, 12, 2, tzinfo=UTC), session_id="s"),
    ]
    seg = Segment(id="seg-x", session_id="s", events=events,
                  started_at=events[0].ts, ended_at=events[-1].ts)
    messages = render_prompt(seg)
    system_and_user = "\n".join(m["content"] for m in messages)
    assert "Debug flaky tests" in system_and_user
    assert "Grep" in system_and_user
    assert "line 88: iat check" in system_and_user


def test_prompt_instructs_the_four_finding_types() -> None:
    seg = Segment.model_validate(json.loads((FIXTURES / "segments" / "seg-001.json").read_text()))
    messages = render_prompt(seg)
    body = "\n".join(m["content"] for m in messages)
    for t in ("learning", "decision", "dead_end", "open_question"):
        assert t in body


def test_prompt_forbids_verbatim_code() -> None:
    """The distiller's contract: findings must be abstracted, not verbatim."""
    seg = Segment.model_validate(json.loads((FIXTURES / "segments" / "seg-001.json").read_text()))
    messages = render_prompt(seg)
    body = "\n".join(m["content"] for m in messages).lower()
    # The prompt must communicate this to the model; exact wording is flexible.
    assert "abstract" in body or "not verbatim" in body or "not raw code" in body
```

- [ ] **Step 3: Run — expect ImportError**

Run: `uv run pytest packages/worker/tests/test_prompt.py packages/worker/tests/test_schema.py -v`
Expected: FAIL — module not found.

- [ ] **Step 4: Implement the schema and prompt renderer**

Create `packages/worker/src/synapse_worker/distiller/__init__.py`:

```python
"""Distiller — turn a Segment into a validated Finding[]."""

from synapse_worker.distiller.core import Distiller, build_default_distiller

__all__ = ["Distiller", "build_default_distiller"]
```

Create `packages/worker/src/synapse_worker/distiller/schema.py`:

```python
"""Strict JSON Schema for the distiller's response.

Passed to ModelProvider.complete(..., response_schema=...). Providers with
native_structured_output=True enforce it directly; providers without it use
this schema to validate a tolerant parse.
"""

FINDING_ITEM_SCHEMA = {
    "type": "object",
    "properties": {
        "type": {
            "type": "string",
            "enum": ["learning", "decision", "dead_end", "open_question"],
        },
        "text": {"type": "string", "minLength": 1},
        "refs": {"type": "array", "items": {"type": "string"}},
    },
    "required": ["type", "text"],
    "additionalProperties": False,
}

FINDINGS_RESPONSE_SCHEMA = {
    "type": "object",
    "properties": {
        "findings": {"type": "array", "items": FINDING_ITEM_SCHEMA},
    },
    "required": ["findings"],
    "additionalProperties": False,
}
```

Create `packages/worker/src/synapse_worker/distiller/prompt.py`:

```python
"""Prompt renderer — Segment → OpenAI-style messages list.

The system prompt encodes the distiller's contract:
- Extract four finding types (learning, decision, dead_end, open_question)
- Findings must be ABSTRACTED, not verbatim code (privacy boundary)
- refs are optional, high-level pointers (file:line or module names)
"""

from __future__ import annotations

from synapse_contracts import Segment

SYSTEM_PROMPT = """You are a code-review analyst distilling a coding-agent session into shared team intelligence.

Given a sequence of agent events (user requests, assistant reasoning, tool calls, tool results), extract structured findings of exactly these four types:

  - learning:       something discovered about the codebase, the problem, or the tools
  - decision:       a choice the agent made and committed to
  - dead_end:       an approach that was tried and did not work
  - open_question:  something the agent flagged as unresolved

Rules:
  1. Findings must be ABSTRACTED. Do not copy verbatim code, tokens, file contents, or user data. Describe *what was learned or decided* in your own words. This is a privacy boundary — raw work stays on the device.
  2. Focus on information a teammate would find useful. Skip trivial tool calls (e.g. a single `ls` to orient) unless they reveal something.
  3. Prefer specificity over completeness. Two precise findings beat five vague ones.
  4. Include a `refs` array of file:line pointers or module names when they anchor the finding — but keep them short and public-safe.
  5. If nothing meaningful happened in the segment, return an empty findings array. Do not invent findings.

Return a JSON object matching the schema exactly. No prose outside the JSON."""


def render_prompt(segment: Segment) -> list[dict[str, str]]:
    lines: list[str] = []
    for e in segment.events:
        prefix = f"[{e.role}:{e.kind}]"
        if e.tool_name:
            prefix += f" tool={e.tool_name}"
        lines.append(f"{prefix} {e.content}")
    user_prompt = "Segment events (chronological):\n\n" + "\n".join(lines)
    return [
        {"role": "system", "content": SYSTEM_PROMPT},
        {"role": "user", "content": user_prompt},
    ]
```

- [ ] **Step 5: Run — verify PASS**

Run: `uv run pytest packages/worker/tests/test_prompt.py packages/worker/tests/test_schema.py -v`
Expected: All tests PASS.

- [ ] **Step 6: Commit**

```bash
git add packages/worker/src/synapse_worker/distiller/__init__.py packages/worker/src/synapse_worker/distiller/prompt.py packages/worker/src/synapse_worker/distiller/schema.py packages/worker/tests/test_prompt.py packages/worker/tests/test_schema.py
git commit -m "$(cat <<'EOF'
feat(distiller): freeze prompt + response schema (Plan B Task 1)

System prompt encodes the four Finding types and the abstraction rule
(no verbatim code — the privacy boundary made explicit to the model).
Strict JSON Schema for the response, additionalProperties:false throughout.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

### Task 2: Tolerant JSON parser for providers without native structured output

**Files:**
- Create: `packages/worker/src/synapse_worker/distiller/parse.py`
- Create: `packages/worker/tests/test_parse.py`

- [ ] **Step 1: Write the failing test suite**

Create `packages/worker/tests/test_parse.py`:

```python
import pytest

from synapse_worker.distiller.parse import ParseError, tolerant_parse_json


def test_parses_clean_json_directly() -> None:
    src = '{"findings": []}'
    assert tolerant_parse_json(src) == {"findings": []}


def test_extracts_json_from_fenced_code_block() -> None:
    src = 'Here is the output:\n```json\n{"findings": []}\n```\nDone.'
    assert tolerant_parse_json(src) == {"findings": []}


def test_extracts_first_balanced_json_object() -> None:
    src = 'Some preamble text.\n{"findings": [{"type": "learning", "text": "x"}]}\nTrailing.'
    result = tolerant_parse_json(src)
    assert result["findings"][0]["type"] == "learning"


def test_handles_trailing_commas() -> None:
    src = '{"findings": [{"type": "learning", "text": "x",},]}'
    result = tolerant_parse_json(src)
    assert len(result["findings"]) == 1


def test_raises_on_truly_broken_output() -> None:
    with pytest.raises(ParseError):
        tolerant_parse_json("no JSON anywhere in this response")


def test_raises_on_empty_string() -> None:
    with pytest.raises(ParseError):
        tolerant_parse_json("")
```

- [ ] **Step 2: Run — expect ImportError**

Run: `uv run pytest packages/worker/tests/test_parse.py -v`
Expected: FAIL — `ImportError`.

- [ ] **Step 3: Implement the tolerant parser**

Create `packages/worker/src/synapse_worker/distiller/parse.py`:

```python
"""Tolerant JSON extraction for models without native structured output.

Small SLMs sometimes wrap JSON in prose, use fenced code blocks, or emit
trailing commas. This parser tries plain json.loads first, then progressively
looser strategies. If none work, raises ParseError so the caller can retry
with a stricter prompt or drop the segment.
"""

from __future__ import annotations

import json
import re
from typing import Any


class ParseError(ValueError):
    """The model output could not be parsed as JSON by any strategy."""


_FENCE_RE = re.compile(r"```(?:json)?\s*(.*?)\s*```", re.DOTALL)
_TRAILING_COMMA_RE = re.compile(r",(\s*[\]}])")


def tolerant_parse_json(text: str) -> Any:
    """Try increasingly loose strategies to extract a JSON object from `text`."""
    text = (text or "").strip()
    if not text:
        raise ParseError("empty response")

    # 1. Straight parse.
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass

    # 2. Fenced code block.
    m = _FENCE_RE.search(text)
    if m:
        try:
            return json.loads(m.group(1))
        except json.JSONDecodeError:
            pass

    # 3. Substring from first `{` to matching balanced `}` (greedy scan).
    start = text.find("{")
    if start != -1:
        depth = 0
        for i in range(start, len(text)):
            c = text[i]
            if c == "{":
                depth += 1
            elif c == "}":
                depth -= 1
                if depth == 0:
                    candidate = text[start : i + 1]
                    # 4. Try candidate; if it fails, strip trailing commas and retry.
                    for variant in (candidate, _TRAILING_COMMA_RE.sub(r"\1", candidate)):
                        try:
                            return json.loads(variant)
                        except json.JSONDecodeError:
                            continue
                    break

    raise ParseError(f"no parseable JSON found in {text[:120]!r}")
```

- [ ] **Step 4: Run — verify PASS**

Run: `uv run pytest packages/worker/tests/test_parse.py -v`
Expected: All tests PASS.

- [ ] **Step 5: Commit**

```bash
git add packages/worker/src/synapse_worker/distiller/parse.py packages/worker/tests/test_parse.py
git commit -m "$(cat <<'EOF'
feat(distiller): tolerant JSON parser (Plan B Task 2)

Handles fenced code blocks, prose-wrapped JSON, and trailing commas.
Used by providers that lack native structured-output guarantees (NPU
ONNX-QNN path typically). Raises ParseError on genuinely broken output
so the Distiller can retry or drop.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

### Task 3: `Distiller` — the core `Segment → Finding[]` component

**Files:**
- Create: `packages/worker/src/synapse_worker/distiller/core.py`
- Create: `packages/worker/tests/test_distiller.py`

- [ ] **Step 1: Write the failing test suite**

Create `packages/worker/tests/test_distiller.py`:

```python
import json
from datetime import datetime, timezone
from pathlib import Path

import pytest

from synapse_contracts import Finding, FindingType, Segment
from synapse_providers import FakeProvider
from synapse_providers.base import ProviderCapabilities

from synapse_worker.distiller import Distiller


UTC = timezone.utc
FIXTURES = Path(__file__).parent.parent.parent.parent / "fixtures"


def _load_segment(name: str) -> Segment:
    return Segment.model_validate(json.loads((FIXTURES / "segments" / name).read_text()))


def _scripted_findings(count: int, kind: str = "learning") -> dict:
    return {
        "findings": [
            {"type": kind, "text": f"finding {i}", "refs": []} for i in range(count)
        ]
    }


@pytest.mark.asyncio
async def test_distills_scripted_findings_into_finding_objects() -> None:
    seg = _load_segment("seg-001.json")
    provider = FakeProvider(scripts=[_scripted_findings(2)])
    d = Distiller(provider=provider, contributor="siddsing")

    findings = await d.distill(seg)

    assert len(findings) == 2
    for f in findings:
        assert isinstance(f, Finding)
        assert f.contributor == "siddsing"
        assert f.source_session == seg.session_id
        # ts is auto-assigned to segment end time
        assert f.ts == seg.ended_at


@pytest.mark.asyncio
async def test_all_four_finding_types_round_trip() -> None:
    seg = _load_segment("seg-001.json")
    scripted = {
        "findings": [
            {"type": "learning", "text": "L"},
            {"type": "decision", "text": "D"},
            {"type": "dead_end", "text": "X"},
            {"type": "open_question", "text": "Q"},
        ]
    }
    provider = FakeProvider(scripts=[scripted])
    d = Distiller(provider=provider, contributor="c")
    findings = await d.distill(seg)
    assert {f.type for f in findings} == set(FindingType)


@pytest.mark.asyncio
async def test_empty_segment_returns_empty_list() -> None:
    seg = _load_segment("seg-001.json")
    provider = FakeProvider(scripts=[{"findings": []}])
    d = Distiller(provider=provider, contributor="c")
    findings = await d.distill(seg)
    assert findings == []


@pytest.mark.asyncio
async def test_tolerant_parse_when_provider_returns_string() -> None:
    """A provider without native_structured_output returns text; the Distiller
    parses it tolerantly using the schema from Task 1 / parser from Task 2."""
    seg = _load_segment("seg-001.json")
    text = 'Here you go:\n```json\n{"findings": [{"type":"learning","text":"x"}]}\n```'

    class _PromptedProvider(FakeProvider):
        @property
        def capabilities(self) -> ProviderCapabilities:
            return ProviderCapabilities(native_structured_output=False, streaming=False)

    provider = _PromptedProvider(scripts=[text])
    d = Distiller(provider=provider, contributor="c")
    findings = await d.distill(seg)
    assert len(findings) == 1
    assert findings[0].type == FindingType.LEARNING


@pytest.mark.asyncio
async def test_retries_once_on_malformed_output() -> None:
    """First call returns garbage; second call returns valid JSON. Distiller
    retries once and succeeds."""
    seg = _load_segment("seg-001.json")

    class _PromptedProvider(FakeProvider):
        @property
        def capabilities(self) -> ProviderCapabilities:
            return ProviderCapabilities(native_structured_output=False, streaming=False)

    provider = _PromptedProvider(scripts=["no json here at all", _scripted_findings(1)])
    d = Distiller(provider=provider, contributor="c")
    findings = await d.distill(seg)
    assert len(findings) == 1


@pytest.mark.asyncio
async def test_returns_empty_after_retries_exhausted() -> None:
    """Two malformed outputs → drop the segment (don't crash the worker)."""
    seg = _load_segment("seg-001.json")

    class _PromptedProvider(FakeProvider):
        @property
        def capabilities(self) -> ProviderCapabilities:
            return ProviderCapabilities(native_structured_output=False, streaming=False)

    provider = _PromptedProvider(scripts=["garbage 1", "garbage 2"])
    d = Distiller(provider=provider, contributor="c")
    findings = await d.distill(seg)
    assert findings == []


@pytest.mark.asyncio
async def test_invalid_finding_type_from_model_is_dropped() -> None:
    """A well-formed JSON where one item has an unknown type: keep the valid ones."""
    seg = _load_segment("seg-001.json")
    scripted = {
        "findings": [
            {"type": "learning", "text": "ok"},
            {"type": "bogus", "text": "drop"},
            {"type": "decision", "text": "ok"},
        ]
    }
    provider = FakeProvider(scripts=[scripted])
    d = Distiller(provider=provider, contributor="c")
    findings = await d.distill(seg)
    assert len(findings) == 2
    assert [f.type for f in findings] == [FindingType.LEARNING, FindingType.DECISION]
```

- [ ] **Step 2: Run — expect ImportError**

Run: `uv run pytest packages/worker/tests/test_distiller.py -v`
Expected: FAIL — `ImportError`.

- [ ] **Step 3: Implement the Distiller**

Create `packages/worker/src/synapse_worker/distiller/core.py`:

```python
"""Distiller — Segment → Finding[] via a ModelProvider.

Flow:
  1. Render segment into messages (prompt.py)
  2. Call provider.complete(messages, response_schema=FINDINGS_RESPONSE_SCHEMA)
  3. If provider has native structured output: use ModelResult.data directly.
     Otherwise: tolerant-parse the text; on ParseError, retry once with the
     same prompt. Two failures → return [] (drop the segment).
  4. For each item: validate against Finding, tagging contributor + timestamp
     from the segment. Drop items that don't validate (unknown type, missing
     text, etc.) — never propagate a partial-bad Finding.

The Distiller is the boundary that enforces the *abstraction* rule
architecturally: whatever the model produces must fit the Finding schema, and
the prompt tells the model to keep it abstract. This is the "raw work stays
on device" guarantee made concrete.
"""

from __future__ import annotations

import logging
import os
from typing import Any

from pydantic import ValidationError

from synapse_contracts import Finding, Segment
from synapse_providers import FakeProvider, ModelProvider

from synapse_worker.distiller.parse import ParseError, tolerant_parse_json
from synapse_worker.distiller.prompt import render_prompt
from synapse_worker.distiller.schema import FINDINGS_RESPONSE_SCHEMA

log = logging.getLogger(__name__)


class Distiller:
    """Segment → Finding[].

    Failure modes:
      - Provider raises: log + return [] (segment dropped; worker keeps going).
      - Malformed structured output: retry once; still bad → return [].
      - Per-item validation failure: drop that item; keep the rest.
    """

    def __init__(self, *, provider: ModelProvider, contributor: str) -> None:
        self._provider = provider
        self._contributor = contributor

    async def distill(self, segment: Segment) -> list[Finding]:
        messages = render_prompt(segment)

        for attempt in (0, 1):
            try:
                result = await self._provider.complete(
                    messages=messages,
                    response_schema=FINDINGS_RESPONSE_SCHEMA,
                )
            except Exception:
                log.exception("provider failed on segment %s (attempt %d)", segment.id, attempt)
                if attempt == 1:
                    return []
                continue

            parsed = self._extract(
                result.data,
                native=self._provider.capabilities.native_structured_output,
            )
            if parsed is None:
                log.warning("could not parse distiller output for %s (attempt %d)", segment.id, attempt)
                continue

            return self._to_findings(parsed, segment)

        return []

    def _extract(self, data: Any, *, native: bool) -> dict | None:
        """Turn provider-returned data into a `dict` matching the response schema.

        native=True means the provider guarantees schema-valid structured output
        (e.g. Claude with output_config.format, vLLM with json_schema response_format),
        so data should already be a dict. When native=False (the NPU path), data
        is the raw text and we always tolerant-parse it.
        """
        if native and isinstance(data, dict):
            return data
        if isinstance(data, str):
            try:
                return tolerant_parse_json(data)
            except ParseError:
                return None
        # Non-native provider returned something non-string, or native provider
        # returned something non-dict — try to coerce as a last resort.
        if isinstance(data, dict):
            return data
        log.warning("unexpected data type %s from provider", type(data).__name__)
        return None

    def _to_findings(self, parsed: dict, segment: Segment) -> list[Finding]:
        items = parsed.get("findings", [])
        if not isinstance(items, list):
            log.warning("'findings' is not a list on segment %s", segment.id)
            return []

        findings: list[Finding] = []
        for i, item in enumerate(items):
            try:
                finding = Finding(
                    type=item["type"],
                    text=item["text"],
                    contributor=self._contributor,
                    ts=segment.ended_at,
                    source_session=segment.session_id,
                    refs=item.get("refs", []) or [],
                )
            except (KeyError, ValidationError, TypeError) as e:
                log.debug("dropping invalid finding %d in %s: %s", i, segment.id, e)
                continue
            findings.append(finding)
        return findings


def build_default_distiller() -> Distiller:
    """Convenience factory used by the worker CLI (Plan A Task 5).

    Picks a provider from environment configuration; falls back to a FakeProvider
    that emits an empty findings list so the CLI stays runnable end-to-end even
    before Plan C's real providers are wired up.

    Config precedence:
      SYNAPSE_DISTILLER_MODE = "claude" | "ollama" | "npu" | "fake" (default: "fake")
      SYNAPSE_DISTILLER_CONTRIBUTOR = <string> (default: os user)
    """
    mode = os.environ.get("SYNAPSE_DISTILLER_MODE", "fake")
    contributor = os.environ.get(
        "SYNAPSE_DISTILLER_CONTRIBUTOR", os.environ.get("USER", "unknown")
    )

    provider: ModelProvider
    if mode == "claude":
        from synapse_providers.claude import ClaudeProvider  # from Plan C
        provider = ClaudeProvider()
    elif mode == "ollama":
        from synapse_providers.openai_compat import OllamaProvider  # from Plan C
        provider = OllamaProvider(model=os.environ.get("SYNAPSE_OLLAMA_MODEL", "llama3.2:3b"))
    elif mode == "npu":
        from synapse_providers.npu import NPUProvider  # from this plan, Task 6
        provider = NPUProvider()
    else:
        # Fake fallback keeps the CLI runnable pre-integration.
        provider = FakeProvider(scripts=[{"findings": []}] * 10_000)

    return Distiller(provider=provider, contributor=contributor)
```

- [ ] **Step 4: Run — verify PASS**

Run: `uv run pytest packages/worker/tests/test_distiller.py -v`
Expected: All tests PASS.

- [ ] **Step 5: Turn the walking-skeleton distiller step green (partially)**

Run: `uv run pytest tests/test_walking_skeleton.py -v`
Expected: The `Distiller` import now succeeds. The full skeleton is still XFAIL because Plan C's synthesis/retrieval imports remain unresolved.

- [ ] **Step 6: Commit**

```bash
git add packages/worker/src/synapse_worker/distiller/core.py packages/worker/tests/test_distiller.py
git commit -m "$(cat <<'EOF'
feat(distiller): Segment → Finding[] with retry + tolerant parse (Plan B Task 3)

Uses ModelProvider.capabilities.native_structured_output to decide whether
to trust the response directly or fall back to tolerant JSON extraction.
One retry on malformed output; invalid items are dropped, not raised.
build_default_distiller() reads SYNAPSE_DISTILLER_MODE from env.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

### Task 4: NPU spike — SLM on Hexagon (X Elite laptop, on-target)

> **⟨AMENDED 2026-08-03⟩** — see `2026-08-03-aic100-cirrascale-amendment.md` Parts 2–3. The runtime is now **GenieX**, not ONNX Runtime GenAI + QNN EP: `geniex serve` exposes an OpenAI-compatible API at `localhost:18181/v1` with `qairt` (NPU-exclusive, pre-compiled AI Hub bundles) and `llama_cpp` (GGUF) backends. New spike order: (1) `/quad-detect` + `/quad-doctor` environment sanity → (2) `geniex serve` with a `qairt` bundle → run the fixture corpus through **Qwen3-4B-Instruct-2507** (primary), **Gemma-4-E4B-it** (GenieX's documented example), **Qwen3-1.7B** (power/speed floor) → (3) fallback GenieX `llama_cpp` → (4) final fallback Ollama, unchanged. Go/no-go axes unchanged (NPU residency, prefill/generate tok/s, power, schema-valid JSON rate); add a 10-minute probe for llama.cpp-style grammar/GBNF support before assuming `native_structured_output=False`. The Phi-3.5-mini / Llama-3.2-3B model choices below are stale — neither is in the GenieX catalog. Steps 1–2's `onnxruntime-genai` setup and `spike_npu.py`'s manual generation loop are superseded: the smoke test becomes "run the fixture corpus through `NPUProvider` pointed at `localhost:18181/v1`". Runbook template and go/no-go checklist remain valid with the model/runtime names swapped. QUAD `profile-device` numbers (latency/power/HTP utilization), where obtainable, feed the Task 6 benchmark table.

**This task is a spike, not a component build.** Output is a runbook + a go/no-go verdict. No new imports into `synapse_worker` until it lands green.

**Kill time:** end of Day 2 of the prep week. If red, the fallback is `OllamaProvider` on the Mac (delivered in Plan C) for the demo, with the NPU number reported separately from a partial benchmark run.

**Files:**
- Create: `docs/spikes/2026-07-26-npu-spike.md`
- Create: `scripts/spike_npu.py`

- [ ] **Step 1: Environment setup on the X Elite laptop**

Install once, then log every step in the runbook:

```bash
# On the X Elite Windows-on-Snapdragon laptop
winget install --id Python.Python.3.12
pip install --upgrade uv
git clone <repo> synapse && cd synapse
uv sync
```

Install the QNN SDK (Qualcomm developer account required) and the QNN Execution Provider for ONNX Runtime:

```bash
# ONNX Runtime GenAI with QNN EP — the officially supported path for Phi-3.5-mini / Llama-3.2-3B on X Elite NPU
uv add onnxruntime-genai onnxruntime-qnn
```

Download a pre-quantized ONNX build from Qualcomm AI Hub:
  - Llama-3.2-3B (INT4/INT8 mixed, w8a16) for Snapdragon X Elite
  - Alternate: Phi-3.5-mini-instruct

- [ ] **Step 2: Write the smoke test**

Create `scripts/spike_npu.py`:

```python
"""NPU smoke test — prove the model runs on Hexagon and can emit structured JSON.

Not part of the CI suite (requires NPU hardware). Run manually on the X Elite:
    uv run python scripts/spike_npu.py --model-dir <path> --segments fixtures/segments
"""

from __future__ import annotations

import argparse
import json
import time
from pathlib import Path


def run(model_dir: Path, segments_dir: Path) -> None:
    # Import inside so the file can be linted on the Mac dev machine without QNN installed.
    import onnxruntime_genai as og  # noqa: F401

    config = og.Config(str(model_dir))
    config.append_provider("QNNExecutionProvider")  # opt in to the Hexagon NPU
    model = og.Model(config)
    tokenizer = og.Tokenizer(model)
    tokenizer_stream = tokenizer.create_stream()

    from synapse_contracts import Segment
    from synapse_worker.distiller.prompt import render_prompt
    from synapse_worker.distiller.schema import FINDINGS_RESPONSE_SCHEMA

    for seg_path in sorted(segments_dir.glob("*.json")):
        seg = Segment.model_validate(json.loads(seg_path.read_text()))
        messages = render_prompt(seg)
        prompt = _render_chat(messages)

        t0 = time.perf_counter()
        input_tokens = tokenizer.encode(prompt)
        params = og.GeneratorParams(model)
        params.set_search_options(max_length=len(input_tokens) + 800, temperature=0.0)
        generator = og.Generator(model, params)
        generator.append_tokens(input_tokens)

        output = ""
        while not generator.is_done():
            generator.generate_next_token()
            new_token = generator.get_next_tokens()[0]
            output += tokenizer_stream.decode(new_token)
        elapsed = time.perf_counter() - t0

        print(f"=== {seg_path.name} ({elapsed:.1f}s) ===")
        print(output[:400])
        print("---")


def _render_chat(messages: list[dict[str, str]]) -> str:
    """Naive chat template. Replace with the model's actual chat template if available."""
    parts = []
    for m in messages:
        parts.append(f"<|{m['role']}|>\n{m['content']}\n")
    parts.append("<|assistant|>\n")
    return "".join(parts)


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("--model-dir", type=Path, required=True)
    p.add_argument("--segments", type=Path, default=Path("fixtures/segments"))
    ns = p.parse_args()
    run(ns.model_dir, ns.segments)


if __name__ == "__main__":
    main()
```

- [ ] **Step 3: Run the smoke test and record numbers**

On the X Elite laptop:

```powershell
uv run python scripts/spike_npu.py --model-dir <path-to-npu-model> --segments fixtures/segments
```

Capture:
  - **Go / No-go**: does at least one segment produce parseable JSON matching the schema?
  - **Prefill tok/s**: measured from the first-token latency and input token count.
  - **Generate tok/s**: measured from the total elapsed minus prefill, divided by output tokens.
  - **Peak memory**: from Task Manager / `nvidia-smi`-equivalent on Snapdragon.
  - **Package power (est.)**: from Windows Battery report or plugged in.

- [ ] **Step 4: Write the runbook**

Create `docs/spikes/2026-07-26-npu-spike.md`:

```markdown
# NPU Spike — SLM on Snapdragon X Elite Hexagon

**Owner:** Aditya
**Prep window:** 2026-07-26 to 2026-07-31
**Kill time:** end of Day 2 (2026-07-27)

## Go / No-go verdict

- [ ] NPU residency confirmed via ONNX Runtime QNN EP
- [ ] Schema-valid JSON output for at least seg-001 (`native_structured_output` capability is False for NPU — tolerant parse expected)
- [ ] Prefill tok/s ≥ ______
- [ ] Generate tok/s ≥ ______
- [ ] Package power ≤ ______ W (sustained)

## Runbook

### One-time setup
1. Install Python 3.12 + `uv` on the X Elite Windows laptop.
2. Register for a Qualcomm developer account and download the QNN SDK.
3. `uv sync` the Synapse repo.
4. `uv add onnxruntime-genai onnxruntime-qnn` in the worker package.
5. Download the pre-quantized model from Qualcomm AI Hub:
   - Preferred: Llama-3.2-3B for Snapdragon X Elite (w8a16)
   - Fallback: Phi-3.5-mini-instruct

### Run
```
uv run python scripts/spike_npu.py --model-dir C:\models\llama-3.2-3b-npu --segments fixtures/segments
```

### Numbers observed (fill in)

| Segment | Prefill tok/s | Generate tok/s | Total (s) | Schema-valid? |
|---|---|---|---|---|
| seg-001.json | | | | |
| seg-002.json | | | | |

## Fallback if red

If the spike is red by end of Day 2:
1. `SYNAPSE_DISTILLER_MODE=ollama` on the Mac becomes the demo path.
2. Present NPU as validated separately with whatever partial numbers were collected — do not block the hackathon build on it.
3. Aditya switches to helping tune the distillation prompt against Claude quality on the Mac.

## Fallback if amber (works but slow or unstructured)

If output is not schema-valid but is close (needs tolerant parse with retries):
1. Keep NPU as the on-target distiller and accept lower schema-valid rate.
2. Emphasize the tolerant-parse behavior in the demo narration.
3. Report schema-valid rate as one of the benchmark axes.
```

- [ ] **Step 5: Commit the spike artifacts**

```bash
git add docs/spikes/2026-07-26-npu-spike.md scripts/spike_npu.py
git commit -m "$(cat <<'EOF'
docs(spike): NPU bring-up runbook + smoke script (Plan B Task 4)

ONNX Runtime GenAI with QNN EP on Snapdragon X Elite. Not in CI — runs
manually on Aditya's laptop. Go/no-go verdict feeds the Day 2 checkpoint.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

### Task 5: `NPUProvider` — wrap the ONNX Runtime GenAI + QNN EP client

> **⟨AMENDED 2026-08-03⟩** — the custom `onnxruntime-genai` wrapper below is **retired**. `NPUProvider` is now a thin subclass of Plan C's `OpenAICompatibleProvider` pointed at GenieX's server (`base_url="http://localhost:18181/v1"`, `provider_id="npu"`, model = spike winner). No optional dep, no chat-template rendering, no token loop — the implementation mirrors `OllamaProvider` (~10 lines). Tests mirror `test_openai_compat.py` against `pytest-httpserver`. Set `native_structured_output` from the Task 4 grammar probe (default False → distiller's tolerant parse).

**This task depends on Task 4 landing green (or amber-with-schema-valid).** If Task 4 is red at the kill time, skip this task; the demo runs with `OllamaProvider` and the on-target story is a Plan-C AI-100 story only.

**Files:**
- Create: `packages/providers/src/synapse_providers/npu.py`
- Modify: `packages/providers/pyproject.toml` (add `onnxruntime-genai` as an optional dep so the Mac dev machine can still install-and-lint)
- Create: `packages/providers/tests/test_npu.py`

- [ ] **Step 1: Add the optional dep**

Edit `packages/providers/pyproject.toml`, adding under `[project]`:

```toml
[project.optional-dependencies]
npu = [
    "onnxruntime-genai>=0.6",
]
```

- [ ] **Step 2: Write the failing test suite (mocked — the real backend needs NPU hardware)**

Create `packages/providers/tests/test_npu.py`:

```python
from unittest.mock import AsyncMock, MagicMock

import pytest

from synapse_providers.base import ProviderCapabilities


@pytest.mark.asyncio
async def test_npu_provider_reports_no_native_structured_output() -> None:
    from synapse_providers.npu import NPUProvider

    provider = NPUProvider.__new__(NPUProvider)  # bypass __init__ (needs model dir)
    caps = ProviderCapabilities(native_structured_output=False, streaming=False)
    # Assert the class-level default rather than instantiating.
    assert NPUProvider.default_capabilities == caps


@pytest.mark.asyncio
async def test_npu_provider_generate_returns_model_result() -> None:
    """With a mocked backend, complete() returns a ModelResult carrying the raw text."""
    from synapse_providers.npu import NPUProvider

    fake_backend = MagicMock()
    fake_backend.generate = AsyncMock(return_value=("{\"findings\": []}", 42, 17))

    provider = NPUProvider.__new__(NPUProvider)
    provider._backend = fake_backend  # type: ignore[attr-defined]
    provider._provider_id = "npu"  # type: ignore[attr-defined]
    provider._caps = ProviderCapabilities(native_structured_output=False)  # type: ignore[attr-defined]

    result = await provider.complete(messages=[{"role": "user", "content": "hi"}])
    assert result.data == '{"findings": []}'
    assert result.provider_id == "npu"
    assert result.usage.input_tokens == 42
    assert result.usage.output_tokens == 17
    assert result.latency_ms >= 0
    assert result.schema_valid is False
```

- [ ] **Step 3: Run — expect ImportError**

Run: `uv run pytest packages/providers/tests/test_npu.py -v`
Expected: FAIL — `ImportError`.

- [ ] **Step 4: Implement `NPUProvider`**

Create `packages/providers/src/synapse_providers/npu.py`:

```python
"""NPUProvider — SLM on Snapdragon X Elite Hexagon via ONNX Runtime GenAI + QNN EP.

This provider does NOT have native structured output — the model produces
text, and the Distiller uses tolerant JSON extraction downstream. This is
expected on the Hexagon NPU path and is reported honestly via
ProviderCapabilities.native_structured_output = False.

Import guard: onnxruntime_genai is not installed on the Mac dev machine
(marked as optional under [project.optional-dependencies].npu). Importing
this module is safe; instantiating NPUProvider without the dependency
installed will raise at construction time with a clear message.
"""

from __future__ import annotations

import asyncio
import os
import time
from typing import Any

from synapse_contracts import ModelResult, ModelUsage

from synapse_providers.base import ModelProvider, ProviderCapabilities


class _NPUBackend:
    """Thin wrapper around onnxruntime_genai for testability."""

    def __init__(self, model_dir: str) -> None:
        try:
            import onnxruntime_genai as og
        except ImportError as e:  # pragma: no cover
            raise ImportError(
                "onnxruntime-genai is not installed. "
                "On the X Elite laptop: `uv add onnxruntime-genai onnxruntime-qnn` "
                "in the providers package (already declared as an optional extra: "
                "install with `uv sync --extra npu`)."
            ) from e

        config = og.Config(model_dir)
        config.append_provider("QNNExecutionProvider")
        self._model = og.Model(config)
        self._tokenizer = og.Tokenizer(self._model)
        self._stream = self._tokenizer.create_stream()
        self._og = og

    async def generate(self, prompt: str, max_new_tokens: int = 800) -> tuple[str, int, int]:
        """Return (text, input_token_count, output_token_count). Runs the
        blocking NPU call in a thread so we don't stall the event loop."""

        def _blocking() -> tuple[str, int, int]:
            input_tokens = self._tokenizer.encode(prompt)
            params = self._og.GeneratorParams(self._model)
            params.set_search_options(
                max_length=len(input_tokens) + max_new_tokens,
                temperature=0.0,
            )
            gen = self._og.Generator(self._model, params)
            gen.append_tokens(input_tokens)

            output = ""
            output_count = 0
            while not gen.is_done():
                gen.generate_next_token()
                new_token = gen.get_next_tokens()[0]
                output += self._stream.decode(new_token)
                output_count += 1
            return output, len(input_tokens), output_count

        return await asyncio.to_thread(_blocking)


class NPUProvider(ModelProvider):
    provider_id = "npu"
    default_capabilities = ProviderCapabilities(native_structured_output=False, streaming=False)

    def __init__(self, *, model_dir: str | None = None) -> None:
        self._provider_id = "npu"
        self._caps = self.default_capabilities
        resolved = model_dir or os.environ.get("SYNAPSE_NPU_MODEL_DIR")
        if not resolved:
            raise ValueError(
                "NPUProvider requires model_dir (or SYNAPSE_NPU_MODEL_DIR env var) "
                "pointing at a QNN-optimized ONNX build (e.g. Llama-3.2-3B for X Elite)."
            )
        self._backend = _NPUBackend(resolved)

    @property
    def capabilities(self) -> ProviderCapabilities:
        return self._caps

    async def complete(
        self,
        messages: list[dict[str, Any]],
        response_schema: dict[str, Any] | None = None,
    ) -> ModelResult:
        prompt = _render_chat(messages)
        t0 = time.perf_counter()
        text, in_tok, out_tok = await self._backend.generate(prompt)
        latency_ms = int((time.perf_counter() - t0) * 1000)
        return ModelResult(
            data=text,
            usage=ModelUsage(input_tokens=in_tok, output_tokens=out_tok),
            latency_ms=latency_ms,
            provider_id=self._provider_id,
            schema_valid=False,  # tolerant-parse path in the Distiller
        )


def _render_chat(messages: list[dict[str, Any]]) -> str:
    """Same naive template used by the spike script. Replace with model's actual chat template when known."""
    parts = []
    for m in messages:
        parts.append(f"<|{m['role']}|>\n{m['content']}\n")
    parts.append("<|assistant|>\n")
    return "".join(parts)
```

- [ ] **Step 5: Run — verify PASS**

Run: `uv run pytest packages/providers/tests/test_npu.py -v`
Expected: All tests PASS.

- [ ] **Step 6: Commit**

```bash
git add packages/providers/src/synapse_providers/npu.py packages/providers/tests/test_npu.py packages/providers/pyproject.toml
git commit -m "$(cat <<'EOF'
feat(providers): NPUProvider — ONNX Runtime GenAI + QNN EP (Plan B Task 5)

Wraps the Hexagon NPU path on Snapdragon X Elite. native_structured_output
is False by design — the Distiller uses tolerant JSON parse downstream.
onnxruntime-genai is an optional extra so Mac dev machines can install
without the QNN toolchain.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

### Task 6: Eval / benchmark harness — corpus × provider → quality/cost/latency table

**This is the "why not just call Claude?" story, made concrete.**

The harness runs the fixture corpus through any set of providers and produces a table with three axes:
  - **Quality**: distilled findings vs golden findings, using a semantic similarity check performed by Claude as an LLM-as-judge. This gives a defensible measurement even though the outputs are natural language.
  - **Cost**: `usage.input_tokens * $price_in + usage.output_tokens * $price_out`. Claude prices from `shared/models.md` ($5 / $25 per 1M for Opus 4.8); local providers are $0.
  - **Latency**: wall-clock per segment.

**Files:**
- Create: `packages/worker/src/synapse_worker/eval/__init__.py`
- Create: `packages/worker/src/synapse_worker/eval/pricing.py`
- Create: `packages/worker/src/synapse_worker/eval/judge.py`
- Create: `packages/worker/src/synapse_worker/eval/harness.py`
- Create: `packages/worker/tests/test_eval.py`
- Create: `scripts/run_benchmark.py`

- [ ] **Step 1: Write the failing test suite**

Create `packages/worker/tests/test_eval.py`:

```python
import json
from pathlib import Path

import pytest

from synapse_contracts import Finding
from synapse_providers import FakeProvider
from synapse_worker.eval.harness import BenchmarkRow, run_benchmark
from synapse_worker.eval.pricing import cost_usd
from synapse_worker.eval.judge import score_findings


FIXTURES = Path(__file__).parent.parent.parent.parent / "fixtures"


def _golden(name: str) -> list[Finding]:
    data = json.loads((FIXTURES / "findings" / f"{name}.findings.json").read_text())
    return [Finding.model_validate(d) for d in data]


def test_cost_usd_for_claude_opus() -> None:
    # $5 per 1M input, $25 per 1M output
    assert cost_usd("claude", 1_000_000, 0) == pytest.approx(5.00)
    assert cost_usd("claude", 0, 1_000_000) == pytest.approx(25.00)


def test_cost_usd_for_local_is_zero() -> None:
    assert cost_usd("fake", 100, 100) == 0.0
    assert cost_usd("npu", 1_000, 1_000) == 0.0
    assert cost_usd("ollama", 1_000, 1_000) == 0.0


@pytest.mark.asyncio
async def test_judge_returns_score_between_zero_and_one() -> None:
    """The judge uses a FakeProvider so the test is deterministic."""
    golden = _golden("seg-001")
    predicted = list(golden)  # identical → high score

    judge = FakeProvider(scripts=[{"score": 0.92, "notes": "matches all golden findings"}])
    result = await score_findings(golden=golden, predicted=predicted, judge=judge)
    assert 0.0 <= result.score <= 1.0
    assert result.score == 0.92


@pytest.mark.asyncio
async def test_run_benchmark_produces_one_row_per_provider_and_segment() -> None:
    from synapse_worker.distiller import Distiller

    # Two providers, one segment each — 2 rows.
    scripted_findings = {"findings": [{"type": "learning", "text": "x"}]}
    provider_a = FakeProvider(scripts=[scripted_findings, scripted_findings])
    provider_b = FakeProvider(scripts=[scripted_findings, scripted_findings])
    judge = FakeProvider(scripts=[{"score": 0.5, "notes": ""}] * 4)

    rows: list[BenchmarkRow] = await run_benchmark(
        providers={"a": Distiller(provider=provider_a, contributor="c"),
                   "b": Distiller(provider=provider_b, contributor="c")},
        segment_dir=FIXTURES / "segments",
        golden_dir=FIXTURES / "findings",
        judge=judge,
    )
    # 2 providers × 2 fixture segments = 4 rows
    assert len(rows) == 4
    assert {r.provider for r in rows} == {"a", "b"}
    for r in rows:
        assert r.latency_ms >= 0
        assert r.quality_score is not None
```

- [ ] **Step 2: Run — expect ImportError**

Run: `uv run pytest packages/worker/tests/test_eval.py -v`
Expected: FAIL — modules don't exist yet.

- [ ] **Step 3: Implement pricing**

Create `packages/worker/src/synapse_worker/eval/__init__.py`:

```python
"""Eval / benchmark harness — quality vs cost vs latency across providers."""

from synapse_worker.eval.harness import BenchmarkRow, run_benchmark
from synapse_worker.eval.judge import JudgeResult, score_findings
from synapse_worker.eval.pricing import cost_usd

__all__ = ["BenchmarkRow", "JudgeResult", "cost_usd", "run_benchmark", "score_findings"]
```

Create `packages/worker/src/synapse_worker/eval/pricing.py`:

```python
"""Per-provider token pricing (USD per token).

Claude Opus 4.8: $5.00 / 1M input, $25.00 / 1M output (from shared/models.md).
Local providers: $0.

If more providers land, add them here — the point of this file is that the
"why not just call Claude?" story reads cleanly off one lookup table.
"""

from __future__ import annotations

_PRICING_PER_MILLION: dict[str, tuple[float, float]] = {
    "claude": (5.00, 25.00),
    "aic100": (0.0, 0.0),  # ⟨A 2026-08-03⟩ hosted Cirrascale, shared hackathon credit pool — report credits/token usage from ModelResult.usage, not $/hour
    "npu": (0.0, 0.0),
    "ollama": (0.0, 0.0),
    "fake": (0.0, 0.0),
}


def cost_usd(provider_id: str, input_tokens: int, output_tokens: int) -> float:
    in_per_m, out_per_m = _PRICING_PER_MILLION.get(provider_id, (0.0, 0.0))
    return (input_tokens / 1_000_000) * in_per_m + (output_tokens / 1_000_000) * out_per_m
```

Create `packages/worker/src/synapse_worker/eval/judge.py`:

```python
"""LLM-as-judge for finding quality.

Given golden findings and predicted findings for the same segment, ask a
capable model (typically Claude) whether the predictions capture the same
insights. Returns a scalar in [0, 1] plus notes.

The judge is itself a ModelProvider — swappable, so tests use FakeProvider
and prod uses ClaudeProvider.
"""

from __future__ import annotations

import json
from dataclasses import dataclass

from synapse_contracts import Finding
from synapse_providers import ModelProvider

_SYSTEM_PROMPT = """You are an expert judge evaluating the quality of extracted findings from a coding-agent session.

You will see two sets of findings for the same session excerpt:
  - GOLDEN: what a human expert extracted (the target)
  - PREDICTED: what an automated distiller produced

Rate how well PREDICTED captures the insights of GOLDEN on a 0.0–1.0 scale:
  1.0 = predicted captures every golden insight (even if worded differently)
  0.5 = predicted captures roughly half of golden's insights
  0.0 = predicted is unrelated to golden or missed everything important

Ignore superficial differences (wording, ordering). Focus on semantic coverage.

Return exactly this JSON: {"score": <float 0-1>, "notes": "<one sentence>"}"""

_JUDGE_SCHEMA = {
    "type": "object",
    "properties": {
        "score": {"type": "number", "minimum": 0, "maximum": 1},
        "notes": {"type": "string"},
    },
    "required": ["score", "notes"],
    "additionalProperties": False,
}


@dataclass
class JudgeResult:
    score: float
    notes: str


async def score_findings(
    *,
    golden: list[Finding],
    predicted: list[Finding],
    judge: ModelProvider,
) -> JudgeResult:
    def _fmt(fs: list[Finding]) -> str:
        return "\n".join(f"- [{f.type}] {f.text}" for f in fs) or "(none)"

    messages = [
        {"role": "system", "content": _SYSTEM_PROMPT},
        {
            "role": "user",
            "content": (
                f"GOLDEN:\n{_fmt(golden)}\n\nPREDICTED:\n{_fmt(predicted)}\n\n"
                "Rate the predicted set."
            ),
        },
    ]
    result = await judge.complete(messages=messages, response_schema=_JUDGE_SCHEMA)
    data = result.data if isinstance(result.data, dict) else json.loads(result.data)
    return JudgeResult(score=float(data["score"]), notes=str(data.get("notes", "")))
```

Create `packages/worker/src/synapse_worker/eval/harness.py`:

```python
"""Benchmark harness — corpus × provider → quality/cost/latency table.

Usage:
    from synapse_worker.distiller import Distiller
    from synapse_providers.claude import ClaudeProvider
    from synapse_providers.openai_compat import OllamaProvider

    distillers = {
        "claude":  Distiller(provider=ClaudeProvider(),  contributor="bench"),
        "ollama":  Distiller(provider=OllamaProvider(model="llama3.2:3b"), contributor="bench"),
    }
    rows = await run_benchmark(providers=distillers, ...)
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass
from pathlib import Path

from synapse_contracts import Finding, Segment
from synapse_providers import ModelProvider

from synapse_worker.distiller import Distiller
from synapse_worker.eval.judge import score_findings
from synapse_worker.eval.pricing import cost_usd


@dataclass
class BenchmarkRow:
    provider: str
    segment_id: str
    quality_score: float | None
    input_tokens: int
    output_tokens: int
    latency_ms: int
    cost_usd: float
    findings_produced: int
    findings_golden: int


async def run_benchmark(
    *,
    providers: dict[str, Distiller],
    segment_dir: Path,
    golden_dir: Path,
    judge: ModelProvider,
) -> list[BenchmarkRow]:
    """Run every provider over every segment; judge quality against goldens."""
    rows: list[BenchmarkRow] = []

    for seg_path in sorted(segment_dir.glob("*.json")):
        seg = Segment.model_validate(json.loads(seg_path.read_text()))
        golden_path = golden_dir / f"{seg_path.stem}.findings.json"
        golden = [
            Finding.model_validate(x) for x in json.loads(golden_path.read_text())
        ]

        for name, distiller in providers.items():
            t0 = time.perf_counter()
            findings = await distiller.distill(seg)
            latency_ms = int((time.perf_counter() - t0) * 1000)

            # Read usage off the last provider call. Distiller doesn't currently
            # expose it — approximate from prompt length. (Improvement: have
            # Distiller.distill() return a (findings, usage) tuple; here we keep
            # the surface simple and use per-call telemetry via the judge.)
            input_tokens = sum(len(e.content) for e in seg.events) // 4
            output_tokens = sum(len(f.text) for f in findings) // 4

            quality = await score_findings(golden=golden, predicted=findings, judge=judge)

            provider_id = distiller._provider.provider_id  # type: ignore[attr-defined]
            rows.append(
                BenchmarkRow(
                    provider=name,
                    segment_id=seg.id,
                    quality_score=quality.score,
                    input_tokens=input_tokens,
                    output_tokens=output_tokens,
                    latency_ms=latency_ms,
                    cost_usd=cost_usd(provider_id, input_tokens, output_tokens),
                    findings_produced=len(findings),
                    findings_golden=len(golden),
                )
            )
    return rows
```

- [ ] **Step 4: Add a benchmark driver script**

Create `scripts/run_benchmark.py`:

```python
"""Run the full benchmark and print a pretty table + write a CSV.

Prereqs:
  - ANTHROPIC_API_KEY set (for ClaudeProvider baseline + judge)
  - Optional: Ollama running with llama3.2:3b for the local baseline

Example:
    uv run python scripts/run_benchmark.py --providers claude ollama --out results.csv
"""

from __future__ import annotations

import argparse
import asyncio
import csv
import sys
from pathlib import Path


async def _main(names: list[str], out: Path) -> int:
    from synapse_providers.claude import ClaudeProvider  # Plan C
    from synapse_providers.openai_compat import OllamaProvider  # Plan C
    from synapse_worker.distiller import Distiller
    from synapse_worker.eval.harness import run_benchmark

    distillers: dict[str, Distiller] = {}
    for name in names:
        if name == "claude":
            distillers["claude"] = Distiller(provider=ClaudeProvider(), contributor="bench")
        elif name == "ollama":
            distillers["ollama"] = Distiller(
                provider=OllamaProvider(model="llama3.2:3b"), contributor="bench"
            )
        elif name == "npu":
            from synapse_providers.npu import NPUProvider
            distillers["npu"] = Distiller(provider=NPUProvider(), contributor="bench")
        else:
            print(f"unknown provider: {name}", file=sys.stderr)
            return 2

    judge = ClaudeProvider()  # LLM-as-judge always uses Claude for a stable baseline
    rows = await run_benchmark(
        providers=distillers,
        segment_dir=Path("fixtures/segments"),
        golden_dir=Path("fixtures/findings"),
        judge=judge,
    )

    # Print table
    print(f"{'provider':10} {'segment':16} {'quality':>7} {'in_tok':>7} {'out_tok':>8} {'lat_ms':>7} {'cost_usd':>10}")
    for r in rows:
        print(
            f"{r.provider:10} {r.segment_id:16} "
            f"{(r.quality_score or 0.0):7.3f} {r.input_tokens:7d} "
            f"{r.output_tokens:8d} {r.latency_ms:7d} {r.cost_usd:10.6f}"
        )

    # CSV
    with out.open("w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["provider", "segment", "quality", "input_tokens",
                    "output_tokens", "latency_ms", "cost_usd",
                    "findings_produced", "findings_golden"])
        for r in rows:
            w.writerow([r.provider, r.segment_id, r.quality_score,
                        r.input_tokens, r.output_tokens, r.latency_ms,
                        r.cost_usd, r.findings_produced, r.findings_golden])

    return 0


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("--providers", nargs="+", default=["claude", "ollama"])
    p.add_argument("--out", type=Path, default=Path("bench_results.csv"))
    ns = p.parse_args()
    raise SystemExit(asyncio.run(_main(ns.providers, ns.out)))


if __name__ == "__main__":
    main()
```

- [ ] **Step 5: Run the unit tests**

Run: `uv run pytest packages/worker/tests/test_eval.py -v`
Expected: All tests PASS.

- [ ] **Step 6: Commit**

```bash
git add packages/worker/src/synapse_worker/eval packages/worker/tests/test_eval.py scripts/run_benchmark.py
git commit -m "$(cat <<'EOF'
feat(eval): quality × cost × latency benchmark harness (Plan B Task 6)

LLM-as-judge scores predicted findings vs hand-authored goldens. Pricing
table for Claude Opus 4.8 ($5/$25 per 1M) makes the "vs cloud" cost
delta a real number, not an assertion. Driver script emits CSV for the
demo slide.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

### Task 7: Exit criteria

- [ ] **Step 1: Distiller + Eval unit suites green**

Run: `uv run pytest packages/worker -v`
Expected: All tests PASS.

- [ ] **Step 2: Full monorepo suite still green**

Run: `uv run pytest -v`
Expected: Everything green except the walking-skeleton test (XFAIL until Plan C's synthesis + retrieval land).

- [ ] **Step 3: Real Claude smoke test**

With `ANTHROPIC_API_KEY` set:

```bash
uv run python -c "
import asyncio, json
from pathlib import Path
from synapse_contracts import Segment
from synapse_providers.claude import ClaudeProvider
from synapse_worker.distiller import Distiller

async def main():
    seg = Segment.model_validate(json.loads(Path('fixtures/segments/seg-001.json').read_text()))
    d = Distiller(provider=ClaudeProvider(), contributor='aditya')
    fs = await d.distill(seg)
    for f in fs:
        print(f'[{f.type}] {f.text}')

asyncio.run(main())
"
```

Expected: Prints 2–4 real, plausible findings for seg-001. Numbers should be similar to but not identical to the golden — that's the quality gap the harness measures.

- [ ] **Step 4: NPU spike verdict recorded**

Fill in `docs/spikes/2026-07-26-npu-spike.md` § Go / No-go verdict with actual measurements from Task 4.

- [ ] **Step 5: Confirm hand-off**

- Akhil's `EdgeWorker` (Plan A Task 5) can now construct a real `Distiller` via `build_default_distiller()` when `SYNAPSE_DISTILLER_MODE` is set.
- Siddsing's benchmark story is provable: run `scripts/run_benchmark.py --providers claude ollama` and see the quality/cost/latency delta.
- The walking-skeleton test now imports `Distiller` successfully; it will turn green when Plan C's `InMemorySynthesizer` and `query_findings` land.

---

## Scope / YAGNI

**In (Plan B):** Distiller with schema-aware or tolerant JSON parsing, one retry, one drop; NPU spike + runbook; `NPUProvider` (conditional on spike); benchmark harness with LLM-as-judge; pricing table.

**Out (stretch):**
- Distiller output caching (identical Segment → cached Finding[]) — nice for eval reruns, not necessary.
- Streaming distillation — not on the demo path.
- Fine-tuned SLM specifically for this schema — out of scope for the hackathon; the target is a pre-optimized GenieX bundle (Qwen3-4B-Instruct-2507 primary; see 2026-08-03 amendment ⟨A⟩).
- Cost model for AI-100 (per-hour vs per-token) — Plan C's synthesis is where AI-100 costs matter; the pricing table can be extended when the number is known.

## Known Risks

| Risk | Mitigation |
|---|---|
| NPU can't emit any schema-valid JSON at all | Tolerant parse + retry masks *some* of this. If schema-valid rate is < 10%, drop `NPUProvider` from the demo and lean on the AI-100 story from Plan C |
| Ollama on Mac produces very different quality from Claude | That's the point of the benchmark — the delta IS the story. If Ollama scores 0.4 vs Claude's 0.9, that's a valid finding to show |
| Judge (Claude) has systematic bias against the local model's phrasing | Note it in the demo. A 5-segment corpus is too small for statistical claims; frame as "directional signal" |
| Fixture corpus is too small (2 segments) to be convincing | Expand to 4–6 hand-authored fixtures during the prep week if bandwidth allows. Not blocking |
