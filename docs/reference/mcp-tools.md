# MCP tools

The Orchestrator is one MCP server per machine (ADR 0001), reachable over
streamable HTTP, never stdio — stdio would spawn one server process per
client and dissolve the single-egress property the Orchestrator exists to
create (`packages/orchestrator/src/synapse_orchestrator/server.py:59-61`).

It exposes **six tools**, registered by `register_tools`
(`server.py:174-953`), all called with an MCP session already connected —
there is no `attach(shared_id)` prompt and never was one meant to exist
(`server.py:11-30`). Two non-tool surfaces sit on the same code path and
belong in the same mental model as the six tools:

- the **arrival briefing**, carried on the MCP `instructions` field a
  connecting client reads at initialize (`briefing.py`, described below);
- the **producer endpoint**, `POST /producer/findings`
  (`packages/orchestrator/src/synapse_orchestrator/app.py:142-201`), which is
  how the Edge Worker's passive path and `contribute`'s distiller both get
  Findings into the Orchestrator. It is an HTTP route, not an MCP tool, and
  is documented in full in the service/orchestrator HTTP reference — it is
  named here only so a count of the Orchestrator's agent-facing surfaces
  comes to eight — six MCP tools, the initialize-time briefing, and this
  route — rather than leaving two of them unaccounted for.

If nothing is joined, a connecting client sees a named server (`"synapse"`)
with an empty capability set until the first `synapse-worker join` or a
lifecycle tool runs (`server.py:117-122`).

## The six tools

| # | Tool | Signature | Registered at |
|---|---|---|---|
| 1 | `query` | `(question: str) -> str` | `server.py:472` (decorator), `:486` (body) |
| 2 | `contribute` | `(text: str) -> str` | `server.py:569` / `:573` |
| 3 | `create_session` | `(purpose: str, agent_session_id: str \| None = None) -> str` | `server.py:640` / `:653` |
| 4 | `join_session` | `(shared_id: str, agent_session_id: str \| None = None) -> str` | `server.py:731` / `:741` |
| 5 | `leave_session` | `() -> str` | `server.py:794` / `:802` |
| 6 | `end_session` | `() -> str` | `server.py:856` / `:864` |

Every tool returns plain prose, never raises, and re-resolves the local
binding fresh at the start of the call — never one captured once at
registration time (`server.py:179-192`). That is why a `synapse-worker join`
or a lifecycle tool call takes effect on the very next tool invocation, in
the same MCP session, with no orchestrator restart.

`agent_session_id` is **accepted on `create_session` and `join_session`
only.** It is not a parameter of `query` or `contribute`, whose signatures
are `query(question: str)` (`server.py:486`) and `contribute(text: str)`
(`server.py:573`). Where it is accepted, it is the calling agent's own session id (Claude Code exports it as
`CLAUDE_CODE_SESSION_ID`) and is matched exactly against transcript
filenames, never against modification time — passing it is what avoids the
ambiguity refusal below when more than one conversation of the same Agent
product is live (`server.py:415-470`).

### 1. `query`

**Purpose:** search the team's shared memory before investigating something
new. Description text verbatim: *"Search the team's shared memory. Call
BEFORE exploring an unfamiliar subsystem, when debugging something a
teammate may also be working on, or before concluding something is a dead
end."* (`server.py:472-485`).

**Arguments:** `question: str` — natural language.

