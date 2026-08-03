# Synapse — Plan 0: Foundation (Day 0 Blocking)

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Scaffold the Synapse monorepo, freeze all cross-track contracts, land the `FakeProvider`, commit hand-authored fixture Segments + golden Findings, and get a red walking-skeleton test that will turn green when the real implementations arrive.

**Architecture:** Python 3.12 + uv + pytest monorepo with focused packages (`contracts`, `providers`, `worker`, `service`) sharing one Pydantic-based contract layer. `FakeProvider` returns scripted, deterministic outputs so every downstream component can be TDD'd without an LLM. Fixture Segments and golden Findings, hand-authored by the team on Day 0, are the ground truth that pins the `Segment` boundary (Akhil produces / Aditya consumes) and defines the eval quality bar.

**Tech Stack:** Python 3.12, uv, pytest, Pydantic v2, ruff (formatter+linter).

**Owners:** All three teammates co-author Day 0 (contracts + fixtures + goldens require alignment). Siddsing drives the scaffold and `FakeProvider`.

---

### Task 1: Initialize the uv-managed monorepo layout

**Files:**
- Create: `pyproject.toml`
- Create: `README.md`
- Create: `.python-version`
- Create: `packages/contracts/pyproject.toml`
- Create: `packages/contracts/src/synapse_contracts/__init__.py`
- Create: `packages/providers/pyproject.toml`
- Create: `packages/providers/src/synapse_providers/__init__.py`
- Create: `packages/worker/pyproject.toml`
- Create: `packages/worker/src/synapse_worker/__init__.py`
- Create: `packages/service/pyproject.toml`
- Create: `packages/service/src/synapse_service/__init__.py`
- Create: `tests/__init__.py`
- Create: `fixtures/README.md`

- [ ] **Step 1: Write root pyproject.toml as a uv workspace**

Create `pyproject.toml`:

```toml
[project]
name = "synapse"
version = "0.1.0"
description = "Shared team intelligence for AI coding agents (Snapdragon Multiverse hackathon)"
requires-python = ">=3.12"
dependencies = [
    "synapse-contracts",
    "synapse-providers",
    "synapse-worker",
    "synapse-service",
]

[tool.uv.workspace]
members = ["packages/*"]

[tool.uv.sources]
synapse-contracts = { workspace = true }
synapse-providers = { workspace = true }
synapse-worker = { workspace = true }
synapse-service = { workspace = true }

[dependency-groups]
dev = [
    "pytest>=8.0",
    "pytest-asyncio>=0.24",
    "ruff>=0.7",
]

[tool.pytest.ini_options]
testpaths = ["tests", "packages"]
asyncio_mode = "auto"
markers = [
    "integration: end-to-end integration tests (may be xfail until all tracks land)",
]

[tool.ruff]
line-length = 100
target-version = "py312"

[tool.ruff.lint]
select = ["E", "F", "I", "N", "UP", "B", "SIM"]
```

- [ ] **Step 2: Create `.python-version`**

Write `.python-version`:

```
3.12
```

- [ ] **Step 3: Write each package's pyproject.toml**

Create `packages/contracts/pyproject.toml`:

```toml
[project]
name = "synapse-contracts"
version = "0.1.0"
requires-python = ">=3.12"
dependencies = ["pydantic>=2.0"]

[build-system]
requires = ["hatchling"]
build-backend = "hatchling.build"

[tool.hatch.build.targets.wheel]
packages = ["src/synapse_contracts"]
```

Create `packages/providers/pyproject.toml`:

```toml
[project]
name = "synapse-providers"
version = "0.1.0"
requires-python = ">=3.12"
dependencies = [
    "synapse-contracts",
    "anthropic>=0.40",
    "httpx>=0.27",
]

[build-system]
requires = ["hatchling"]
build-backend = "hatchling.build"

[tool.hatch.build.targets.wheel]
packages = ["src/synapse_providers"]

[tool.uv.sources]
synapse-contracts = { workspace = true }
```

Create `packages/worker/pyproject.toml`:

```toml
[project]
name = "synapse-worker"
version = "0.1.0"
requires-python = ">=3.12"
dependencies = [
    "synapse-contracts",
    "synapse-providers",
    "httpx>=0.27",
]

[build-system]
requires = ["hatchling"]
build-backend = "hatchling.build"

[tool.hatch.build.targets.wheel]
packages = ["src/synapse_worker"]

[tool.uv.sources]
synapse-contracts = { workspace = true }
synapse-providers = { workspace = true }
```

Create `packages/service/pyproject.toml`:

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
]

[build-system]
requires = ["hatchling"]
build-backend = "hatchling.build"

[tool.hatch.build.targets.wheel]
packages = ["src/synapse_service"]

[tool.uv.sources]
synapse-contracts = { workspace = true }
synapse-providers = { workspace = true }
```

- [ ] **Step 4: Create empty package __init__.py files**

Write `packages/contracts/src/synapse_contracts/__init__.py`:

```python
"""Frozen cross-track contracts for Synapse."""
```

Write `packages/providers/src/synapse_providers/__init__.py`:

```python
"""ModelProvider implementations."""
```

Write `packages/worker/src/synapse_worker/__init__.py`:

```python
"""Edge worker: source adapters, follower, segmenter, sync client."""
```

Write `packages/service/src/synapse_service/__init__.py`:

```python
"""Synapse service: ingest API, synthesis, MCP retrieval server."""
```

Write `tests/__init__.py`:

```python
```

Write `fixtures/README.md`:

```markdown
# Fixtures

