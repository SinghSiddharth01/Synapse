# Session lifecycle — create, join, leave, end

**Status:** approved 2026-08-06 · **Author:** siddsing (with Claude)

Synapse today has `query` and `contribute` on MCP and `synapse-worker join` on the
CLI. There is no way to create a Shared Session, no way to leave one, and no way
to end one. `POST /v1/sessions` exists but has no caller outside
`packages/service/tests/` and `scripts/serve_local.py`. This spec closes that.

## Decisions

| Question | Decision |
|---|---|
| What does "end" mean? | `leave` and `end` are **separate operations**. Leave detaches one member; end closes the session for everyone. |
| What happens to a terminated session's memory? | **Fully closed** — reads and writes both rejected. The log persists for audit/replay only. |
| Where does lifecycle live? | **The orchestrator**, exposed as MCP tools. See "Why not the worker CLI". |
| What keys the watermark and self-suppression? | **`contributor`**, not the conversation UUID. |

## Why not the worker CLI

`join_session`'s docstring states the constraint:

> Contributor registration with the service deliberately does NOT happen here …
> the orchestrator is the single egress and the worker must not open its own
> connection to the service.

The worker may not talk to the service. A `synapse-worker create` would have to
proxy through the orchestrator, which already hosts MCP and is already the single
egress. Lifecycle therefore belongs on the orchestrator.

`synapse-worker join` keeps working unchanged — it only writes the binding file
and never calls the service, so it remains the headless path.

Note on precedent: Plan D records that an MCP prompt (`/mcp__synapse__start`) was
built, **worked, and was verified live**, then deleted for contradicting D.3's
"there is no `attach(shared_id)`". That was a plan-conformance decision, not a
finding that MCP lifecycle is unsound. This spec supersedes it deliberately, and
D.3's tool list is amended rather than violated silently.

## Requirement: bind the session we started from

**The problem.** `agent_session_id` comes from `transcript.session_id`
(`discovery.py:345`), which is `path.stem` (`discovery.py:112`) — the transcript
filename. `find_live_transcript` returns the **most recently modified** transcript
under `project_slug(cwd)` within a 30-minute window. With two Claude Code windows
open, or with the orchestrator resolving against a different `cwd` than the
conversation's, this binds the wrong session silently.

**The fix.** Claude Code exports `CLAUDE_CODE_SESSION_ID`, and it is exactly the
transcript stem. Verified 2026-08-06:

```
CLAUDE_CODE_SESSION_ID=360475a1-ba53-4734-ba8e-c3703d1bffcd
~/.claude/projects/-Users-siddharthsingh/360475a1-ba53-4734-ba8e-c3703d1bffcd.jsonl
```

So:

1. `create_session` and `join_session` take an **optional explicit
   `agent_session_id`**. When supplied, the orchestrator resolves the transcript
   by **filename match across all project slugs**, never by mtime. This is exact.
2. The calling agent supplies it from its own environment. Because the MCP server
   is a separate process, it cannot read the caller's env — the value must travel
   as a tool argument.
3. When it is **not** supplied, fall back to today's detection, but:
   - the tool **returns the transcript path and session id it bound**, so the
     caller can verify;
   - if two or more transcripts are inside the live window and their mtimes are
     within `AMBIGUITY_WINDOW_SECONDS` (5s) of each other, **refuse** and list the
     candidates rather than guessing.
4. Every lifecycle tool result names the bound session id explicitly. Silent
   binding is the defect; visible binding is the fix.

`resolve_transcript`'s existing pinned path (`discovery.py:405-413`) already
reuses `pinned.agent_session_id` when the transcript file still exists, so a
correctly-bound session stays correct across restarts.

## Requirement: what happens when a session closes

A session can close underneath a member who is still bound to it. Required
behaviour:

| Actor | On close |
|---|---|
| Service | `status` flips to `ended`; every route returns **409** with `{"error": "session_ended"}`. |
| `query` / `contribute` | Return plain prose — "This Shared Session has ended." — never raise. Matches the existing fail-open discipline in `contribute()`. |
| Local binding | Cleared on the first 409 observed, so the next call reports "not joined" rather than retrying a dead session forever. |
| Relay WAL | Findings recorded for an ended session are **dropped with a log line**, never retargeted to another session. This is the trap #8 discipline applied to termination. |
| Briefing | Reports the session as ended rather than showing stale counts. |
| `resync` | Must **not** resurrect an ended session. See "Durability caveat". |

## Components

### `packages/contracts`

- `SessionStatus` enum: `ACTIVE`, `ENDED`.
- `SessionContext.status: SessionStatus = ACTIVE`.

### `packages/service`

