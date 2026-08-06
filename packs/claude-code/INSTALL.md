# Installing the Claude Code awareness pack

This directory (`packs/claude-code/`) is a shipped artifact — copy it onto
a teammate's machine and wire it into *their* project's Claude Code
configuration. It is never installed into this repo's own `.claude/`; that
directory does not exist here on purpose.

The pack gives Claude Code two of the four awareness signals
(`docs/architecture.html#awareness`) that don't come for free from the MCP
protocol itself:

- **signal ③, the freshness pointer** — `hooks/freshness_pointer.py`, a
  `UserPromptSubmit` hook that speaks one line on the first turn after
  shared memory changes, then stays silent.
- **signal ④, the relevance trigger** — `skills/synapse-shared-memory/`, a
  skill that auto-loads when the work looks like something a teammate may
  already have been through.

(Signals ① and ② — ambient tool descriptions and the arrival briefing — need
no pack at all; they ride the MCP `instructions` field and are already live
the moment `synapse-orchestrator` is running and this project has joined a
Shared Session with `synapse-worker join <shared_id>`.)

## Connecting Claude Code to the orchestrator (do this first)

The pack below adds signals ③ and ④. Signals ① and ② need only the MCP
connection itself — and that connection has to be registered with Claude Code
before any of it is reachable.

This repo ships a project-scoped [`.mcp.json`](../../.mcp.json):

```json
{"mcpServers": {"synapse": {"type": "http", "url": "http://127.0.0.1:8787/mcp"}}}
```

Copy it to the root of whatever project you want shared memory in, or run the
equivalent from that project:

```bash
claude mcp add --transport http --scope project synapse http://127.0.0.1:8787/mcp
```

`--scope project` writes `.mcp.json`, which is committable — everyone who
clones the repo gets the connection. `--scope user` (in `~/.claude.json`)
covers every project on your machine instead and is not shared.

Then **start a new Claude Code session** — the file is read at session start,
not live — and **approve the server** when prompted. Project-scoped servers
are gated on approval by design, so a cloned repo cannot wire an agent to a
server you never agreed to. Approval is remembered per project.

Verify with `claude mcp list` (expect `✔ Connected  synapse`) or `/mcp` inside
a session. The tools appear as `mcp__synapse__query` and
`mcp__synapse__contribute`.

**Three things that will bite you if you skip them:**

- **Join first.** Until `synapse-worker join <shared_id>` has been run from a
  terminal in that project, both tools return "not joined" rather than
  erroring. The orchestrator re-reads the binding on every single tool call,
  so joining *after* Claude Code is already running takes effect on the next
  call — no restart.
- **`contribute` needs a model.** It distils your prose through the same NPU
  seam the worker uses (`SYNAPSE_BASE_URL`). On the X Elite that is
  `geniex serve`; on a machine without an NPU, point it at
  `scripts/local_model_server.py`. With nothing there it fails soft — your
  note is not recorded and it says so.
- **One binding per Agent product per machine.** Every Claude Code session on
  the laptop shares the one `bindings/claude-code.json`, so they are all in
  the same Shared Session. Two projects in two different Shared Sessions at
  once is not something this supports today.

## Prerequisites

- `synapse-orchestrator` reachable at its default `http://127.0.0.1:8899`
  Synapse Service URL. If you've customized `--service-url` (or the Synapse
  Service's own port), see **Custom service URL** below.
- This project has joined a Shared Session: `synapse-worker join
  <shared_id>` has been run at least once from a terminal in this project's
  root. Until then, the hook stays silent (nothing to check yet) and the
  skill's tools say so plainly rather than erroring.

## Install

Two locations work, and the repo uses both. Know which one you are in:

| | skill goes to | hook goes to | who does it |
|---|---|---|---|
| **Per project** (this section) | `<project>/.claude/skills/` | `<project>/.claude/synapse-pack/hooks/` | you, by hand |
| **Per user** | `~/.claude/skills/` | *(not installed)* | `install.sh` / `install.ps1`, phase P5 |

`install.sh` copies `skills/`, `commands/` and `agents/` into `~/.claude/`, so
the skill is available in every project on the machine without repeating this
section. It deliberately does **not** install the hook and does **not** touch
`settings.json`: `settings-snippet.json`'s command is
`$CLAUDE_PROJECT_DIR/.claude/synapse-pack/hooks/freshness_pointer.py`, which is
per-project by construction, and rewriting a settings file the installer does
not own is an overwrite risk. If you want the hook, do the `hooks` half of the
steps below even after running the installer — the installer prints the two
commands for exactly that. `scripts/doctor.py`'s `awareness` check looks at
`~/.claude/skills/synapse-shared-memory`, i.e. the per-user row; a per-project
install is invisible to it and that WARN is then expected.

