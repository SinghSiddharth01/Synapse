# Service HTTP reference

`synapse-service` (`packages/service/src/synapse_service/`) is a single Starlette
app, built by `build_app()` in `api.py:149` and served by `cli.py`. All
product routes live in `api.py:762-771`; two read-only debug routes are added
by `debug.py:157-160` when debug is enabled. There is no other HTTP surface.

Default bind: `127.0.0.1:8899` (`cli.py:63-64`). `SYNAPSE_SYNTHESIZER`
(`fake` default, `aic100`, `npu`, `anthropic`) picks the model provider
(`cli.py:16-38`).

## In-memory persistence — read this first

`InMemoryStore` (`store.py:32-52`) holds every session, its Finding Log, and
its `SessionContext` in process memory only. **A service restart wipes
everything** — sessions, findings, watermarks, conflicts, topics
(`store.py:11-13`). There is no database and no file behind it. The
documented recovery path is client-side: each orchestrator keeps its own
retained log and resyncs it into the *same* `shared_id` after a restart
(`store.py:59-63`, `docs/JOIN.md`). `POST /v1/sessions` is create-or-return
specifically to make that resync safe to call unconditionally
(`store.py:65-79`) — a restarted service accepts the same `shared_id` again
and starts empty; a still-live service hands back the existing session
untouched.

One consequence worth stating explicitly: `SynapseSession.created_by` is
`str | None`, and `None` means "recreated after a restart, nobody here knows
who owned it" (`schemas.py:178-203`). `POST .../end` has a fallback rule for
exactly that case — see below.

## Error shape and status codes

Every error response is `{"error": "<message>"}`. Codes used across the
surface:

| Code | Meaning |
|---|---|
| `404` | unknown `shared_id` |
| `409` | `{"error": "session_ended"}` — the session is closed (see the liveness gate below) |
| `422` | malformed body: a required key is absent, or a `Finding` fails schema validation |
| `403` | `end_session` only: caller is not authorized to end this session |

`_missing()` (`api.py:89-101`) checks **key presence**, not truthiness —
`{"created_by": null}` is a valid, accepted body; an absent `created_by` key
is a 422. This distinction exists so a malformed client request fails fast
with a terminal 422 instead of a 500 that the caller's Relay retries forever
(`api.py:93-96`).

### The liveness gate

`_unavailable(sid)` (`api.py:266-294`) is the single liveness check, applied
uniformly to `push_findings`, `synthesize`, `watermark`, and `query`: 404 if
the session never existed, 409 `{"error": "session_ended"}` if
`SessionStatus.ENDED`, else `None` (proceed). 404 is checked before 409 so a
typo'd id doesn't read as "this session ended."

It is **deliberately not applied** to `add_member`, `POST /end`, or
`DELETE /members/{contributor}` — ending a session must stay idempotent
rather than 409 on a retry, and a member of a session that just closed must
still be able to leave it (`api.py:284-288`).

---

## Session lifecycle

### `POST /v1/sessions` — create or rejoin a session

Handler: `create_session`, `api.py:320-344`.

Body (JSON): `purpose` (str, required), `created_by` (str or `null`,
required key), `shared_id` (str, optional).

- If `shared_id` is omitted, one is minted: `f"sh-{uuid.uuid4().hex[:8]}"`
  (`store.py:83`).
- If `shared_id` names an existing session, that session is returned
  **unchanged** — `purpose`/`created_by` in the body are ignored
  (`store.py:65-79`). This is what makes an unconditional resync safe.
- Response is `SynapseSession.model_dump()`: `shared_id`, `purpose`,
  `members`, `created_by`.
- Status: `201` if this created a new session, `200` if it returned an
  existing one.

Errors: `422` if `purpose` or `created_by` key is missing.

```bash
curl -s -X POST http://127.0.0.1:8899/v1/sessions \
  -H 'content-type: application/json' \
  -d '{"purpose": "fix the auth flow", "created_by": "aditya"}'
```

### `POST /v1/sessions/{sid}/members` — join

Handler: `add_member`, `api.py:346-373`.

Body: `contributor` (str, required).

