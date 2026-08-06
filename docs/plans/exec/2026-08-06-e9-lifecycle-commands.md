# E9 — Lifecycle commands (`synapse up` / `down` / `status`)

> **For agentic workers:** execute task by task, ticking checkboxes. Each task
> ends with a green test run and a commit. A red gate stops the plan — report
> it rather than pushing past it.

**Goal:** let a developer start and stop the local Synapse processes from
inside a Claude Code session, explicitly, without leaving the terminal and
without anything running when they aren't using it.

**Architecture:** a new `packages/lifecycle` package owns process lifecycle and
nothing else. `synapse up` starts each component detached (`start_new_session=True`)
with a PID file under `.synapse/run/`, waits for readiness, and **returns
immediately** — so the agent that invoked it is not holding a long-running
process and nothing dies when the turn ends. `synapse status` and `synapse down`
read the same PID files. A slash command in the awareness pack is the explicit
user-facing trigger; the freshness hook gains one line telling you the stack is
down when it is.

**Tech stack:** Python 3.12+, stdlib only in the new package (`subprocess`,
`socket`, `signal`, `json`, `pathlib`). No new third-party dependency. Existing
console scripts (`synapse-service`, `synapse-orchestrator`, `synapse-worker`)
are spawned as subprocesses, not imported.

## Global constraints

- **`npu` stays the default distiller.** Nothing in this plan changes which
  model runs. `--distiller {npu,anthropic,claude-cli}` is passed through
  verbatim to the processes that already accept `SYNAPSE_DISTILLER`.
- **Nothing starts automatically.** No launchd, no systemd, no login item, no
  SessionStart hook that spawns anything. Every start is a command a human
  typed. The hook may *report*; it may never *start*.
- **One orchestrator per machine.** It holds the laptop's binding and stamps
  attribution. `up` must refuse to start a second one rather than let it steal
  identity — see `scripts/serve_local.py`'s port guard and
  `packages/orchestrator/src/synapse_orchestrator/app.py`'s module docstring.
- **The service is the only shareable component.** `up` never binds the
  orchestrator to anything but `127.0.0.1`.
- **Fail loudly, never half-up.** A partial start (service up, orchestrator
  failed) is worse than no start: the MCP path is dead while everything looks
  alive. `up` tears down what it started and reports.
- **Secrets stay out of argv.** Keys go into a child's environment, never onto
  a command line where `ps` shows them. Cf. `scripts/serve_local.py`'s
  `_anthropic_key`.
- **`.synapse/` is gitignored.** PID files and logs live there; nothing this
  plan writes is ever committed.

## File structure

| File | Responsibility |
|---|---|
| `packages/lifecycle/pyproject.toml` | new package; declares the `synapse` console script |
| `packages/lifecycle/src/synapse_lifecycle/__init__.py` | exports `Component`, `read_record`, `probe` |
| `packages/lifecycle/src/synapse_lifecycle/registry.py` | what the components ARE — name, port, argv, env, readiness URL. The one place a component is described. |
| `packages/lifecycle/src/synapse_lifecycle/state.py` | PID files: write, read, remove; liveness and port probing; the four states |
| `packages/lifecycle/src/synapse_lifecycle/cli.py` | `up` / `down` / `status` argument parsing and output |
| `packages/lifecycle/tests/test_state.py` | state machine incl. stale and foreign |
| `packages/lifecycle/tests/test_registry.py` | argv/env composition, secrets not in argv |
| `packages/lifecycle/tests/test_cli.py` | up/down/status behaviour with spawning faked |
| `packages/orchestrator/src/synapse_orchestrator/idle.py` | idle-stop: last-MCP-traffic stamp + shutdown |
| `packs/claude-code/commands/synapse-up.md` | the `/synapse-up` slash command |
| `packs/claude-code/commands/synapse-down.md` | the `/synapse-down` slash command |
| `packs/claude-code/hooks/freshness_pointer.py` | one added line when the stack is down |
| `packs/claude-code/INSTALL.md` | install the commands dir; document the lifecycle |
| `docs/STATE.md` | replace the manual four-terminal instructions |

Why a new package rather than adding to an existing one: this code *manages*
the orchestrator, the service and the worker, so it cannot live inside any of
them without a package depending on its own supervisor. It imports none of
them — it spawns their console scripts — so the dependency graph stays a tree.

---

### Task 1 — `synapse status`: know the truth before changing it

Read-only, and the foundation both other verbs stand on. Four states, because
three of them are real failures we have already hit tonight:

- `running` — our PID is alive **and** the port answers
- `stale` — a PID file exists, the process is gone (crash, reboot, `kill -9`)
- `foreign` — the port answers but the PID is not ours, or there is no PID
  file. This is the one that matters: a second orchestrator on 8787 would
  serve a different binding and silently stamp another developer's identity
  onto findings.
- `stopped` — no PID file, nothing on the port

**Files:**
- Create: `packages/lifecycle/pyproject.toml`, `src/synapse_lifecycle/__init__.py`,
  `src/synapse_lifecycle/state.py`, `src/synapse_lifecycle/registry.py`,
  `src/synapse_lifecycle/cli.py`
- Test: `packages/lifecycle/tests/test_state.py`
- Modify: root `pyproject.toml` (add to `[tool.uv.workspace] members`)

**Interfaces produced** (later tasks depend on these exact names):