Ground truth for cross-track integration:

- `raw_session.jsonl` — a real Claude Code session excerpt (input to the source adapter).
- `segments/*.json` — hand-authored `Segment` blobs produced by segmenting the raw session.
- `findings/*.json` — golden `Finding[]` produced by distilling each segment.

The Segments pin the boundary between Akhil (Worker) and Aditya (Distiller): the segmenter must reproduce them exactly. The Findings define the quality bar the eval harness measures against.
```

Write `README.md`:

```markdown
# Synapse

Shared team intelligence for AI coding agents. Observes coding-agent sessions (Claude Code, Codex) unmodified, distills them into structured findings on-device, and synthesizes shared team memory in the cloud.

Snapdragon Multiverse hackathon (build week Aug 3–7 2026).

## Development

Requires Python 3.12 + [uv](https://docs.astral.sh/uv/).

```bash
uv sync                    # install workspace + dev deps
uv run pytest              # run all tests
uv run ruff check .        # lint
uv run ruff format .       # format
```

## Layout

- `packages/contracts` — frozen cross-track schemas (Pydantic v2)
- `packages/providers` — `ModelProvider` interface + implementations (Fake, Claude, Ollama, AIC100, NPU)
- `packages/worker` — edge worker (source adapters, follower, segmenter, sync client)
- `packages/service` — Synapse service (ingest API, synthesis, MCP retrieval server)
- `fixtures/` — hand-authored ground-truth data (see `fixtures/README.md`)
- `docs/superpowers/specs/2026-07-25-synapse-design.md` — design spec
- `docs/superpowers/plans/` — per-track implementation plans
```

- [ ] **Step 5: Run `uv sync` and verify workspace resolves**

Run: `uv sync`
Expected: Prints resolution and installation lines; exits 0. The generated `uv.lock` is committed later with the rest of Day 0.

- [ ] **Step 6: Run pytest — expect no tests yet, but pytest itself should succeed**

Run: `uv run pytest`
Expected: `no tests ran` (exit code 5 is fine — it's "no tests collected", not an error) OR `0 passed`. If pytest fails to *import* anything, fix that before proceeding.

- [ ] **Step 7: Commit the scaffold**

```bash
git add pyproject.toml README.md .python-version packages tests fixtures uv.lock
git commit -m "$(cat <<'EOF'
feat: scaffold Synapse monorepo (Plan 0 Task 1)

uv workspace with four packages (contracts, providers, worker, service).
Empty __init__ modules; no tests yet. This unblocks parallel tracks.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

### Task 2: Freeze the contracts (`AgentEvent`, `Segment`, `Finding`, `SynapseSession`, `LocalBinding`, `Conflict`, `SessionContext`, `ModelResult`)

**Files:**
- Create: `packages/contracts/src/synapse_contracts/schemas.py`
- Create: `packages/contracts/src/synapse_contracts/__init__.py` (overwrite from Task 1)
- Create: `packages/contracts/tests/__init__.py`
- Create: `packages/contracts/tests/test_schemas.py`

- [ ] **Step 1: Write the failing test for schema round-tripping**

Create `packages/contracts/tests/__init__.py` as an empty file.

Create `packages/contracts/tests/test_schemas.py`:

```python
from datetime import datetime, timezone

from synapse_contracts import (
    AgentEvent,
    Conflict,
    Finding,
    FindingType,
    LocalBinding,
    ModelResult,
    ModelUsage,
    Segment,
    SessionContext,
    SynapseSession,
)


UTC = timezone.utc


def _event(kind: str, content: str) -> AgentEvent:
    return AgentEvent(
        role="assistant",
        kind=kind,
        content=content,
        ts=datetime(2026, 7, 25, 12, 0, tzinfo=UTC),
        session_id="local-abc",
        cwd="/repo",
        git_branch="main",
    )


def test_agent_event_round_trip() -> None:
    e = _event("text", "hello world")
    dumped = e.model_dump()
    reloaded = AgentEvent.model_validate(dumped)
    assert reloaded == e


def test_segment_holds_multiple_events() -> None:
    events = [_event("text", "a"), _event("tool_use", "grep")]
    seg = Segment(
        id="seg-1",
        session_id="local-abc",
        events=events,
        started_at=events[0].ts,
        ended_at=events[-1].ts,
    )
    reloaded = Segment.model_validate(seg.model_dump())
    assert reloaded.events == events


def test_finding_types_are_exhaustive() -> None:
    assert set(FindingType) == {"learning", "decision", "dead_end", "open_question"}


def test_finding_carries_attribution() -> None:
    f = Finding(
        type="learning",
        text="Auth middleware rejects tokens with clock skew > 60s",
        contributor="siddsing",
        ts=datetime(2026, 7, 25, 12, 5, tzinfo=UTC),
        source_session="local-abc",
        refs=["auth/middleware.py:88"],
    )
    reloaded = Finding.model_validate(f.model_dump())
    assert reloaded.contributor == "siddsing"
    assert reloaded.type == "learning"


def test_synapse_session_and_local_binding() -> None:
    s = SynapseSession(
        shared_id="shared-1",
        purpose="Debug flaky auth tests",
        members=["siddsing", "akhil"],
        created_by="siddsing",
    )
    binding = LocalBinding(
        local_agent_session_id="local-abc",
        shared_id=s.shared_id,
        contributor="siddsing",
    )
    assert binding.shared_id == s.shared_id


def test_conflict_references_two_findings() -> None:
    f1 = Finding(
        type="decision",
        text="Use JWT for auth",
        contributor="siddsing",
        ts=datetime(2026, 7, 25, 12, 0, tzinfo=UTC),
        source_session="local-abc",
    )
    f2 = Finding(
        type="decision",
        text="Use session cookies for auth",
        contributor="akhil",
        ts=datetime(2026, 7, 25, 12, 1, tzinfo=UTC),
        source_session="local-def",
    )
    c = Conflict(finding_a=f1, finding_b=f2, description="Auth strategy disagreement")
    reloaded = Conflict.model_validate(c.model_dump())
    assert reloaded.description == "Auth strategy disagreement"


def test_session_context_holds_memory_and_conflicts() -> None:
    ctx = SessionContext(
        shared_id="shared-1",
        purpose="Debug flaky auth tests",
        working_memory="No findings yet.",
        conflicts=[],
    )
    reloaded = SessionContext.model_validate(ctx.model_dump())
    assert reloaded.purpose == ctx.purpose


def test_model_result_carries_usage_and_latency() -> None:
    r = ModelResult(
        data={"findings": []},
        usage=ModelUsage(input_tokens=100, output_tokens=20),
        latency_ms=1234,
        provider_id="fake",
        schema_valid=True,
    )
    reloaded = ModelResult.model_validate(r.model_dump())
    assert reloaded.usage.input_tokens == 100
    assert reloaded.latency_ms == 1234
    assert reloaded.schema_valid is True
```

- [ ] **Step 2: Run the test to verify it fails with ImportError**

Run: `uv run pytest packages/contracts/tests/test_schemas.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'synapse_contracts.schemas'` (or ImportError on the names).

- [ ] **Step 3: Write the schema definitions**

Create `packages/contracts/src/synapse_contracts/schemas.py`:

```python
"""Frozen cross-track schemas.

These types define the handoff points between components:
- AgentEvent: what the Source adapter produces per raw JSONL line (internal to worker)
- Segment: bounded run of AgentEvents; the Distiller's input (worker → distiller boundary)
- Finding: distilled unit of team intelligence (distiller → service boundary)
- SynapseSession / LocalBinding: shared-session identity and per-machine attribution
- Conflict: two Findings the synthesizer judged to disagree
- SessionContext: merged working memory + conflicts, returned by synthesis
- ModelResult: what any ModelProvider.complete() returns; usage/latency baked in
"""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import Any, Literal

from pydantic import BaseModel, Field


class AgentEvent(BaseModel):
    """Normalized event emitted by a Source adapter (agent-agnostic)."""

    role: Literal["user", "assistant", "system"]
    kind: Literal["text", "thinking", "tool_use", "tool_result"]
    content: str
    tool_name: str | None = None
    ts: datetime
    session_id: str
    cwd: str | None = None
    git_branch: str | None = None


class Segment(BaseModel):
    """A bounded run of AgentEvents split on turn boundary.

    This is the distiller's input. Hand-authored fixture Segments in
    fixtures/segments/*.json pin the boundary between Akhil (segmenter)
    and Aditya (distiller); the segmenter must reproduce them exactly.
    """

    id: str
    session_id: str
    events: list[AgentEvent]
    started_at: datetime
    ended_at: datetime


class FindingType(StrEnum):
    LEARNING = "learning"
    DECISION = "decision"
    DEAD_END = "dead_end"
    OPEN_QUESTION = "open_question"


class Finding(BaseModel):
    """A single distilled unit of team intelligence.

    `text` is abstracted, not verbatim code. The distiller redacts by design
    so that raw work stays on the device — only sanitized findings cross the
    device boundary.
    """

    type: FindingType
    text: str
    contributor: str
    ts: datetime
    source_session: str
    refs: list[str] = Field(default_factory=list)


class SynapseSession(BaseModel):
    """A shared team-intelligence session."""

    shared_id: str
    purpose: str
    members: list[str]
    created_by: str


class LocalBinding(BaseModel):
    """Maps a local agent-session ID to a shared Synapse session, with attribution."""

    local_agent_session_id: str
    shared_id: str
    contributor: str


class Conflict(BaseModel):
    """Two Findings the synthesizer judged to disagree."""

    finding_a: Finding
    finding_b: Finding
    description: str


class SessionContext(BaseModel):
    """Merged working memory + conflicts, organized by session purpose."""

    shared_id: str
    purpose: str
    working_memory: str
    conflicts: list[Conflict] = Field(default_factory=list)


class ModelUsage(BaseModel):
    input_tokens: int
    output_tokens: int


class ModelResult(BaseModel):
    """Return type of ModelProvider.complete().

    `data` is the raw text (str) when no response_schema was supplied,
    or the validated structured object (dict/list) when one was.
    """

    data: Any
    usage: ModelUsage
    latency_ms: int
    provider_id: str
    schema_valid: bool
```

- [ ] **Step 4: Re-export names from the package root**

Overwrite `packages/contracts/src/synapse_contracts/__init__.py`:

```python
"""Frozen cross-track contracts for Synapse."""

from synapse_contracts.schemas import (
    AgentEvent,
    Conflict,
    Finding,
    FindingType,
    LocalBinding,
    ModelResult,
    ModelUsage,
    Segment,
    SessionContext,
    SynapseSession,
)

__all__ = [
    "AgentEvent",
    "Conflict",
    "Finding",
    "FindingType",
    "LocalBinding",
    "ModelResult",
    "ModelUsage",
    "Segment",
    "SessionContext",
    "SynapseSession",
]
```

- [ ] **Step 5: Run the test to verify it passes**

Run: `uv run pytest packages/contracts/tests/test_schemas.py -v`
Expected: All tests PASS.

- [ ] **Step 6: Commit**

```bash
git add packages/contracts
git commit -m "$(cat <<'EOF'
feat(contracts): freeze cross-track schemas (Plan 0 Task 2)

AgentEvent, Segment, Finding, SynapseSession, LocalBinding, Conflict,
SessionContext, ModelResult. Pydantic v2. These are the immutable handoff
types every track builds against — treat any change as a spec-level decision.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

### Task 3: Define the `ModelProvider` interface and `ProviderCapabilities`

**Files:**
- Create: `packages/providers/src/synapse_providers/base.py`
- Create: `packages/providers/src/synapse_providers/__init__.py` (overwrite from Task 1)
- Create: `packages/providers/tests/__init__.py`
- Create: `packages/providers/tests/test_base.py`

- [ ] **Step 1: Write the failing test for the interface shape**

Create `packages/providers/tests/__init__.py` as an empty file.

Create `packages/providers/tests/test_base.py`:

```python
import inspect

from synapse_providers import ModelProvider, ProviderCapabilities


def test_model_provider_is_a_protocol_with_complete_method() -> None:
    # ModelProvider is a Protocol / ABC — should expose `complete` and `capabilities`.
    assert hasattr(ModelProvider, "complete")
    assert hasattr(ModelProvider, "capabilities")


def test_capabilities_flags_structured_output() -> None:
    caps = ProviderCapabilities(
        native_structured_output=True,
        streaming=False,
    )
    assert caps.native_structured_output is True
    assert caps.streaming is False


def test_complete_signature_takes_messages_and_optional_schema() -> None:
    sig = inspect.signature(ModelProvider.complete)
    params = sig.parameters
    assert "messages" in params
    assert "response_schema" in params
    assert params["response_schema"].default is None
```

- [ ] **Step 2: Run to verify failure**

Run: `uv run pytest packages/providers/tests/test_base.py -v`
Expected: FAIL — `ImportError: cannot import name 'ModelProvider' from 'synapse_providers'`.

- [ ] **Step 3: Implement the interface**

Create `packages/providers/src/synapse_providers/base.py`:

```python
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
```

- [ ] **Step 4: Re-export from package root**

Overwrite `packages/providers/src/synapse_providers/__init__.py`:

```python
"""ModelProvider implementations."""

from synapse_providers.base import ModelProvider, ProviderCapabilities

__all__ = ["ModelProvider", "ProviderCapabilities"]
```

- [ ] **Step 5: Run the test to verify it passes**

Run: `uv run pytest packages/providers/tests/test_base.py -v`
Expected: All tests PASS.

- [ ] **Step 6: Commit**

```bash
git add packages/providers
git commit -m "$(cat <<'EOF'
feat(providers): define ModelProvider interface (Plan 0 Task 3)

Abstract base with one method — complete(messages, response_schema?) →
ModelResult — plus ProviderCapabilities for the structured-output capability
flag. Async by design.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

### Task 4: Implement `FakeProvider` — scripted, deterministic, CI-safe

**Files:**
- Create: `packages/providers/src/synapse_providers/fake.py`
- Modify: `packages/providers/src/synapse_providers/__init__.py`
- Create: `packages/providers/tests/test_fake.py`

- [ ] **Step 1: Write the failing test for FakeProvider behavior**

Create `packages/providers/tests/test_fake.py`:

```python
import pytest

from synapse_providers import FakeProvider


@pytest.mark.asyncio
async def test_fake_provider_returns_scripted_text_when_no_schema() -> None:
    fake = FakeProvider(scripts=["hello world"])
    result = await fake.complete(messages=[{"role": "user", "content": "hi"}])
    assert result.data == "hello world"
    assert result.provider_id == "fake"
    assert result.schema_valid is True
    assert result.usage.input_tokens > 0
    assert result.usage.output_tokens > 0


@pytest.mark.asyncio
async def test_fake_provider_returns_scripted_structured_output() -> None:
    scripted = {"findings": [{"type": "learning", "text": "x", "contributor": "c",
                              "ts": "2026-07-25T12:00:00Z", "source_session": "s"}]}
    fake = FakeProvider(scripts=[scripted])
    schema = {"type": "object", "properties": {"findings": {"type": "array"}}}
    result = await fake.complete(messages=[], response_schema=schema)
    assert result.data == scripted
    assert result.schema_valid is True


@pytest.mark.asyncio
async def test_fake_provider_is_deterministic_across_calls() -> None:
    fake = FakeProvider(scripts=["one", "two", "three"])
    r1 = await fake.complete(messages=[])
    r2 = await fake.complete(messages=[])
    r3 = await fake.complete(messages=[])
    assert (r1.data, r2.data, r3.data) == ("one", "two", "three")


@pytest.mark.asyncio
async def test_fake_provider_exhausts_and_raises() -> None:
    fake = FakeProvider(scripts=["only one"])
    await fake.complete(messages=[])
    with pytest.raises(RuntimeError, match="exhausted"):
        await fake.complete(messages=[])


@pytest.mark.asyncio
async def test_fake_provider_reports_capabilities() -> None:
    fake = FakeProvider(scripts=["x"])
    assert fake.capabilities.native_structured_output is True
```

- [ ] **Step 2: Run to verify failure**

Run: `uv run pytest packages/providers/tests/test_fake.py -v`
Expected: FAIL — `ImportError: cannot import name 'FakeProvider'`.

- [ ] **Step 3: Implement FakeProvider**

Create `packages/providers/src/synapse_providers/fake.py`:

```python
"""FakeProvider — scripted, deterministic, offline. The TDD backbone.

Every unit and contract test in Synapse runs against FakeProvider so tests
stay CI-safe (no API keys, no network, no GPU). LLM quality is a *measurement*
made by the eval harness, not a pass/fail gate in unit tests.
"""

from __future__ import annotations

from typing import Any

from synapse_contracts import ModelResult, ModelUsage

from synapse_providers.base import ModelProvider, ProviderCapabilities


class FakeProvider(ModelProvider):
    """Returns a pre-scripted sequence of outputs, one per complete() call.

    Usage:
        fake = FakeProvider(scripts=["response 1", {"key": "structured"}])
        result = await fake.complete(messages=[...])  # → "response 1"
        result = await fake.complete(messages=[...])  # → {"key": "structured"}
    """

    provider_id = "fake"

    def __init__(self, scripts: list[Any]) -> None:
        self._scripts = list(scripts)
        self._index = 0

    @property
    def capabilities(self) -> ProviderCapabilities:
        return ProviderCapabilities(native_structured_output=True, streaming=False)

    async def complete(
        self,
        messages: list[dict[str, Any]],
        response_schema: dict[str, Any] | None = None,
    ) -> ModelResult:
        if self._index >= len(self._scripts):
            raise RuntimeError(
                f"FakeProvider scripts exhausted after {self._index} call(s); "
                f"add more scripts or reset the provider."
            )
        data = self._scripts[self._index]
        self._index += 1

        # Deterministic pseudo-usage: token count proxies from character length.
        # These are stable across runs so tests can assert on them if needed.
        input_chars = sum(len(str(m.get("content", ""))) for m in messages)
        output_chars = len(str(data))
        return ModelResult(
            data=data,
            usage=ModelUsage(
                input_tokens=max(1, input_chars // 4),
                output_tokens=max(1, output_chars // 4),
            ),
            latency_ms=0,
            provider_id=self.provider_id,
            schema_valid=True,
        )
```

- [ ] **Step 4: Re-export from package root**

Overwrite `packages/providers/src/synapse_providers/__init__.py`:

```python
"""ModelProvider implementations."""

from synapse_providers.base import ModelProvider, ProviderCapabilities
from synapse_providers.fake import FakeProvider

__all__ = ["FakeProvider", "ModelProvider", "ProviderCapabilities"]
```

- [ ] **Step 5: Run the test to verify it passes**

Run: `uv run pytest packages/providers/tests/test_fake.py -v`
Expected: All tests PASS.

- [ ] **Step 6: Commit**

```bash
git add packages/providers
git commit -m "$(cat <<'EOF'
feat(providers): implement FakeProvider (Plan 0 Task 4)

Scripted, deterministic, offline. Every downstream unit test builds against
this so CI stays fast, key-free, and GPU-free. LLM quality is measured by
the eval harness in Plan B, not asserted in unit tests.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

### Task 5: Hand-author fixture Segments and golden Findings

This task is a *co-authoring exercise* between all three teammates. It cannot be automated — it defines the quality bar and pins the segmenter/distiller boundary.

**Files:**
- Create: `fixtures/raw_session.jsonl`
- Create: `fixtures/segments/seg-001.json`
- Create: `fixtures/segments/seg-002.json`
- Create: `fixtures/findings/seg-001.findings.json`
- Create: `fixtures/findings/seg-002.findings.json`
- Create: `fixtures/__init__.py`
- Create: `tests/test_fixtures_load.py`

- [ ] **Step 1: Extract a real Claude Code session excerpt**

Copy a ~50-line, ~2-turn slice from `~/.claude/projects/<any-project>/<uuid>.jsonl` into `fixtures/raw_session.jsonl`. Pick a slice where the agent (a) reads/greps to understand something, (b) tries an approach that doesn't work, and (c) commits to a different approach — a real learning + dead_end + decision + open_question mix.

**Redact** anything workplace-sensitive: file paths under Qualcomm-internal directories, real customer identifiers, internal project code names. Replace with placeholders (`/repo/module.py`, `PROJECT_X`). The fixture ships in the repo; assume it's public.

- [ ] **Step 2: Hand-author two `Segment` JSON blobs**

Split the raw session into two Segments on turn boundaries (each ends with `role: assistant` + `kind: tool_result` clusters closing out a user turn). Fill in `id: "seg-001"` / `seg-002`, `session_id`, `started_at`, `ended_at`, and the `events` array using the exact `AgentEvent` shape from Task 2.

> ⚠️ **Segmentation invariant** — each Segment MUST begin with a `role: user, kind: text` event. This matches the segmenter's boundary rule from Plan A Task 3 ("a new user text turn after any assistant activity closes the previous segment"), so the segmenter can reproduce these fixtures deterministically. If a natural split lands elsewhere in the raw session, either adjust the split point or coordinate with Akhil to update the segmenter's boundary rule *in the same commit* as the fixture change.

Create `fixtures/segments/seg-001.json`:

```json
{
  "id": "seg-001",
  "session_id": "local-fixture-abc",
  "started_at": "2026-07-25T12:00:00Z",
  "ended_at": "2026-07-25T12:04:30Z",
  "events": [
    {
      "role": "user",
      "kind": "text",
      "content": "The auth tests are flaky. Figure out why.",
      "ts": "2026-07-25T12:00:00Z",
      "session_id": "local-fixture-abc",
      "cwd": "/repo",
      "git_branch": "main"
    }
  ]
}
```

Fill in the rest of the events based on the raw session. Same shape for `seg-002.json`.

- [ ] **Step 3: Hand-author golden `Finding[]` for each Segment**

Sit together and write what a *good* distillation of each Segment looks like. Discuss until all three teammates agree. Each Finding must be abstracted (no verbatim code), tagged with contributor + timestamp.

Create `fixtures/findings/seg-001.findings.json`:

```json
[
  {
    "type": "learning",
    "text": "Auth middleware rejects tokens whose 'iat' claim is more than 60 seconds in the future — used to diagnose flaky tests where the test clock drifts.",
    "contributor": "siddsing",
    "ts": "2026-07-25T12:04:30Z",
    "source_session": "local-fixture-abc",
    "refs": ["auth/middleware.py:88-102"]
  },
  {
    "type": "dead_end",
    "text": "Increasing the test-runner's global timeout does not fix the flakiness — the tokens are rejected before the test's assertion runs.",
    "contributor": "siddsing",
    "ts": "2026-07-25T12:03:00Z",
    "source_session": "local-fixture-abc"
  }
]
```

Do the same for `seg-002.findings.json`. Aim for 2–4 findings per segment, covering learning / decision / dead_end / open_question across the two fixtures. **Do not shortcut this — the quality bar is exactly what a human judges to be a good distillation. That is the eval target.**

- [ ] **Step 4: Write a loader test that proves the fixtures parse**

Create `fixtures/__init__.py` as an empty file.

Create `tests/test_fixtures_load.py`:

```python
"""Fixtures must parse into the frozen contract types.

If this fails, either the fixtures drifted from the schemas or the schemas
drifted from the fixtures. Either way, land here first before other tests
that consume the fixtures.
"""

import json
from pathlib import Path

from synapse_contracts import Finding, Segment


FIXTURES = Path(__file__).parent.parent / "fixtures"


def test_all_segments_parse_as_segment() -> None:
    seg_files = sorted((FIXTURES / "segments").glob("*.json"))
    assert len(seg_files) >= 2, f"need ≥2 fixture segments; got {len(seg_files)}"
    for path in seg_files:
        data = json.loads(path.read_text())
        seg = Segment.model_validate(data)
        assert seg.events, f"{path.name} has no events"
        assert seg.started_at <= seg.ended_at


def test_every_segment_has_a_findings_file() -> None:
    seg_ids = {p.stem for p in (FIXTURES / "segments").glob("*.json")}
    findings_ids = {p.stem.replace(".findings", "") for p in (FIXTURES / "findings").glob("*.findings.json")}
    assert seg_ids == findings_ids, (
        f"segments and findings must correspond 1:1; "
        f"segments={seg_ids}, findings={findings_ids}"
    )


def test_all_findings_parse_as_finding_list() -> None:
    finding_files = sorted((FIXTURES / "findings").glob("*.findings.json"))
    for path in finding_files:
        data = json.loads(path.read_text())
        assert isinstance(data, list), f"{path.name} must be a JSON array"
        assert data, f"{path.name} is empty; each segment needs ≥1 finding"
        for item in data:
            Finding.model_validate(item)


def test_findings_cover_all_four_types_across_fixtures() -> None:
    all_types: set[str] = set()
    for path in (FIXTURES / "findings").glob("*.findings.json"):
        data = json.loads(path.read_text())
        for item in data:
            all_types.add(item["type"])
    assert all_types == {"learning", "decision", "dead_end", "open_question"}, (
        f"fixtures must exercise all four Finding types; got {all_types}"
    )
```

- [ ] **Step 5: Run the fixture tests**

Run: `uv run pytest tests/test_fixtures_load.py -v`
Expected: All tests PASS. If `test_findings_cover_all_four_types_across_fixtures` fails, expand the fixture set until all four types appear.

- [ ] **Step 6: Commit**

```bash
git add fixtures tests/test_fixtures_load.py
git commit -m "$(cat <<'EOF'
feat(fixtures): hand-author fixture Segments and golden Findings (Plan 0 Task 5)

Two Segments (seg-001, seg-002) extracted from a real Claude Code session,
with 2–4 golden Findings each. Cover all four Finding types. These are the
cross-track ground truth: the segmenter must reproduce these Segments and
the distiller's output is scored against these Findings.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

### Task 6: Freeze the ingest API contract (request/response schemas)

**Files:**
- Create: `packages/contracts/src/synapse_contracts/ingest_api.py`
- Modify: `packages/contracts/src/synapse_contracts/__init__.py`
- Create: `packages/contracts/tests/test_ingest_api.py`

- [ ] **Step 1: Write the failing test for ingest API shapes**

Create `packages/contracts/tests/test_ingest_api.py`:

```python
from datetime import datetime, timezone

from synapse_contracts import (
    CreateSessionRequest,
    CreateSessionResponse,
    Finding,
    JoinSessionRequest,
    JoinSessionResponse,
    PushFindingsRequest,
    PushFindingsResponse,
)


UTC = timezone.utc


def test_create_session_request_shape() -> None:
    req = CreateSessionRequest(purpose="Debug flaky auth tests", created_by="siddsing")
    assert req.model_dump() == {
        "purpose": "Debug flaky auth tests",
        "created_by": "siddsing",
    }


def test_create_session_response_returns_shared_id() -> None:
    resp = CreateSessionResponse(shared_id="shared-xyz")
    assert resp.shared_id == "shared-xyz"


def test_join_session_request_binds_local_to_shared() -> None:
    req = JoinSessionRequest(
        shared_id="shared-xyz",
        local_agent_session_id="local-abc",
        contributor="akhil",
    )
    assert req.contributor == "akhil"


def test_join_session_response_confirms_binding() -> None:
    resp = JoinSessionResponse(ok=True)
    assert resp.ok is True


def test_push_findings_request_shape() -> None:
    f = Finding(
        type="learning",
        text="x",
        contributor="siddsing",
        ts=datetime(2026, 7, 25, 12, 0, tzinfo=UTC),
        source_session="local-abc",
    )
    req = PushFindingsRequest(shared_id="shared-xyz", findings=[f])
    assert len(req.findings) == 1


def test_push_findings_response_reports_accepted() -> None:
    resp = PushFindingsResponse(accepted=3)
    assert resp.accepted == 3
```

- [ ] **Step 2: Run to verify failure**

Run: `uv run pytest packages/contracts/tests/test_ingest_api.py -v`
Expected: FAIL — ImportError.

- [ ] **Step 3: Implement the ingest API contracts**

Create `packages/contracts/src/synapse_contracts/ingest_api.py`:

```python
"""Ingest API — the wire contract between edge workers and the Synapse service.

Freezing this Day 0 lets Akhil test his sync client against a mock HTTP server
without depending on Siddsing's real service being live yet.
"""

from __future__ import annotations

from pydantic import BaseModel

from synapse_contracts.schemas import Finding


class CreateSessionRequest(BaseModel):
    purpose: str
    created_by: str


class CreateSessionResponse(BaseModel):
    shared_id: str


class JoinSessionRequest(BaseModel):
    shared_id: str
    local_agent_session_id: str
    contributor: str


class JoinSessionResponse(BaseModel):
    ok: bool


class PushFindingsRequest(BaseModel):
    shared_id: str
    findings: list[Finding]


class PushFindingsResponse(BaseModel):
    accepted: int
```

- [ ] **Step 4: Re-export**

Overwrite `packages/contracts/src/synapse_contracts/__init__.py`:

```python
"""Frozen cross-track contracts for Synapse."""

from synapse_contracts.ingest_api import (
    CreateSessionRequest,
    CreateSessionResponse,
    JoinSessionRequest,
    JoinSessionResponse,
    PushFindingsRequest,
    PushFindingsResponse,
)
from synapse_contracts.schemas import (
    AgentEvent,
    Conflict,
    Finding,
    FindingType,
    LocalBinding,
    ModelResult,
    ModelUsage,
    Segment,
    SessionContext,
    SynapseSession,
)

__all__ = [
    "AgentEvent",
    "Conflict",
    "CreateSessionRequest",
    "CreateSessionResponse",
    "Finding",
    "FindingType",
    "JoinSessionRequest",
    "JoinSessionResponse",
    "LocalBinding",
    "ModelResult",
    "ModelUsage",
    "PushFindingsRequest",
    "PushFindingsResponse",
    "Segment",
    "SessionContext",
    "SynapseSession",
]
```

- [ ] **Step 5: Run tests**

Run: `uv run pytest packages/contracts -v`
Expected: All tests PASS (test_schemas.py + test_ingest_api.py both green).

- [ ] **Step 6: Commit**

```bash
git add packages/contracts
git commit -m "$(cat <<'EOF'
feat(contracts): freeze ingest API (Plan 0 Task 6)

CreateSession, JoinSession, PushFindings — request/response Pydantic
models. Unblocks Akhil's sync client (tests against a mock HTTP server
using these types).

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

### Task 7: Land the walking-skeleton test (red)

The walking skeleton wires the thinnest end-to-end path — FakeSource → single Segment → FakeProvider distiller → in-memory synthesis → MCP-style query — using only fakes. Ships red now; each track's real implementation turns pieces of it green. If the contracts compose wrong, we find out here on Day 0, not during integration.

**Files:**
- Create: `tests/test_walking_skeleton.py`

- [ ] **Step 1: Write the red test**

Create `tests/test_walking_skeleton.py`:

```python
"""Walking skeleton — end-to-end integration proof.

Ships RED. Turns green incrementally as each track lands:
- Plan A (Akhil): worker.segmenter turns fixture events → fixture Segment
- Plan B (Aditya): worker.distiller.Distiller(fake_provider) → Finding[]
- Plan C (Siddsing): service.InMemorySynthesizer + service.query_findings

If this test can't be written because a contract is wrong, the contract is
wrong. That's the point — find contract bugs on Day 0, not during integration.
"""

import json
from datetime import datetime, timezone
from pathlib import Path

import pytest

from synapse_contracts import Finding, Segment


FIXTURES = Path(__file__).parent.parent / "fixtures"


@pytest.mark.integration
@pytest.mark.xfail(reason="walking skeleton is red until Plans A/B/C land", strict=False)
async def test_end_to_end_fake_pipeline() -> None:
    """FakeSource → Segment → FakeProvider distiller → in-memory synthesis → query."""

    # --- Distiller (Plan B; imported here so this test breaks red when it lands) ---
    from synapse_providers import FakeProvider
    from synapse_worker.distiller import Distiller  # noqa: F401 (Plan B ships this)

    seg_data = json.loads((FIXTURES / "segments" / "seg-001.json").read_text())
    segment = Segment.model_validate(seg_data)

    findings_data = json.loads((FIXTURES / "findings" / "seg-001.findings.json").read_text())
    scripted = {"findings": findings_data}
    fake_provider = FakeProvider(scripts=[scripted])
    distiller = Distiller(provider=fake_provider, contributor="siddsing")

    findings: list[Finding] = await distiller.distill(segment)
    assert len(findings) == len(findings_data)

    # --- Synthesis (Plan C) ---
    from synapse_service.synthesis import InMemorySynthesizer  # noqa: F401

    fake_synth_provider = FakeProvider(scripts=["The team is debugging flaky auth tests."])
    synth = InMemorySynthesizer(provider=fake_synth_provider)
    ctx = await synth.merge(shared_id="shared-1", purpose="Debug flaky auth tests", new_findings=findings)
    assert ctx.working_memory  # non-empty

    # --- Retrieval (Plan C) ---
    from synapse_service.retrieval import query_findings  # noqa: F401

    fake_retriever = FakeProvider(scripts=[{"ranked": [f.model_dump(mode="json") for f in findings]}])
    ranked = await query_findings(
        query="What did we learn about the auth tests?",
        context=ctx,
        findings=findings,
        provider=fake_retriever,
    )
    assert len(ranked) >= 1
```

- [ ] **Step 2: Run to verify it fails (expected)**

Run: `uv run pytest tests/test_walking_skeleton.py -v`
Expected: XFAIL (or FAIL on the import at line for `Distiller`). Either is fine — this is the marker test that turns green as tracks land.

- [ ] **Step 3: Verify the rest of the suite is green**

Run: `uv run pytest --ignore=tests/test_walking_skeleton.py -v`
Expected: All PASS.

- [ ] **Step 4: Commit**

```bash
git add tests/test_walking_skeleton.py
git commit -m "$(cat <<'EOF'
test: land red walking-skeleton (Plan 0 Task 7)

End-to-end integration proof using only fakes: FakeSource → Segment →
FakeProvider distiller → in-memory synthesis → query. Ships XFAIL; turns
green incrementally as Plans A/B/C land. If this can't be written because
a contract is wrong, the contract is wrong — that's the point.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

### Task 8: Verify Day 0 exit criteria

- [ ] **Step 1: Run the full suite**

Run: `uv run pytest -v`
Expected: All tests pass except `test_walking_skeleton.py::test_end_to_end_fake_pipeline` which is XFAIL.

- [ ] **Step 2: Run lint**

Run: `uv run ruff check .`
Expected: No errors.

Run: `uv run ruff format --check .`
Expected: All files formatted.

- [ ] **Step 3: Sanity-check the git log**

Run: `git log --oneline`
Expected: 7 commits (one per task from 1–7). Each has the `Co-Authored-By` trailer.

- [ ] **Step 4: Confirm the hand-off is real**

- Akhil can now start Plan A: he has frozen `AgentEvent`, `Segment`, ingest API contracts, and the fixture Segments his segmenter must reproduce.
- Aditya can now start Plan B: he has the frozen `Segment` (input), `Finding` (output), `ModelProvider` interface, `FakeProvider`, and the golden `Finding[]` his distiller's quality is scored against.
- You (Siddsing) can now start Plan C: you have the frozen `Finding`, `SessionContext`, `Conflict`, `ModelProvider`, `ModelResult`, and the ingest API contracts you need to serve.

**If any of the four blocking artifacts (contracts / FakeProvider / fixture Segments / golden Findings / red walking-skeleton test) is missing or drifting from what the plans reference, stop and fix it before parallel work begins.**

---

## Scope / YAGNI

**In (Plan 0):** monorepo scaffold, frozen contracts (schemas + ingest API), `ModelProvider` interface + `FakeProvider`, fixture Segments + golden Findings, red walking-skeleton test.

**Out (deferred to per-track plans):**
- No source adapter, no follower, no segmenter (Plan A)
- No distiller, no NPU spike, no eval harness (Plan B)
- No `ClaudeProvider` / `OllamaProvider` / `AIC100Provider`, no synthesis service, no MCP server, no AI-100 spike (Plan C)

## Known Risks

| Risk | Mitigation |
|---|---|
| Fixture Segments encode a segmentation decision Akhil later disagrees with | Co-author them; the segmenter's contract IS the fixture, not a doc |
| Golden Findings encode an aesthetic Aditya's distiller can't reach | Co-author them; if quality vs golden is too low even for Claude, revisit the fixtures — the fixture IS the quality bar |
| `ModelProvider` signature turns out to need streaming or an `embed()` method | Both explicitly out of scope. Retrieval is LLM-as-retriever (single `complete()` call over the shared-memory doc). Vector RAG is a stretch goal |
| Contracts drift between packages after Day 0 | Every contract lives in `synapse_contracts`; other packages import, never redefine. Enforced by code review, not tooling |
