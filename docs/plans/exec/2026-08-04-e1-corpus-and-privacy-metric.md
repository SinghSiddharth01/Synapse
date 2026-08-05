# E1 — Corpus Completion + Identifier-Leak Metric Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Grow the fixture corpus from 2 to 8 and replace the blind 8-gram privacy metric with an identifier-leak detector, so every quality/privacy claim stops resting on n=2 and a number that cannot see real leaks.

**Architecture:** Fixtures are data files under `fixtures/` loaded by `synapse_distiller.fixtures` (`load_segment` / `load_goldens` / `available_fixtures`). The leak detector is a pure function added to `synapse_distiller.evaluation` and wired into `FixtureScore` → `score_fixture` → `scripts/run_npu_eval.py`. No new packages, no model calls in any test.

**Tech Stack:** Python 3.12, pytest, Pydantic v2. Runs on any machine (Mac or the ARM64 Windows box).

## Global Constraints

- On the Windows/NPU box, every `uv` command needs the interpreter pin: `uv sync --python "$env:LOCALAPPDATA\Programs\Python\Python312-arm64\python.exe"`. On Mac, plain `uv sync`.
- All tests run offline against committed data. No `geniex serve`, no network.
- Fixture ids and file layout are fixed by the loader: `fixtures/segments/<id>.json` (a `Segment`), `fixtures/findings/<id>.findings.json` (a `Finding[]`).
- **Golden co-authoring gate:** golden `text` values written solo are PROVISIONAL. Committing them is fine (tests check shape, not prose), but `fixtures/README.md` must list them as unsigned until all three teammates review. This is the lesson of the `v1-baseline` contamination — one person wrote both the prompt and the target.
- Never reuse wording from any prompt pack in `config/prompts/*.toml` inside a fixture. Before committing a fixture, grep the packs for its distinctive phrases (Task 6 automates this).
- `CONTEXT.md` vocabulary applies: Agent Session, Contributor, Attribution. Fixture attributions use `agent_session` ids of the form `as-fixture-<nnn>`.

---

### Task 1: seg-002 — insight with no error

The hardest triage case: a decision reached conversationally, with no tool call, no error, nothing for a keyword filter to catch. Triage must KEEP it (E2 pins that); the distiller must find the decision.

**Files:**
- Create: `fixtures/segments/seg-002.json`
- Create: `fixtures/findings/seg-002.findings.json`
- Create: `packages/distiller/tests/test_fixture_corpus.py`

**Interfaces:**
- Produces: fixture ids `seg-002` consumable via `load_segment("seg-002")` / `load_goldens("seg-002")`. E2's triage tests and the eval harness consume these by id.

- [x] **Step 1: Write the failing corpus test**

```python
# packages/distiller/tests/test_fixture_corpus.py
"""Shape tests for every committed fixture. Prose quality is co-review's job."""
import pytest

from synapse_contracts import Finding, Segment
from synapse_distiller.fixtures import available_fixtures, load_goldens, load_segment

EXPECTED_IDS = ["seg-001", "seg-002", "seg-003", "seg-004",
                "seg-005a", "seg-005b", "seg-006", "seg-007"]


def test_corpus_is_complete():
    assert available_fixtures() == EXPECTED_IDS


@pytest.mark.parametrize("fixture_id", EXPECTED_IDS)
def test_fixture_parses(fixture_id):
    segment = load_segment(fixture_id)
    assert isinstance(segment, Segment)
    assert segment.events, f"{fixture_id} has no events"
    goldens = load_goldens(fixture_id)
    for f in goldens:
        assert isinstance(f, Finding)
        assert f.attributions, f"{fixture_id} golden {f.id} has no attributions"
        for a in f.attributions:
            assert a.agent_session.startswith("as-fixture-")


def test_seg002_is_conversational():
    segment = load_segment("seg-002")
    assert all(e.kind == "text" for e in segment.events), \
        "seg-002 must contain no tool events — that is its entire point"
    types = {f.type.value for f in load_goldens("seg-002")}
    assert "decision" in types
```

- [x] **Step 2: Run to verify it fails**

Run: `uv run pytest packages/distiller/tests/test_fixture_corpus.py -v`
Expected: FAIL — `test_corpus_is_complete` (only seg-001/seg-004 exist), and the parametrized cases for missing ids fail with `FileNotFoundError`.

