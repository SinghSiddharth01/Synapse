# Troubleshooting

Failures a teammate actually hits, in the order you hit them: getting the
code running, starting your half, joining a session, watching memory move,
and cleaning up. Each row is symptom → cause → fix. Citations are
`file:line` against this checkout.

## Setup

### `uv sync` fails with a Rust/`cryptography` build error (Windows/ARM64)

**Cause.** `uv`'s managed interpreter resolves to an x86_64 build running
under Prism emulation. NPU wheels do not install under emulation, and the
failure reads like an unrelated build error rather than a wrong interpreter.

**Fix.** Pin the ARM64 interpreter explicitly:

```powershell
uv sync --python "$env:LOCALAPPDATA\Programs\Python\Python312-arm64\python.exe"
```

If the path is wrong on your box: `Get-ChildItem
"$env:LOCALAPPDATA\Programs\Python" -Directory`.
(`docs/JOIN-WINDOWS.md:24-36`, `docs/NPU-RUNBOOK.md:13-15`)

### `cryptography` fails to build after `mcp` was "upgraded"

**Cause.** `mcp` is pinned to `1.9.4` in the lockfile. Every version from
`1.9.4` through `1.29.0`, and all of `2.x`, pulls `pyjwt[crypto]` →
`cryptography`, which has no ARM64-Windows wheel and falls back to a source
build that fails.

**Fix.** Do not let anything bump `mcp` off `1.9.4`. If something already
did, revert the lockfile change. (`docs/JOIN-WINDOWS.md:44-47`,
`docs/NPU-RUNBOOK.md:16-17`)

### `--distiller anthropic` says "Could not resolve authentication method"

**Cause.** The key lives in the `anthropic` block of `secrets.jsonc` (where
every other credential in this project lives), but `scripts/serve_local.py`
only reads `ANTHROPIC_API_KEY` from the environment or that same block — a
key placed anywhere else in the file, or a malformed `secrets.jsonc`, is
silently treated as absent (`scripts/serve_local.py:61-92`, `_anthropic_key`
returns `None` on a `JSONDecodeError` rather than raising).

**Fix.** Either `export ANTHROPIC_API_KEY=sk-ant-...`, or add
`"anthropic": {"api_key": "sk-ant-..."}` to `secrets.jsonc`. The script's own
error names both options (`scripts/serve_local.py:433-438`). `secrets.jsonc`
is gitignored (`.gitignore:4-6`) and the key is never printed or logged.

### `secrets.jsonc` / `api-1.json` show up in `git status`

**Cause/fix.** These are the canonical credentials file and are meant to be
gitignored (`.gitignore:4-6`, comment: *"secrets.jsonc is the canonical team
credentials file, distributed offline only"*). If either is staged, unstage
it — do not commit. `secrets.example.jsonc` is the committed template.

## Starting your half (`serve_local.py`)

### `ports already in use: 18181` (or 8787/8899)

**Cause.** `serve_local.py` starts three processes — service, orchestrator,
model stand-in — and a plain `pkill -f synapse-` only matches two names
(`synapse-service`, `synapse-orchestrator`); the third process is
`local_model_server.py` and is left running, then blocks the next start.

**Fix.**

```bash
pkill -f synapse-service; pkill -f synapse-orchestrator; pkill -f local_model_server
```

(`docs/JOIN.md:41-49`). On Windows, `pkill` isn't there — match on command
line and stop by PID instead:

```powershell
Get-CimInstance Win32_Process |
  Where-Object { $_.CommandLine -match 'synapse-service|synapse-orchestrator|local_model_server' } |
  ForEach-Object { Stop-Process -Id $_.ProcessId -Force }
```

then confirm the ports are actually free with `Get-NetTCPConnection
-LocalPort 8787,18181 -State Listen` — no output means free.
(`docs/JOIN-WINDOWS.md:49-67`)

`claim_ports` (`scripts/serve_local.py:147-177`) is what raises this message
before anything else runs; it exists specifically because a second
`serve_local` on the same machine used to sail past the orchestrator's own
bind failure — the *existing* orchestrator answers its health check, so the
new one looks up — and by then it had already overwritten
`.synapse/bindings/claude-code.json` with its own contributor, silently
turning the host's agent into somebody else (observed 2026-08-06, comment at
`scripts/serve_local.py:150-157`). If you're trying to **listen** alongside
a host you're already running, you don't need a second process at all —
your existing orchestrator is already connected to that session; the
message says so.

