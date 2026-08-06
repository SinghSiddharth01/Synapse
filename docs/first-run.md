# First run

Ten minutes on your own machine, on your own. No NPU, no cloud credentials, nothing shared. You end with Claude Code able to call `query` and `contribute` against a Shared Session you created.

If instead you are joining a session a teammate is already hosting, go straight to [JOIN.md](JOIN.md) — the flag that makes that work (`--service-url`) is the one step that cannot be skipped, and skipping it silently gives you a second, empty memory.

> **On Windows or a Snapdragon X Elite box?** Use [JOIN-WINDOWS.md](JOIN-WINDOWS.md) instead. Same journey, PowerShell throughout, and the two ARM64 traps in full: pin `uv` at the ARM64 interpreter (a bare `uv venv` can provision an emulated x86_64 environment and break NPU wheel installs) and do not move `mcp` off `1.9.4` (newer pulls `cryptography`, which has no ARM64-Windows wheel). The `\` line continuations below do not work in PowerShell.

## What you need

- **Python 3.12+** (`pyproject.toml:5`, `requires-python = ">=3.12"`)
- **[`uv`](https://docs.astral.sh/uv/)** — the workspace is six `uv` workspace members under `packages/`
- **[Claude Code](https://docs.anthropic.com/en/docs/claude-code)** — the agent that connects over MCP
- Nothing else. No NPU and no API key are required for this walkthrough; `scripts/serve_local.py` starts a model stand-in for you.

A one-command installer (`install.sh` / `install.ps1`) is being added — use it once it is merged; until then the steps below are the path.

## 1. Get the code running

```bash
git clone https://github.com/SinghSiddharth01/Synapse.git && cd Synapse
uv sync
uv run pytest -q          # expect all green — if not, stop and say so
```

## 2. Secrets (optional — skip on the first run)

Credentials live in `secrets.jsonc` at the repo root. It is gitignored, distributed offline, and **never commit it**. Nothing in this walkthrough needs it:

| block | who reads it | needed for |
|---|---|---|
| `anthropic.api_key` | `serve_local.py:82-92` (falls back to `$ANTHROPIC_API_KEY`) | `--distiller anthropic` |
| `inference_cloud` | `scripts/local_model_server.py` in proxy mode | `--live` |

Keys are only injected into a child process's environment — never printed, never logged. Do not paste one into a chat with an agent either: anything in a transcript is in that transcript for good.

## 3. Clear anything already running

`serve_local` starts **three** processes and `pkill -f synapse-` only matches two; the orphaned model stand-in then blocks the next start with *"ports already in use: 18181"*.

```bash
pkill -f synapse-service; pkill -f synapse-orchestrator; pkill -f local_model_server
```

## 4. Start the stack

```bash
uv run python scripts/serve_local.py \
  --purpose "my first shared session" \
  --contributor <your-name>
```

This starts the service (`:8899`), the model stand-in (`:18181`) and your orchestrator (`:8787`), creates a Shared Session, and then gets out of the way. **What you should see** — five banner lines, roughly:

```
model      stand-in on http://127.0.0.1:18181/v1 (replays this repo's corpus; …)
service    http://127.0.0.1:8899  · dashboard http://127.0.0.1:8899/debug
           reachable on the LAN at http://192.168.x.x:8899 — no auth, …
