# Joining the team's Synapse — start here

Ten minutes, six steps. You end up with your coding agent able to ask what
the team already learned, and able to put what *you* learn back.

**What you are joining.** One person (currently Siddsing) hosts the **Synapse
Service** on the LAN — that is the shared memory. Everyone else runs their own
**orchestrator** locally. That split is not a preference: the orchestrator
stamps your identity onto everything it sends, so if you point your agent at
someone else's, your findings get credited to them and hidden from you.

Ask the host for two things before you start: their **service URL** (looks
like `http://192.168.4.44:8899`) and the current **shared-id** (looks like
`sh-bbe76a56` — it changes when they restart, because the store is in memory).

> **On a Snapdragon X Elite / Windows box?** Use
> [`docs/JOIN-WINDOWS.md`](./JOIN-WINDOWS.md) instead — same journey, PowerShell
> throughout, and the ARM64 traps written out in full. The `\` line
> continuations below do not work in PowerShell.

---

## 1. Get the code running

```bash
git clone https://github.com/SinghSiddharth01/Synapse.git && cd Synapse
uv sync
uv run pytest -q          # expect all green — if not, stop and say so
```

**On Windows/ARM64 (the X Elite box), pin the interpreter or you get an
x86 one under emulation and a Rust build error:**

```powershell
uv sync --python "$env:LOCALAPPDATA\Programs\Python\Python312-arm64\python.exe"
```

Do not "upgrade" `mcp` off `1.9.4` — newer pulls `cryptography`, which has no
ARM64-Windows wheel.

## 2. Clear anything already running

`serve_local` starts **three** processes, and `pkill -f synapse-` only matches
two of them — the model stand-in orphans and blocks the next start with *"ports
already in use: 18181"*:

```bash
pkill -f synapse-service; pkill -f synapse-orchestrator; pkill -f local_model_server
```

## 3. Start your half, pointed at the host

```bash
uv run python scripts/serve_local.py \
  --service-url http://<host-ip>:8899 \
  --shared-id <shared-id> \
  --contributor <your-name>
```

**Which model does your distilling** — pick the row that matches your machine:

| your situation | what to pass |
|---|---|
| you have a GenieX NPU box | `--npu` — starts `geniex serve` for you if it is not already up, adopts it if it is, and supervises it either way |
| GenieX for distilling **and** the real 70B for synthesis | `--npu --live` (needs `inference_cloud` credentials) |
| Claude on your own API key | `--distiller anthropic --claude-model claude-haiku-4-5-20251001` |
| Claude on your **subscription**, no key at all | `--distiller claude-cli --claude-model haiku` |
| no NPU, no key, read-only | `--listen` |
| just wiring up | *(nothing — a stand-in answers locally)* |

The two Claude arms want different spellings — `anthropic` takes a full Messages
API id, `claude-cli` takes the short alias the binary accepts. The banner names
whichever model actually got used, so read it rather than assuming.

This starts only what belongs to you: a model seam and your own orchestrator
on `127.0.0.1:8787`. It does **not** start a service — you are using theirs.

With none of those flags a stand-in answers locally. It only knows this repo's
fixture corpus, so **your own words will not distil into findings** — it returns
empty for anything unfamiliar, deliberately, because a stub that invented
findings would be lying about which component did the work. Fine for proving the
wiring, not for real use.

**No NPU, no key, nothing?** Add `--listen`. You still get the arrival
briefing and full `query` — reading needs no model on your side at all,
because the HOST's service does the ranking. Only `contribute` needs a
distiller locally, and it declines politely rather than failing: *"Couldn't
process that right now — your note was not recorded."* Verified against an
orchestrator with nothing whatsoever on its model port. Listening and
learning is the zero-setup path; contributing is what costs you a model.

One machine runs one Synapse. If you are already hosting, you do not need a
second instance to listen — your own orchestrator is already connected. The
script refuses rather than starting one, because a second copy would
overwrite your binding and quietly turn your agent into somebody else.

## 4. Connect Claude Code

From whatever project you want shared memory in:

```bash
claude mcp add --transport http --scope project synapse http://127.0.0.1:8787/mcp
```

Then start a **new** Claude Code session and **approve** the server when it
asks. `claude mcp list` should say `✔ Connected  synapse`.

Note the URL is your **own** localhost, not the host's machine. See the rule
at the bottom.

## 5. Install the awareness pack

Without this your agent has the tools but rarely reaches for them on its own —
another loaded skill will simply take over.

```bash
mkdir -p ~/.claude/skills
cp -r packs/claude-code/skills/synapse-shared-memory ~/.claude/skills/
```

User scope, not per-project, so it applies wherever you work. Restart Claude
Code. (`packs/claude-code/INSTALL.md` also has the freshness hook if you want
signal ③.)

## 6. Check it works

In a new session, ask something the team would know without naming the tool —
for example *"has anyone hit Cirrascale rejecting an API key?"* You should see
it call `synapse` and answer with a teammate's name attached.

Then push something back:

> "Contribute this to team memory: <a real thing you just learned>"

You should get `N finding(s) shared with the team.` Watch it land on the host's
dashboard at `http://<host-ip>:8899/debug`.