```python
# registry.py
@dataclass(frozen=True)
class Component:
    name: str                 # "service" | "orchestrator" | "worker" | "model"
    port: int | None          # None for the worker, which listens only when --debug-port is given
    ready_url: str | None     # probed for readiness; None means "PID liveness is the only signal"

def components(args) -> list[Component]: ...        # in start order
def argv_for(c: Component, args) -> list[str]: ...
def env_for(c: Component, args) -> dict[str, str]: ...

# state.py
RUN_DIR_NAME = "run"                                  # .synapse/run/
@dataclass(frozen=True)
class Record:
    pid: int
    port: int | None
    started_at: str                                   # ISO 8601
    argv: list[str]

def record_path(state_dir: Path, name: str) -> Path: ...
def write_record(state_dir: Path, name: str, record: Record) -> None: ...
def read_record(state_dir: Path, name: str) -> Record | None: ...   # None if absent or unreadable
def remove_record(state_dir: Path, name: str) -> None: ...
def pid_alive(pid: int) -> bool: ...
def port_answers(port: int, timeout: float = 0.4) -> bool: ...
def status_of(state_dir: Path, c: Component) -> str: ...  # "running"|"stale"|"foreign"|"stopped"
```

- [ ] **Step 1: write the failing tests**

```python
# packages/lifecycle/tests/test_state.py
import os
from datetime import datetime, timezone

from synapse_lifecycle.registry import Component
from synapse_lifecycle.state import (
    Record, pid_alive, read_record, remove_record, status_of, write_record,
)

ORCH = Component(name="orchestrator", port=8787, ready_url="http://127.0.0.1:8787/mcp")


def _record(pid: int) -> Record:
    return Record(pid=pid, port=8787,
                  started_at=datetime.now(timezone.utc).isoformat(),
                  argv=["synapse-orchestrator"])


def test_no_record_and_a_quiet_port_reads_as_stopped(tmp_path, monkeypatch):
    monkeypatch.setattr("synapse_lifecycle.state.port_answers", lambda *a, **k: False)
    assert status_of(tmp_path, ORCH) == "stopped"


def test_our_live_pid_with_an_answering_port_reads_as_running(tmp_path, monkeypatch):
    write_record(tmp_path, "orchestrator", _record(os.getpid()))
    monkeypatch.setattr("synapse_lifecycle.state.port_answers", lambda *a, **k: True)
    assert status_of(tmp_path, ORCH) == "running"


def test_a_record_whose_process_died_reads_as_stale_not_running(tmp_path, monkeypatch):
    """A crash, a reboot, or `kill -9` leaves the file behind. Reporting that
    as running is how you end up debugging a stack that is not there."""
    write_record(tmp_path, "orchestrator", _record(999_999))
    monkeypatch.setattr("synapse_lifecycle.state.port_answers", lambda *a, **k: False)
    assert status_of(tmp_path, ORCH) == "stale"


def test_someone_elses_process_on_our_port_reads_as_foreign(tmp_path, monkeypatch):
    """THE state that matters. A second orchestrator on 8787 serves a different
    binding, so `contribute` would stamp another developer's contributor and
    agent session onto findings. `up` must refuse, not add to the pile."""
    monkeypatch.setattr("synapse_lifecycle.state.port_answers", lambda *a, **k: True)
    assert status_of(tmp_path, ORCH) == "foreign"          # no record at all


def test_a_dead_record_over_an_answering_port_is_also_foreign(tmp_path, monkeypatch):
    write_record(tmp_path, "orchestrator", _record(999_999))
    monkeypatch.setattr("synapse_lifecycle.state.port_answers", lambda *a, **k: True)
    assert status_of(tmp_path, ORCH) == "foreign"


def test_an_unreadable_record_is_treated_as_absent(tmp_path):
    """Torn write, or a half-flushed file after a crash. Absent is recoverable;
    raising here would make `status` fail exactly when it is most needed."""
    path = tmp_path / "run" / "orchestrator.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("{not json")
    assert read_record(tmp_path, "orchestrator") is None


def test_pid_alive_is_false_for_a_pid_that_cannot_exist():
    assert pid_alive(999_999) is False
    assert pid_alive(os.getpid()) is True


def test_remove_record_is_idempotent(tmp_path):
    remove_record(tmp_path, "orchestrator")            # must not raise
    write_record(tmp_path, "orchestrator", _record(os.getpid()))
    remove_record(tmp_path, "orchestrator")
    assert read_record(tmp_path, "orchestrator") is None
```

- [ ] **Step 2: run them and watch them fail**

Run: `uv run pytest packages/lifecycle/tests/test_state.py -q`
Expected: collection error — `No module named 'synapse_lifecycle'`.

- [ ] **Step 3: create the package skeleton**

`packages/lifecycle/pyproject.toml`:

```toml
[project]
name = "synapse-lifecycle"
version = "0.1.0"
requires-python = ">=3.12"
dependencies = []                      # stdlib only, on purpose

[project.scripts]
synapse = "synapse_lifecycle.cli:main"

[build-system]
requires = ["hatchling"]
build-backend = "hatchling.build"

[tool.hatch.build.targets.wheel]
packages = ["src/synapse_lifecycle"]
```

Add `"packages/lifecycle"` to the root `pyproject.toml`'s
`[tool.uv.workspace] members` list, and `synapse-lifecycle` to the root
project's dependencies alongside the other workspace packages, then
`uv sync`.