orchestr.  http://127.0.0.1:8787/mcp  ← point Claude Code here
session    sh-xxxxxxxx  (contributor: <your-name>)
```

followed by the `claude mcp add` line, the log directory (`.synapse/logs`), and — because the service is bound to the LAN — the exact command to hand a teammate, with your IP and shared-id already filled in (`serve_local.py:494-509`). Ctrl-C stops everything.

Two things to read rather than skim:

- The **shared-id** (`sh-…`) is what teammates join. It changes on every restart, because the store is in memory.
- The banner names whichever **model** actually got used. Believe it over what you think you passed.

**Which model does your distilling** — pick the row that matches your machine:

| your situation | what to pass |
|---|---|
| just wiring up, first run | *(nothing — a stand-in answers locally)* |
| `geniex serve` already running on `:18181` | `--npu` |
| Claude on your own API key | `--distiller anthropic --claude-model claude-haiku-4-5-20251001` |
| Claude on your **subscription**, no key at all | `--distiller claude-cli --claude-model haiku` |
| no NPU, no key, read-only | `--listen` |

The two Claude arms want different spellings: `anthropic` takes a full Messages API id, `claude-cli` takes the short alias the binary accepts (`serve_local.py:235-244`). `--claude-model` with `--distiller npu` is refused rather than ignored.

With no flags a stand-in answers locally. It only knows this repo's fixture corpus, so **your own words will not distil into findings** — it returns empty for anything unfamiliar, deliberately, because a stub that invented findings would be lying about which component did the work. Fine for proving the wiring, not for real use. `--live` proxies the seam to the real models in `secrets.jsonc` and costs real requests (~20/hour/key).

## 5. Connect Claude Code

From whatever project you want shared memory in:

```bash
claude mcp add --transport http --scope project synapse http://127.0.0.1:8787/mcp
```

Then start a **new** Claude Code session and **approve** the server when it asks. `claude mcp list` should say `✔ Connected  synapse`; `/mcp` inside the session shows the same. Already registered and showing "failed"? Pick Reconnect in `/mcp`.

Your MCP URL is always your **own** `127.0.0.1:8787`, never the host's. One orchestrator per laptop: theirs stamps their identity onto everything it sends, so pointing at it would credit your findings to them and hide them from you.

You should now have six tools, prefixed `mcp__synapse__` (`server.py:472, 569, 640, 731, 794, 856`):

| tool | what it does |
|---|---|
| `query(question)` | search the team's shared memory |
| `contribute(text)` | push an insight back |
| `create_session(purpose)` | start a new Shared Session |
| `join_session(shared_id)` | join an existing one |
| `leave_session()` | detach this conversation; the session lives on |
| `end_session()` | close it for everyone — creator only, and it refuses while other members are joined |

`serve_local`'s banner still names only `query` and `contribute` (`serve_local.py:491`); the other four are there.

## 6. Install the awareness pack

Without it your agent has the tools but rarely reaches for them unprompted — another loaded skill simply takes over.

```bash
mkdir -p ~/.claude/skills
cp -r packs/claude-code/skills/synapse-shared-memory ~/.claude/skills/
```

User scope, not per-project, so it applies wherever you work. Restart Claude Code. `packs/claude-code/INSTALL.md` also has the freshness hook.

## 7. Check it works

In a new session, push something back without naming the tool:

> "Contribute this to team memory: \<a real thing you just learned\>"

You should get `N finding(s) shared with the team.` Watch it land on `http://127.0.0.1:8899/debug`.

Then ask for it back. **The working memory can lag by up to 60 seconds** — synthesis is debounced to at most one merge per minute (`api.py:48`, `SYNAPSE_MERGE_MIN_INTERVAL_S`). That is a rate limit, not a hang. `POST /v1/sessions/<shared-id>/synthesize` forces a merge now. Retrieval itself reads the Finding Log, so a `query` finds a new Finding before the memory has folded it in.

## Adding a second machine

Hand your teammate the command `serve_local` printed for you — it already carries your LAN IP and shared-id:

```bash
uv run python scripts/serve_local.py \
  --service-url http://<your-lan-ip>:8899 \
  --shared-id sh-xxxxxxxx \
  --contributor <their-name>
```

`--service-url` is what makes it a *join*. Without it, `serve_local` starts a second service on their own `:8899` and creates a private, empty memory — which looks exactly like success: a working stack, no error anywhere. Full walkthrough in [JOIN.md](JOIN.md); Windows in [JOIN-WINDOWS.md](JOIN-WINDOWS.md).

## Letting it tail your conversation (optional)

Steps 1–7 are the **active** path: you call `query` and `contribute`. The **passive** path — a worker reading your transcript and distilling it unasked — is opt-in and off by default. `serve_local` deliberately binds a *scratch* file rather than a real transcript, because "a session transcript may hold secrets that were pasted into a chat" (`serve_local.py:20-27`). To bind a real one:

```bash
uv run synapse-worker join <shared-id> --contributor <your-name>
uv run synapse-worker status     # read the transcript= line
uv run synapse-worker run --interval 15 --ticks 4
```

`transcript=` pointing into `~/.claude/projects/…` means it is on your real conversation; pointing at `.synapse/scratch-transcript.jsonl` means it is tailing nothing. Distillation abstracts but is **not** a redaction guarantee — measured verbatim overlap 0.10.

## When something looks broken

The symptom table in [JOIN.md](JOIN.md#when-something-looks-broken) covers the common failures. The three worth knowing before you hit them:

- **Both tools say "not joined"** — no binding. Re-run step 4.
- **`ports already in use: 18181`** — orphaned stand-in. Step 3.
- **`contribute` says nothing durable was extracted** — the stand-in only knows the fixture corpus. Use `--live`, a real key, or an NPU.