**Returns:** if not joined, `_NOT_JOINED` (`server.py:125-130`, see below).
If joined, either "Team memory has nothing relevant to that. (Checked — not
skipped.)" or a bulleted list, one line per Finding, `[type] text —
contributor(s)` — every attributed Contributor is listed, not just the
first, because a Synthesized Finding can carry several
(`server.py:526-535`). On an unreachable service or a malformed response the
tool says so in prose rather than raising (`server.py:536-544`).

**Mechanics:** posts to `POST /v1/sessions/{shared_id}/query` with both
`agent_session` and `contributor` in the body (`server.py:502-505`). Sending
both is what lets this Orchestrator work correctly whichever identity a
given Synapse Service build reads for suppression and the watermark — the
service takes one field first and falls back to the other
(`api._asking_contributor`). *Which field governs suppression is under
revision as of 2026-08-06* (workstream W2); this doc intentionally does not
assert one.

### 2. `contribute`

**Purpose:** push a durable insight into shared memory. Description
verbatim: *"Push an insight to the team's shared memory. Call when you have
learned something non-obvious a teammate would benefit from — a root cause,
a dead end, a decision and its why."* (`server.py:569-572`).

**Arguments:** `text: str` — a few sentences of plain prose.

**Returns:** `_NOT_JOINED` if unbound. Otherwise the text is wrapped as one
`Segment` and run through the same on-device Distiller the passive path
uses (`server.py:577-597`), and the reply is either "N finding(s) shared
with the team." / "N finding(s) queued (P pending)." or, if nothing durable
was extracted, "Nothing durable extracted from that — try stating the
insight directly." (`server.py:606-630`). Every Finding it produces is
tagged `provenance=CONTRIBUTED` (`server.py:604-605`).

**Mechanics:** `relay.record(findings, shared_id=binding.shared_id)` uses
the binding resolved at the *start of this call*, not the Relay's own
possibly-stale `shared_id`, so a `contribute` and a `query` issued back to
back never disagree about which Shared Session they mean
(`server.py:608-615`). If the target session turns out to already be ended,
the reply says the note was **not** recorded (`server.py:624-628`).

### 3. `create_session`

**Purpose:** start a brand-new Shared Session and bind this conversation to
it. Description verbatim: *"Start a NEW Shared Session for the team and
attach this conversation to it. Call when the user wants to begin sharing
what this session learns … or when `query` says you are not joined and
there is no existing session to join."* (`server.py:640-652`).

**Arguments:** `purpose: str` (required — what the session is for);
`agent_session_id: str | None = None`.

**Returns:** the created `shared_id`, the identity it was created as, and a
summary of every transcript it bound
(`f"{b.agent} conversation {b.agent_session_id} ({b.transcript_path})"`,
`server.py:410-413,726-729`) — or a refusal naming why binding failed while
still reporting the session id, since the session exists from the moment
`POST /v1/sessions` succeeds regardless of what happens to the binding
(`server.py:714-725`).

**Mechanics:** `POST /v1/sessions` with `{"purpose", "created_by"}`, then a
best-effort `POST /v1/sessions/{shared_id}/members` to register the creator
as a member immediately — before this, a creator who had not yet produced a
Finding was invisible to `end_session`'s "others are still members" gate
(`server.py:680-703`). The creator/purpose pair is retained locally
(`_remember`, `server.py:277-295`) so a service restart's resync can report
the real creator instead of a placeholder.

**When to reach for it:** beginning a piece of work several people are on,
or when `query`/`_NOT_JOINED` comes back and there is no existing session to
join instead.

### 4. `join_session`

**Purpose:** attach this conversation to a Shared Session a teammate already
started. Description verbatim: *"Attach this conversation to an EXISTING
Shared Session a teammate has already started. Call when the user gives you
a session id (sh-…), or when they name a session someone else is working
in."* (`server.py:731-740`).

**Arguments:** `shared_id: str` (required, `sh-…`); `agent_session_id: str |
None = None`.

**Returns:** on success, the joined `shared_id`, the identity joined as, and
the transcript(s) bound (`server.py:789-792`). On a bad or ended id: "No
Shared Session {id!r} exists" (404) or "has ended, so there is nothing to
join" (`server.py:756-762`).

**Mechanics:** probes `GET /v1/sessions/{shared_id}/watermark` *before*
registering as a member — liveness-first, so joining an already-ended
session never writes a binding in the first place
(`server.py:748-763`). Then `POST /v1/sessions/{shared_id}/members`, then
binds via the same `_worker_join_session` writer `synapse-worker join` uses
(one binding format, one code path — `server.py:52-57,220-226`).

**When to reach for it:** the user hands you an id, or names a session
someone else is already working in.

### 5. `leave_session`

**Purpose:** detach only this conversation, leaving the session open for
everyone else. Description verbatim: *"Detach THIS conversation from the
Shared Session it is in, leaving the session open for everyone else. …This
is not how a session is closed — use `end_session` for that."*
(`server.py:794-801`).

**Arguments:** none.

**Returns:** `_NOT_JOINED` if unbound. Otherwise "Left Shared Session {id}
…" naming the transcript(s) unbound; after this, `query`/`contribute` report
not-joined (`server.py:849-854`).

**Mechanics:** `DELETE`s every Contributor identity this machine attached to
that session — not just the one the resolved binding picked, since two
Agent products can be bound under two different Contributors
(`server.py:807-821`). If the service is unreachable, the local binding is
cleared anyway: staying bound because the service could not confirm the
departure was judged worse than a stale member-list entry
(`server.py:828-839`).

### 6. `end_session`

**Purpose:** close a Shared Session for everyone, permanently. Description
verbatim: *"CLOSE a Shared Session for everyone, permanently. … Only the
session's creator can end it, and this refuses while other contributors are
still members, naming them."* (`server.py:856-863`).

**Arguments:** none.

**Returns:** `_NOT_JOINED` if unbound. A refusal naming the still-present
members if any exist beyond this machine's own identities
(`server.py:892-903`); a 403 pass-through naming the creator if this
contributor isn't it (`server.py:906-911`); otherwise "Shared Session {id}
is now ended for everyone …" naming the transcript(s) unbound
(`server.py:944-952`).

**Mechanics:** three refusal layers, only the last two enforced here — layer
1 is the harness's own permission prompt, layer 2 is creator-only
enforcement in the Synapse Service, layer 3 is this tool's own "no other
members" check, which is a courtesy against an honest mistake, not a
security boundary (`server.py:876-903`). On success it also durably records
the closure locally (`ended.py`) and tells the in-process Relay
(`relay.note_ended`), because the service's own session store is in-memory
and a restart would otherwise un-end the session and let a resync refill it
(`server.py:928-940`).

## Failure strings every tool can return

These are constants, not one-offs — they read the same across every tool
that can hit them:

- **`_NOT_JOINED`** (`server.py:125-130`) — nothing bound; names
  `create_session`, `join_session`, and the terminal `synapse-worker join`
  fallback, and states that no restart is needed once one exists.
- **`_SESSION_ENDED`** family (`server.py:144-162`) — starts "This Shared
  Session has ended." verbatim on every path so an agent reads the same
  sentence to a user every time; the middle clause differs depending on
  whether the local binding could actually be cleared
  (`_SESSION_ENDED_CLEARED` vs `_SESSION_ENDED_UNCLEARED`).
- **`_NO_STATE_DIR`** (`server.py:168-171`) — orchestrator started without
  `--state-dir`; only reachable in a test fixture predating the lifecycle
  tools, since `cli.main` always passes one in production.
- **Ambiguity refusal** (`server.py:446-452`, inside `_bind`, which begins at `:415`) — two or more
  live transcripts of the same Agent product within
  `AMBIGUITY_WINDOW_SECONDS` of each other; the tool refuses to guess and
  asks for `agent_session_id` explicitly.
- **Unmatched `agent_session_id` refusal** (`server.py:455-461`) — an
  explicit `agent_session_id` matched no transcript under any registered
  Agent's root; the tool will not fall back to "most recently modified"
  because that would silently bind a different conversation than the one
  named.

No tool ever lets an exception escape to the agent as a raw internal error —
every lifecycle tool wraps unexpected failures through `_unexpected`
(`server.py:250-261`), which logs and returns a plain "couldn't complete"
message naming `synapse-worker status` as the next check.

## The lifecycle arc

```
create_session  ──┐
                   ├──> query / contribute (repeatable, any order) ──> leave_session
