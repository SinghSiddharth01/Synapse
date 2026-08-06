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

**This pack is Claude Code's.** Bindings, per-conversation identity and
finding routing are agent-agnostic — `bindings/<agent>/<session>.json`,
resolution dispatched through the agent registry — so Codex joins, binds one
file per conversation, and egresses to its own Shared Session with nothing
here installed. What it does not get is signals ③ and ④: the hook is a
`UserPromptSubmit` hook and the skill is a Claude Code skill, and neither has
an equivalent to install into. A Codex pack is a separate piece of work, not
a configuration of this one.

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
- **One window is one participant.** Since 2026-08-06 each Claude Code
  conversation gets its own binding file, `.synapse/bindings/claude-code/
  <session-id>.json`, written by `synapse-worker join --agent-session-id
  "$CLAUDE_CODE_SESSION_ID"`. Two windows can therefore be in two different
  Shared Sessions at once, and two windows in the *same* one are teammates:
  each sees the other's findings, neither sees its own echoed back. The old
  single `bindings/claude-code.json` is still written as a mirror of the
  most recent join, for tooling that predates this.
- **Pass your session id on the tool calls too.** `query`, `contribute` and
  `leave_session` each take an `agent_session_id`. It is the only thing that
  tells the orchestrator which of the machine's conversations is calling —
  MCP itself carries no per-call identity — and the skill instructs the
  agent to send it. Omitted, every call falls back to the machine's most
  recently joined binding, which is right with one window open and a guess
  with two.

## Prerequisites

- `synapse-orchestrator` reachable at its default `http://127.0.0.1:8899`
  Synapse Service URL. If you've customized `--service-url` (or the Synapse
  Service's own port), see **Custom service URL** below.
- This project has joined a Shared Session: `synapse-worker join
  <shared_id>` has been run at least once from a terminal in this project's
  root. Until then, the hook stays silent (nothing to check yet) and the
  skill's tools say so plainly rather than erroring.

## Install

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
behaviour described next: run from a terminal, its stdin is a TTY, and the
hook treats a TTY exactly like "no session_id available" — the same
fallback as before conversation-scoping existed. Use it to confirm the
hook is wired up and reaching the service at all; it will not tell you
*which* Claude Code window, if any, it is currently speaking for.

### Verify two windows are two participants

The check worth doing once, because it is the thing most likely to be
quietly wrong. Open **two** Claude Code windows on this project and, from a
terminal inside each one, run the join **naming that window's own session
id**:

```bash
synapse-worker join <shared_id> --agent-session-id "$CLAUDE_CODE_SESSION_ID"
```

The flag is what makes this a two-window check rather than a coin toss.
Without it, `join` picks the most recently written transcript under this
project (see the troubleshooting section below), so with two live windows
both joins can bind the *same* conversation and leave the other window
unbound — which looks identical to a working install until findings start
going missing. Then:

```bash
ls .synapse/bindings/claude-code/
```

Expect **two** files, one per conversation, named after each window's own
session id — not one file overwritten by the second join. (A single
`.synapse/bindings/claude-code.json` sitting alongside them is the
compatibility mirror, not a third window.)

Now `mcp__synapse__contribute` something from window A and
`mcp__synapse__query` for it from window B. B should get it back; A should
not get its own contribution back from its own `query`. That asymmetry —
each window a teammate to the other, neither reading its own notes as team
knowledge — is the whole point of the per-conversation identity, and both
halves of it depend on each window passing its own `agent_session_id` on the
tool call, as `skills/synapse-shared-memory/SKILL.md` instructs. Omitted,
both tools fall back to the machine's most recently joined binding, so both
windows speak as whichever one joined last — no error, just the wrong
answer.

## Troubleshooting: the pointer stays silent in one window but not another

The hook only ever speaks for the Claude Code conversation that invoked it.
It looks for that conversation's own binding —
`.synapse/bindings/claude-code/<session_id>.json`, keyed by the `session_id`
Claude Code pipes to it on stdin — and, failing that, falls back to the
`bindings/claude-code.json` mirror only when the mirror names this same
conversation or declares itself machine-scoped. Every other window's
invocation returns before it even reaches the network, by design: a window
that never joined must not receive shared-memory content, and must not
consume a joined window's own pending notice.

So a window that never ran `synapse-worker join` for its own conversation
is silent, permanently, with no error — because a not-joined window and
"nothing changed yet" produce identical (empty) output.

**How `join` picks, exactly.** Without `--agent-session-id` it binds
*whichever* Claude Code Agent Session it finds live in this project —
meaning the transcript written to most recently. It does **not** know which
terminal or window you ran it from; nothing in a subprocess tells it that.
So with two live windows here it binds whichever one typed last, which may
not be yours. It refuses rather than guesses only when two transcripts were
written within seconds of each other; a window that has been quiet for a
minute simply loses.

If you're confident shared memory has moved (a teammate confirms it, or
`query` returns findings you haven't seen) but your prompts never surface
a notice, the fix is to join **naming your own Agent Session**:

```bash
synapse-worker join <shared_id> --agent-session-id "$CLAUDE_CODE_SESSION_ID"
```

That is an exact match on the id, never a modification-time guess, and it
searches every project directory rather than only this one — which also
covers a window started from a different directory than the one you are
joining from. It writes a binding for
whichever Agent Session is live in *that* window, alongside — not over —
any other window's, which fixes that window going forward and leaves the
others working.

`synapse-worker join <shared_id>` with no flag still works and is still
right when only one window is open.

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
