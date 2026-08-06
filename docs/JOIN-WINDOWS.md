# Joining Synapse from a Snapdragon X Elite (Windows/ARM64)

Copy-paste runbook for the NPU machines. Everything here is **PowerShell** —
`docs/JOIN.md` is the same journey in bash, and the two are meant to stay in
step. If you are on macOS or Linux, use that one instead.

PowerShell continues a line with a backtick `` ` ``, not a backslash. Every
command below is already written that way, so it pastes as-is.

**Ask the host for two things first:** their service URL (looks like
`http://192.168.4.44:8899`) and the current shared-id (looks like
`sh-bbe76a56`). The shared-id **changes when they restart** — the store is in
memory — so get it fresh, not from yesterday's message.

---

## 1. Get the code running

```powershell
git clone https://github.com/SinghSiddharth01/Synapse.git
cd Synapse
```

**Pin the ARM64 interpreter.** A bare `uv sync` silently builds an x86_64 venv
under Prism emulation, where the NPU wheels cannot install and you get a Rust
build error that looks like something else entirely:

```powershell
uv sync --python "$env:LOCALAPPDATA\Programs\Python\Python312-arm64\python.exe"
```

If that path is wrong on your box, find it:

```powershell
Get-ChildItem "$env:LOCALAPPDATA\Programs\Python" -Directory | Select-Object Name
```

Then check the suite:

```powershell
uv run pytest -q          # expect all green — if not, stop and say so
```

> **Do not upgrade `mcp` off `1.9.4`.** Every version from `1.9.4` through
> `1.29.0`, and all of `2.x`, pulls `pyjwt[crypto]` → `cryptography`, which has
> no ARM64-Windows wheel and fails building from source. If something offers to
> bump it, decline.

## 2. Clear anything already running

`serve_local` starts **three** processes and the model stand-in orphans easily,
then blocks the next start with *"ports already in use: 18181"*.

```powershell
Get-CimInstance Win32_Process |
  Where-Object { $_.CommandLine -match 'synapse-service|synapse-orchestrator|local_model_server' } |
  ForEach-Object { Stop-Process -Id $_.ProcessId -Force }
```

Confirm the ports are actually free:

```powershell
Get-NetTCPConnection -LocalPort 8787,18181 -State Listen -ErrorAction SilentlyContinue |
  Select-Object LocalPort, OwningProcess
```

No output means free.

## 3. Start your half, pointed at the host

You run an orchestrator and a model seam. You do **not** run a service — you
are using the host's.

```powershell
uv run python scripts/serve_local.py `
  --service-url http://<HOST-IP>:8899 `
  --shared-id <SHARED-ID> `
  --contributor <your-name> `
  --npu
```

`--npu` means *"a real model is already serving on :18181, don't start the
stand-in"* — so start `geniex serve` **first**. Without it the script exits
telling you nothing is listening there.

| your situation | what to pass |
|---|---|
| `geniex serve` is running | `--npu` |
| no NPU yet, just wiring up | *(nothing — a stand-in answers locally)* |
| no NPU, no key, read-only | `--listen` |

The stand-in only knows this repo's fixture corpus, so **your own words will not
distil into findings** — it answers empty for anything unfamiliar. That is
deliberate: a stub that invented findings would be lying about which component
did the work. It is fine for proving the wiring, not for real use.

`--listen` is the zero-setup path: the arrival briefing and `query` work fully,
because the **host's** service does the ranking. Only `contribute` needs a model
on your machine, and it declines politely rather than crashing.

## 4. Connect Claude Code

From whatever project you want shared memory in:

```powershell
claude mcp add --transport http --scope project synapse http://127.0.0.1:8787/mcp
```

Start a **new** Claude Code session and **approve** the server when asked.

```powershell
claude mcp list           # expect: ✔ Connected  synapse
```

> The URL is your **own** `127.0.0.1`, never the host's. One orchestrator per
> laptop. Theirs stamps *their* identity onto everything it sends, so pointing
> at it would credit your findings to them and hide them from you — the exact
> opposite of the point.

## 5. Install the awareness pack

Without it your agent has the tools but rarely reaches for them — another loaded
skill simply takes over.

```powershell
New-Item -ItemType Directory -Force -Path "$env:USERPROFILE\.claude\skills" | Out-Null
Copy-Item -Recurse -Force `
  packs\claude-code\skills\synapse-shared-memory `
  "$env:USERPROFILE\.claude\skills\"
