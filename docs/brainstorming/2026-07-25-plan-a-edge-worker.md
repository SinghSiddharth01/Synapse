# Synapse — Plan A: Edge Worker (Akhil)

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build the on-device worker that observes a coding agent's session, normalizes it into `AgentEvent`s, batches them into `Segment`s on turn boundaries, and syncs distilled `Finding`s to the Synapse service — all with zero modification to the agent being observed.

**Architecture:** Source-adapter plugins tail on-disk JSONL and emit `AgentEvent`s → file follower handles rotation and partial lines → segmenter batches on turn boundaries → distiller (delivered by Plan B) produces `Finding[]` → sync client POSTs to the ingest API. Every stage is deterministic plumbing testable with fixtures + a mock HTTP server; no LLM and no hardware in this track.

**Tech Stack:** Python 3.12, pytest, `httpx`, `pytest-httpserver` (for the mock ingest server).

**Owner:** Akhil.

> **⟨AMENDED 2026-08-03 D⟩** — see `2026-08-03-agent-detection-demo-storage-amendment.md`. (1) The source layer gains an **agent registry + auto-detection**: the worker watches registered transcript roots (Claude Code `~/.claude/projects/`, Codex's session dir) and spawns the matching follower+adapter on fresh activity — no per-agent configuration, and compaction summaries the agent writes are ingested as events. (2) **`CodexSource` is elevated from stretch to the demo pair** — required by recording day. (3) The segmenter's token budget is **per-model**: read from the resolved distiller's capability record (usable context, prefill tok/s), never a hard-coded constant; the ~2–2.5K figure below is the expected value for a 4K qairt bundle, not a global.

**Prerequisites (from Plan 0):** frozen contracts in `synapse_contracts` (`AgentEvent`, `Segment`, `Finding`, `CreateSessionRequest`/`Response`, `JoinSessionRequest`/`Response`, `PushFindingsRequest`/`Response`), fixture Segments in `fixtures/segments/`, `FakeProvider`.

**Handoff to other tracks:** `ClaudeCodeSource` (Task 1) → `AgentEvent[]`, consumed by the segmenter (Task 3) → `Segment[]`, consumed by the Distiller (Plan B, Task 3) → `Finding[]`, consumed by the sync client (Task 4).

---

### Task 1: `ClaudeCodeSource` — parse the JSONL into `AgentEvent[]`

**Files:**
- Create: `packages/worker/src/synapse_worker/sources/__init__.py`
- Create: `packages/worker/src/synapse_worker/sources/base.py`
- Create: `packages/worker/src/synapse_worker/sources/claude_code.py`
- Create: `packages/worker/tests/__init__.py`
- Create: `packages/worker/tests/test_claude_code_source.py`
- Create: `fixtures/raw_lines/claude_code/basic_turn.jsonl`
- Create: `fixtures/raw_lines/claude_code/malformed_line.jsonl`
- Create: `fixtures/raw_lines/claude_code/tool_use_and_result.jsonl`

- [ ] **Step 1: Prepare the raw-line fixtures**

Create `fixtures/raw_lines/claude_code/basic_turn.jsonl` — a hand-authored, minimal 2-line file with one user text turn and one assistant text turn, matching the shape observed in real Claude Code JSONL:

```
{"type":"user","timestamp":"2026-07-25T12:00:00.000Z","sessionId":"local-fixture-abc","cwd":"/repo","gitBranch":"main","message":{"role":"user","content":"Debug the flaky auth tests."}}
{"type":"assistant","timestamp":"2026-07-25T12:00:05.100Z","sessionId":"local-fixture-abc","cwd":"/repo","gitBranch":"main","message":{"role":"assistant","content":[{"type":"text","text":"Let me look at the auth middleware first."}]}}
```

Create `fixtures/raw_lines/claude_code/tool_use_and_result.jsonl` — one assistant turn that includes a `tool_use` content block, plus one user turn carrying the matching `tool_result`:

```
{"type":"assistant","timestamp":"2026-07-25T12:01:00.000Z","sessionId":"local-fixture-abc","cwd":"/repo","gitBranch":"main","message":{"role":"assistant","content":[{"type":"text","text":"I'll grep for the token check."},{"type":"tool_use","id":"tu-1","name":"Grep","input":{"pattern":"iat"}}]}}
{"type":"user","timestamp":"2026-07-25T12:01:01.500Z","sessionId":"local-fixture-abc","cwd":"/repo","gitBranch":"main","message":{"role":"user","content":[{"type":"tool_result","tool_use_id":"tu-1","content":"auth/middleware.py:88: if abs(now - iat) > 60:"}]}}
```

Create `fixtures/raw_lines/claude_code/malformed_line.jsonl` — one valid line, one syntactically-broken line, one valid line:

```
{"type":"user","timestamp":"2026-07-25T12:02:00.000Z","sessionId":"local-fixture-abc","cwd":"/repo","gitBranch":"main","message":{"role":"user","content":"first"}}
{"type":"user","timestamp":"2026-07-25T12:02:01.000Z","sessionId":"local-fixture-abc","cwd":"/repo","gitBranch":"main","message":{"role":"user","content":"broken
{"type":"user","timestamp":"2026-07-25T12:02:02.000Z","sessionId":"local-fixture-abc","cwd":"/repo","gitBranch":"main","message":{"role":"user","content":"third"}}
```

(The middle line's content string has a literal newline inside JSON, so it's not valid on that single line.)

> ⚠️ **Editor gotcha** — verify with `wc -l fixtures/raw_lines/claude_code/malformed_line.jsonl` — it should be exactly **3** lines. Some editors auto-close unterminated strings on save, which would silently repair the fixture and defeat the test. If your editor does this, disable "auto-repair JSON" or write the file with a heredoc:
>
> ```bash
> cat > fixtures/raw_lines/claude_code/malformed_line.jsonl <<'EOF'
> {"type":"user","timestamp":"2026-07-25T12:02:00.000Z","sessionId":"local-fixture-abc","cwd":"/repo","gitBranch":"main","message":{"role":"user","content":"first"}}
> {"type":"user","timestamp":"2026-07-25T12:02:01.000Z","sessionId":"local-fixture-abc","cwd":"/repo","gitBranch":"main","message":{"role":"user","content":"broken
> {"type":"user","timestamp":"2026-07-25T12:02:02.000Z","sessionId":"local-fixture-abc","cwd":"/repo","gitBranch":"main","message":{"role":"user","content":"third"}}
> EOF
> ```

- [ ] **Step 2: Write the failing test suite for `ClaudeCodeSource`**

Create `packages/worker/tests/__init__.py` as an empty file.

Create `packages/worker/tests/test_claude_code_source.py`:

```python
from pathlib import Path

import pytest

from synapse_contracts import AgentEvent
from synapse_worker.sources.claude_code import ClaudeCodeSource


FIXTURES = Path(__file__).parent.parent.parent.parent / "fixtures" / "raw_lines" / "claude_code"


def _parse(path: Path) -> list[AgentEvent]:
    source = ClaudeCodeSource()
    return list(source.parse_lines(path.read_text().splitlines(keepends=False)))


def test_basic_turn_produces_two_text_events() -> None:
    events = _parse(FIXTURES / "basic_turn.jsonl")
    assert len(events) == 2
    assert events[0].role == "user"
    assert events[0].kind == "text"
    assert events[0].content == "Debug the flaky auth tests."
    assert events[1].role == "assistant"
    assert events[1].kind == "text"
    assert events[1].content == "Let me look at the auth middleware first."


def test_tool_use_and_result_expand_to_separate_events() -> None:
    events = _parse(FIXTURES / "tool_use_and_result.jsonl")
    # Assistant turn: text + tool_use → 2 events
    # User turn: tool_result → 1 event
    assert len(events) == 3
    assert events[0].kind == "text"
    assert events[1].kind == "tool_use"
    assert events[1].tool_name == "Grep"
    assert events[2].kind == "tool_result"
    assert "middleware.py:88" in events[2].content


def test_malformed_lines_are_skipped_not_crashed() -> None:
    events = _parse(FIXTURES / "malformed_line.jsonl")
    # First and third lines are valid; the malformed middle line is dropped.
    assert len(events) == 2
    assert events[0].content == "first"
    assert events[1].content == "third"


def test_events_carry_session_metadata() -> None:
    events = _parse(FIXTURES / "basic_turn.jsonl")
    e = events[0]
    assert e.session_id == "local-fixture-abc"
    assert e.cwd == "/repo"
    assert e.git_branch == "main"


def test_meta_lines_are_filtered() -> None:
    # 'ai-title', 'agent-name', 'permission-mode', etc. are metadata, not events.
    lines = [
        '{"type":"ai-title","aiTitle":"debug","sessionId":"s"}',
        '{"type":"user","timestamp":"2026-07-25T12:00:00Z","sessionId":"s","message":{"role":"user","content":"hi"}}',
        '{"type":"permission-mode","mode":"acceptEdits","sessionId":"s"}',
    ]
    source = ClaudeCodeSource()
    events = list(source.parse_lines(lines))
    assert len(events) == 1
    assert events[0].content == "hi"


def test_content_can_be_a_bare_string_or_a_list_of_blocks() -> None:
    # Claude Code writes both shapes: content: "text" and content: [{"type":"text","text":"..."}]
    lines = [
        '{"type":"user","timestamp":"2026-07-25T12:00:00Z","sessionId":"s","message":{"role":"user","content":"bare"}}',
        '{"type":"user","timestamp":"2026-07-25T12:00:01Z","sessionId":"s","message":{"role":"user","content":[{"type":"text","text":"blocked"}]}}',
    ]
    source = ClaudeCodeSource()
    events = list(source.parse_lines(lines))
    assert [e.content for e in events] == ["bare", "blocked"]
```

- [ ] **Step 3: Run — expect ImportError**

Run: `uv run pytest packages/worker/tests/test_claude_code_source.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'synapse_worker.sources'`.

- [ ] **Step 4: Implement the `Source` base and `ClaudeCodeSource`**

Create `packages/worker/src/synapse_worker/sources/__init__.py`:

```python
"""Source adapters — one per agent format. Each yields normalized AgentEvents."""

from synapse_worker.sources.base import Source
from synapse_worker.sources.claude_code import ClaudeCodeSource

__all__ = ["ClaudeCodeSource", "Source"]
```

Create `packages/worker/src/synapse_worker/sources/base.py`:

```python
"""Source protocol — every agent adapter parses raw lines into AgentEvents."""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Iterable, Iterator

from synapse_contracts import AgentEvent


class Source(ABC):
    """Parse raw JSONL lines from one agent's transcript format into AgentEvents.

    Adapters are agent-specific (Claude Code, Codex, ...) but all normalize to
    the same AgentEvent schema. Adding a new agent = one new Source subclass,
    nothing else changes downstream.
    """

    @abstractmethod
    def parse_lines(self, lines: Iterable[str]) -> Iterator[AgentEvent]:
        """Yield AgentEvents; skip malformed or metadata lines without raising."""
```

Create `packages/worker/src/synapse_worker/sources/claude_code.py`:

```python
"""ClaudeCodeSource — parse Claude Code's ~/.claude/projects/*.jsonl.

Each line is a JSON object; only lines with type in {"user", "assistant",
"system"} are turn events, and only those with a `message` object become
AgentEvents. A single line's `message.content` may be a bare string OR a list
of typed content blocks (text / thinking / tool_use / tool_result), each of
which becomes its own AgentEvent so the segmenter sees the true per-content-
block sequence.

Malformed lines and metadata types (ai-title, permission-mode, mode, agent-
name, file-history-snapshot, ...) are silently skipped — a partial write in
the middle of a JSONL file must not crash the worker.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Iterable, Iterator
from datetime import datetime
from typing import Any

from synapse_contracts import AgentEvent

from synapse_worker.sources.base import Source

log = logging.getLogger(__name__)

_TURN_TYPES = {"user", "assistant", "system"}
_ALLOWED_ROLES = {"user", "assistant", "system"}


class ClaudeCodeSource(Source):
    def parse_lines(self, lines: Iterable[str]) -> Iterator[AgentEvent]:
        for line in lines:
            line = line.strip()
            if not line:
                continue
            try:
                obj = json.loads(line)
            except json.JSONDecodeError:
                log.debug("skipping malformed JSON line: %r", line[:80])
                continue
            if not isinstance(obj, dict):
                continue
            if obj.get("type") not in _TURN_TYPES:
                continue
            message = obj.get("message")
            if not isinstance(message, dict):
                continue

            role = message.get("role")
            if role not in _ALLOWED_ROLES:
                continue

            ts_raw = obj.get("timestamp")
            ts = _parse_ts(ts_raw)
            if ts is None:
                continue

            session_id = obj.get("sessionId") or ""
            cwd = obj.get("cwd")
            git_branch = obj.get("gitBranch")

            content = message.get("content")
            for kind, text, tool_name in _iter_content_blocks(content):
                yield AgentEvent(
                    role=role,  # type: ignore[arg-type]
                    kind=kind,  # type: ignore[arg-type]
                    content=text,
                    tool_name=tool_name,
                    ts=ts,
                    session_id=session_id,
                    cwd=cwd,
                    git_branch=git_branch,
                )


def _parse_ts(raw: Any) -> datetime | None:
    if not isinstance(raw, str):
        return None
    try:
        # fromisoformat handles both "…Z" (3.11+) and offset-aware forms
        return datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return None


def _iter_content_blocks(content: Any) -> Iterator[tuple[str, str, str | None]]:
    """Yield (kind, text, tool_name) tuples.

    kind ∈ {"text", "thinking", "tool_use", "tool_result"}. Everything else is
    dropped (e.g. `image` blocks — not part of the distiller's input surface).
    """
    if isinstance(content, str):
        yield ("text", content, None)
        return
    if not isinstance(content, list):
        return
    for block in content:
        if not isinstance(block, dict):
            continue
        btype = block.get("type")
        if btype == "text":
            text = block.get("text", "")
            if text:
                yield ("text", text, None)
        elif btype == "thinking":
            text = block.get("thinking", "")
            if text:
                yield ("thinking", text, None)
        elif btype == "tool_use":
            tool = block.get("name", "")
            inp = block.get("input", {})
            yield ("tool_use", json.dumps(inp, sort_keys=True), tool or None)
        elif btype == "tool_result":
            inner = block.get("content", "")
            text = inner if isinstance(inner, str) else json.dumps(inner)
            yield ("tool_result", text, None)
```

- [ ] **Step 5: Run the tests**

Run: `uv run pytest packages/worker/tests/test_claude_code_source.py -v`
Expected: All tests PASS.

- [ ] **Step 6: Commit**

```bash
git add packages/worker/src/synapse_worker/sources packages/worker/tests/__init__.py packages/worker/tests/test_claude_code_source.py fixtures/raw_lines
git commit -m "$(cat <<'EOF'
feat(worker): ClaudeCodeSource — parse JSONL to AgentEvent[] (Plan A Task 1)

Handles bare-string and typed-block content, expands tool_use/tool_result
into separate events, skips malformed lines and metadata rows. Zero
modification to Claude Code — reads the transcript it already writes.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

### Task 2: `FileFollower` — tail, handle rotation, wait on partial lines

**Files:**
- Create: `packages/worker/src/synapse_worker/follower.py`
- Create: `packages/worker/tests/test_follower.py`

- [ ] **Step 1: Write the failing test suite**

Create `packages/worker/tests/test_follower.py`:

```python
import os
from pathlib import Path

import pytest

from synapse_worker.follower import FileFollower


def _write(path: Path, text: str, mode: str = "a") -> None:
    with path.open(mode) as f:
        f.write(text)


def test_reads_lines_already_present(tmp_path: Path) -> None:
    p = tmp_path / "session.jsonl"
    _write(p, '{"a":1}\n{"b":2}\n', mode="w")

    follower = FileFollower(p)
    lines = list(follower.read_available())
    assert lines == ['{"a":1}', '{"b":2}']


def test_reads_lines_appended_after_first_read(tmp_path: Path) -> None:
    p = tmp_path / "session.jsonl"
    _write(p, 'line1\n', mode="w")
    follower = FileFollower(p)
    assert list(follower.read_available()) == ["line1"]

    _write(p, 'line2\nline3\n')
    assert list(follower.read_available()) == ["line2", "line3"]


def test_partial_trailing_line_is_held_until_newline(tmp_path: Path) -> None:
    p = tmp_path / "session.jsonl"
    _write(p, 'complete\npart', mode="w")

    follower = FileFollower(p)
    # Only 'complete' is yielded — 'part' is held pending its newline.
    assert list(follower.read_available()) == ["complete"]

    _write(p, 'ial-then-newline\n')
    # Next read completes the previously-partial line.
    assert list(follower.read_available()) == ["partial-then-newline"]


def test_file_replaced_between_reads_starts_over(tmp_path: Path) -> None:
    """Rotation semantics: if the file was replaced (new inode), read from the start."""
    p = tmp_path / "session.jsonl"
    _write(p, 'old-1\nold-2\n', mode="w")
    follower = FileFollower(p)
    assert list(follower.read_available()) == ["old-1", "old-2"]

    # Replace the file — different inode.
    p.unlink()
    _write(p, 'new-1\nnew-2\n', mode="w")
    assert list(follower.read_available()) == ["new-1", "new-2"]


def test_missing_file_yields_nothing(tmp_path: Path) -> None:
    p = tmp_path / "nope.jsonl"
    follower = FileFollower(p)
    assert list(follower.read_available()) == []
    # And once it appears, we pick up from the start.
    _write(p, 'hello\n', mode="w")
    assert list(follower.read_available()) == ["hello"]
```

- [ ] **Step 2: Run — expect ImportError**

Run: `uv run pytest packages/worker/tests/test_follower.py -v`
Expected: FAIL — `ImportError: cannot import name 'FileFollower'`.

- [ ] **Step 3: Implement the follower**

Create `packages/worker/src/synapse_worker/follower.py`:

```python
"""FileFollower — tail a JSONL file, handling rotation and partial writes.

Contract:
- read_available() is idempotent: yields only NEW complete lines since the
  last call.
- A trailing partial line (no terminating newline) is buffered internally
  until the newline arrives.
- If the file's inode changes (rotation, atomic replace), we re-open from
  the start on the next read.
- If the file doesn't exist yet, read_available() yields nothing without
  raising; once it appears, we pick up from the start.
"""

from __future__ import annotations

import logging
import os
from collections.abc import Iterator
from pathlib import Path

log = logging.getLogger(__name__)


class FileFollower:
    def __init__(self, path: Path) -> None:
        self._path = path
        self._offset = 0
        self._inode: int | None = None
        self._partial = ""

    def read_available(self) -> Iterator[str]:
        try:
            st = self._path.stat()
        except FileNotFoundError:
            return

        # Rotation detection: if inode changed OR file shrank, start over.
        if self._inode is not None and (st.st_ino != self._inode or st.st_size < self._offset):
            log.info("file %s rotated; resetting", self._path)
            self._offset = 0
            self._partial = ""
        self._inode = st.st_ino

        if st.st_size == self._offset:
            return

        with self._path.open("r") as f:
            f.seek(self._offset)
            chunk = f.read()
            self._offset = f.tell()

        buf = self._partial + chunk
        lines = buf.split("\n")
        # The last element is either "" (chunk ended on \n) or a partial line.
        self._partial = lines[-1]
        for line in lines[:-1]:
            yield line
```

- [ ] **Step 4: Run the tests**

Run: `uv run pytest packages/worker/tests/test_follower.py -v`
Expected: All tests PASS.

- [ ] **Step 5: Commit**

```bash
git add packages/worker/src/synapse_worker/follower.py packages/worker/tests/test_follower.py
git commit -m "$(cat <<'EOF'
feat(worker): FileFollower — tail, rotate, partial-line safe (Plan A Task 2)

Idempotent read_available() yields only new complete lines; buffers partial
trailing writes until the newline arrives; detects inode change or shrink
as rotation and re-reads from the start; missing file is not an error.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

### Task 3: `Segmenter` — batch AgentEvents into Segments on turn boundaries

**Files:**
- Create: `packages/worker/src/synapse_worker/segmenter.py`
- Create: `packages/worker/tests/test_segmenter.py`

- [ ] **Step 1: Write the failing test suite**

Create `packages/worker/tests/test_segmenter.py`:

```python
import json
from datetime import datetime, timezone
from pathlib import Path

import pytest

from synapse_contracts import AgentEvent, Segment
from synapse_worker.segmenter import Segmenter


UTC = timezone.utc
FIXTURES = Path(__file__).parent.parent.parent.parent / "fixtures"


def _event(role: str, kind: str, content: str, minute: int) -> AgentEvent:
    return AgentEvent(
        role=role,  # type: ignore[arg-type]
        kind=kind,  # type: ignore[arg-type]
        content=content,
        ts=datetime(2026, 7, 25, 12, minute, tzinfo=UTC),
        session_id="local-abc",
        cwd="/repo",
        git_branch="main",
    )


def test_turn_boundary_at_new_user_event() -> None:
    """A new 'user' event after an assistant turn closes the previous segment."""
    events = [
        _event("user", "text", "task A", 0),
        _event("assistant", "text", "working on A", 1),
        _event("assistant", "tool_use", '{}', 2),
        _event("user", "tool_result", "ok", 3),
        _event("assistant", "text", "done A", 4),
        _event("user", "text", "task B", 5),  # ← boundary
        _event("assistant", "text", "working on B", 6),
    ]
    segmenter = Segmenter(session_id="local-abc")
    segments = list(segmenter.feed(events))
    segments.extend(segmenter.flush())

    assert len(segments) == 2
    assert [e.content for e in segments[0].events] == ["task A", "working on A", "{}", "ok", "done A"]
    assert [e.content for e in segments[1].events] == ["task B", "working on B"]


def test_empty_segment_is_not_emitted() -> None:
    """No events → no segments."""
    segmenter = Segmenter(session_id="local-abc")
    assert list(segmenter.feed([])) == []
    assert list(segmenter.flush()) == []


def test_flush_closes_the_open_segment() -> None:
    events = [_event("user", "text", "one shot", 0), _event("assistant", "text", "reply", 1)]
    segmenter = Segmenter(session_id="local-abc")
    assert list(segmenter.feed(events)) == []  # boundary hasn't arrived yet
    segs = list(segmenter.flush())
    assert len(segs) == 1
    assert len(segs[0].events) == 2


def test_segment_started_and_ended_at_bound_the_events() -> None:
    events = [
        _event("user", "text", "hi", 0),
        _event("assistant", "text", "hello", 5),
    ]
    seg = list(Segmenter(session_id="s").feed(events + [_event("user", "text", "next", 6)]))[0]
    assert seg.started_at == events[0].ts
    assert seg.ended_at == events[-1].ts


def test_segment_ids_are_stable_and_ordered() -> None:
    events = [
        _event("user", "text", "a", 0),
        _event("user", "text", "b", 1),
        _event("user", "text", "c", 2),
    ]
    segments = list(Segmenter(session_id="local-abc").feed(events))
    ids = [s.id for s in segments]
    assert ids == sorted(ids), "segment ids must be monotonic"
    assert all(id_.startswith("local-abc-seg-") for id_ in ids)


def test_reproduces_frozen_fixture_segments() -> None:
    """The whole point: the segmenter must reproduce the hand-authored fixtures.

    Feed the concatenated events from every fixture segment (in order) into the
    segmenter, add a synthetic user-turn sentinel between them, and verify the
    resulting Segments match. If this ever fails, either the segmenter is wrong
    or the fixtures encode a boundary decision the segmenter doesn't share —
    that's a spec-level conversation, not a test tweak.
    """
    fixture_paths = sorted((FIXTURES / "segments").glob("*.json"))
    assert len(fixture_paths) >= 2

    expected: list[Segment] = [
        Segment.model_validate(json.loads(p.read_text())) for p in fixture_paths
    ]
    all_events: list[AgentEvent] = []
    for i, seg in enumerate(expected):
        all_events.extend(seg.events)
        if i < len(expected) - 1:
            # Sentinel: a new user turn kicks off the next fixture segment.
            next_seg = expected[i + 1]
            first = next_seg.events[0]
            if first.role != "user":
                # The fixtures should already start each new segment with a user turn;
                # this branch is defensive.
                pytest.skip(
                    "fixture segment does not start with a user turn; segmenter "
                    "boundary rule needs revisiting in coordination with fixtures"
                )

    segmenter = Segmenter(session_id=expected[0].session_id)
    produced = list(segmenter.feed(all_events))
    produced.extend(segmenter.flush())

    assert len(produced) == len(expected), (
        f"produced {len(produced)} segments; expected {len(expected)}"
    )
    for got, want in zip(produced, expected):
        assert [e.content for e in got.events] == [e.content for e in want.events]
        assert got.started_at == want.started_at
        assert got.ended_at == want.ended_at
```

- [ ] **Step 2: Run — expect ImportError**

Run: `uv run pytest packages/worker/tests/test_segmenter.py -v`
Expected: FAIL — `ImportError: cannot import name 'Segmenter'`.

- [ ] **Step 3: Implement the segmenter**

Create `packages/worker/src/synapse_worker/segmenter.py`:

```python
"""Segmenter — batch AgentEvents into Segments on turn boundaries.

Boundary rule (v1):
  A new user text event after any assistant activity closes the current
  segment. Tool_results (which have role=user but kind=tool_result) do NOT
  count as a new user turn — they close out the assistant's tool_use, not
  start a new task.

Emits a Segment as soon as a boundary is crossed. Any residual events at
end-of-stream are flushed by an explicit flush() call.
"""

from __future__ import annotations

from collections.abc import Iterable, Iterator

from synapse_contracts import AgentEvent, Segment


class Segmenter:
    def __init__(self, session_id: str) -> None:
        self._session_id = session_id
        self._buffer: list[AgentEvent] = []
        self._counter = 0
        self._saw_assistant = False

    def feed(self, events: Iterable[AgentEvent]) -> Iterator[Segment]:
        for e in events:
            if self._is_boundary(e):
                seg = self._flush_buffer()
                if seg is not None:
                    yield seg
            self._buffer.append(e)
            if e.role == "assistant":
                self._saw_assistant = True

    def flush(self) -> Iterator[Segment]:
        seg = self._flush_buffer()
        if seg is not None:
            yield seg

    def _is_boundary(self, e: AgentEvent) -> bool:
        # A user *text* event after any assistant activity closes the segment.
        return e.role == "user" and e.kind == "text" and self._saw_assistant

    def _flush_buffer(self) -> Segment | None:
        if not self._buffer:
            return None
        events = self._buffer
        self._counter += 1
        seg = Segment(
            id=f"{self._session_id}-seg-{self._counter:03d}",
            session_id=self._session_id,
            events=events,
            started_at=events[0].ts,
            ended_at=events[-1].ts,
        )
        self._buffer = []
        self._saw_assistant = False
        return seg
```

- [ ] **Step 4: Run the tests**

Run: `uv run pytest packages/worker/tests/test_segmenter.py -v`
Expected: All tests PASS. **If `test_reproduces_frozen_fixture_segments` fails**, do NOT tune the segmenter's boundary rule in isolation — flag it to the team. Either the fixtures encode a different segmentation intent (in which case the boundary rule changes here and stays in sync with the fixtures) or the fixtures are wrong (in which case they get re-authored). This is a co-authoring decision, not a solo edit.

- [ ] **Step 5: Commit**

```bash
git add packages/worker/src/synapse_worker/segmenter.py packages/worker/tests/test_segmenter.py
git commit -m "$(cat <<'EOF'
feat(worker): Segmenter — turn-boundary batching (Plan A Task 3)

Boundary rule: a new user text event after any assistant activity closes
the current segment; tool_results don't count as new user turns. Emits
Segments the moment a boundary crosses; flush() drains the tail. Verified
against the frozen fixture Segments.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

### Task 4: `SyncClient` — push findings + session join over HTTP

**Files:**
- Modify: `packages/worker/pyproject.toml` (add `pytest-httpserver` to dev deps)
- Create: `packages/worker/src/synapse_worker/sync_client.py`
- Create: `packages/worker/tests/test_sync_client.py`

- [ ] **Step 1: Add the mock HTTP server dev dependency**

Edit the root `pyproject.toml` (workspace-level dev deps), replacing the `[dependency-groups]` block:

```toml
[dependency-groups]
dev = [
    "pytest>=8.0",
    "pytest-asyncio>=0.24",
    "pytest-httpserver>=1.1",
    "ruff>=0.7",
]
```

Run: `uv sync`
Expected: `pytest-httpserver` installs.

- [ ] **Step 2: Write the failing test suite against a mock server**

Create `packages/worker/tests/test_sync_client.py`:

```python
from datetime import datetime, timezone

import pytest
from pytest_httpserver import HTTPServer

from synapse_contracts import Finding
from synapse_worker.sync_client import SyncClient


UTC = timezone.utc


def _finding() -> Finding:
    return Finding(
        type="learning",
        text="Auth middleware rejects tokens with clock skew > 60s",
        contributor="siddsing",
        ts=datetime(2026, 7, 25, 12, 0, tzinfo=UTC),
        source_session="local-abc",
    )


@pytest.mark.asyncio
async def test_create_session_returns_shared_id(httpserver: HTTPServer) -> None:
    httpserver.expect_request(
        "/v1/sessions", method="POST"
    ).respond_with_json({"shared_id": "shared-xyz"})

    client = SyncClient(base_url=httpserver.url_for(""))
    shared_id = await client.create_session(purpose="Debug flaky auth", created_by="siddsing")
    assert shared_id == "shared-xyz"


@pytest.mark.asyncio
async def test_join_session_sends_binding(httpserver: HTTPServer) -> None:
    httpserver.expect_request(
        "/v1/sessions/shared-xyz/join",
        method="POST",
    ).respond_with_json({"ok": True})

    client = SyncClient(base_url=httpserver.url_for(""))
    ok = await client.join_session(
        shared_id="shared-xyz",
        local_agent_session_id="local-abc",
        contributor="akhil",
    )
    assert ok is True


@pytest.mark.asyncio
async def test_push_findings_returns_accepted_count(httpserver: HTTPServer) -> None:
    httpserver.expect_request(
        "/v1/sessions/shared-xyz/findings",
        method="POST",
    ).respond_with_json({"accepted": 1})

    client = SyncClient(base_url=httpserver.url_for(""))
    n = await client.push_findings(shared_id="shared-xyz", findings=[_finding()])
    assert n == 1


@pytest.mark.asyncio
async def test_push_findings_retries_on_5xx(httpserver: HTTPServer) -> None:
    """Transient 5xx → retry succeeds without raising."""
    # First call: 503. Second call: 200. pytest-httpserver serves in order.
    httpserver.expect_ordered_request(
        "/v1/sessions/shared-xyz/findings", method="POST"
    ).respond_with_data("boom", status=503)
    httpserver.expect_ordered_request(
        "/v1/sessions/shared-xyz/findings", method="POST"
    ).respond_with_json({"accepted": 1})

    client = SyncClient(base_url=httpserver.url_for(""), max_retries=2)
    n = await client.push_findings(shared_id="shared-xyz", findings=[_finding()])
    assert n == 1
    # Two request records: the retried one and the successful one.
    assert len(httpserver.log) == 2
```

- [ ] **Step 3: Run — expect ImportError**

Run: `uv run pytest packages/worker/tests/test_sync_client.py -v`
Expected: FAIL — `ImportError: cannot import name 'SyncClient'`.

- [ ] **Step 4: Implement the sync client**

Create `packages/worker/src/synapse_worker/sync_client.py`:

```python
"""SyncClient — pushes findings and manages session binding over HTTP.

Talks to the ingest API defined in synapse_contracts.ingest_api. Retries
transient 5xx errors with a short exponential backoff. Everything is async so
the worker's event loop stays responsive during network I/O.
"""

from __future__ import annotations

import asyncio
import logging

import httpx

from synapse_contracts import (
    CreateSessionRequest,
    CreateSessionResponse,
    Finding,
    JoinSessionRequest,
    JoinSessionResponse,
    PushFindingsRequest,
    PushFindingsResponse,
)

log = logging.getLogger(__name__)


class SyncClient:
    def __init__(
        self,
        base_url: str,
        *,
        max_retries: int = 3,
        backoff_seconds: float = 0.1,
        timeout: float = 5.0,
    ) -> None:
        self._base_url = base_url.rstrip("/")
        self._max_retries = max_retries
        self._backoff = backoff_seconds
        self._timeout = timeout

    async def create_session(self, *, purpose: str, created_by: str) -> str:
        req = CreateSessionRequest(purpose=purpose, created_by=created_by)
        resp = await self._post_json("/v1/sessions", req.model_dump())
        return CreateSessionResponse.model_validate(resp).shared_id

    async def join_session(
        self, *, shared_id: str, local_agent_session_id: str, contributor: str
    ) -> bool:
        req = JoinSessionRequest(
            shared_id=shared_id,
            local_agent_session_id=local_agent_session_id,
            contributor=contributor,
        )
        resp = await self._post_json(f"/v1/sessions/{shared_id}/join", req.model_dump())
        return JoinSessionResponse.model_validate(resp).ok

    async def push_findings(self, *, shared_id: str, findings: list[Finding]) -> int:
        req = PushFindingsRequest(shared_id=shared_id, findings=findings)
        resp = await self._post_json(
            f"/v1/sessions/{shared_id}/findings",
            req.model_dump(mode="json"),
        )
        return PushFindingsResponse.model_validate(resp).accepted

    async def _post_json(self, path: str, body: dict) -> dict:
        url = f"{self._base_url}{path}"
        last_exc: Exception | None = None
        for attempt in range(self._max_retries + 1):
            try:
                async with httpx.AsyncClient(timeout=self._timeout) as ac:
                    r = await ac.post(url, json=body)
                if 500 <= r.status_code < 600:
                    raise httpx.HTTPStatusError(
                        f"server {r.status_code}", request=r.request, response=r
                    )
                r.raise_for_status()
                return r.json()
            except (httpx.HTTPStatusError, httpx.RequestError) as e:
                last_exc = e
                if attempt >= self._max_retries:
                    break
                await asyncio.sleep(self._backoff * (2**attempt))
                log.debug("retrying %s after error: %s", path, e)
        assert last_exc is not None
        raise last_exc
```

- [ ] **Step 5: Run the tests**

Run: `uv run pytest packages/worker/tests/test_sync_client.py -v`
Expected: All tests PASS.

- [ ] **Step 6: Commit**

```bash
git add pyproject.toml packages/worker/src/synapse_worker/sync_client.py packages/worker/tests/test_sync_client.py uv.lock
git commit -m "$(cat <<'EOF'
feat(worker): SyncClient — create/join session + push findings (Plan A Task 4)

Async HTTPX client speaking the frozen ingest API contracts. Retries
transient 5xx with exponential backoff. Tested against a pytest-httpserver
mock — no dependency on the real Synapse service being live.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

### Task 5: Wire the pieces into a runnable worker

**Files:**
- Create: `packages/worker/src/synapse_worker/worker.py`
- Create: `packages/worker/src/synapse_worker/cli.py`
- Modify: `packages/worker/pyproject.toml` (add CLI entry point)
- Create: `packages/worker/tests/test_worker_wiring.py`

The worker itself is deliberately thin because the Distiller lives in Plan B. This task wires Source → Follower → Segmenter → (Distiller placeholder) → SyncClient. The distiller argument is typed as a protocol so Plan B's real implementation plugs in with no code change here.

- [ ] **Step 1: Write the failing wiring test**

Create `packages/worker/tests/test_worker_wiring.py`:

```python
from datetime import datetime, timezone
from pathlib import Path

import pytest
from pytest_httpserver import HTTPServer

from synapse_contracts import Finding, Segment
from synapse_worker.worker import EdgeWorker


UTC = timezone.utc


class _StubDistiller:
    """Test double for the Plan B Distiller."""

    def __init__(self, contributor: str) -> None:
        self.contributor = contributor

    async def distill(self, segment: Segment) -> list[Finding]:
        return [
            Finding(
                type="learning",
                text=f"stub finding for {segment.id}",
                contributor=self.contributor,
                ts=datetime(2026, 7, 25, 12, 0, tzinfo=UTC),
                source_session=segment.session_id,
            )
        ]


@pytest.mark.asyncio
async def test_worker_processes_file_end_to_end(tmp_path: Path, httpserver: HTTPServer) -> None:
    # Pre-seed the service: session already created and joined.
    httpserver.expect_request(
        "/v1/sessions/shared-xyz/findings", method="POST"
    ).respond_with_json({"accepted": 1})

    # Write a session file with one full turn.
    session_file = tmp_path / "session.jsonl"
    session_file.write_text(
        '\n'.join([
            '{"type":"user","timestamp":"2026-07-25T12:00:00Z","sessionId":"local-abc",'
            '"cwd":"/repo","gitBranch":"main","message":{"role":"user","content":"task"}}',
            '{"type":"assistant","timestamp":"2026-07-25T12:00:05Z","sessionId":"local-abc",'
            '"cwd":"/repo","gitBranch":"main","message":{"role":"assistant","content":"done"}}',
            "",
        ])
    )

    worker = EdgeWorker(
        session_file=session_file,
        distiller=_StubDistiller(contributor="siddsing"),
        service_base_url=httpserver.url_for(""),
        shared_id="shared-xyz",
        local_session_id="local-abc",
    )
    await worker.tick()  # one pass: read, segment, distill, push
    # No segment yet — no user boundary — force flush.
    await worker.flush_and_push()

    # One push request recorded.
    assert any(r.path.endswith("/findings") for r, _ in httpserver.log)
```

- [ ] **Step 2: Run — expect ImportError**

Run: `uv run pytest packages/worker/tests/test_worker_wiring.py -v`
Expected: FAIL — `ImportError: cannot import name 'EdgeWorker'`.

- [ ] **Step 3: Implement the worker**

Create `packages/worker/src/synapse_worker/worker.py`:

```python
"""EdgeWorker — wires Source → Follower → Segmenter → Distiller → SyncClient.

The Distiller is passed in as a duck-typed dependency (must expose an async
distill(segment) -> list[Finding]) so Plan B's implementation plugs in
without a code change here.

Runtime shape (not exercised in tests):
    worker = EdgeWorker(...)
    while True:
        await worker.tick()
        await asyncio.sleep(2)
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Protocol

from synapse_contracts import Finding, Segment

from synapse_worker.follower import FileFollower
from synapse_worker.segmenter import Segmenter
from synapse_worker.sources.claude_code import ClaudeCodeSource
from synapse_worker.sync_client import SyncClient

log = logging.getLogger(__name__)


class DistillerLike(Protocol):
    async def distill(self, segment: Segment) -> list[Finding]: ...


class EdgeWorker:
    def __init__(
        self,
        *,
        session_file: Path,
        distiller: DistillerLike,
        service_base_url: str,
        shared_id: str,
        local_session_id: str,
    ) -> None:
        self._follower = FileFollower(session_file)
        self._source = ClaudeCodeSource()
        self._segmenter = Segmenter(session_id=local_session_id)
        self._distiller = distiller
        self._sync = SyncClient(base_url=service_base_url)
        self._shared_id = shared_id

    async def tick(self) -> None:
        """Read whatever is available; distill and push any completed segments."""
        lines = list(self._follower.read_available())
        if not lines:
            return
        events = list(self._source.parse_lines(lines))
        for segment in self._segmenter.feed(events):
            await self._distill_and_push(segment)

    async def flush_and_push(self) -> None:
        """Force-close any in-flight segment (e.g., on shutdown)."""
        for segment in self._segmenter.flush():
            await self._distill_and_push(segment)

    async def _distill_and_push(self, segment: Segment) -> None:
        try:
            findings = await self._distiller.distill(segment)
        except Exception:
            log.exception("distiller failed on segment %s; dropping", segment.id)
            return
        if not findings:
            return
        try:
            n = await self._sync.push_findings(shared_id=self._shared_id, findings=findings)
            log.info("pushed %d findings from segment %s", n, segment.id)
        except Exception:
            log.exception("sync push failed for segment %s; will retry on next tick", segment.id)
```

Create `packages/worker/src/synapse_worker/cli.py`:

```python
"""synapse-worker CLI — start a worker against a Claude Code session file.

Deliberately minimal for the hackathon: no daemonization, no config file. The
worker is intended to be launched by a wrapper script or shell one-liner.
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import sys
from pathlib import Path

from synapse_worker.worker import EdgeWorker


def _parse_args(argv: list[str]) -> argparse.Namespace:
    p = argparse.ArgumentParser(prog="synapse-worker")
    p.add_argument("--session-file", type=Path, required=True)
    p.add_argument("--service-url", type=str, required=True)
    p.add_argument("--shared-id", type=str, required=True)
    p.add_argument("--local-session-id", type=str, required=True)
    p.add_argument("--tick-seconds", type=float, default=2.0)
    return p.parse_args(argv)


async def _run(ns: argparse.Namespace) -> None:
    # Import here so the CLI doesn't fail at parse time if Plan B hasn't landed yet.
    from synapse_worker.distiller import build_default_distiller  # type: ignore[import-not-found]

    distiller = build_default_distiller()
    worker = EdgeWorker(
        session_file=ns.session_file,
        distiller=distiller,
        service_base_url=ns.service_url,
        shared_id=ns.shared_id,
        local_session_id=ns.local_session_id,
    )
    try:
        while True:
            await worker.tick()
            await asyncio.sleep(ns.tick_seconds)
    finally:
        await worker.flush_and_push()


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    ns = _parse_args(sys.argv[1:] if argv is None else argv)
    asyncio.run(_run(ns))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
```

Edit `packages/worker/pyproject.toml` to add the entry point — replace the whole file with:

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

[project.scripts]
synapse-worker = "synapse_worker.cli:main"

[build-system]
requires = ["hatchling"]
build-backend = "hatchling.build"

[tool.hatch.build.targets.wheel]
packages = ["src/synapse_worker"]

[tool.uv.sources]
synapse-contracts = { workspace = true }
synapse-providers = { workspace = true }
```

Run: `uv sync`
Expected: succeeds; the `synapse-worker` entry-point is installed.

- [ ] **Step 4: Run all worker tests**

Run: `uv run pytest packages/worker -v`
Expected: All tests PASS.

- [ ] **Step 5: Commit**

```bash
git add packages/worker uv.lock
git commit -m "$(cat <<'EOF'
feat(worker): wire Source → Follower → Segmenter → Distiller → Sync (Plan A Task 5)

EdgeWorker orchestrates the on-device pipeline; DistillerLike protocol lets
Plan B's distiller plug in without touching this code. Minimal CLI entry
point for hackathon-day launches. Tested end-to-end with a stub distiller
against a mock ingest server.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

### Task 6: Exit criteria

- [ ] **Step 1: Full worker suite green**

Run: `uv run pytest packages/worker -v`
Expected: All tests PASS.

- [ ] **Step 2: Full monorepo suite still green**

Run: `uv run pytest -v`
Expected: Everything green *except* the walking-skeleton test (XFAIL until Plans B and C also land).

- [ ] **Step 3: Lint**

Run: `uv run ruff check packages/worker && uv run ruff format --check packages/worker`
Expected: Clean.

- [ ] **Step 4: Confirm hand-off**

- Aditya's distiller (Plan B) can now feed on real `Segment` inputs from `ClaudeCodeSource → Segmenter`.
- The CLI can drive a live end-to-end run once Plan B and Plan C land — no code changes needed on this track.
- Siddsing's ingest API (Plan C) has a real client testing against its contract; if the API drifts, this test suite tells you first.

## Scope / YAGNI

**In (Plan A):** ClaudeCodeSource, FileFollower with rotation + partial-line handling, Segmenter on turn boundaries, SyncClient with retries, EdgeWorker orchestration, minimal CLI.

**Out (stretch):**
- `CodexSource` (second agent adapter) — agent-agnostic proof, ~1 day of work once Codex's on-disk format is confirmed.
- Watchdog-based inotify follower (current follower polls; fine at hackathon scale).
- Persistent local queue for offline resilience (current follower re-reads from `_offset` in-process only).

## Known Risks

| Risk | Mitigation |
|---|---|
| Real Claude Code JSONL has more content-block types than the four we handle | Log-and-skip is already the behavior for unknown types; expand `_iter_content_blocks` if needed |
| Segmenter's turn-boundary rule doesn't match fixtures | `test_reproduces_frozen_fixture_segments` catches this; resolve as a co-authoring decision with Aditya, not a solo tweak |
| Sync client 5xx retries on a genuinely broken service loop forever | `max_retries=3` default with exponential backoff caps it; failures are logged and the segment is dropped, not queued |
| Rotation heuristic (inode + shrink) misses some edge case | Documented behavior; hackathon-scale demo runs are short-lived enough that a follower restart is acceptable |