### `--npu given but nothing is serving on :18181`

**Cause.** `--npu` tells the script *"a real model is already serving on
:18181, don't start the stand-in"* — it does not start `geniex serve` for
you.

**Fix.** Start `geniex serve` first, then re-run with `--npu`, or drop the
flag to use the stand-in instead. (`scripts/serve_local.py:281-285`)

### Queries come back empty with an HTTP 200, only when `--npu` is set

**Cause.** The stand-in serves both `/chat/completions` and `/completions`;
`geniex serve` (the real NPU endpoint) has **no `/completions` route at
all**. `AIC100Provider` — the synthesizer — needs `/completions`, so against
a real NPU it 410s on every synthesis call, and separately the host's own
queries can come back empty with a 200. Observed live 2026-08-06, confirmed
against Qualcomm's published endpoint list.

**Fix.** No code fix exists for this yet; know it's the endpoint shape, not
a broken query, before you go looking elsewhere.
(`scripts/serve_local.py:318-326`)

### `nothing is answering at <service-url>` when joining with `--service-url`

**Cause.** `serve_local.py` probes `<service-url>/debug` before doing
anything else and refuses if nothing answers within 8s
(`scripts/serve_local.py:306-313`) — either the host hasn't started their
service, or they bound it to `127.0.0.1` instead of the LAN (`serve_local.py`
binds `0.0.0.0` by default; `--host 127.0.0.1` opts out).

**Fix.** Ask the host to confirm the service is up and reachable, and that
they didn't pass `--host 127.0.0.1`.

### `<service-url> is up but holds no Shared Session yet`

**Cause.** You joined with `--service-url` but no `--shared-id`, and the
host's service currently has zero sessions
(`scripts/serve_local.py:367-371`) — `serve_local.py` deliberately refuses
to create one for you here, because a session created on your machine alone
would look like success (a working stack, an empty memory, no error
anywhere) while being exactly the isolated-memory outcome joining exists to
avoid (`scripts/serve_local.py:362-366`).

**Fix.** Ask the host for the `sh-…` id and pass `--shared-id` explicitly.

## Session lifecycle / MCP tools

### Both tools say "not joined"

**Cause.** No session binding exists yet for this agent.

**Fix.** Call `create_session` or `join_session <shared_id>` from the agent,
or re-run `serve_local.py` with `--shared-id`. No restart needed —
`resolve_binding` re-checks the binding on every call
(`packages/orchestrator/src/synapse_orchestrator/server.py:125-130`).

### `/mcp` says failed, "unable to connect"

**Cause.** Your orchestrator isn't running.

**Fix.** Start it (`serve_local.py`, or the standalone `synapse-orchestrator`
command), then in Claude Code pick **Reconnect** in `/mcp` — no need to
re-add the server. (`docs/JOIN.md:224`)

### Everything returns `409`

**Cause.** Somebody ended the Shared Session. Once `end_session()` runs,
`query`, `contribute`, pushes, and the watermark all return `409` for every
member — the log is kept for audit only.