- [x] **Step 3: Write the fixture**

```json
// fixtures/segments/seg-002.json
{
  "id": "seg-002",
  "agent_session_id": "as-fixture-002",
  "events": [
    {"role": "user", "kind": "text",
     "content": "Should we cache the JWTs in redis so we stop hitting the auth service on every request?",
     "tool_name": null, "ts": "2026-08-04T09:00:00Z",
     "agent_session_id": "as-fixture-002", "cwd": "/repo", "git_branch": "main"},
    {"role": "assistant", "kind": "text",
     "content": "Caching would work, but these tokens expire after ninety seconds, so a cache would be stale almost immediately and we'd need invalidation logic anyway. A simpler approach: keep calling the auth service, and only refresh a token when a request actually comes back 401. The auth service call is cheap; the complexity of a cache is not. Let's go with refresh-on-401 instead of caching.",
     "tool_name": null, "ts": "2026-08-04T09:00:20Z",
     "agent_session_id": "as-fixture-002", "cwd": "/repo", "git_branch": "main"},
    {"role": "user", "kind": "text",
     "content": "ok do that",
     "tool_name": null, "ts": "2026-08-04T09:00:35Z",
     "agent_session_id": "as-fixture-002", "cwd": "/repo", "git_branch": "main"}
  ],
  "started_at": "2026-08-04T09:00:00Z",
  "ended_at": "2026-08-04T09:00:35Z"
}
```

```json
// fixtures/findings/seg-002.findings.json
[
  {
    "id": "f-002-01", "type": "decision",
    "text": "Chose refresh-on-401 over caching auth tokens: the tokens expire so quickly that a cache would be stale almost immediately and would add invalidation complexity for no benefit.",
    "attributions": [{"contributor": "aditya", "agent_session": "as-fixture-002", "agent": "claude-code"}],
    "ts": "2026-08-04T09:00:20Z", "refs": [],
    "provenance": "distilled", "status": "kept",
    "merged_from": [], "merged_into": null
  },
  {
    "id": "f-002-02", "type": "learning",
    "text": "The auth tokens have a very short lifetime, which makes any caching strategy for them largely pointless.",
    "attributions": [{"contributor": "aditya", "agent_session": "as-fixture-002", "agent": "claude-code"}],
    "ts": "2026-08-04T09:00:20Z", "refs": [],
    "provenance": "distilled", "status": "kept",
    "merged_from": [], "merged_into": null
  }
]
```

- [x] **Step 4: Run the seg-002-specific tests**

Run: `uv run pytest packages/distiller/tests/test_fixture_corpus.py::test_seg002_is_conversational packages/distiller/tests/test_fixture_corpus.py::test_fixture_parses -v -k "seg-002 or conversational"`
Expected: PASS for seg-002 cases. `test_corpus_is_complete` still fails — that is the tracking test for the whole plan and goes green at Task 4.

- [x] **Step 5: Commit**

```bash
git add fixtures/segments/seg-002.json fixtures/findings/seg-002.findings.json packages/distiller/tests/test_fixture_corpus.py
git commit -m "test(fixtures): seg-002 — conversational decision, no error signal (Plan 0.3)"
```

---

### Task 2: seg-003 — oversized tool_result with a buried error

Plan 0.3's compaction fixture. Compaction (A.5) is unbuilt, so today this pins two things: budget-splitting doesn't lose the error line, and `dead_end` recall when the signal lives in a `tool_result`. Its compaction assertion activates when A.5 lands.

**Files:**
- Create: `fixtures/segments/seg-003.json`
- Create: `fixtures/findings/seg-003.findings.json`
- Modify: `packages/distiller/tests/test_fixture_corpus.py` (append)

**Interfaces:**
- Produces: `seg-003`, whose middle `tool_result` event exceeds 4000 chars with the error at roughly the midpoint.

- [x] **Step 1: Append the failing test**

```python
def test_seg003_error_is_buried_in_an_oversized_tool_result():
    segment = load_segment("seg-003")
    big = [e for e in segment.events if e.kind == "tool_result" and len(e.content) > 4000]
    assert big, "seg-003 must contain a tool_result over 4000 chars"
    assert "ConnectionResetError" in big[0].content
    pos = big[0].content.index("ConnectionResetError") / len(big[0].content)
    assert 0.3 < pos < 0.7, "the error must be buried mid-log, not at head or tail"
    types = {f.type.value for f in load_goldens("seg-003")}
    assert "dead_end" in types
```

