# W8 SCOPE — one-command install/run for macOS and Windows

## 0. Ground truth found in the repo (evidence)

| fact | evidence |
|---|---|
| **No install script of any kind exists today.** `git ls-files` matched no `*.sh`, `*.ps1`, `*.bat`, `*.cmd`, `Makefile`, or `justfile`. Only `.mcp.json` is tracked. | `git ls-files` filter over `sh/ps1/bat/cmd/Makefile/justfile` → only `.mcp.json` |
| The install journey exists only as prose, in three places that must not drift: | `docs/JOIN.md` (bash, 6 steps), `docs/JOIN-WINDOWS.md` (PowerShell, 6 steps), `packs/claude-code/INSTALL.md:24-102`, `README.md:53-94` |
| Workspace is uv, `requires-python = ">=3.12"`, six workspace members, no `.python-version` file (so a bare `uv sync` picks uv's *managed* interpreter — the ARM64 trap) | `/Users/siddharthsingh/Dev/synapse/pyproject.toml:5,15-24`; no `.python-version` in repo root |
| `mcp` is locked at **1.9.4** and must not move | `uv.lock:374-375`; `docs/JOIN-WINDOWS.md:44-48`; `docs/NPU-RUNBOOK.md:16-18` |
| Console entry points the scripts must exercise | `packages/orchestrator/pyproject.toml:33`, `packages/service/pyproject.toml:21`, `packages/worker/pyproject.toml:19` |
| Ports: service 8899, orchestrator 8787, model seam 18181 | `scripts/serve_local.py:53-56`, `:265-267` |
| `serve_local.py` refuses to start if any needed port is held, with an explicit message | `scripts/serve_local.py:147-177` (`claim_ports`), message `"ports already in use: …"` |
| `secrets.jsonc` is gitignored; `.gitignore` **claims** `secrets.example.jsonc` is committed as the template — **it is not tracked**. That template does not exist. | `.gitignore` lines "…`secrets.example.jsonc` (committed) is the template" vs `git ls-files` → no match |
| Secret schema actually read: `inference_cloud.{api_key,base_url,model}` (bare top-level `api_key` also accepted) and `anthropic.api_key`; JSONC comments stripped with `^\s*//.*$` | `scripts/local_model_server.py:448-470`; `scripts/serve_local.py:61-92`; `scripts/rehearse_demo.py:189-198` (also reads `api-1.json`) |
| House rule: **scripts** read `secrets.jsonc`, **packages** read the environment | `scripts/serve_local.py:64-68` |

## 1. What an install script must cover, per OS

Same eight phases both OSes; the differences are called out.

**P1 — prerequisites (check, then offer to install; never silently install)**
- `git` — presence check only.
- `uv` — check `uv --version`. Install if absent: macOS `curl -LsSf https://astral.sh/uv/install.sh | sh`; Windows `irm https://astral.sh/uv/install.ps1 | iex`. After install, re-probe `~/.local/bin/uv` / `$env:USERPROFILE\.local\bin\uv.exe` because `PATH` in the current shell is stale.
- **Python 3.12+**: macOS may rely on uv's managed interpreter. **Windows must not.** On ARM64 the script must locate a native ARM64 interpreter and pass it explicitly, or uv provisions an x86_64-under-Prism venv and NPU wheels + `cryptography` fail with a Rust build error that looks unrelated (`docs/JOIN-WINDOWS.md:24-31`, `docs/NPU-RUNBOOK.md:14-18`).
  - Detect: `$env:PROCESSOR_ARCHITECTURE -eq 'ARM64'`; then resolve via `Get-ChildItem "$env:LOCALAPPDATA\Programs\Python" -Directory` looking for `Python31*-arm64`, and *verify* with `& $py -c "import platform;print(platform.machine())"` → must print `ARM64`. Do not trust the directory name.
  - Non-ARM Windows: plain `uv sync` is fine.
- `claude` CLI — required for MCP registration and for `--distiller claude-cli`. Presence check; if missing, skip P5 with a printed manual command rather than failing the run.
- Optional: `geniex` on PATH (only gates the `--npu` arm).

**P2 — repo**
- If run from inside a checkout, use it. If run standalone (`curl … | sh` from GitHub), `git clone https://github.com/SinghSiddharth01/Synapse.git` into `./Synapse` and `cd`.
- Must be idempotent: existing checkout → `git pull --ff-only`, never a clobber.

**P3 — dependency sync**
- macOS/Linux: `uv sync`
- Windows ARM64: `uv sync --python "<resolved-arm64-python.exe>"`
- Then assert `mcp==1.9.4` post-sync: `uv run python -c "import importlib.metadata as m;print(m.version('mcp'))"` and **fail loudly** if it moved. This is the single highest-cost regression on the Windows box (`cryptography` has no ARM64-Windows wheel).

**P4 — secrets**
- Write `secrets.example.jsonc` **into the repo as a tracked file** (this is a W8 deliverable — the `.gitignore` already promises it and it is missing). Shape, matching the readers:
  ```jsonc
  {
    // Cirrascale / Cloud AI 100 — synthesis
    "inference_cloud": { "api_key": "", "base_url": "", "model": "Llama-3.3-70B" },
    // Optional: Claude distiller arm (--distiller anthropic)
    "anthropic": { "api_key": "" }
  }
  ```
- Installer copies example → `secrets.jsonc` **only if absent**, never overwrites, then prints where to paste keys. On Windows, write with UTF-8 explicitly (see §2).
- Verify `secrets.jsonc` is ignored before proceeding: `git check-ignore -q secrets.jsonc` must exit 0; if not, abort with a message. This is a real guard, not ceremony — the repo has an explicit "never commit secrets.jsonc" rule in both join docs (`docs/JOIN.md:215`, `docs/JOIN-WINDOWS.md:218`).
- The installer must never echo a key, never pass one on a command line, and never write one into a log.

**P5 — MCP registration**
- `claude mcp add --transport http --scope project synapse http://127.0.0.1:8787/mcp` (`packs/claude-code/INSTALL.md:40`, `README.md:83`).
- The URL is always the operator's **own** `127.0.0.1:8787`, never the host's — one orchestrator per laptop or attribution is stamped wrong (`scripts/serve_local.py:494-509`, `docs/JOIN.md` "Two rules").
- Registration targets the *project the teammate wants shared memory in*, not the Synapse checkout. Script takes `--project <path>` (default: cwd if it is not the Synapse repo, else prompt) and runs `claude mcp add` from there. Idempotent: `claude mcp list` first; if `synapse` is present, skip and print "already registered — pick Reconnect in `/mcp` if it shows failed".
- Awareness pack (`docs/JOIN.md:118-127`, `JOIN-WINDOWS.md:120-131`): copy `packs/claude-code/skills/synapse-shared-memory` to user scope — macOS `~/.claude/skills/`, Windows `$env:USERPROFILE\.claude\skills\`. Optionally the freshness hook + `settings-snippet.json` merge into the target project's `.claude/settings.json` (a merge, not an overwrite — `packs/claude-code/INSTALL.md:93-98`).

**P6 — port hygiene**
- Pre-clear orphans before start; `serve_local` starts three processes and the stand-in orphans on 18181.
  - macOS: `pkill -f synapse-service; pkill -f synapse-orchestrator; pkill -f local_model_server`
  - Windows: the `Get-CimInstance Win32_Process | Where-Object CommandLine -match …| Stop-Process -Force` form in `docs/JOIN-WINDOWS.md:54-58`, then confirm with `Get-NetTCPConnection -LocalPort 8787,8899,18181 -State Listen`.
- Do this only behind an explicit flag (`--clean`) or an interactive confirm; killing a teammate's running host by surprise is worse than a clear error.

**P7 — start**
- Host: `uv run python scripts/serve_local.py --purpose "<purpose>"`
- Joiner: `uv run python scripts/serve_local.py --service-url http://<host-ip>:8899 --shared-id <sh-…> --contributor <name>`
- Model arm, passed through verbatim by the installer: `--npu` | `--distiller anthropic --claude-model claude-haiku-4-5-20251001` | `--distiller claude-cli --claude-model haiku` | `--listen` | nothing (fixture stand-in). Table at `docs/JOIN.md:62-70`.
- The installer must **not** re-implement any of this. `serve_local.py` already claims ports, waits on health, adopts the host session, writes the binding, and prints the copy-paste teammate command (`scripts/serve_local.py:265-509`). W8's job is to get to that line reliably, not to replace it.
- Default for a live demo to a joining teammate: run it **foreground** in its own terminal window so the operator can Ctrl-C. If backgrounded, tee to `.synapse/logs/` (which `spawn` already uses, `scripts/serve_local.py:180-186`).

**P8 — verify** — see §4.

## 2. The Windows Unicode failure — evidence, and where the fix belongs

**This is real, currently unfixed on the primary entry point, and it is the first thing a Windows teammate would see.**

Windows writes piped/redirected stdout in the locale codepage (cp1252 on the X Elite), not UTF-8. Any codepoint with no cp1252 mapping raises `UnicodeEncodeError` **before the first line of output**, so the process looks like it died for an unrelated reason.

Already fixed, per-file, with an inline guard:
- `scripts/run_npu_eval.py:30-40` — the origin comment: the runbook's own `| tee` into `.measurements/` "died with `UnicodeEncodeError` before the canary printed a single line". Explicitly says it chose `sys.stdout.reconfigure` over `PYTHONUTF8`/`PYTHONIOENCODING` to keep the documented invocation correct with no env setup.
- `scripts/verify_orchestrator.py:38-44`, `scripts/trace_one.py:24-30` — same guard, same reason.
- `packages/worker/src/synapse_worker/cli.py:322` — an em dash was mangled on the X Elite; that line was forced to plain ASCII.
- `scripts/rehearse_demo.py:577-581` — `Path.write_text` pinned to `encoding="utf-8"` because the locale default is cp1252 and cannot encode service-supplied topic labels.

**Still unguarded, and on the one-command path** (scan of every `scripts/*.py` and `packages/*/src/**/*.py` for non-cp1252 codepoints outside comments):

| file | codepoint | line | guard present |
|---|---|---|---|
| **`scripts/serve_local.py`** | `←` U+2190 | **482** (`"orchestr.  …/mcp  ← point Claude Code here"`) | **no** |
| `scripts/demo_local.py` | `─` U+2500, `↳` U+21B3, `→` U+2192 | 91, 104, 313, 545-546 | no |
| `packages/worker/src/synapse_worker/debug_server.py` | `─`, `→` | 91, 153, 393 | no |
| `packages/service/src/synapse_service/debug.py` | `─`, `→` | 242, 530 | no |
| `packages/worker/src/synapse_worker/compaction.py` | `⋯` U+22EF | 172-173 (`_OMISSION_MARKER`) | no |
| `packages/distiller/src/synapse_distiller/fixtures.py` | `⚠️` | 7 | no |
| `scripts/calibrate_prompt.py`, `scripts/demo_say.py` | `⚠️`, `→` | 74; 145,162,175 | no |

So: `uv run python scripts/serve_local.py > install.log 2>&1` on the X Elite crashes at line 482 — the exact line telling the teammate where to point Claude Code. `serve_local.py` prints it, and it is the last useful thing before the process idles. A demo where the operator redirects output (or the installer captures it, which an installer must) fails at the finish line.

**Spec the fix into the install path — three layers, all of them:**

1. **Installer sets the env for every child it spawns.** `.ps1` sets `$env:PYTHONUTF8 = "1"` (and `$env:PYTHONIOENCODING = "utf-8"` as belt-and-braces for pre-3.7 semantics / non-CPython) *before* the first `uv run`, and passes it down. `PYTHONUTF8=1` is the right lever because it fixes both stdout encoding **and** the default `open()`/`Path.read_text()` encoding, which is the second half of the bug class (`rehearse_demo.py:579`).
2. **Console codepage.** `chcp 65001` (or `[Console]::OutputEncoding = [Text.UTF8Encoding]::new()`) at the top of the `.ps1`, so the *interactive* window renders the characters rather than mojibake. `PYTHONUTF8` fixes the encode; the codepage fixes the display. Both are needed for a live demo on a projector.
3. **Source guard on `serve_local.py`** (and `demo_local.py`), matching the existing house pattern verbatim so it reads as one decision, not two:
   ```python
   if hasattr(sys.stdout, "reconfigure"):
       sys.stdout.reconfigure(encoding="utf-8", errors="replace")
   if hasattr(sys.stderr, "reconfigure"):
       sys.stderr.reconfigure(encoding="utf-8", errors="replace")
   ```
   This is load-bearing because `serve_local.py` is documented as a *direct* invocation in `README.md:77`, `docs/JOIN.md:53`, `docs/JOIN-WINDOWS.md:75` — people will run it without the installer. The env var alone would only protect the installer's own path. `run_npu_eval.py:35-36` already states this rationale as policy.
   - Cheaper alternative for `serve_local.py:482` specifically: replace `←` with `<-`. Do **both** — ASCII the arrow *and* add the guard — because the guard also covers anything a teammate's contributor name or a service-supplied topic label drags in.
4. **Repo-wide belt:** add `PYTHONUTF8 = "1"` under `[tool.pytest.ini_options] env` is not available without a plugin; instead the installer exports it, and W8 should note (not fix) that `packages/service/.../debug.py` and `worker/debug_server.py` emit box-drawing into HTML — harmless in a browser, fatal only if those strings ever reach a redirected console.

**Windows-specific bugs in the same family, already fixed, to not regress:** `rehearse_demo.py:585-587` — `rm -rf` is not a Windows command and raised `FileNotFoundError` out of a `finally`, replacing real failures with a traceback. Any new install script must use PowerShell-native removal (`Remove-Item -Recurse -Force -ErrorAction SilentlyContinue`), never shell out to POSIX tools.

## 3. What "one command" literally is

**macOS / Linux — `install.sh` at repo root** (POSIX `sh`, works under `curl | sh`):
```bash
curl -LsSf https://raw.githubusercontent.com/SinghSiddharth01/Synapse/main/install.sh | sh
```
and, from a checkout:
```bash
./install.sh --purpose "demo session"                       # host
./install.sh --service-url http://192.168.4.44:8899 \
             --shared-id sh-bbe76a56 --contributor akhil    # joiner
```

**Windows — `install.ps1` at repo root** (`.ps1`, not `.bat`; the existing Windows runbook is PowerShell throughout and `.bat` cannot do the ARM64 interpreter probe or `Get-NetTCPConnection`):
```powershell
irm https://raw.githubusercontent.com/SinghSiddharth01/Synapse/main/install.ps1 | iex
```
and, from a checkout:
```powershell
powershell -ExecutionPolicy Bypass -File .\install.ps1 -SharedId sh-bbe76a56 -ServiceUrl http://192.168.4.44:8899 -Contributor akhil -Npu
```
Ship a **two-line `install.bat`** whose only job is `powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0install.ps1" %*` — double-clickable, and it removes the execution-policy question from the live demo. That is the accessibility point the rubric is buying.

**Shared flag surface (identical names, OS-idiomatic casing):** `--purpose/-Purpose`, `--shared-id/-SharedId`, `--service-url/-ServiceUrl`, `--contributor/-Contributor`, `--npu/-Npu`, `--listen/-Listen`, `--distiller/-Distiller`, `--claude-model/-ClaudeModel`, `--project/-Project`, `--clean/-Clean`, `--doctor-only/-DoctorOnly`, `--no-start/-NoStart`.

Every flag not consumed by the installer passes through to `serve_local.py` unchanged, so the two never drift.

## 4. What must be verifiable at the end — `scripts/doctor.py`

A single Python script (one implementation, both OSes — that is the whole reason to make it Python rather than duplicating checks in sh and ps1), invoked by both installers as the last step and runnable standalone: `uv run python scripts/doctor.py`. It carries the same `sys.stdout.reconfigure` guard.

Checks, each printing PASS/FAIL and a one-line remedy, exit non-zero on any FAIL:

1. **`uv` present**, version printed.
2. **Interpreter**: `sys.version_info >= (3,12)`; on Windows also `platform.machine()` — FAIL with the Prism-emulation remedy if it is not `ARM64` on an ARM64 host.
3. **`mcp` version == `1.9.4`** — remedy: "do not upgrade; re-sync from `uv.lock`".
4. **Unicode canary** — print a line containing `←  →  ─  ⋯  ⚠` and confirm no exception. This is the regression test for §2 and it must run *through the installer's own redirection*, not just to a TTY, or it proves nothing. On Windows, doctor also reports `sys.stdout.encoding` and `PYTHONUTF8`.
5. **Ports** 8787 / 8899 / 18181 — report listening/free with owning PID, so "ports already in use: 18181" never has to be diagnosed by hand (`scripts/serve_local.py:171-177`).
6. **Secrets** — `secrets.jsonc` parses after comment-stripping; report *which blocks are present* (`inference_cloud`, `anthropic`) and **never the values**; confirm `git check-ignore` says it is ignored.
7. **Service reachable** — `GET <service-url>/debug` 200 (`scripts/serve_local.py:308,349`).
8. **Orchestrator serving MCP** — a real MCP client `initialize()` against `http://127.0.0.1:8787/mcp` that asserts the server's `instructions` contain `SENTINEL`. `scripts/verify_instructions.py` already does exactly this (`scripts/verify_instructions.py:19-28`) — doctor should reuse it, not reimplement. This proves signals ① and ② are live, which is the actual product claim.
9. **Tools listed** — the client sees `query` and `contribute` (plus the four lifecycle tools: `create_session`, `join_session`, `leave_session`, `end_session`, per `docs/JOIN.md` lifecycle table).
10. **Binding** — `.synapse/bindings/claude-code.json` exists, and its `shared_id` matches the session just created/joined, and `contributor` matches what was passed. Catches the "second serve_local overwrote your binding" failure (`scripts/serve_local.py:147-157`).
11. **Session live** — `GET <service-url>/debug/stats.json` lists the shared_id; report member count. (409 everywhere = someone ended it.)
12. **Round trip** — `POST /v1/sessions/<id>/query` returns 200. Note explicitly in the output that *your own findings are suppressed for you* (invariant 3, `docs/NPU-RUNBOOK.md:95-97`), so "0 results" from your own contributor is a PASS, not a failure. Getting this wrong will read as a broken install on stage.
13. **Awareness pack** — `~/.claude/skills/synapse-shared-memory/` (or `%USERPROFILE%\.claude\skills\…`) exists; report whether the freshness hook is wired into the target project's `.claude/settings.json`.
14. **`claude mcp list`** shows `synapse` connected, if the `claude` binary is present.

`--doctor-only` runs 1-6 and 13-14 without needing a running stack, so a teammate can diagnose before starting anything.

**The live-demo acceptance criterion**: on a clean machine with only `git` and a browser, one pasted command ends with doctor printing all-PASS and the `serve_local` banner naming the shared-id and MCP URL — and the joining teammate's Claude Code answers a question with a teammate's name attached (`docs/JOIN.md:130-140`).

## 5. Explicit NON-goals

- **Not a rewrite of `serve_local.py`.** No process supervision, no port claiming, no session adoption, no health-waiting in the installer. All of it exists (`scripts/serve_local.py:132-177, 265-509`) and duplicating it creates two sources of truth for the startup contract.
- **Not a packaging change.** No PyPI publish, no `pipx`, no Docker, no systemd/launchd/Windows Service, no auto-start on boot.
- **Not NPU provisioning.** The script never installs GenieX, qairt bundles, or GGUF models, and never runs `geniex serve`. It only *detects* whether something answers on `:18181` and passes `--npu` through — mirroring `serve_local.py:281-285`, which already fails with the right message.
- **Not secret distribution.** The installer creates an empty `secrets.jsonc` from a template and tells the operator where to paste. It never fetches, prompts-and-stores, or syncs a key. Cirrascale keys stay offline-distributed (`docs/NPU-RUNBOOK.md:99-103`).
- **Not a fix for the awareness-pack multi-window binding ambiguity** (`packs/claude-code/INSTALL.md:133-161`). Doctor may *report* an `agent_session_id` mismatch; resolving it is out of scope.
- **Not Linux-first.** `install.sh` should work on Linux incidentally, but macOS is the tested target; no distro package-manager branches.
- **Not a Codex install path.** `CodexSource` provenance is unproven against a live transcript (`docs/NPU-RUNBOOK.md:117-122`).
- **Not CI.** No GitHub Actions workflow to run the installer; W8 delivers the scripts and doctor, not the pipeline.
- **Not a repo-wide Unicode purge.** Fix the guard on the entry points on the run path (`serve_local.py`, `demo_local.py`, `doctor.py`) and set `PYTHONUTF8` for children. The box-drawing inside `debug.py` / `debug_server.py` HTML is browser-bound and stays.

## 6. Deliverable file list

| path | purpose |
|---|---|
| `install.sh` | macOS/Linux one-command entry, POSIX sh, `curl \| sh`-safe |
| `install.ps1` | Windows entry; ARM64 probe, `PYTHONUTF8`/`chcp 65001`, PowerShell-native cleanup |
| `install.bat` | 2-line shim → `install.ps1`, double-clickable, bypasses execution policy |
| `scripts/doctor.py` | the 14 checks in §4; reuses `verify_instructions.py`'s MCP probe |
| `secrets.example.jsonc` | **tracked** template `.gitignore` already promises but does not have |
| edit `scripts/serve_local.py` | stdout/stderr `reconfigure` guard + ASCII the `←` at line 482 |
| edit `scripts/demo_local.py` | same guard |
| edit `README.md`, `docs/JOIN.md`, `docs/JOIN-WINDOWS.md` | lead with the one command; keep the manual steps below it as the fallback the three docs already are |