Body also accepts `agent_session` (str, optional, 2026-08-06): the window
joining. A join naming a window retracts *that* window's departure; a join
without one retracts only a person-level departure, and deliberately leaves
per-window ones standing — `Relay._register_members` POSTs the contributor
alone on every push, and if that cleared window departures, one window's push
would resurrect the window that left.

Response: `{"members": [...], "created_by": ..., "purpose": ...}`.
`created_by`/`purpose` were added 2026-08-06 so a joining orchestrator can
record the session's ownership from the one call it's guaranteed to make —
without this, a joiner recovering after a restart had nothing to resync from
(`api.py:347-358`).

Not gated by `_unavailable` — see above.

Errors: `404` unknown session; `422` missing `contributor`.

```bash
curl -s -X POST http://127.0.0.1:8899/v1/sessions/sh-abc12345/members \
  -H 'content-type: application/json' \
  -d '{"contributor": "sid"}'
```

### `DELETE /v1/sessions/{sid}/members/{contributor}` — leave

Handler: `leave_session`, `api.py:454-467`.

Detaches one member; the session stays open for everyone else and their
already-pushed findings keep their attribution. Idempotent — removing a
contributor who isn't a member is not an error. Not gated by `_unavailable`,
so this is how a client cleans up after seeing a 409 from a closed session.

Query (optional): `agent_session` — **which window** is leaving (2026-08-06).
Two Claude Code windows of one person are two participants and `/debug` has
shown one row each since W2, but the contributor in the path can't tell them
apart: before this, the first window to leave marked the person departed and
*both* their rows flipped to `left` while the second was still bound and still
pushing. With it, only that window is marked departed and the contributor stays
on `members` until the last of their windows has gone — which is what
`POST /end`'s membership gate reads. Omitted, it still means the person, every
window of them, which is the honest reading of a DELETE that names no window
and what pre-W2 clients send (`store.remove_member`).

Response: `{"members": [...]}`.

Errors: `404` unknown session.

```bash
# the person leaves, every window of them
curl -s -X DELETE http://127.0.0.1:8899/v1/sessions/sh-abc12345/members/sid
# just this window
curl -s -X DELETE \
  'http://127.0.0.1:8899/v1/sessions/sh-abc12345/members/sid?agent_session=conv-1'
```

### `POST /v1/sessions/{sid}/end` — close for everyone

Handler: `end_session`, `api.py:375-452`.

Body: `ended_by` (str, required).

Authorization (`api.py:429-449`):
- If the session has a real `created_by`: only that contributor may end it —
  `403 {"error": "only <creator> can end this session"}` otherwise.
- If `created_by is None` (session was recreated after a restart, owner
  unknown): any **current member** may end it. A non-member gets `403` with
  an explanatory message naming the restart as the cause.

There is no authentication anywhere in Synapse — `ended_by` is
self-asserted and `POST .../members` is ungated, so this gate is a guardrail
against accidental closes (wrong session id, a session merely joined), not a
security control (`api.py:396-407`).

Ending an already-ended session is `200`, not `409` — the fold keeps the
first `SessionEnded`, so a retried `POST` is inert and reports the original
closer (`api.py:409-412`).

Response: `{"shared_id": ..., "status": "ended", "ended_by": <original closer>}`.

Errors: `404` unknown session; `422` missing `ended_by`; `403` unauthorized
(message names the required creator, or explains the restart fallback).

```bash
curl -s -X POST http://127.0.0.1:8899/v1/sessions/sh-abc12345/end \
  -H 'content-type: application/json' \
  -d '{"ended_by": "aditya"}'
```

---

## Push and query

### `POST /v1/sessions/{sid}/findings` — push findings

Handler: `push_findings`, `api.py:469-561`.

Gated by `_unavailable`.

Body: `{"findings": [<Finding>, ...]}`. Each `Finding` (see
`packages/contracts/src/synapse_contracts/schemas.py:129-168`) needs at
least: `id`, `type` (`learning|decision|dead_end|open_question`), `text`,
`attributions` (list of `{contributor, agent_session, agent}`), `ts`. `refs`,
`provenance` (`distilled|contributed|synthesized`) default sensibly.

Upsert is first-write-wins by `Finding.id` (`store.py:5-9`) — a retried push
of an already-stored id is a no-op against the fold, though it does append a
new `FindingAppended` log entry (see the log-growth note in `api.py:531-539`).