- [x] **Step 2: Run to verify it fails**

Run: `uv run pytest packages/distiller/tests/test_fixture_corpus.py::test_seg003_error_is_buried_in_an_oversized_tool_result -v`
Expected: FAIL with `FileNotFoundError`.

- [x] **Step 3: Write the fixture**

Build the big log programmatically once, paste the result. Generator (run ad hoc, do not commit):

```python
# scratch — produces the content string for the big tool_result
noise = [f"[worker-{i%4}] request {1000+i} handled in {8+(i%37)}ms status=200" for i in range(60)]
error = ["[worker-2] request 1042 FAILED", "ConnectionResetError: [Errno 104] Connection reset by peer",
         "  during streaming response flush; client had already disconnected"]
tail = [f"[worker-{i%4}] request {1100+i} handled in {9+(i%29)}ms status=200" for i in range(55)]
print(len("\n".join(noise + error + tail)))  # must be > 4000
```

`fixtures/segments/seg-003.json` — events, in order:
1. `user/text`: "load test the streaming endpoint and see why clients drop" (`ts` 2026-08-04T10:00:00Z)
2. `assistant/text`: "Running the load test against the streaming endpoint now." (10:00:10Z)
3. `assistant/tool_use`, `tool_name: "Bash"`: "python scripts/load_test.py --streaming --clients 50" (10:00:12Z)
4. `user/tool_result`, `tool_name: "Bash"`: the generated log string above (10:02:00Z)
5. `assistant/text`: "The load test mostly passes but request 1042 died with a connection reset during the response flush — the client disconnected mid-stream and we don't handle that. Retrying with more clients reproduces it every time. Increasing the flush buffer didn't help; this is not a buffer size problem, it's a missing disconnect handler. Abandoning the buffer-tuning approach." (10:03:00Z)

All events: `agent_session_id: "as-fixture-003"`, `cwd: "/repo"`, `git_branch: "main"`. Segment `id: "seg-003"`, `started_at`/`ended_at` from first/last ts.

`fixtures/findings/seg-003.findings.json`:

```json
[
  {
    "id": "f-003-01", "type": "dead_end",
    "text": "Tuning the flush buffer size does not fix the streaming disconnects — the failure is a missing handler for clients that disconnect mid-stream, not a buffer problem.",
    "attributions": [{"contributor": "aditya", "agent_session": "as-fixture-003", "agent": "claude-code"}],
    "ts": "2026-08-04T10:03:00Z", "refs": [],
    "provenance": "distilled", "status": "kept", "merged_from": [], "merged_into": null
  },
  {
    "id": "f-003-02", "type": "learning",
    "text": "Under load, streaming responses fail with a connection reset when the client disconnects during the response flush, and this reproduces reliably.",
    "attributions": [{"contributor": "aditya", "agent_session": "as-fixture-003", "agent": "claude-code"}],
    "ts": "2026-08-04T10:03:00Z", "refs": [],
    "provenance": "distilled", "status": "kept", "merged_from": [], "merged_into": null
  }
]
```

- [x] **Step 4: Run to verify it passes**

Run: `uv run pytest packages/distiller/tests/test_fixture_corpus.py -v -k seg003`
Expected: PASS.

- [x] **Step 5: Commit**

```bash
git add fixtures/segments/seg-003.json fixtures/findings/seg-003.findings.json packages/distiller/tests/test_fixture_corpus.py
git commit -m "test(fixtures): seg-003 — oversized tool_result, buried error (Plan 0.3)"
```

---

### Task 3: seg-005a / seg-005b — the semantic-merge pair

Two Contributors reach overlapping halves of one insight. E3's synthesis test consumes `load_goldens("seg-005a") + load_goldens("seg-005b")` directly — these goldens ARE the merge test's input, which is why the ids and `agent_session` values below are load-bearing.