From this project's root:

```bash
mkdir -p .claude/skills .claude/synapse-pack
cp -r <path-to-synapse-repo>/packs/claude-code/skills/synapse-shared-memory .claude/skills/synapse-shared-memory
cp -r <path-to-synapse-repo>/packs/claude-code/hooks .claude/synapse-pack/hooks
```

Then merge `settings-snippet.json`'s contents into your project's
`.claude/settings.json` (create the file if it doesn't exist yet). If you
already have other `UserPromptSubmit` hooks configured, add this pack's
entry to the existing `hooks.UserPromptSubmit` array rather than
overwriting it — the format is a list precisely so several hooks can sit
side by side.

Restart Claude Code (or start a new Agent Session) so it picks up the new
`settings.json` and the new skill. No orchestrator restart is needed — the
skill and hook both talk to whatever is running right now.

## Verify

Ask a teammate to push a finding (or push one yourself with
`mcp__synapse__contribute` from another joined Agent Session), then submit any
prompt in this project. If the shared-memory version moved since the last
time this hook checked, your next prompt gets a line like:

    Synapse: shared memory for this Shared Session moved to v4 (2 new since
    last checked). Call the `query` tool if this is relevant to what
    you're doing. Topics: prompt caching.

Nothing printing is the expected, common case — silence is signal ③'s
whole design, not a sign the hook isn't running. To confirm the hook itself
is wired up rather than just quiet, run it directly:

```bash
python3 .claude/synapse-pack/hooks/freshness_pointer.py; echo "exit: $?"
```

`exit: 0` with no output either way (nothing to report, or nothing joined
yet) is correct — see **Fail-open, by design** below.

Note that this direct-run command cannot reproduce the multi-window
mismatch described next: run from a terminal, its stdin is a TTY, and the
hook treats a TTY exactly like "no session_id available" — the same
fallback as before conversation-scoping existed. Use it to confirm the
hook is wired up and reaching the service at all; it will not tell you
*which* Claude Code window, if any, it is currently speaking for.

## Troubleshooting: the pointer stays silent in one window but not another

The hook only ever speaks for the Claude Code conversation whose own
`session_id` (piped to it on stdin by Claude Code) matches the
`agent_session_id` recorded in `.synapse/bindings/claude-code.json` when
you last ran `synapse-worker join` — every other window's invocation
returns before it even reaches the network, by design (a window that
never joined must not receive shared-memory content, and must not consume
the joined window's own pending notice).

`synapse-worker join` binds *whichever* Claude Code Agent Session it finds live
at the moment you run it. If two Claude Code windows on the same project
are open when you join, the binding can end up pointing at a window other
than the one you're actually working in — and from then on the pointer is
permanently silent in your window, with no error, because a mismatch and
"nothing changed yet" produce identical (empty) output. This gets more
likely the more windows you have open on one project at once.

If you're confident shared memory has moved (a teammate confirms it, or
`query` returns findings you haven't seen) but your prompts never surface
a notice, the fix is to re-join **from a terminal inside the window you
are actually using**:

```bash
synapse-worker join <shared_id>
```

This rebinds `claude-code.json` to whichever Agent Session is live in *that*
terminal, which fixes the mismatch for that window going forward.

## Custom service URL

If `synapse-orchestrator` was started with a non-default `--service-url`,
set the same value for the hook via an environment variable Claude Code
will pass through to hook subprocesses — export it in your shell profile,
or add an `"env"` block to `.claude/settings.json`:

```json
{
  "env": { "SYNAPSE_SERVICE_URL": "http://127.0.0.1:9000" }
}
```

## Fail-open, by design

This hook never blocks a prompt and never surfaces an error to you. If the
Synapse Service is down, slow, or returns something unexpected, or this
project hasn't joined a Shared Session, the hook exits `0` with empty
output — indistinguishable, from Claude Code's side, from "nothing new to
report." That is intentional: a memory feature that can interrupt or break
a coding session is worse than no memory feature. If you suspect the hook
isn't reaching the service at all, use the direct-run command above and
check your shell's own network access to the service URL — the hook itself
will never tell you.