Push does two things: it upserts immediately (so `/query` and `/watermark`
see the findings right away), then decides whether to run a synthesis
("merge") round now or defer it:

- **Debounced** (`deferred: true`, logged at `info`) if fewer than
  `SYNAPSE_MERGE_MIN_INTERVAL_S` seconds (default `60`) have passed since the
  last merge on this session.
- **Budget-deferred** (`deferred: true`, logged at `warning`) if the interval
  has passed but the hourly token/request budget for the synthesis key is
  exhausted (`_affordable()`, `api.py:238-261`; ceilings are
  `SYNAPSE_SYNTHESIS_KEYS × 25,000` tokens/hour and `× 20` requests/hour).
- Otherwise a merge runs now, and deferred findings from prior pushes are
  folded in with it (`_pending`, `api.py:187-192`).

Response:
```json
{"accepted": <int>, "memory_version": <int>, "synthesized": <bool>,
 "deferred": <bool>, "pending": <int>}
```
- `accepted` — how many findings in this push were new (not already stored).
- `synthesized` — `true` only if a merge ran **and** actually moved
  `memory_version` this round; a merge that ran but failed (provider outage,
  a verdict that fails schema validation) leaves this `false` — same as
  never running one. `deferred` is what tells the two apart.
- `pending` — findings still waiting for the next merge round.

Errors: `404`/`409` from the liveness gate; `422` if any element of
`findings` fails `Finding` schema validation.

```bash
curl -s -X POST http://127.0.0.1:8899/v1/sessions/sh-abc12345/findings \
  -H 'content-type: application/json' \
  -d '{"findings": [{
        "id": "f-001", "type": "learning",
        "text": "Cirrascale API key needs to be rotated monthly",
        "attributions": [{"contributor": "sid", "agent_session": "as-1", "agent": "claude-code"}],
        "ts": "2026-08-06T10:00:00Z"
      }]}'
```

### `POST /v1/sessions/{sid}/synthesize` — force a merge round now

Handler: `synthesize`, `api.py:563-599`.

Gated by `_unavailable`. Body: none required.

Two purposes:
1. **Self-heal**: re-runs `merge()` over what's already stored, with no new
   findings, for a session whose last push failed synthesis and has nothing
   new to push that would re-trigger it.
2. **Force-now override**: since the debounce landed, this ignores
   `MERGE_MIN_INTERVAL_S` and flushes whatever `push_findings` has been
   holding in `_pending`. It's also the replay primitive for a full resync —
   push a machine's entire retained log as one batch, then call this once.

Draining `_pending` here (not merging `[]`) is what stops the next push from
re-offering findings this round already synthesized.

Response: `{"memory_version": <int>, "synthesized": <bool>, "flushed": <int>}`
(`flushed` = how many pending findings this round consumed).

Charged against the same hourly synthesis budget as an ordinary merge —
calling this repeatedly against an empty session can, since the 2026-08-06
correction, no longer book phantom spend, because `_record_spend` now skips
rounds that never reached the provider (`api.py:206-236`).

```bash
curl -s -X POST http://127.0.0.1:8899/v1/sessions/sh-abc12345/synthesize
```

### `GET /v1/sessions/{sid}/watermark` — has memory moved, and for whom

Handler: `watermark`, `api.py:601-663`.

Gated by `_unavailable`. Query params: `contributor` and/or
`agent_session` (see identity resolution below); neither is required.

Splits fields into two kinds:
- **Change fields** — `version` (`SessionContext.memory_version`, i.e.
  verdict rounds applied, not the count of completed merges) and `new_since` (`version -
  store.last_seen(sid, contributor)`) — global, unfiltered by suppression.
  They answer "has the memory moved," not "did it move for you."
- **Content fields** — `by_type` (Counter of finding types), `conflicts`
  (count touching at least one visible finding), `topics`, `purpose`,
  `members` — filtered through the same suppression rule `/query` uses
  (invariant 3, contributor-scoped — see `retrieval.py:visible_to`).

`new_since` can be `> 0` while `by_type == {}` — e.g. a version bump entirely
from the asker's own findings merging with each other. That is intentional,
not a bug: "the memory changed" and "here is what changed for you" are
different signals (`api.py:609-629`).

