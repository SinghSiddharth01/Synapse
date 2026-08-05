# E6 — Debug Dashboards Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Two self-served, real-time debug dashboards — `synapse-worker` at `:8790/debug`, `synapse-service` at `:8899/debug` — each an instrumented, **tagged event feed** plus live counters, so the slow end-to-end (NPU ~10 s/segment, pushes, merges) is watchable and debuggable in a browser.

## Spec (approved design + amendment, 2026-08-05)

Two separate pages, per process (user's explicit choice). Worker grows a tiny stdlib HTTP server in a daemon thread (`--debug-port`, default 8790, `0` disables). Service mounts `/debug` routes on its existing Starlette app. Both: self-contained HTML, inline CSS/JS, 1 s `fetch` polling of a stats JSON endpoint, dark, `tabular-nums`, **no chart libraries and no chart markup** — counters, tagged feeds, one ticking elapsed timer.

**Amendment (user, mid-design): "we need to see the LLM calls, compaction results, merges etc — tag all of those things."** Hence:
- `RecordingProvider` — wraps any `ModelProvider`, records every `complete()` call: component, provider_id, tokens in/out, latency, schema_valid, prompt/output previews (~200 chars). No existing provider changes.
- Worker feed tags: `tick` · `triage` · `render` · `llm` · `push` · `error`. The `render` tag records per-segment what the distiller actually saw (events by kind in → retained under `distil_kinds`, est vs measured tokens) — honestly named until A.5 compaction lands, then it becomes the compaction view.
- Service feed tags: `log` (the brain's own entry kinds: FindingAppended/Merged/TopicAssigned/TopicSplit/MarkedTrivial) · `llm` · `query` · `synthesis` (full verdict summaries). The append-only log *is* the merge feed — tail it, don't duplicate it.
- UI: one feed per page, filter chips by tag.

Read-only, no model calls from any debug endpoint (pinned by test), localhost-only, no auth — consistent with the repo's named auth stance.

## Global Constraints

- Mac: `export PATH="/opt/homebrew/bin:$PATH"`; plain `uv sync`. Regression floor: **526 green**; closed-loop test unmodified.
- No new dependencies anywhere. Worker's server is stdlib `http.server` + `threading` (the worker deliberately has no Starlette). Service reuses Starlette.
- Debug surfaces read state; they never mutate it and never call a provider. `RecordingProvider` must be transparent: same result object returned, exceptions propagate unchanged (record the failure, re-raise).
- Vocabulary: `CONTEXT.md`. Feed entries use its terms (Finding, Segment, Topic, View — never "row"/"cluster").

---

### Task 1: `RecordingProvider` + `CallLog`

**Files:**
- Create: `packages/providers/src/synapse_providers/recording.py`
- Modify: `packages/providers/src/synapse_providers/__init__.py` (export `RecordingProvider`, `CallLog`, `LLMCall`)
- Create: `packages/providers/tests/test_recording.py`

**Interfaces:**
- Produces: `CallLog(maxlen=200)` with `.append(call)`, `.snapshot() -> list[dict]`; `LLMCall` dataclass `{ts_iso, component, provider_id, input_tokens, output_tokens, latency_ms, schema_valid, ok, prompt_preview, output_preview}`; `RecordingProvider(inner: ModelProvider, component: str, log: CallLog)` implementing `ModelProvider`. Tasks 2 and 4 wrap providers with exactly this.

- [ ] **Step 1: Failing tests**

```python
# packages/providers/tests/test_recording.py
import pytest
from synapse_providers import FakeProvider
from synapse_providers.recording import CallLog, RecordingProvider


async def test_records_a_call_and_returns_the_inner_result_unchanged():
    log = CallLog()
    inner = FakeProvider(scripts=["hello world"])
    provider = RecordingProvider(inner, "distiller", log)
    result = await provider.complete([{"role": "user", "content": "hi there"}])
    assert result.data == "hello world"
    [call] = log.snapshot()
    assert call["component"] == "distiller"
    assert call["provider_id"] == "fake"
    assert call["ok"] is True
    assert call["input_tokens"] >= 1 and call["output_tokens"] >= 1
    assert "hi there" in call["prompt_preview"]
    assert "hello world" in call["output_preview"]


async def test_exceptions_propagate_and_are_recorded_as_failed():
    log = CallLog()
    provider = RecordingProvider(FakeProvider(scripts=[]), "synthesis", log)  # exhausted -> raises
    with pytest.raises(RuntimeError):
        await provider.complete([{"role": "user", "content": "x"}])
    [call] = log.snapshot()
    assert call["ok"] is False and call["component"] == "synthesis"


async def test_capabilities_and_provider_id_pass_through():
    provider = RecordingProvider(FakeProvider(scripts=[]), "retrieval", CallLog())
    assert provider.capabilities.native_structured_output is True
    assert provider.provider_id == "fake"


def test_ring_buffer_bounds():
    log = CallLog(maxlen=3)
    for i in range(5):
        log.append_raw({"n": i})
    assert [c["n"] for c in log.snapshot()] == [2, 3, 4]
```

- [ ] **Step 2: Run** — `uv run pytest packages/providers/tests/test_recording.py -v` → FAIL (`ModuleNotFoundError`).

- [ ] **Step 3: Implement**

```python
# packages/providers/src/synapse_providers/recording.py
"""RecordingProvider — transparent instrumentation for any ModelProvider.

Wraps, records, re-raises. The debug dashboards read CallLog.snapshot();
nothing else in the system may depend on it. Previews are truncated hard so
a dashboard never becomes a second place raw content accumulates.
"""

from __future__ import annotations

import time
from collections import deque
from datetime import datetime, timezone
from typing import Any

from synapse_contracts import ModelResult

from synapse_providers.base import ModelProvider, ProviderCapabilities

PREVIEW_CHARS = 200


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

    async def complete(self, messages: list[dict[str, Any]],
                       response_schema: dict[str, Any] | None = None) -> ModelResult:
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
            self.log.append_raw({**base, "ok": False, "input_tokens": 0, "output_tokens": 0,
                                 "latency_ms": int((time.perf_counter() - started) * 1000),
                                 "schema_valid": False, "output_preview": ""})
            raise
        self.log.append_raw({**base, "ok": True,
                             "input_tokens": result.usage.input_tokens,
                             "output_tokens": result.usage.output_tokens,
                             "latency_ms": result.latency_ms,
                             "schema_valid": result.schema_valid,
                             "output_preview": _preview(str(result.data))})
        return result
```

Export from `__init__.py`.

- [ ] **Step 4: Run** → PASS. **Step 5: Commit** — `feat(providers): RecordingProvider — tagged LLM-call instrumentation`.

---

### Task 2: worker `StatsBuffer` + loop instrumentation

**Files:**
- Create: `packages/worker/src/synapse_worker/stats.py`
- Modify: `packages/worker/src/synapse_worker/loop.py` (feed events at the named points)
- Create: `packages/worker/tests/test_stats.py`

**Interfaces:**
- Produces: `StatsBuffer(llm: CallLog)` with `.event(tag, summary, **detail)`, `.tick(result_dict)`, `.distil_started(segment_id, events)/.distil_finished()`, `.snapshot() -> dict` (JSON-safe: `{now, current, ticks, events, llm}`). `WorkerLoop.__init__` gains optional `stats: StatsBuffer | None = None`; every hook below is a no-op when `stats is None`.

- [ ] **Step 1: Failing tests** — assert: `snapshot()` is `json.dumps`-able; `distil_started` sets `current = {segment_id, started_iso, events}` and `distil_finished` clears it; `.event("triage", …)` lands in `events` with its tag and ISO ts; ring bounds hold (`maxlen=200` events, 100 ticks); **and an integration test**: run a `WorkerLoop.tick()` (arrange exactly like the nearest existing test in `test_loop.py`) with a `StatsBuffer` attached and assert the snapshot contains ≥1 `tick` event, a `render` event whose detail has `events_in`/`retained`/`kinds`, and (via a `RecordingProvider`-wrapped `FakeProvider`) ≥1 `llm` call with `component == "distiller"`.
- [ ] **Step 2: Run** → FAIL.
- [ ] **Step 3: Implement.** `stats.py`: dataclass-free, deques + one dict, all values already JSON-safe. Loop instrumentation points, exactly these, all guarded by `if self.stats:`:
  - end of `tick()`: `stats.tick(asdict(result))` + `stats.event("tick", result.summary())`
  - triage skip branch: `stats.event("triage", f"skip {segment.id} ({decision.reason})", segment=segment.id, reason=decision.reason)`; keep branch: same with `keep`
  - around distil: `stats.distil_started(segment.id, len(segment.events))` before, `stats.distil_finished()` in a `finally:`
  - after distil: `stats.event("render", f"{segment.id}: {retained}/{len(segment.events)} events reached the model", events_in=len(segment.events), retained=retained, kinds=list(self.distiller.kinds), input_tokens=stats_obj.input_tokens)` where `retained = sum(1 for e in segment.events if e.kind in set(self.distiller.kinds))` and `stats_obj` is the `DistillStats` the call returned
  - producer flush result: `stats.event("push", f"{sent} sent, {pending} queued", sent=sent, queued=pending)`
  - every `except` branch that appends to `result.errors`: `stats.event("error", …)`
- [ ] **Step 4: Run full worker suite** → PASS, 526 floor intact. **Step 5: Commit** — `feat(worker): StatsBuffer — tagged tick/triage/render/llm/push/error feed`.

---

### Task 3: worker debug server + page

**Files:**
- Create: `packages/worker/src/synapse_worker/debug_server.py`
- Modify: `packages/worker/src/synapse_worker/cli.py` (`--debug-port` on `run`, default 8790, `0` disables; wrap the distiller's provider in `RecordingProvider(…, "distiller", stats_log)` when enabled; env `SYNAPSE_DEBUG_PORT`)
- Create: `packages/worker/tests/test_debug_server.py`

**Interfaces:**
- Produces: `DebugServer(stats: StatsBuffer, port: int)` with `.start() -> int` (returns bound port; port 0 = ephemeral for tests) and `.stop()`. Routes: `GET /debug` → HTML, `GET /debug/stats.json` → `stats.snapshot()`, anything else → 404.

- [ ] **Step 1: Failing tests** — start on port 0, then over real HTTP (`urllib.request`): `/debug/stats.json` parses as JSON and matches a fresh snapshot; `/debug` returns 200 `text/html` containing `id="feed"` and `id="npu-now"`; a POST anywhere returns 405 (read-only pinned); `.stop()` frees the port.
- [ ] **Step 2: Run** → FAIL.
- [ ] **Step 3: Implement.** `http.server.ThreadingHTTPServer` + `BaseHTTPRequestHandler` in a daemon thread; handler holds a reference to the buffer via closure. `do_POST/PUT/DELETE` → 405. The page, complete and inline (structure fixed; visual polish is the implementer's):
  - header strip: transcript path · tick count · WAL `sent/queued` · held events + turn-open age
  - `#npu-now`: current segment id + **elapsed timer ticking client-side** from `current.started_iso`; shows `idle` when `current` is null
  - `#feed`: newest-first list, each entry `[HH:MM:SS] [tag] summary`, tag rendered as a colored chip; **filter chips** for `tick/triage/render/llm/push/error` toggling visibility client-side; clicking an `llm` entry expands its previews/token/latency detail
  - JS: `setInterval(refresh, 1000)`, one `fetch('/debug/stats.json')`, no libraries; `font-variant-numeric: tabular-nums`; failed fetch shows a red "worker unreachable" banner rather than console noise
- [ ] **Step 4: Run** → PASS; CLI coverage stays 100% (extend `test_cli.py` following its pattern for the new flag, including `--debug-port 0` meaning disabled). **Step 5: Commit** — `feat(worker): /debug dashboard — live NPU-now, tagged feed, read-only`.

---

### Task 4: service debug routes + page

**Files:**
- Create: `packages/service/src/synapse_service/debug.py`
- Modify: `packages/service/src/synapse_service/api.py` (mount routes; wrap the provider once per component: `RecordingProvider(provider, "synthesis", call_log)` for the synthesizer, `RecordingProvider(provider, "retrieval", call_log)` for `query_findings`; record a `query` feed event in the query route: question preview, candidate count, ranked count; record a `synthesis` feed event after each merge: new ids, merges applied, trivial count, conflicts, version)
- Create: `packages/service/tests/test_debug.py`

**Interfaces:**
- Produces: `GET /debug` (HTML) and `GET /debug/stats.json?session=<sid>` → `{sessions: [...], session: {sid, memory_version, watermark, view: {visible, superseded, trivial}, topics: [{label, size}], log_tail: [{position, kind, summary, ts}], merges: [...], queries: [...], llm: [...]}`. `session` omitted → first session. The log tail is read from the brain's append-only log — **the log is the merge/topic feed; do not build a second one.**

- [ ] **Step 1: Failing tests** — over ASGI (`httpx.ASGITransport`, no sockets, matching `test_api.py`'s style): push the seg-005 golden pair with a scripted merge (reuse `test_synthesis.py`'s `MERGE_SCRIPT` shape), then `GET /debug/stats.json` and assert: `log_tail` contains a `Merged` entry; `view.visible == 1` and `view.superseded == 2`; `llm` contains a call with `component == "synthesis"`; a query adds a `retrieval` call and a `queries` record. **Pin read-only + no model calls:** `GET /debug/stats.json` with a `FakeProvider(scripts=[])` must succeed (an exhausted fake raises on any call — success proves the endpoint never touches the provider) and must not change `memory_version`. `/debug` returns 200 HTML containing `id="log-tail"` and `id="feed"`.
- [ ] **Step 2: Run** → FAIL.
- [ ] **Step 3: Implement.** `debug.py` exposes `debug_routes(store, call_log, feed) -> list[Route]`; `api.build_app` creates one `CallLog` + one bounded feed deque and mounts. Page structure: session selector · counters strip (version, visible/superseded/trivial, conflicts, topics) · `#log-tail` colored by entry kind (`FindingAppended` grey · `Merged` green · `MarkedTrivial` amber · `TopicAssigned`/`TopicSplit` blue) · Working Memory collapsed `<details>` · `#feed` with `llm/query/synthesis` filter chips, same interaction pattern as the worker page. Same 1 s polling, same unreachable banner.
- [ ] **Step 4: Run** → PASS, full suite ≥526 green. **Step 5: Commit** — `feat(service): /debug dashboard — log tail, verdicts, tagged llm/query feed`.

---

### Task 5: demo-script wiring + docs

- [ ] Add to `docs/demo-script.md` §B, right after the two boot steps: open `http://127.0.0.1:8790/debug` and `http://127.0.0.1:8899/debug` side by side — beats 3–5 are *watchable* (NPU-now counts the distil seconds; the Merged entry appears in the service log tail the moment beat 5's push lands).
- [ ] `docs/STATE.md`: one line under what exists; `docs/plans/README.md`: E6 row.
- [ ] Full suite; commit — `docs: dashboards wired into the demo runbook`.

---

## Done when

1. `uv run pytest -q` ≥ 526 + new tests, green offline; closed-loop test untouched.
2. `synapse-worker run --debug-port 8790` serves a live page: NPU-now ticks during a distil, triage skips appear with reasons, LLM calls expand with previews.
3. The service page shows a `Merged` log entry and its synthesis verdict within one poll of a push landing.
4. Both stats endpoints are read-only and provider-untouching, **pinned by test**, not by intention.
5. No new dependencies; no chart markup anywhere.