- `store.py`
  - `last_seen(shared_id, contributor)` / `mark_seen(shared_id, contributor)` —
    re-keyed from `agent_session` to `contributor`.
  - `end_session(shared_id, ended_by)` — records the terminate event; `status` is
    a fold over it, per `adr/0004` ("the log is append-only and state is a fold").
  - `remove_member(shared_id, contributor)`.
- `retrieval.py`
  - `visible_to(candidates, asking_contributor)` — compares `a.contributor`
    instead of `a.agent_session`. The `f.attributions and` guard stays exactly as
    is; invariant 3 is unchanged.
- `api.py`
  - `POST /v1/sessions/{sid}/end` — body `{"ended_by": <contributor>}`.
    **Creator-only**: 403 unless `ended_by == SessionContext.created_by`.
  - `DELETE /v1/sessions/{sid}/members/{contributor}`.
  - A single shared guard returning 409 on every existing route
    (`query`, `push_findings`, `synthesize`, `watermark`) when status is `ended`.
    One helper, applied uniformly — not repeated inline.
  - `query` and `watermark` read `contributor` from the request. Accept
    `contributor` in the body while still tolerating `agent_session` so the
    contract change is additive.

### `packages/orchestrator`

- `server.py` — four new tools:
  - `create_session(purpose, agent_session_id=None)` → `POST /v1/sessions`, then
    bind. Returns the new `shared_id` **and** the bound transcript.
  - `join_session(shared_id, agent_session_id=None)` → `POST .../members`, bind.
  - `leave_session()` → `DELETE .../members/{contributor}`, delete the binding.
  - `end_session()` → `POST .../end`, delete the binding.
  - `query`/`contribute` send `contributor` alongside `agent_session`, and render
    a 409 as "this session has ended" prose.
- Binding writes go through the worker's existing `write_binding` /
  `binding_path_for_agent`. **One code path** — the orchestrator must not invent
  its own binding format.
- `relay.py` — drop findings destined for an ended session, with a log line.

### `packages/worker`

- `discovery.py`
  - `find_transcript_by_session_id(session_id, agent)` — exact filename match
    across project slugs.
  - `find_live_transcript` gains ambiguity detection as described above.
- No new CLI commands in this pass. `join` stays as-is.

## Gating on `end_session`

Three independent layers, because "fully closed" makes this the one call that can
destroy the team's memory:

1. **Harness permission prompt** — MCP tool calls prompt unless allowlisted. A
   human approves. Free, and not something a model can satisfy by retrying.
2. **Creator-only** — enforced in the service, not the client.
3. **Refuse when others are still members** — `end_session` declines and names
   them; closing a session with live teammates needs a deliberate override.

## Error handling

| Condition | Behaviour |
|---|---|
| Session ended | 409 → prose, binding cleared, no raise |
| Not the creator | 403 → "only `<creator>` can end this session" |
| Other members present | Refuse, name them |
| Unbound | Existing `_NOT_JOINED` |
| Service unreachable | Existing fail-open — prose, never an exception |
| Ambiguous transcript | Refuse, list candidates, tell the caller to pass `agent_session_id` |

Nothing may raise out of an MCP tool. FastMCP would surface a raw exception
string to the agent.

## Testing

Same discipline as the existing suite: in-process ASGI, injected `httpx`
transports, zero real sockets.

First failing tests:

1. `end_session` by a non-creator is rejected; by the creator it succeeds.
2. An ended session returns 409 on `query`, `push_findings`, `synthesize`, and
   `watermark`.
3. `leave_session` removes the binding; the next `query` reports not-joined.
4. **Re-join preserves your place across a new conversation** — same
   `contributor`, a *different* `agent_session_id`, and `last_seen` is retained.
   This is the regression the identity re-key exists for.
5. Self-suppression still holds for the same contributor across two different
   agent sessions.
6. `join_session(shared_id, agent_session_id=X)` binds exactly X's transcript
   even when a more recently modified transcript exists.
7. Ambiguous detection (two transcripts within 5s) refuses rather than guessing.
8. A finding recorded for an ended session is dropped, not retargeted.

## Durability caveat — known, accepted for this pass

`Store` is entirely in-memory (`store.py:48-52`); nothing is written to disk.
Therefore:

- A service restart **un-ends** an ended session.
- `synapse-orchestrator resync` is create-or-return and would resurrect it.

Modelling terminate as a log event is what makes this correct once the
service-side log persistence item lands (STATE.md's first post-demo entry). Until
then, `resync` skips any session whose id appears in a locally retained
`ended.json` set in the orchestrator's state dir. This is a stopgap and is
labelled as one in the code.

## Known limitation — not closed by this spec

MCP identifies the client **product**, not the conversation (Plan D line 26).
Passing `agent_session_id` explicitly resolves it for any caller that does so.
A caller that omits it on a machine with two live Claude Code windows still hits
the documented ambiguity — now surfaced as a refusal rather than a silent
mis-bind, which is the best this layer can do.