Response:
```json
{"version": <int>, "new_since": <int>, "by_type": {"learning": 2, ...},
 "conflicts": <int>, "topics": [{"id": ..., "size": ..., "label": ...}],
 "purpose": <str>, "members": [<str>, ...]}
```

```bash
curl -s "http://127.0.0.1:8899/v1/sessions/sh-abc12345/watermark?contributor=sid"
```

### `POST /v1/sessions/{sid}/query` — ask the memory a question

Handler: `query`, `api.py:665-760`.

Gated by `_unavailable`. Body: `{"query": <str>, "contributor": <str>}` (or
`agent_session` — see below). `query` is required.

Identity resolution used by both `watermark` and `query`
(`_asking_contributor`, `api.py:104-123`; `_legacy_agent_session`,
`api.py:126-146`): the asker's identity is `contributor` if sent, else
`agent_session`, else `""`. An empty string in `contributor` is treated as
absent (falls back to `agent_session`), not as an anonymous identity of its
own. Suppression and watermarks are keyed on Contributor as of 2026-08-06 —
see `retrieval.py` for the mechanics; the legacy `agent_session`-only path
still exists for un-upgraded clients and its exact keying is under revision
alongside that work.

Selection: suppressed findings are excluded first; if what's left fits in one
prompt (`≤ TOP_K`, `lanes.DEFAULT_TOP_K`), every visible finding is sent,
ordered by the lane ranker rather than arrival order; above that, the lanes'
top-`TOP_K` candidates are sent. The ranked result comes back from
`query_findings` against the retrieval provider.

Response: `{"findings": [<Finding>, ...]}` (each `Finding.model_dump()`).

A `POST` to a session whose provider is unreachable does not error — the
provider layer catches and the route returns findings from whatever ranking
succeeded, which can be an empty list with `200` (see the NPU/`GenieX`
`/completions` trap noted in `cli.py:16-30`).

Errors: `404`/`409` from the liveness gate; `422` if `query` key is missing.

```bash
curl -s -X POST http://127.0.0.1:8899/v1/sessions/sh-abc12345/query \
  -H 'content-type: application/json' \
  -d '{"query": "why did the Cirrascale key fail", "contributor": "sid"}'
```

---

## Debug (read-only, `debug=True` default)

Mounted only when `build_app(debug=True)` — the CLI default; `--no-debug` or
`SYNAPSE_SERVICE_DEBUG=0` turns it off (`cli.py:44-56`). Both routes are
`GET`-only by construction and never write to the store, call the
synthesizer, or call a provider (`debug.py:1-8`). There is no auth on
`/debug` — `cli.py:72-77` prints a warning at boot if `--host` is not
localhost and debug is still on.

### `GET /debug` — dashboard HTML

Handler: `debug_page`, `debug.py:154-155,159`. Returns a static HTML/JS page
that polls `/debug/stats.json` every second.

```bash
curl -s http://127.0.0.1:8899/debug
```

### `GET /debug/stats.json` — dashboard data

Handler: `stats_json`, `debug.py:143-152`. Query param: `session` (optional
`shared_id`; defaults to the first known session if omitted or unknown).

Response: `{"sessions": [{"shared_id": ..., "purpose": ...}, ...], "session": <payload or null>}`.
When a session is selected, `session` is (`_session_payload`,
`debug.py:105-139`):
```json
{"sid": ..., "memory_version": ..., "watermark": ...,
 "working_memory": ..., "conflicts": <int>,
 "view": {"visible": <int>, "superseded": <int>, "trivial": <int>},
 "topics": [{"label": ..., "size": ...}],
 "log_tail": [{"position": ..., "kind": ..., "summary": ..., "ts": ...}],
 "merges": [...], "queries": [...],
 "llm": [...],
 "purpose": ..., "members": [...]}
```
`log_tail` is capped at the last 200 log entries; the merge/query feed is a
bounded ring buffer of the last 200 events across all sessions
(`debug.py:33-34,44`). `llm` (`call_log.snapshot()`) is **not** filtered by
session — one `CallLog` covers every session this service instance holds, so
it shows the whole recording regardless of which session is selected
(`debug.py:129-136`).

```bash
curl -s "http://127.0.0.1:8899/debug/stats.json?session=sh-abc12345"
```