**Files:**
- Create: `fixtures/segments/seg-005a.json`, `fixtures/segments/seg-005b.json`
- Create: `fixtures/findings/seg-005a.findings.json`, `fixtures/findings/seg-005b.findings.json`
- Modify: `packages/distiller/tests/test_fixture_corpus.py` (append)

**Interfaces:**
- Produces: golden ids `f-005a-01` (contributor `aditya`) and `f-005b-01` (contributor `akhil`) — near-duplicate texts. E3 Task 3 references these exact ids.

- [x] **Step 1: Append the failing test**

```python
def test_seg005_pair_is_a_merge_candidate():
    a = load_goldens("seg-005a")
    b = load_goldens("seg-005b")
    assert [f.id for f in a] == ["f-005a-01"]
    assert [f.id for f in b] == ["f-005b-01"]
    assert a[0].attributions[0].contributor == "aditya"
    assert b[0].attributions[0].contributor == "akhil"
    assert a[0].attributions[0].contributor != b[0].attributions[0].contributor
    # Same fact, different halves: both mention the 40ms window, only b has load.
    assert "40" in a[0].text and "40" in b[0].text
    assert "load" in b[0].text.lower() and "load" not in a[0].text.lower()
```

- [x] **Step 2: Run to verify it fails**

Run: `uv run pytest packages/distiller/tests/test_fixture_corpus.py::test_seg005_pair_is_a_merge_candidate -v`
Expected: FAIL with `FileNotFoundError`.

- [x] **Step 3: Write the four files**