**Fix.** The tool's own response says so in plain words rather than
erroring (`_SESSION_ENDED_HEAD`,
`packages/orchestrator/src/synapse_orchestrator/server.py:144-147`) and
tells you what to do next: `create_session` a new one, or `join_session` the
one a teammate moved to (`_SESSION_ENDED_TAIL`, `server.py:158-161`). If the
orchestrator was started without `--state-dir`, the local binding could not
even be cleared and every later call keeps reporting the same dead session
until it's restarted with `--state-dir <dir>` (`_SESSION_ENDED_UNCLEARED`,
`server.py:153-157`).

### "Refusing to guess which claude-code conversation this is" (ambiguity refusal)

**Cause.** Two or more live transcripts for the same agent product were
written within the ambiguity window of each other, and binding intentionally
does not fall back to "pick the most recently modified one" — that would
bind a different conversation than you meant, silently.
(`packages/orchestrator/src/synapse_orchestrator/server.py:106-121`)

**Fix.** Call the tool again passing `agent_session_id` explicitly — Claude
Code exports it as `$CLAUDE_CODE_SESSION_ID`. It's an exact match on the
transcript filename and never consults modification times.

### "No transcript anywhere matches agent_session_id …"

**Cause.** You passed an `agent_session_id` that doesn't match any detected
transcript. The tool will not fall back to the most-recently-modified
conversation, because that would silently bind the wrong one.

**Fix.** Check the id — `echo $CLAUDE_CODE_SESSION_ID` (bash) / `echo
$env:CLAUDE_CODE_SESSION_ID` (PowerShell) — and confirm it matches what you
passed. (`server.py:150-163`)

### `contribute` says "nothing durable extracted" / declines politely

**Cause.** Two distinct situations produce this: (1) you're on the model
stand-in, which only knows this repo's fixture corpus and deliberately
returns nothing for unfamiliar prose rather than inventing a finding
(`docs/JOIN.md:77-81`); or (2) you passed `--listen`, which starts no model
at all — reading works because the **host's** service does the ranking, but
`contribute` needs a distiller on your own machine and fails soft rather
than crashing (`docs/JOIN.md:83-89`, verified against an orchestrator with
nothing on its model port).

**Fix.** For (1), pass `--live`, `--distiller anthropic`, or `--distiller
claude-cli` to get a real model. For (2), `--listen` is read-only by design
— add one of the distiller flags if you also want to contribute.

### Queries return nothing at all, for everything