---

## Optional: let it tail your conversation

Steps 1–6 give you the **active** path — you call `query` and `contribute`
yourself. The **passive** path, where a worker reads your transcript and distils
it without being asked, is opt-in and off by default.

It is off on purpose. `serve_local` binds a *scratch* file rather than a real
transcript, in its own words, because "a session transcript may hold secrets
that were pasted into a chat". So running `synapse-worker run` straight after
step 3 tails an empty scratch file and produces nothing — you have to bind a
real one first:

```bash
uv run synapse-worker join <shared-id> --contributor <your-name>
uv run synapse-worker status
```

Read the `transcript=` line. That is the whole check:

- points into `~/.claude/projects/...` → it is on your real conversation
- points at `.synapse/scratch-transcript.jsonl` → it is tailing nothing

`agent_session_id` should equal `$CLAUDE_CODE_SESSION_ID`. Then run it bounded,
so you can watch it rather than daemonise it:

```bash
uv run synapse-worker run --interval 15 --ticks 4
tail -f .synapse/relay/findings.jsonl
```

> Distillation abstracts, but it is **not** a redaction guarantee — measured
> verbatim overlap is currently 0.10, up from 0.00. Point this at a conversation
> you would be happy to read aloud.

## Session lifecycle

Four MCP tools sit alongside `query` and `contribute`. The host usually drives
them, but they work from any joined machine:

| tool | what it does |
|---|---|
| `create_session(purpose)` | starts a new Shared Session |
| `join_session(shared_id)` | joins an existing one |
| `leave_session()` | detaches you; the session lives on for everyone else |
| `end_session()` | closes it **for everyone** — creator only, and it refuses while other members are still joined |

**Two different ids are in play**, and mixing them up is the most common
confusion:

- **shared-id** (`sh-…`) — the *team's* session. What everyone joins.
- **agent_session_id** (`$CLAUDE_CODE_SESSION_ID`) — *which of your
  conversations* is connected. It is literally the transcript's filename.

Pass the second explicitly when you have more than one Claude Code window open.
Binding refuses rather than guessing when two transcripts look equally live, so
without it you may simply be told to pick:

```bash
echo $CLAUDE_CODE_SESSION_ID
```

Leaving and rejoining **keeps your place** — the watermark follows your
contributor name, not the conversation, so coming back in a fresh Claude Code
session does not replay everything as new.

Once a session is ended, every route returns `409` — `query`, `contribute`,
pushes and the watermark all stop, and your agent reports it in plain words
rather than erroring.

## Two rules

**Never point Claude Code at the host's `:8787`.** One orchestrator per laptop.
Theirs stamps their binding — their contributor, their agent session — so your
findings would be credited to them and suppressed from you, which is the
opposite of what this system is for. Your MCP URL is always `127.0.0.1:8787`.

**Never commit `secrets.jsonc` or `api-1.json`.** They are gitignored. Do not
paste keys into a chat with an agent either — anything in a transcript is in
that transcript for good, and we are already rotating keys after the 7th for
exactly that reason.

## When something looks broken

| symptom | cause |
|---|---|
| `/mcp` says failed, "unable to connect" | your orchestrator isn't running — step 3 |
| both tools say "not joined" | no binding; re-run step 3 with `--shared-id` |
| `contribute` says "nothing durable extracted" | the stand-in only knows the fixture corpus — you need `--live` or an NPU |
| queries return nothing at all | the host restarted; memory is in-process, so it is genuinely empty |
| agent has the tools but never uses them | the pack from step 5 isn't installed |
| `ports already in use: 18181` | orphaned stand-in from a previous run — step 2 |
| `--npu` says `geniex` is not on PATH | install GenieX and `geniex pull` a model — nothing to start and nothing to adopt |
| `something is holding :18181 but it is not answering` | a GenieX that has already gone idle-dead: kill it and re-run, and this script will launch and supervise its own |
| `SUPERVISOR: GIVING UP` in the terminal | three restarts in ten minutes did not hold — take the fallback the banner names (`--distiller claude-cli`, or drop `--npu`) |
| queries say "Shared memory is DOWN, not empty" | the retrieval backend is not answering. Real outage, not an empty memory — check the `SUPERVISOR:` lines and `.synapse/logs/supervisor.log` |
| everything returns `409` | somebody ended the session |
| the worker runs but nothing is ever distilled | it is bound to the scratch transcript — see "let it tail your conversation" |
| the banner names a model you did not pick | you passed `--claude-model` to the wrong arm, or not at all |

Windows/ARM64: [`docs/JOIN-WINDOWS.md`](./JOIN-WINDOWS.md).
Full detail: [`packs/claude-code/INSTALL.md`](https://github.com/SinghSiddharth01/Synapse/blob/main/packs/claude-code/INSTALL.md).
On-hardware NPU work: [`docs/NPU-RUNBOOK.md`](./NPU-RUNBOOK.md).