```

User scope, not per-project, so it applies wherever you work. Restart Claude
Code afterwards.

## 6. Check it works

Ask something the team would know, without naming the tool:

> *"Has anyone hit Cirrascale rejecting an API key?"*

You should see it call `synapse` and answer **with a teammate's name attached**.
Then push something back:

> *"Contribute this to team memory: &lt;a real thing you just learned&gt;"*

Expect `N finding(s) shared with the team.` Watch it land on the host's
dashboard at `http://<HOST-IP>:8899/debug`.

---

## Optional: let it tail your conversation

Steps 1–6 give you the **active** path — you call `query` and `contribute`
yourself. The **passive** path, where a worker reads your transcript and distils
it automatically, is opt-in and off by default.

It is off on purpose: `serve_local` binds a *scratch* file rather than a real
transcript, because a session transcript can hold secrets someone pasted into a
chat. Turning this on points a model at your actual conversation.

```powershell
uv run synapse-worker join <SHARED-ID> --contributor <your-name>
uv run synapse-worker status
```

Read the `transcript=` line in the output — that is the whole check:

- points into `...\.claude\projects\...` → it is on your real conversation
- points at `.synapse\scratch-transcript.jsonl` → it is tailing nothing

`agent_session_id` should equal `$env:CLAUDE_CODE_SESSION_ID`. Then run it
bounded so you can watch rather than daemonise:

```powershell
uv run synapse-worker run --interval 15 --ticks 4
Get-Content .synapse\relay\findings.jsonl -Tail 20 -Wait
```

> Distillation abstracts, but it is **not** a redaction guarantee — measured
> verbatim overlap is currently 0.10, up from 0.00. Point this at a conversation
> you would be happy to read aloud.

## Session lifecycle (the host usually drives this)

Four MCP tools sit alongside `query` and `contribute`:

| tool | what it does |
|---|---|
| `create_session(purpose)` | starts a new Shared Session |
| `join_session(shared_id)` | joins an existing one |
| `leave_session()` | detaches you; the session lives on |
| `end_session()` | closes it **for everyone** — creator only |

Two different ids are in play and it is worth keeping them straight:

- **shared-id** (`sh-…`) — the *team's* session. What everyone joins.
- **agent_session_id** (`$env:CLAUDE_CODE_SESSION_ID`) — *which of your
  conversations* is connected. It is literally the transcript filename.

Pass the second one explicitly when you have more than one Claude Code window
open, or binding will refuse rather than guess which one you meant:

```powershell
echo $env:CLAUDE_CODE_SESSION_ID
```

Once a session is ended, every route returns `409` — `query`, `contribute`,
pushes and the watermark all stop, and your agent will say so in plain words
rather than erroring.

---

## Two rules

**Never point Claude Code at the host's `:8787`.** Your MCP URL is always
`127.0.0.1:8787`.

**Never commit `secrets.jsonc`.** It is gitignored. Do not paste keys into a
chat with an agent either — anything in a transcript is in that transcript for
good.

## When something looks broken

| symptom | cause |
|---|---|
| `ports already in use: 18181` | orphaned stand-in — step 2 |
| `--npu given but nothing is serving on :18181` | start `geniex serve` first, or drop `--npu` |
| Rust build error during `uv sync` | x86 interpreter under emulation — pin ARM64, step 1 |
| `cryptography` fails to build | something bumped `mcp` off `1.9.4` |
| `/mcp` says failed, "unable to connect" | your orchestrator isn't running — step 3 |
| both tools say "not joined" | no binding; re-run step 3 with `--shared-id` |
| `contribute` says "nothing durable extracted" | the stand-in only knows the fixture corpus — you need the NPU |
| queries return nothing at all | the host restarted; memory is in-process, so it is genuinely empty |
| everything 409s | somebody ended the session |
| agent has the tools but never uses them | the pack from step 5 isn't installed |

Bash version: [`docs/JOIN.md`](./JOIN.md) · On-hardware NPU bring-up:
[`docs/NPU-RUNBOOK.md`](./NPU-RUNBOOK.md)