- [ ] **Step 4: implement `state.py`**

```python
"""PID files and probes — what is actually running, as opposed to what a
previous run left behind.

`pid_alive` uses signal 0, which asks the kernel "may I signal this pid"
without sending anything. A PID file alone is not evidence: a crash, a reboot
or `kill -9` all leave one behind, and reporting that as running sends someone
to debug a stack that is not there.

The port matters as much as the pid, and disagreements between them are the
interesting cases rather than edge cases -- see `status_of`.
"""
from __future__ import annotations

import json
import os
import socket
from dataclasses import asdict, dataclass
from pathlib import Path

RUN_DIR_NAME = "run"


@dataclass(frozen=True)
class Record:
    pid: int
    port: int | None
    started_at: str
    argv: list[str]


def record_path(state_dir: Path, name: str) -> Path:
    return Path(state_dir) / RUN_DIR_NAME / f"{name}.json"


def write_record(state_dir: Path, name: str, record: Record) -> None:
    path = record_path(state_dir, name)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(asdict(record), indent=2), encoding="utf-8")
    os.replace(tmp, path)                      # atomic: no torn file to read


def read_record(state_dir: Path, name: str) -> Record | None:
    path = record_path(state_dir, name)
    if not path.is_file():
        return None
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
        return Record(pid=int(raw["pid"]), port=raw.get("port"),
                      started_at=str(raw.get("started_at", "")),
                      argv=list(raw.get("argv", [])))
    except (json.JSONDecodeError, KeyError, TypeError, ValueError, OSError):
        return None                            # absent is recoverable; raising is not


def remove_record(state_dir: Path, name: str) -> None:
    record_path(state_dir, name).unlink(missing_ok=True)


def pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True                            # exists, owned by someone else
    return True


def port_answers(port: int, timeout: float = 0.4) -> bool:
    with socket.socket() as probe:
        probe.settimeout(timeout)
        return probe.connect_ex(("127.0.0.1", port)) == 0


def status_of(state_dir: Path, component) -> str:
    record = read_record(state_dir, component.name)
    ours_alive = record is not None and pid_alive(record.pid)
    listening = component.port is not None and port_answers(component.port)

    if component.port is None:                 # the worker, with no debug port
        return "running" if ours_alive else ("stale" if record else "stopped")
    if listening and ours_alive:
        return "running"
    if listening:
        return "foreign"                       # someone else holds the port
    return "stale" if record is not None else "stopped"
```

- [ ] **Step 5: implement the minimum of `registry.py` these tests need**

Only `Component` is needed for Task 1; `argv_for`/`env_for` land in Task 2.

```python
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Component:
    name: str
    port: int | None
    ready_url: str | None
```

- [ ] **Step 6: run the tests**

Run: `uv run pytest packages/lifecycle/tests/test_state.py -q`
Expected: 8 passed.

- [ ] **Step 7: `synapse status` prints it**

```python
def cmd_status(args) -> int:
    state_dir = Path(args.state_dir)
    for component in components(args):
        state = status_of(state_dir, component)
        record = read_record(state_dir, component.name)
        where = f"pid {record.pid}" if record else "-"
        port = f":{component.port}" if component.port else ""
        print(f"  {component.name:<13} {state:<8} {port:<6} {where}")
    return 0
```

- [ ] **Step 8: verify by hand against the stack that is running right now**

Run: `uv run synapse status`
Expected: `service running`, `orchestrator running`, `worker stopped`.
Then `pkill -f synapse-orchestrator; uv run synapse status` →
`orchestrator stopped` (or `stale`, if a record existed).

- [ ] **Step 9: commit**

```bash
git add packages/lifecycle pyproject.toml uv.lock
git commit -m "feat(lifecycle): synapse status — four states, because three of them are failures we have hit"
```

---

### Task 2 — `synapse up`: detached, idempotent, all-or-nothing

**Files:**
- Modify: `packages/lifecycle/src/synapse_lifecycle/registry.py`,
  `src/synapse_lifecycle/cli.py`
- Test: `packages/lifecycle/tests/test_registry.py`, `tests/test_cli.py`

**Interfaces consumed:** `Component`, `status_of`, `write_record`, `Record`
(Task 1). **Produces:** `argv_for`, `env_for`, `cmd_up`.

Component set, in start order — each conditional, so `up` starts the least it
can:

| Component | Started when | Port |
|---|---|---|
| `model` | no `--npu` (i.e. no GenieX already serving) | 18181 |
| `service` | no `--service-url` (i.e. not joining someone else's) | 8899 |
| `orchestrator` | always | 8787 |
| `worker` | `--worker` only | none, unless `--debug-port` |

- [ ] **Step 1: write the failing tests**

```python
# packages/lifecycle/tests/test_cli.py
import os
from pathlib import Path

import pytest
from synapse_lifecycle import cli


class _FakeProc:
    def __init__(self, pid=4242):
        self.pid, self._alive = pid, True
    def poll(self):
        return None if self._alive else 1
    def terminate(self):
        self._alive = False
    def wait(self, timeout=None):
        self._alive = False
        return 0
    def kill(self):
        self._alive = False


@pytest.fixture
def spawned(monkeypatch, tmp_path):
    """Record every spawn instead of performing it."""
    calls = []

    def fake_popen(argv, **kwargs):
        calls.append({"argv": argv, "env": kwargs.get("env") or {},
                      "new_session": kwargs.get("start_new_session")})
        return _FakeProc(pid=4242 + len(calls))

    monkeypatch.setattr(cli.subprocess, "Popen", fake_popen)
    monkeypatch.setattr(cli, "_wait_ready", lambda *a, **k: True)
    monkeypatch.setattr("synapse_lifecycle.state.port_answers", lambda *a, **k: False)
    monkeypatch.setattr("synapse_lifecycle.state.pid_alive", lambda pid: True)
    return calls


def test_up_detaches_every_child_so_it_survives_the_session(spawned, tmp_path):
    """The agent that runs `up` exits its turn seconds later. Without a new
    session the children are in the caller's process group and die with it."""
    assert cli.main(["up", "--state-dir", str(tmp_path)]) == 0
    assert spawned, "nothing was spawned"
    assert all(c["new_session"] is True for c in spawned)


def test_up_writes_a_record_per_component_so_down_can_find_them(spawned, tmp_path):
    cli.main(["up", "--state-dir", str(tmp_path)])
    from synapse_lifecycle.state import read_record
    for name in ("model", "service", "orchestrator"):
        assert read_record(tmp_path, name) is not None, name


def test_up_does_not_start_a_worker_unless_asked(spawned, tmp_path):
    """The worker polls and calls a model; the orchestrator is nearly free.
    Splitting them is what makes an idle stack cheap enough to leave up."""
    cli.main(["up", "--state-dir", str(tmp_path)])
    assert not any("synapse-worker" in " ".join(c["argv"]) for c in spawned)
    spawned.clear()
    cli.main(["up", "--worker", "--state-dir", str(tmp_path)])
    assert any("synapse-worker" in " ".join(c["argv"]) for c in spawned)


def test_up_is_idempotent_and_says_so(spawned, tmp_path, monkeypatch, capsys):
    """Running it twice must not produce a second orchestrator: it holds the
    machine's binding, and two of them stamp different identities."""
    monkeypatch.setattr("synapse_lifecycle.state.port_answers", lambda *a, **k: True)
    from synapse_lifecycle.state import Record, write_record
    write_record(tmp_path, "orchestrator",
                 Record(pid=os.getpid(), port=8787, started_at="", argv=[]))
    assert cli.main(["up", "--state-dir", str(tmp_path)]) == 0
    assert not any("synapse-orchestrator" in " ".join(c["argv"]) for c in spawned)
    assert "already running" in capsys.readouterr().out


def test_up_refuses_when_the_port_is_held_by_something_that_is_not_ours(
    spawned, tmp_path, monkeypatch, capsys
):
    """`foreign` is the state that silently corrupts attribution."""
    monkeypatch.setattr("synapse_lifecycle.state.port_answers", lambda *a, **k: True)
    assert cli.main(["up", "--state-dir", str(tmp_path)]) == 2
    out = capsys.readouterr().out
    assert "foreign" in out or "not ours" in out


def test_a_component_that_never_becomes_ready_tears_down_what_up_started(
    spawned, tmp_path, monkeypatch, capsys
):
    """A half-started stack is worse than none: the MCP path is dead while
    everything looks alive."""
    monkeypatch.setattr(cli, "_wait_ready", lambda component, *a, **k:
                        component.name != "orchestrator")
    assert cli.main(["up", "--state-dir", str(tmp_path)]) == 1
    from synapse_lifecycle.state import read_record
    assert read_record(tmp_path, "service") is None, "service record left behind"
    assert "rolled back" in capsys.readouterr().out


def test_the_anthropic_key_goes_in_the_environment_never_in_argv(spawned, tmp_path,
                                                                monkeypatch):
    """`ps` shows every argument. A key on a command line is a key in every
    process listing on the machine."""
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-SECRET")
    cli.main(["up", "--distiller", "anthropic", "--state-dir", str(tmp_path)])
    for call in spawned:
        assert "sk-ant-SECRET" not in " ".join(call["argv"])
    orch = [c for c in spawned if "synapse-orchestrator" in " ".join(c["argv"])][0]
    assert orch["env"]["ANTHROPIC_API_KEY"] == "sk-ant-SECRET"
    assert orch["env"]["SYNAPSE_DISTILLER"] == "anthropic"
```

- [ ] **Step 2: run them and watch them fail**

Run: `uv run pytest packages/lifecycle/tests/test_cli.py -q`
Expected: `AttributeError: module 'synapse_lifecycle.cli' has no attribute 'main'`.

- [ ] **Step 3: implement `argv_for` / `env_for` in `registry.py`**

```python
def argv_for(component: Component, args) -> list[str]:
    if component.name == "model":
        argv = [sys.executable, str(REPO / "scripts" / "local_model_server.py")]
        if args.live:
            argv += ["--mode", "proxy"]
        return argv
    if component.name == "service":
        return ["synapse-service", "--host", args.listen, "--port", "8899"]
    if component.name == "orchestrator":
        return ["synapse-orchestrator", "--port", "8787",
                "--service-url", args.service_url or "http://127.0.0.1:8899",
                "--state-dir", args.state_dir]
    return ["synapse-worker", "run", "--interval", str(args.interval),
            "--debug-port", str(args.debug_port)]


def env_for(component: Component, args) -> dict[str, str]:
    """Secrets ride here, never in argv — `ps` shows arguments to every user
    on the machine."""
    env = {}
    if component.name in ("orchestrator", "worker"):
        env["SYNAPSE_DISTILLER"] = args.distiller
        if args.distiller == "anthropic" and (key := anthropic_key()):
            env["ANTHROPIC_API_KEY"] = key
    return env
```

`anthropic_key()` does not exist yet in this package. `scripts/serve_local.py`
has `_anthropic_key()` — env first, then `secrets.jsonc`'s `inference_cloud`-
sibling `anthropic` block. **Move it here** as `secrets.anthropic_key()` (new
module `src/synapse_lifecycle/secrets.py`, stdlib only) and have
`serve_local.py` import it, so there is one definition rather than two that
drift. Keep the reasoning comment that came with it: scripts read
`secrets.jsonc`, packages read the environment — and this package is the
script-shaped one, so it is the right home.

- [ ] **Step 4: implement `cmd_up`**

Order: probe every component first, refuse on any `foreign`, skip any
`running`, then start the rest — recording each PID as it goes so a rollback
knows exactly what to undo.

```python
def cmd_up(args) -> int:
    state_dir = Path(args.state_dir)
    wanted = components(args)

    foreign = [c for c in wanted if status_of(state_dir, c) == "foreign"]
    if foreign:
        for c in foreign:
            print(f"  {c.name}: port {c.port} is held by a process that is not ours "
                  f"(foreign). Refusing to start a second one — an orchestrator "
                  f"holds this machine's binding and two of them stamp different "
                  f"identities onto findings.")
        print("  Stop the other one, or `synapse down` if it is a stale run of ours.")
        return 2

    started: list[tuple[Component, subprocess.Popen]] = []
    for component in wanted:
        if status_of(state_dir, component) == "running":
            print(f"  {component.name:<13} already running")
            continue
        remove_record(state_dir, component.name)         # clear any stale file
        log = (state_dir / "logs" / f"{component.name}.log")
        log.parent.mkdir(parents=True, exist_ok=True)
        handle = log.open("a")
        process = subprocess.Popen(
            argv_for(component, args),
            cwd=REPO, env={**os.environ, **env_for(component, args)},
            stdout=handle, stderr=subprocess.STDOUT,
            start_new_session=True,        # survives the caller's session
        )
        write_record(state_dir, component.name,
                     Record(pid=process.pid, port=component.port,
                            started_at=datetime.now(timezone.utc).isoformat(),
                            argv=argv_for(component, args)))
        if not _wait_ready(component, process):
            print(f"  {component.name}: did not become ready — see {log}")
            _roll_back(state_dir, started + [(component, process)])
            print("  rolled back; nothing left running")
            return 1
        started.append((component, process))
        print(f"  {component.name:<13} started  pid {process.pid}")

    print(f"\n  MCP:  http://127.0.0.1:8787/mcp")
    print(f"  stop: synapse down")
    return 0
```

- [ ] **Step 5: implement the two helpers `cmd_up` calls**

```python
def _wait_ready(component: Component, process: subprocess.Popen,
                seconds: float = 25.0) -> bool:
    """Up, and up because OUR child is serving it.

    Polling the URL alone is not enough: a child that dies instantly on
    EADDRINUSE is indistinguishable from a healthy one, because whatever
    already holds the port answers happily. `serve_local.py` hit exactly this.
    """
    deadline = time.time() + seconds
    while time.time() < deadline:
        if process.poll() is not None:
            return False                      # died; the log says why
        if component.ready_url is None:
            return True                       # PID liveness is the only signal
        try:
            urllib.request.urlopen(component.ready_url, timeout=2.0).read()
            return True
        except urllib.error.HTTPError:
            return True                       # answered at all: it is up
        except (urllib.error.URLError, TimeoutError, OSError):
            time.sleep(0.4)
    return False


def _roll_back(state_dir: Path, started: list[tuple[Component, subprocess.Popen]]) -> None:
    """Undo a partial start, newest first. A half-up stack is worse than none:
    the MCP path is dead while `status` shows components running."""
    for component, process in reversed(started):
        if process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
        remove_record(state_dir, component.name)
```

- [ ] **Step 6: run the tests**

Run: `uv run pytest packages/lifecycle/tests -q`
Expected: all pass (8 from Task 1 + 7 here).

- [ ] **Step 7: mutant-verify the detachment test**

Change `start_new_session=True` to `False` and re-run:
`test_up_detaches_every_child_so_it_survives_the_session` must fail. Restore.
A test that passes either way is not protecting the property this task exists
for.

- [ ] **Step 8: verify by hand, end to end**

```bash
pkill -f synapse-orchestrator; pkill -f synapse-service; pkill -f local_model_server
uv run synapse up --live --distiller claude-cli
uv run synapse status                  # three running
```
Then **exit the shell that ran it entirely**, open a new one, and run
`uv run synapse status` again: still three running. That is the property.

- [ ] **Step 9: commit**

```bash
git add packages/lifecycle
git commit -m "feat(lifecycle): synapse up — detached, idempotent, refuses a foreign port"
```

---

### Task 3 — `synapse down`: the answer to "it's eating my RAM"

Cheap to reach and honest about what it stopped. Never kills a process it did
not start: a `foreign` port might be a teammate's service, or an unrelated
program.

**Files:** modify `src/synapse_lifecycle/cli.py`; test in `tests/test_cli.py`.

- [ ] **Step 1: write the failing tests**

```python
def test_down_stops_what_we_started_and_clears_the_records(tmp_path, monkeypatch, capsys):
    from synapse_lifecycle.state import Record, read_record, write_record
    killed = []
    monkeypatch.setattr(cli.os, "kill",
                        lambda pid, sig: killed.append((pid, sig)))
    monkeypatch.setattr("synapse_lifecycle.state.pid_alive",
                        lambda pid: (pid, 15) not in killed)
    for name, port in (("service", 8899), ("orchestrator", 8787)):
        write_record(tmp_path, name, Record(pid=5000 + port, port=port,
                                            started_at="", argv=[]))

    assert cli.main(["down", "--state-dir", str(tmp_path)]) == 0
    assert {pid for pid, _ in killed} == {5000 + 8899, 5000 + 8787}
    assert read_record(tmp_path, "orchestrator") is None
    assert "stopped" in capsys.readouterr().out


def test_down_escalates_to_sigkill_only_after_a_grace_period(tmp_path, monkeypatch):
    """SIGTERM lets uvicorn close its sockets. SIGKILL leaves the port in
    TIME_WAIT and the next `up` fails to bind for no visible reason."""
    from synapse_lifecycle.state import Record, write_record
    signals = []
    monkeypatch.setattr(cli.os, "kill", lambda pid, sig: signals.append(sig))
    monkeypatch.setattr("synapse_lifecycle.state.pid_alive", lambda pid: True)  # never dies
    monkeypatch.setattr(cli.time, "sleep", lambda s: None)
    write_record(tmp_path, "orchestrator", Record(pid=6001, port=8787,
                                                  started_at="", argv=[]))

    cli.main(["down", "--state-dir", str(tmp_path)])
    import signal as sig_mod
    assert signals[0] == sig_mod.SIGTERM
    assert signals[-1] == sig_mod.SIGKILL


def test_down_never_kills_a_process_it_did_not_start(tmp_path, monkeypatch, capsys):
    """A foreign holder of 8899 may be a teammate's service. Killing it because
    it is on a port we wanted is not ours to do."""
    monkeypatch.setattr("synapse_lifecycle.state.port_answers", lambda *a, **k: True)
    killed = []
    monkeypatch.setattr(cli.os, "kill", lambda pid, s: killed.append(pid))
    assert cli.main(["down", "--state-dir", str(tmp_path)]) == 0
    assert killed == []
    assert "not ours" in capsys.readouterr().out


def test_down_on_an_already_stopped_stack_is_not_an_error(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr("synapse_lifecycle.state.port_answers", lambda *a, **k: False)
    assert cli.main(["down", "--state-dir", str(tmp_path)]) == 0
    assert "nothing running" in capsys.readouterr().out
```

- [ ] **Step 2: run and watch them fail.**
Run: `uv run pytest packages/lifecycle/tests/test_cli.py -k down -q` → 4 failed.

- [ ] **Step 3: implement `cmd_down`**

```python
def cmd_down(args) -> int:
    state_dir = Path(args.state_dir)
    stopped, foreign = [], []

    for component in reversed(components(args)):
        record = read_record(state_dir, component.name)
        if record is None:
            # Nothing of ours. If the port answers anyway it belongs to someone
            # else -- a teammate's service, or an unrelated program -- and
            # killing it because it sits on a port we wanted is not ours to do.
            if component.port is not None and port_answers(component.port):
                foreign.append(component)
            continue

        if pid_alive(record.pid):
            # SIGTERM first so uvicorn closes its listening sockets. Going
            # straight to SIGKILL leaves the port in TIME_WAIT and the next
            # `up` fails to bind for no visible reason.
            os.kill(record.pid, signal.SIGTERM)
            deadline = time.time() + args.grace
            while time.time() < deadline and pid_alive(record.pid):
                time.sleep(0.2)
            if pid_alive(record.pid):
                os.kill(record.pid, signal.SIGKILL)
            stopped.append((component.name, record.pid))
        remove_record(state_dir, component.name)

    for name, pid in stopped:
        print(f"  {name:<13} stopped  (pid {pid})")
    for component in foreign:
        print(f"  {component.name:<13} left alone — port {component.port} is held by "
              f"a process that is not ours")
    if not stopped and not foreign:
        print("  nothing running")
    return 0
```

- [ ] **Step 4: tests pass.** Run: `uv run pytest packages/lifecycle/tests -q`

- [ ] **Step 5: verify by hand:** `uv run synapse up && uv run synapse down && uv run synapse status` → all stopped, no leftover listeners in `lsof -nP -iTCP -sTCP:LISTEN | grep -E '8787|8899|18181'`.

- [ ] **Step 6: commit**

```bash
git commit -am "feat(lifecycle): synapse down — SIGTERM then SIGKILL, and never a process we did not start"
```

---

### Task 4 — `--idle-stop`: not a daemon, but not immortal either

The middle path for the RAM objection: the orchestrator exits after N minutes
with no MCP traffic. Nothing survives a reboot; nothing is registered with
launchd.

**Files:** create `packages/orchestrator/src/synapse_orchestrator/idle.py`;
modify `packages/orchestrator/src/synapse_orchestrator/cli.py` (add
`--idle-stop`, default `0` = off); test
`packages/orchestrator/tests/test_idle.py`.

Traffic is stamped by a Starlette middleware on every request whose path
starts with `/mcp`. A watchdog task on the same lifespan the briefing
refresher already uses (`briefing.attach_briefing_refresher`) checks the stamp
and, past the deadline, signals its own process.

- [ ] **Step 1: write the failing tests**

```python
async def test_idle_stop_fires_only_after_the_window_with_no_mcp_traffic(monkeypatch):
    from synapse_orchestrator.idle import IdleTimer
    now = {"t": 1000.0}
    stopped = []
    timer = IdleTimer(window_seconds=60, clock=lambda: now["t"],
                      stop=lambda: stopped.append(True))
    timer.touch()
    now["t"] = 1059.0
    assert timer.expired() is False
    now["t"] = 1061.0
    assert timer.expired() is True


async def test_traffic_resets_the_window(monkeypatch):
    from synapse_orchestrator.idle import IdleTimer
    now = {"t": 0.0}
    timer = IdleTimer(window_seconds=60, clock=lambda: now["t"], stop=lambda: None)
    timer.touch()
    now["t"] = 59.0
    timer.touch()
    now["t"] = 100.0
    assert timer.expired() is False        # 41s since the second touch


def test_idle_stop_defaults_to_off(monkeypatch, tmp_path):
    """A developer who never asks for it must never have a process vanish
    mid-session."""
    from synapse_orchestrator.cli import build_parser
    assert build_parser().parse_args([]).idle_stop == 0
```

- [ ] **Step 2: run and watch fail.** Expected: `No module named 'synapse_orchestrator.idle'`.

- [ ] **Step 3: implement `IdleTimer`** — `touch()`, `expired()`, injected
`clock` and `stop` so the test needs no sleeping and no real signals.

- [ ] **Step 4: wire the middleware and the watchdog** into `build_app` /
the lifespan, guarded by `if args.idle_stop > 0`.

- [ ] **Step 5: tests pass**, plus the full suite: `uv run pytest -q`.

- [ ] **Step 6: verify by hand:** `uv run synapse up --idle-stop 1m`, wait
seventy seconds without touching it, then `uv run synapse status` →
`orchestrator stopped`. Then repeat, issuing an MCP `query` at the 50-second
mark, and confirm it is still running at ninety seconds.

- [ ] **Step 7: commit**

```bash
git commit -am "feat(orchestrator): --idle-stop, so an unused stack does not outlive your interest in it"
```

---

### Task 5 — the explicit trigger: `/synapse-up` and `/synapse-down`

**Files:** create `packs/claude-code/commands/synapse-up.md`,
`packs/claude-code/commands/synapse-down.md`; modify
`packs/claude-code/INSTALL.md`; modify `tests/test_awareness_pack_content.py`.

- [ ] **Step 1: write the failing test**

```python
# tests/test_awareness_pack_content.py
COMMANDS = PACK / "commands"


def test_the_lifecycle_commands_exist_and_never_imply_automatic_startup():
    """Nothing in this pack may start a process on its own. The commands are
    the record of that: they are things a human types."""
    up = (COMMANDS / "synapse-up.md").read_text(encoding="utf-8")
    down = (COMMANDS / "synapse-down.md").read_text(encoding="utf-8")

    assert "synapse up" in up and "synapse down" in down
    assert "synapse down" in up, "starting must always name how to stop"
    for text in (up, down):
        for forbidden in ("automatically", "on startup", "launchd", "daemon"):
            assert forbidden not in text.lower(), forbidden
```

- [ ] **Step 2: run it and watch it fail.** Expected: `FileNotFoundError`.

- [ ] **Step 3: write `synapse-up.md`**

```markdown
---
description: Start the local Synapse processes (orchestrator, and the service
  and model seam if they are not already up) so this session's MCP tools work.
---

Run `uv run synapse up` from the Synapse checkout, then report exactly what it
printed — which components started, which were already running, and the MCP
URL.

Flags worth offering if the user's situation calls for them:
- `--worker` also starts passive capture. It polls and calls a model, so it is
  the expensive half; leave it off unless they want their transcript watched.
- `--distiller claude-cli` distils with Claude on their own subscription, no
  API key. `--distiller anthropic` uses an API key. Default is the NPU.
- `--service-url http://<host>:8899` joins a service a teammate is hosting
  instead of starting one.
- `--idle-stop 30m` makes the stack shut itself down after half an hour of no
  MCP traffic.

If it reports a `foreign` port, do not try to work around it — say which port
and that something else is already there, then stop. A second orchestrator
would stamp a different developer's identity onto this machine's findings.

Finish by telling them `/synapse-down` stops everything.
```

- [ ] **Step 4: write `synapse-down.md`** — run `uv run synapse down`, report
what stopped, and note that anything it calls "not ours" was left alone
deliberately.

- [ ] **Step 5: document installation** in `INSTALL.md`: copy `commands/` to
`.claude/commands/`, alongside the existing skill and hook steps.

- [ ] **Step 6: tests pass.** Run: `uv run pytest tests/test_awareness_pack_content.py -q`

- [ ] **Step 7: verify by hand** — install into `~/.claude/commands/`, start a
new session, run `/synapse-up`, confirm the stack comes up and the session's
`/mcp` shows `synapse` connected.

- [ ] **Step 8: commit**

```bash
git add packs/claude-code tests/test_awareness_pack_content.py
git commit -m "feat(pack): /synapse-up and /synapse-down — explicit triggers, no automatic startup"
```

---

### Task 6 — the hook says so when the stack is down

One line, on the turn where it matters. **Only when joined but unreachable**:
before a join, Synapse is inert and silence is correct
(`architecture.html`); after a join, an unreachable orchestrator means the
tools this session was told about will fail.

**Files:** modify `packs/claude-code/hooks/freshness_pointer.py`; test
`tests/test_awareness_pack_hook.py`.

- [ ] **Step 1: write the failing tests**

```python
def test_the_hook_says_the_stack_is_down_when_joined_but_unreachable(tmp_path, monkeypatch):
    """The failure this closes: a session is told in its briefing that two
    tools exist, then every call fails, and nothing anywhere says the
    orchestrator is not running."""
    _write_binding(tmp_path)
    monkeypatch.setattr(hook, "_orchestrator_answers", lambda: False)
    out = hook.compose_notice_for_test(state_dir=tmp_path)
    assert "synapse-up" in out


def test_the_hook_stays_silent_when_nothing_has_joined(tmp_path, monkeypatch):
    """Before a join, Synapse is inert. Nagging an uninvolved project is how a
    hook gets uninstalled."""
    monkeypatch.setattr(hook, "_orchestrator_answers", lambda: False)
    assert hook.compose_notice_for_test(state_dir=tmp_path) == ""


def test_the_down_notice_does_not_repeat_every_turn(tmp_path, monkeypatch):
    """Same discipline as the freshness pointer: speak on the change, then be
    quiet. A line on every prompt is noise, and noise gets muted."""
    _write_binding(tmp_path)
    monkeypatch.setattr(hook, "_orchestrator_answers", lambda: False)
    first = hook.compose_notice_for_test(state_dir=tmp_path)
    second = hook.compose_notice_for_test(state_dir=tmp_path)
    assert first and second == ""
```

- [ ] **Step 2: run and watch fail.**

- [ ] **Step 3: implement** `_orchestrator_answers()` — a 0.2s
`socket.connect_ex` to 8787, stdlib only, matching the file's existing
constraint — plus a `stack_down_notified` flag in the hook's own state file so
it speaks once per outage.

- [ ] **Step 4: tests pass**, and the hook still exits 0 on every failure path
(`uv run pytest tests/test_awareness_pack_hook.py -q`).

- [ ] **Step 5: verify by hand:** `synapse down`, submit a prompt in a joined
project, see the line once; submit again, see nothing; `synapse up`, and
confirm the next notice is a normal freshness pointer.

- [ ] **Step 6: commit**

```bash
git commit -am "feat(pack): the hook says when the stack is down, once per outage"
```

---

### Task 7 — retire the manual instructions

The plan is not done while the docs still tell people to open four terminals.

**Files:** modify `docs/STATE.md`, `docs/JOIN.md`, `packs/claude-code/INSTALL.md`,
`docs/NPU-RUNBOOK.md` (Phase 3's four-terminal block).

- [ ] **Step 1** — replace the four-terminal blocks with `synapse up`,
keeping the manual commands in a "if you want to see the processes
individually" aside. The runbook's Phase 3 keeps its explicit form: on the
X Elite the point is watching each process, and `--npu` is the flag that
matters there.
- [ ] **Step 2** — `uv run pytest -q` (docs tests exist:
`tests/test_docs_*.py` if present) and a manual read-through.
- [ ] **Step 3: commit**

```bash
git commit -am "docs: synapse up replaces the four-terminal ritual"
```

---

## Scope and estimate

| Task | Agentic estimate | Risk |
|---|---|---|
| 1 · `status` + package | 15 min | low — pure functions, no processes |
| 2 · `up` | 25 min | medium — detachment and rollback are the fiddly parts |
| 3 · `down` | 15 min | low |
| 4 · `--idle-stop` | 20 min | medium — the only task touching a shipped path |
| 5 · slash commands | 10 min | low |
| 6 · hook line | 15 min | low, but it is a hook: must exit 0 on every path |
| 7 · docs | 10 min | low |
| **Total** | **~1h50** | plus a review pass |

Tasks 1–3 are the deliverable. **If time runs short before the demo, stop
after Task 3** — `up`/`down`/`status` alone removes the four-terminal ritual,
and 4–6 are polish. Task 4 is the only one that modifies a path the demo
depends on; if the demo is close, defer it.

## What this deliberately does not do

- **No automatic startup.** No launchd, no systemd, no login item, no
  SessionStart hook that spawns. Every start is typed by a human.
- **No survival across reboots.** `up` starts processes, not services.
- **No remote control.** `up` manages this machine only. The service can be
  *hosted* for the LAN (`--listen 0.0.0.0`), but nothing lets one laptop start
  or stop another's processes.
- **No change to which model runs.** `npu` remains the default on every path.
- **No killing of processes we did not start.** A `foreign` port is reported,
  never resolved.

## Verification the whole plan is done

- [ ] `uv run synapse up --live --distiller claude-cli`, then close that
  terminal entirely; a new terminal's `synapse status` still shows running.
- [ ] `/synapse-up` from inside a Claude Code session brings the stack up and
  `/mcp` shows `synapse` connected, without leaving the session.
- [ ] `synapse down` leaves nothing on 8787, 8899 or 18181.
- [ ] `synapse up` twice in a row starts exactly one orchestrator.
- [ ] With something else on 8787, `synapse up` exits 2 and starts nothing.
- [ ] Full suite green (`uv run pytest -q`), and the count has gone up by the
  number of tests this plan adds — roughly 25.