**Cause.** The host restarted their service. The store is in-memory
(`docs/JOIN.md:14`: the shared-id "changes when they restart, because the
store is in memory"), so a restart genuinely empties it — this is not a bug
on your end.

**Fix.** Get the new shared-id from the host and rejoin. If your own
findings need to come back, resync your retained log
(`packages/orchestrator/src/synapse_orchestrator/cli.py`, the `resync` CLI
path) — each machine holds only its own relay, so every contributor has to
resync independently after a host restart.

### Your own findings don't come back to you as "team knowledge" — or one Claude Code window sees the other's findings

**Cause.** Suppression (invariant 3) hides a finding from an asker only when
every attribution on it is the asker's own — the mechanism lives in
`visible_to` (`packages/service/src/synapse_service/retrieval.py:41-75`).
**Which identity field the comparison keys on was under revision through
2026-08-06 and is now settled** (`docs/overnight/decisions/001`, and the
CONTEXT.md note it landed with): suppression keys on the **Agent Session**,
the watermark on the **Contributor**. Two windows of one human are two
participants — each sees the other's findings, neither sees its own — while
`last_seen` is still per person, so a new conversation does not replay the
memory. Do not assume single-key scoping from an older doc without
cross-checking the code. What's stable regardless of the key: a finding with
zero attributions is never suppressed (the empty-list guard exists on
purpose), and a request in the old wire shape (`agent_session` only, no
`contributor`) is handled by the primary path rather than a special case,
since that field is the key again — so an un-upgraded client's suppression
behaviour doesn't silently change depending on when the service and
orchestrator each redeploy.

**Fix.** If findings you expect to see are missing or findings you expect
suppressed are showing, check which identity (`contributor` vs
`agent_session`) the request actually sent, and which one the running
service compares against, before assuming it's a data problem.

## Memory not moving

These three causes produce the *same* symptom — findings are queryable
immediately, but the synthesized working memory (what a *query*'s ranking
draws on, what the briefing summarizes) doesn't move — and they need
different fixes. `push_findings`'s response distinguishes them:
`synthesized` (did the version actually move this round), `deferred` (did
we choose not to run synthesis at all), and `pending` (how many findings are
waiting). (`packages/service/src/synapse_service/api.py:559-561`)

### Deferred: "N second(s) since last round (minimum 60s)"

**Cause.** The debounce. Synthesis is rate-governed by
`SYNAPSE_MERGE_MIN_INTERVAL_S` (default 60s) so a live `synapse-worker run`
pushing every couple of minutes doesn't blow through the hourly token/request
ceiling on the shared synthesis key. Findings are queryable the instant
they're pushed; only the *synthesized* memory lags — it catches up next
round with the whole accumulated batch. (`api.py:483-510`, ADR 0005 §7)

**Fix.** Wait out the interval, or call `POST
/v1/sessions/{sid}/synthesize` to force a round now, ignoring the debounce
— the documented mid-demo override. It also drains the pending queue so the
next push doesn't re-offer what this round already synthesized.
(`api.py:563-598`)

### Deferred on BUDGET, not latency

**Cause.** The interval has passed but the hourly token/request budget is
exhausted — a separate governor tracks real spend (including failed rounds:
a truncated verdict burns the same tokens as a good one) and defers rather
than blow past the shared synthesis key's ceiling. Distinct log line on
purpose: `"Synthesis for %s deferred on BUDGET, not latency"`
(`api.py:517-522`).

**Fix.** More keys (`SYNAPSE_SYNTHESIS_KEYS` /
`INFERENCE_CLOUD_API_KEYS`) — lowering `SYNAPSE_MERGE_MIN_INTERVAL_S` does
nothing here, that's the point of logging the two cases distinctly. One key
holds roughly 6 rounds/hour under the governor (ADR 0005, "One key cannot
deliver 60-second latency"), which is why quiet periods get their 60s and
busy periods stretch.

### `Synthesis returned schema_valid=False … findings are landed, memory unchanged`

**Cause.** A truncated verdict. This was a real 2026-08-06 outage: the
prompt asked for a working memory "under 500 words" while the provider's
output cap made that arithmetically impossible, so every response was cut
off mid-JSON and the parser returned `None`. `SynthesisBudget.derive`
(`packages/service/src/synapse_service/synthesis.py`) now derives the
working-memory word cap and verdict room from the actual `max_tokens`
instead of stating them independently, and refuses to start on an impossible
configuration rather than fail silently at merge time. Full account: ADR
0005.

**Fix.** If you see this today, it means the running configuration has
`max_tokens` too small for the current working memory size — the numbers to
check are `INFERENCE_CLOUD_MAX_TOKENS` and the derived budget row it
produces (ADR 0005's table: 800 → 270 words / 4 merges; 1600 → 500 words /
10 merges, which is what `serve_local.py` sets:
`scripts/serve_local.py:50-51,336-345`). This is a config problem, not a
crash to route around — do not raise `max_tokens` without also raising
`INFERENCE_CLOUD_TIMEOUT`, or you trade this failure for a `ReadTimeout`
that produces the identical "landed, memory unchanged" symptom from a
different cause (ADR 0005, "Raising max_tokens without raising the
timeout…").

### A push you retried shows up twice in the dashboard's log tail

**Cause.** `push_findings` upserts, then `merge()` upserts the same list
again — that used to append every finding to the log twice
(`api.py:530-540`). Since ADR 0005 #8, `upsert` skips the append when the
stored finding compares equal, so an **identical** resend is no longer a
log entry. A resend whose content genuinely changed is still appended — the
log is the record, and dropping a changed finding because its id was seen
before would lose data.

**Fix.** If duplicates still appear, check whether the resend actually
changed content (expected to append) versus is byte-identical (should now
be deduped) — that distinction is the whole point of the fix.

## Orphaned processes / cleanup

`serve_local.py` starts **three** processes per run (service, orchestrator,
model stand-in), all children of the script. A plain `Ctrl-C` is caught and
runs `stop_all()`, which terminates them in reverse order and force-kills
anything that doesn't exit within 5s (`scripts/serve_local.py:189-197,
514-528`). If the parent script itself dies uncleanly (killed, crashed,
terminal closed), the children are orphaned and hold their ports — this is
what produces `ports already in use` on the next start. The kill patterns
above (`pkill -f synapse-service; pkill -f synapse-orchestrator; pkill -f
local_model_server` / the `Get-CimInstance` equivalent on Windows) match all
three process names and are the standard way to clear a stuck state before
starting fresh.

## Freshness pointer (signal ③) silent when you expect a nudge

**Cause.** This is by design in several distinct ways, not a bug list:

- It fires **only** when the watermark moved since this hook's own last
  observed version — not since your last `query` call, which would fire on
  nearly every turn (`packs/claude-code/hooks/freshness_pointer.py`, "WHAT
  'SINCE THIS AGENT LAST LOOKED' MEANS HERE").
- It's rate-limited independently of the watermark: a move inside the
  notice cooldown stays silent *this* turn but stays pending — it is not
  dropped, it fires on the next turn once cooled down (`freshness_pointer.py`,
  "RATE-LIMITED…").
- It's scoped to the Agent Session that actually invoked it. A second
  Claude Code window on the same project, not the one that's joined, gets no
  injection at all (`freshness_pointer.py`, "SCOPED TO THE CONVERSATION…").
- It fails open, silently, on any error: unreachable service, slow
  response, malformed JSON — one blanket `except Exception`, always exits 0,
  never prints anything until the final line so no partial output leaks.
  This is deliberate ("a memory service that can break someone's coding
  session is worse than no memory service") — it will never surface a
  service outage as a hook failure.

**Fix.** If you expect a nudge and don't see one, check in order: is this
the joined window (not a second one on the same project)? did the watermark
actually move since this hook last recorded a version? is the cooldown still
active? None of these are errors to chase — read the hook's own docstring
before assuming it's broken, and remember a genuine service outage here is
invisible by design.

## Empty query string

**Cause.** `query`'s required-field check (`_missing`,
`packages/service/src/synapse_service/api.py:89-101`) tests key
**presence**, not truthiness — the same split used for `created_by: null`
on session creation. `{"query": ""}` passes validation; only an entirely
missing `query` key returns `422`.

**Fix.** An empty-string query is not an error and will not 422 — it is
handed to candidate ranking and the retrieval model as-is
(`retrieval.py:78-105`, `api.py` query handler). If you're debugging "why
did this return nothing/everything", check whether the query string itself
is empty before looking elsewhere.

## Ports at a glance

| port | what |
|---|---|
| 8787 | your own orchestrator (`ORCHESTRATOR_URL`, `scripts/serve_local.py:54`) — MCP always points here, never the host's |
| 8899 | the Synapse Service (`SERVICE_URL`, `scripts/serve_local.py:53`) — yours if hosting, the host's if `--service-url` |
| 18181 | model seam: the stand-in or `geniex serve` (`STANDIN_URL`/`NPU_URL`, `scripts/serve_local.py:55-56`) |
| 8790 | `synapse-worker --debug-port` dashboard, first worker (`packages/worker/src/synapse_worker/cli.py:69`, `DEFAULT_DEBUG_PORT`) |
| 8791 | second worker's debug dashboard in the two-worker demo shape (`scripts/demo_local.py:58-59`) — not a fixed default, just what the demo script assigns |