`seg-005a.json` (`as-fixture-005a`, contributor aditya's session): two events —
1. `user/text`: "narrow down when the fec decode failure happens" (11:00:00Z)
2. `assistant/text`: "Bisecting the timing shows the decode failure only occurs when the gap between the two DMA writes exceeds roughly forty milliseconds. Below that threshold it never reproduces. So this is a timing window of about 40 ms." (11:04:00Z)

`seg-005b.json` (`as-fixture-005b`): two events —
1. `user/text`: "can you reproduce that decode failure on the bench?" (11:10:00Z)
2. `assistant/text`: "Reproduced — but only under load. With the system otherwise idle the delay stays short and decode succeeds; once background traffic pushes the delay past about 40 ms the failure appears. It fails when the delay exceeds ~40 ms under load." (11:15:00Z)

```json
// fixtures/findings/seg-005a.findings.json
[
  {
    "id": "f-005a-01", "type": "learning",
    "text": "The decode failure is a timing window: it occurs only when the gap between the two DMA writes exceeds roughly 40 ms.",
    "attributions": [{"contributor": "aditya", "agent_session": "as-fixture-005a", "agent": "claude-code"}],
    "ts": "2026-08-04T11:04:00Z", "refs": [],
    "provenance": "distilled", "status": "kept", "merged_from": [], "merged_into": null
  }
]
```

```json
// fixtures/findings/seg-005b.findings.json
[
  {
    "id": "f-005b-01", "type": "learning",
    "text": "The decode failure reproduces only under load, when background traffic pushes the delay past about 40 ms.",
    "attributions": [{"contributor": "akhil", "agent_session": "as-fixture-005b", "agent": "codex"}],
    "ts": "2026-08-04T11:15:00Z", "refs": [],
    "provenance": "distilled", "status": "kept", "merged_from": [], "merged_into": null
  }
]
```

(Note `agent: "codex"` on the second — free realism for the cross-agent story; nothing downstream keys on it yet.)

- [x] **Step 4: Run to verify it passes**

Run: `uv run pytest packages/distiller/tests/test_fixture_corpus.py -v -k seg005`
Expected: PASS.

- [x] **Step 5: Commit**

```bash
git add fixtures/segments/seg-005a.json fixtures/segments/seg-005b.json fixtures/findings/seg-005a.findings.json fixtures/findings/seg-005b.findings.json packages/distiller/tests/test_fixture_corpus.py
git commit -m "test(fixtures): seg-005a/b — near-duplicate pair across Contributors (ADR 0002 input)"
```

---

### Task 4: seg-006 and seg-007 — the adversarial triage pair, plus the triage expectation map

seg-006: an error that is NOT insight (typo, fixed in seconds). seg-007: noise that LOOKS like signal (grep output full of the word "error", nothing failed). Their goldens are empty arrays. The expectation map records what triage *should* do with every fixture — E2 consumes it.

**Files:**
- Create: `fixtures/segments/seg-006.json`, `fixtures/segments/seg-007.json`
- Create: `fixtures/findings/seg-006.findings.json`, `fixtures/findings/seg-007.findings.json` (both `[]`)
- Create: `fixtures/triage.json`
- Modify: `packages/distiller/tests/test_fixture_corpus.py` (append)

**Interfaces:**
- Produces: `fixtures/triage.json` — `{"<fixture_id>": {"expected": "keep"|"skip", "note": str}}`. E2 Task 4 parametrizes over it verbatim.

- [x] **Step 1: Append the failing tests**

```python
import json
from synapse_distiller.fixtures import fixtures_root


def test_adversarial_fixtures_have_empty_goldens():
    assert load_goldens("seg-006") == []
    assert load_goldens("seg-007") == []


def test_triage_expectation_map_covers_the_whole_corpus():
    raw = json.loads((fixtures_root() / "triage.json").read_text(encoding="utf-8"))
    assert set(raw) == set(EXPECTED_IDS)
    assert all(v["expected"] in ("keep", "skip") for v in raw.values())
    # The two load-bearing entries: all-noise is skipped, quiet insight is kept.
    assert raw["seg-004"]["expected"] == "skip"
    assert raw["seg-002"]["expected"] == "keep"
```

- [x] **Step 2: Run to verify they fail**

Run: `uv run pytest packages/distiller/tests/test_fixture_corpus.py -v -k "adversarial or expectation"`
Expected: FAIL with `FileNotFoundError`.

- [x] **Step 3: Write the fixtures and the map**

`seg-006.json` (`as-fixture-006`): four events —
1. `user/text`: "run the unit tests" (12:00:00Z)
2. `assistant/tool_use` `Bash`: "uv run pytest -q" (12:00:05Z)
3. `user/tool_result` `Bash`: "E       NameError: name 'confg' is not defined\ntests/test_settings.py:14: NameError\n1 failed, 41 passed" (12:00:30Z)
4. `assistant/text`: "Just a typo — `confg` for `config` on line 14. Fixed; all 42 tests pass now." (12:00:55Z)

`seg-007.json` (`as-fixture-007`): three events —
1. `user/text`: "how many places do we log errors?" (12:10:00Z)
2. `assistant/tool_use` `Grep`: "pattern: logger.error" (12:10:03Z)
3. `user/tool_result` `Grep`: "api/routes.py:88: logger.error(\"upstream error: %s\", exc)\nworker/loop.py:141: logger.error(\"fatal error in tick\")\nstore/db.py:52: logger.error(\"connection error, retrying\")\n3 matches" (12:10:05Z)

Then `assistant/text`: "Three call sites log errors: the API routes, the worker tick, and the db retry path." (12:10:20Z)

```json
// fixtures/triage.json
{
  "seg-001":  {"expected": "keep", "note": "real work: decisions, a dead end, tool errors"},
  "seg-002":  {"expected": "keep", "note": "insight with no error — the case keyword filters miss; default-keep must catch it"},
  "seg-003":  {"expected": "keep", "note": "buried real error"},
  "seg-004":  {"expected": "skip", "note": "lint run, everything fixed, nothing remaining — the canonical skip"},
  "seg-005a": {"expected": "keep", "note": "decision language, real learning"},
  "seg-005b": {"expected": "keep", "note": "real learning under load"},
  "seg-006":  {"expected": "keep", "note": "ACCEPTED FALSE POSITIVE: a typo-error segment trips the error rule; recall-tuning means we pay NPU time here rather than risk skipping real errors"},
  "seg-007":  {"expected": "keep", "note": "ACCEPTED FALSE POSITIVE: grep output containing the word 'error'; a keyword triage cannot tell mentions from failures, and skipping on this rule would also skip real failures"}
}
```

The two ACCEPTED FALSE POSITIVE entries are the honest part: recall-tuned triage deliberately keeps them, synthesis's trivia filter is the layer that catches what they produce. If a later, smarter triage can flip them to `skip`, this file is where that improvement becomes measurable.

- [x] **Step 4: Run the whole corpus suite**

Run: `uv run pytest packages/distiller/tests/test_fixture_corpus.py -v`
Expected: ALL PASS — including `test_corpus_is_complete`, now that all 8 ids exist.

- [x] **Step 5: Commit**

```bash
git add fixtures/segments/seg-006.json fixtures/segments/seg-007.json fixtures/findings/seg-006.findings.json fixtures/findings/seg-007.findings.json fixtures/triage.json packages/distiller/tests/test_fixture_corpus.py
git commit -m "test(fixtures): adversarial triage pair + expectation map — corpus complete at 8"
```

---

### Task 5: identifier-leak detector

The 8-gram metric scored **0.00** on a finding containing `default_pool_size=25`. This detector sees what it cannot: identifier-shaped tokens that appear in both the finding and the segment.

**Files:**
- Modify: `packages/distiller/src/synapse_distiller/evaluation.py`
- Create: `packages/distiller/tests/test_identifier_leaks.py`

**Interfaces:**
- Produces: `identifier_leaks(finding_text: str, segment: Segment, allowlist: frozenset[str] = DEFAULT_ALLOWLIST) -> list[str]`; `FixtureScore.leaked_identifiers: list[str]` populated by `score_fixture`. E1 Task 6 and any future harness read `leaked_identifiers`.

- [x] **Step 1: Write the failing tests**

```python
# packages/distiller/tests/test_identifier_leaks.py
"""The red-team test the old metric failed: single-token leaks."""
from datetime import datetime, timezone

from synapse_contracts import AgentEvent, Segment
from synapse_distiller.evaluation import DEFAULT_ALLOWLIST, identifier_leaks, verbatim_overlap

TS = datetime(2026, 8, 4, tzinfo=timezone.utc)


def _segment(content: str) -> Segment:
    event = AgentEvent(role="assistant", kind="text", content=content,
                       ts=TS, agent_session_id="as-leak-001")
    return Segment(id="leak-001", agent_session_id="as-leak-001",
                   events=[event], started_at=TS, ended_at=TS)


def test_the_documented_failure_case_is_now_caught():
    seg = _segment("set default_pool_size=25 in the pgbouncer config")
    finding = "Raised default_pool_size=25 to handle the connection load."
    assert verbatim_overlap(finding, seg) == 0.0          # the old metric stays blind
    assert "default_pool_size=25" in identifier_leaks(finding, seg)  # the new one is not


def test_shapes_snake_camel_dotted_path_and_fileext():
    seg = _segment("in auth_helper we call TokenValidator via api.internal.example "
                   "reading config/settings.py and /etc/synapse/keys")
    finding = ("auth_helper uses TokenValidator against api.internal.example, "
               "configured in config/settings.py under /etc/synapse/keys")
    leaks = identifier_leaks(finding, seg)
    for expected in ("auth_helper", "TokenValidator", "api.internal.example",
                     "config/settings.py", "/etc/synapse/keys"):
        assert expected in leaks


def test_public_vocabulary_is_not_a_leak():
    seg = _segment("switched from pgbouncer to asyncpg after running ruff")
    finding = "Switched from pgbouncer to asyncpg after a ruff pass."
    assert identifier_leaks(finding, seg) == []
    assert {"pgbouncer", "asyncpg", "ruff"} <= DEFAULT_ALLOWLIST


def test_identifier_only_in_finding_is_not_a_leak():
    # Invented by the model, not copied from the session — a fidelity problem,
    # not a privacy one. This metric must not conflate the two.
    seg = _segment("the request handler is slow")
    assert identifier_leaks("slowness in fast_path_v2 handler", seg) == []


def test_plain_prose_never_flags():
    seg = _segment("the connection pool was exhausted under load")
    assert identifier_leaks("The connection pool was exhausted under load.", seg) == []
```

- [x] **Step 2: Run to verify they fail**

Run: `uv run pytest packages/distiller/tests/test_identifier_leaks.py -v`
Expected: FAIL with `ImportError: cannot import name 'identifier_leaks'`.

- [x] **Step 3: Implement in evaluation.py**

Append to `packages/distiller/src/synapse_distiller/evaluation.py`:

```python
# ── identifier leaks ─────────────────────────────────────────────────────────
# The n-gram metric above cannot see single-token leaks: it scored 0.00 on a
# finding containing `default_pool_size=25`. An identifier-shaped token that
# appears in BOTH the finding and the segment is copied source vocabulary.
#
# Not every identifier is private — some are the public names the goldens use
# deliberately. Those live in DEFAULT_ALLOWLIST (lowercased). A token only in
# the finding is NOT a leak: the model invented it, which is a fidelity
# problem, not a privacy one.

DEFAULT_ALLOWLIST: frozenset[str] = frozenset({
    # public tools & libraries the corpus names on purpose
    "pgbouncer", "asyncpg", "ruff", "pytest", "redis", "uv", "jsonl", "json",
    # our own public vocabulary (CONTEXT.md terms that look identifier-shaped)
    "claude-code", "codex", "tool_use", "tool_result", "dead_end", "open_question",
})

_IDENTIFIER_RE = re.compile(
    r"""(?x)
    (?<![\w./\\-])(
        (?:/|[A-Za-z]:\\)[\w.\\/-]+           # absolute path (unix or windows)
      | [\w-]+(?:/[\w.-]+)+                   # relative path with a slash
      | \w+=[^\s,;]+                          # key=value
      | [A-Za-z_]\w*(?:\.[A-Za-z_]\w*)+       # dotted.name (incl. file.ext, host)
      | [a-z0-9]+(?:_[a-z0-9]+)+              # snake_case
      | [a-z0-9]+(?:-[a-z0-9]+)+              # kebab-case
      | [a-z]+(?:[A-Z][a-z0-9]*)+             # camelCase
      | [A-Z][a-z0-9]+(?:[A-Z][a-z0-9]*)+     # CamelCase
    )(?![\w./\\-])
    """
)


def _identifier_tokens(text: str) -> set[str]:
    return {m.group(1) for m in _IDENTIFIER_RE.finditer(text)}


def identifier_leaks(
    finding_text: str,
    segment: Segment,
    allowlist: frozenset[str] = DEFAULT_ALLOWLIST,
) -> list[str]:
    """Identifier-shaped tokens copied from the segment into the finding.

    Sorted for stable output. Empty list == no detected leak — which is
    evidence, not proof; the allowlist is a judgment call and reviewed with
    the corpus.
    """
    source = " ".join(e.content for e in segment.events)
    in_both = _identifier_tokens(finding_text) & _identifier_tokens(source)
    return sorted(t for t in in_both if t.lower() not in allowlist)
```

Then wire into the score. Add `field` to the existing `dataclasses` import, and add one field to `FixtureScore`, **last**, so the no-default fields above it are unaffected:

```python
    leaked_identifiers: list[str] = field(default_factory=list)
```

In `score_fixture(...)`, after the existing `overlaps = ...` line:

```python
    leaks = sorted({t for f in findings for t in identifier_leaks(f.text, segment)})
```

and pass `leaked_identifiers=leaks` in the `FixtureScore(...)` constructor call.

- [x] **Step 4: Run to verify they pass**

Run: `uv run pytest packages/distiller/tests/test_identifier_leaks.py packages/distiller/tests/ -q`
Expected: PASS, and the rest of the distiller suite stays green.

- [x] **Step 5: Commit**

```bash
git add packages/distiller/src/synapse_distiller/evaluation.py packages/distiller/tests/test_identifier_leaks.py
git commit -m "feat(eval): identifier-leak detector — catches the single-token leaks the 8-gram metric misses"
```

---

### Task 6: harness wiring + contamination guard + README

**Files:**
- Modify: `scripts/run_npu_eval.py`
- Create: `packages/distiller/tests/test_fixture_contamination.py`
- Modify: `fixtures/README.md`

**Interfaces:**
- Consumes: `FixtureScore.leaked_identifiers` from Task 5.

- [x] **Step 1: Write the failing contamination test**

The `v1-baseline` incident, automated: no committed fixture may share a distinctive phrase with any prompt pack's few-shots.

```python
# packages/distiller/tests/test_fixture_contamination.py
"""No fixture may overlap a prompt pack's few-shots — the v1-baseline lesson."""
import re
from pathlib import Path

import pytest

from synapse_distiller.fixtures import available_fixtures, fixtures_root, load_segment

def _packs_dir() -> Path:
    for parent in Path(__file__).resolve().parents:
        candidate = parent / "config" / "prompts"
        if candidate.is_dir():
            return candidate
    raise FileNotFoundError("config/prompts not found above test file")


def _sixgrams(text: str) -> set[tuple[str, ...]]:
    words = re.findall(r"\w+", text.lower())
    return {tuple(words[i:i+6]) for i in range(len(words) - 5)}


@pytest.mark.parametrize("fixture_id", available_fixtures())
def test_fixture_shares_no_sixgram_with_any_pack(fixture_id):
    fixture_text = " ".join(e.content for e in load_segment(fixture_id).events)
    fixture_grams = _sixgrams(fixture_text)
    for pack_path in sorted(_packs_dir().glob("*.toml")):
        overlap = fixture_grams & _sixgrams(pack_path.read_text(encoding="utf-8"))
        assert not overlap, (
            f"{fixture_id} shares wording with {pack_path.name}: {sorted(overlap)[:3]} — "
            "a few-shot that duplicates a fixture measures pattern-matching, not generalization"
        )
```

- [x] **Step 2: Run it**

Run: `uv run pytest packages/distiller/tests/test_fixture_contamination.py -v`
Expected: **likely one FAILURE** — `v1-baseline` is documented as contaminated against `seg-004`. If it fails there: that pack is frozen as evidence, so add the documented exception at the top of the test rather than editing the pack:

```python
KNOWN_CONTAMINATED = {("seg-004", "v1-baseline.toml")}  # frozen evidence; declared in the pack itself
```

and inside the loop: `if (fixture_id, pack_path.name) in KNOWN_CONTAMINATED: continue`. Every NEW fixture must pass clean.

- [x] **Step 3: Wire leaks into the eval output**

In `scripts/run_npu_eval.py`, the per-fixture reporting loop already prints from each `score` (a `FixtureScore`). Immediately after the existing per-fixture print block that reports `max_verbatim_overlap`, add:

```python
        if score.leaked_identifiers:
            print(f"  LEAKED IDENTIFIERS: {', '.join(score.leaked_identifiers)}")
```

and in the summary section at the bottom (after the voided-pack report), add:

```python
    all_leaks = sorted({t for s in scores for t in s.leaked_identifiers})
    if all_leaks:
        print(f"\n  identifier leaks across corpus: {', '.join(all_leaks)}")
        print("  The privacy claim does NOT hold for this run. Do not demo this table.")
    else:
        print("\n  identifier leaks: none detected (allowlist applies — see evaluation.py)")
```

- [x] **Step 4: Update fixtures/README.md**

Replace its provisional-status section with:

```markdown
## Status — 2026-08-04

8 fixtures. seg-001/seg-004 predate the corpus completion; the rest landed with plan E1.

**Golden sign-off (co-authoring gate — Plan 0.3):**

| fixture | authored by | signed off by |
|---|---|---|
| seg-001 | aditya | — PROVISIONAL |
| seg-002…seg-007 | (E1 author) | — PROVISIONAL |

Goldens are the eval target and the quality bar. Until each row has all three
names, treat judge scores as directional. `fixtures/triage.json` records what
triage should do per fixture, including two ACCEPTED FALSE POSITIVE entries.
`test_fixture_contamination.py` enforces zero six-gram overlap with prompt packs.
```

- [x] **Step 5: Run the full distiller suite, then commit**

Run: `uv run pytest packages/distiller -q`
Expected: PASS.

```bash
git add scripts/run_npu_eval.py packages/distiller/tests/test_fixture_contamination.py fixtures/README.md
git commit -m "feat(eval): leak column in the harness + fixture/pack contamination guard"
```

---

## Deferred, with reasons

- **The "dead_end whose pivot lands in the next segment" case** (implementation report Part 6). It needs *two* consecutive segment fixtures and a cross-segment eval axis, and the current harness scores strictly per-segment — the fixture would sit unmeasurable. Build it together with the harness change, as one piece, after E2 lands. Recorded here so it is a decision, not an omission.

## Done when

1. `uv run pytest packages/distiller -q` green offline.
2. `available_fixtures()` returns all 8; `fixtures/triage.json` covers all 8.
3. The planted `default_pool_size=25` case is caught; the plain-prose case is not.
4. A fixture sharing a six-gram with a pack fails CI.
5. `fixtures/README.md` shows the sign-off table — goldens stay PROVISIONAL until co-review.