join_session    ──┘                                                       or
                                                                       end_session
```

A conversation reaches shared memory by exactly one of two doors:
`create_session` (start something new) or `join_session` (attach to a
teammate's `sh-…` id). Both bind the conversation the same way — through
`synapse_worker.discovery.join_session`, the same writer `synapse-worker
join` uses from a terminal — so there is exactly one binding format on disk
regardless of which door was used (`server.py:52-57,209-226`).

Once bound, `query` and `contribute` are the steady-state loop: `query`
first ("checked, not skipped" — `server.py:546`), `contribute` whenever
something durable and non-obvious turns up. Both re-resolve the binding on
every call, so they always act on whatever the *current* binding is, even if
a `join_session`/`create_session` happened moments earlier in the same
session.

The arc ends one of two ways. `leave_session` detaches only this
conversation — the session and everyone else in it keep going, and this
machine can `join_session`/`create_session` again afterward. `end_session`
closes the Shared Session for every member at once; it is creator-only at
the service and refuses (naming names) while other Contributors are still
members, so it is not the tool for "I'm done for today" — that's
`leave_session`.

A session that ends while this conversation is still bound to it surfaces on
the *next* call to any tool that touches the service (`query`, `contribute`,
`join_session`'s liveness probe, `end_session`'s own watermark check) as the
`_SESSION_ENDED` text, and the local binding is cleared the first time that
409/ended-body is observed (`_forget_ended`, `server.py:375-402`) — so a
stale session reads as "not joined" on the call after that, not as a
repeating dead end.

## The arrival briefing (non-tool)

Not a tool call — it rides the MCP `instructions` field a client reads once
at `initialize`, composed by `briefing.build_briefing`
(`packages/orchestrator/src/synapse_orchestrator/briefing.py:79-196`) from
the current binding's watermark. Fail-open by design: any error in the HTTP
round trip, the JSON parse, or the string composition itself yields the
plain unbound default text rather than ever breaking a session start
(`briefing.py:12-18,190-192`). Headline-only and hard-capped at 1200 chars
(`briefing.py:53-55`) — finding bodies never appear, only counts, types, and
version deltas. It re-composes on an interval (`DEFAULT_REFRESH_SECONDS =
10.0`, `briefing.py:227`) hung off the ASGI lifespan, so a session already
open keeps the briefing it arrived with while a newly-connecting client
always gets a current one (`briefing.py:199-287`). A stable sentinel string,
`SENTINEL = "[synapse-briefing]"` (`server.py:104`), opens every variant of
this text and is asserted through a real MCP client by
`scripts/verify_instructions.py`.
