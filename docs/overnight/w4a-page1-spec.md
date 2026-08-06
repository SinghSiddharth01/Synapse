# W4a — Page 1, the Memory dashboard: implementation-ready spec

**Status:** BUILT (2026-08-06). Decision recorded in
`docs/overnight/decisions/003-dashboard-extends-debug.md`. Four deviations from
this spec, all recorded in §8 at the bottom — read that before treating any
section here as a description of the shipped code.
**Tree this was read against:** `origin/main` @ `4c495be` (W2 merged).
**Requirement, one line:** *the top level shows the state of the brain, not the
log.*

Every element below names its data source as `file:line`, or says **DOES NOT
EXIST** and what is shown instead. Nothing on this page is computed from a
number that is not already in the process.

---

## 1. Routes

| Route | Method | What it is |
|---|---|---|
| `/debug` | GET | **NEW.** The brain page (Page 1). Server-rendered, inline CSS + JS, no external request. |
| `/debug/brain.json` | GET | **NEW.** The brain page's only data source, polled every 1s. |
| `/debug/log` | GET | The page that is at `/debug` today, **moved verbatim** — same `_PAGE` constant (debug.py:170), same script, new path. Becomes Page 3 in W4b. |
| `/debug/stats.json` | GET | Unchanged shape, **plus two additive fields** (§5). Three scripts parse it; nothing is renamed or removed. |

All four mount inside `debug_routes(...)` (debug.py:142-160) and stay GET-only,
so Starlette answers 405 to anything else (debug.py:1-8) — `test_debug_rejects_
writes` already pins that for `stats.json` and gets a sibling for `brain.json`.
All four disappear when `build_app(debug=False)` (api.py:783-784).

`debug_routes` grows one parameter: `wm_log: WorkingMemoryLog`. Same shape as
`call_log` and `feed` — constructed in `build_app` next to them (api.py:175-176),
`None` when debug is off.

**Nav.** Both pages carry the same header strip: `BRAIN` · `LOG` (and a disabled
`MEMORY` placeholder for W4b), current page marked `aria-current="page"`. On
`/debug/log` this is the *only* edit to the moved page — if the implementer
prefers a truly byte-identical move, skip it there and link back from the brain
page only. Byte-identical is worth more than symmetry.

---

## 2. `/debug/brain.json` — payload and derivation

```jsonc
{
  "sessions": [ {"shared_id": "...", "purpose": "...", "status": "active"} ],
  "session": {
    "sid": "sh-1a2b3c4d",
    "purpose": "...",
    "created_by": "sid",            // may be null — see schemas.py:203
    "status": "active",             // "active" | "ended"
    "memory_version": 7,
    "counts": {
      "contributors": 2,            // len(members)
      "contributors_in_log": 3,     // distinct contributors across attributions
      "conversations": 4,           // distinct agent_session across attributions
      "visible": 18, "superseded": 6, "trivial": 2,
      "conflicts": 1,
      "log_entries": 63
    },
    "working_memory": {
      "text": "...", "words": 212, "version": 7,
      "updated_iso": "2026-08-06T04:11:09+00:00",   // null if never observed
      "revisions": [                                 // newest first, max 10
        {"version": 7, "ts_iso": "...", "words": 212, "delta_words": +18, "text": "..."}
      ]
    },
    "participants": [
      {
        "contributor": "sid", "agent": "claude-code",
        "agent_session": "0f3c…-a91",
        "contributions": 6,
        "first_contribution_iso": "...", "last_contribution_iso": "...",
        "last_query_iso": "...",            // null when unknown
        "last_query_scope": "conversation", // "conversation" | "contributor" | "none"
        "last_seen_version": 6, "behind": 1,
        "state": "active"                   // "active" | "listening" | "left"
      }
    ],
    "recent": [
      {
        "id": "f-005a-01", "ts_iso": "...", "type": "learning",
        "provenance": "distilled",          // distilled | contributed | synthesized
        "text": "…", "authors": ["sid"],
        "attributions": [{"contributor": "sid", "agent_session": "…", "agent": "claude-code"}],
        "status": "kept", "merged_into": null
      }
    ]
  }
}
```

`sessions` / session selection copies `stats_json` exactly (debug.py:143-152):
enumerate `store.session_ids()` (store.py:149), take `?session=` when it is a
known id, else the first. `session` is `null` when the store holds none.

### 2.1 Session identity

| Field | Source |
|---|---|
| `sid`, `purpose` | `store.get_context(sid)` → `SessionContext.purpose` (store.py:339; schemas.py:250-251) |
| `created_by` | `store.get_session(sid).created_by` (store.py:92; schemas.py:203) — legitimately `null` |
| `status` | `store.get_context(sid).status.value` — folded from `SessionEnded` on every call (store.py:339-359; schemas.py:96-113). **This is the routing note's "one field away" and it is one field.** |
| `memory_version` | `SessionContext.memory_version` — verdict rounds applied, *not* `Log.version` (schemas.py:241-248; log.py:199-213) |

### 2.2 Counts

| Field | Source |
|---|---|
| `visible` / `superseded` / `trivial` | `store.view(sid)` → `View.visible_ids` / `superseded_by` / `trivial` (store.py:171-176) — same three the existing page's fold node shows (debug.py:120-124) |
| `conflicts` | `len(ctx.conflicts)` (schemas.py:254) |
| `log_entries` | `len(store.log_entries(sid))` (store.py:165). Note: the existing page shows `log_tail.length`, capped at 200 (debug.py:33-34) — this is the true count |
| `contributors` | `len(store.get_session(sid).members)` (store.py:95-98) |
| `contributors_in_log`, `conversations` | distinct `Attribution.contributor` / `.agent_session` over the roster walk below |

### 2.3 The participant roster — the part that did not exist before tonight

**The routing note is right: there is no roster today.** Here is the one that can
be built from what exists, and exactly what it can and cannot say.

**Walk `store.log_entries(sid)` (store.py:165) and consider `FindingAppended`
entries only** (log.py:48-57). Key each attribution as
`(contributor, agent_session, agent)` (schemas.py:116-127). Accumulate
`contributions += 1`, `first/last_contribution_iso` from `entry.finding.ts`.

- **`Merged` results are deliberately skipped** (log.py:60-72). A Synthesized
  Finding carries *every source's* attributions (schemas.py:141-143), and those
  sources were each already counted from their own `FindingAppended` entry.
  Counting merges too would inflate every author's number every time synthesis
  ran, and would invent no new participant but would make all of them look
  busier after a merge than before. Pinned by test 7 in §6.
- One row **per Agent Session**, not per Contributor. That is the W2 property
  landed tonight: two windows of one human are two participants
  (CONTEXT.md:121-125), and before W2 the second window silently *became* the
  first. This page is where that becomes visible.

Then union in membership:

| `state` | Condition | Meaning |
|---|---|---|
| `active` | contributor ∈ `members` **and** has ≥1 conversation in the log | joined and producing |
| `listening` | contributor ∈ `members`, **no** attributions anywhere | joined, contributed nothing yet — one row, `agent_session: null`. This is the demo's teammate #2 before their first finding |
| `left` | has attributions, contributor ∉ `members`, **and this process observed the DELETE** | was here, has been removed via `DELETE /members/{c}` (api.py:453-466 → store.py:100-126). Their findings stay in the log, attributed to them, so the row stays |
| `unregistered` (rendered **not a member**) | has attributions, contributor ∉ `members`, **no departure observed** | ⟨amended 2026-08-06, adversarial review⟩ the honest unknown. `∉ members` has three causes and only the first is `left`: removed; **never registered** (nothing on the ingest path calls `add_member` — a raw `POST /findings`, which is what `docs/demo-script.md` Beats 1–6 do, registers nobody); or **registered before a restart** (`Relay._registered` caches per process and will not re-POST, while the restarted store starts at `members=[]`). Rendering all three as `left` asserts a human action that may not have happened |

`left` is separable from `unregistered` only because `store.remove_member`
now also writes `_departed` (store.py) — a process-local record that the
DELETE *was seen*, not a second representation of membership. It is empty
after a restart, and `has_departed() == False` therefore means UNKNOWN, never
"still here". That is exactly why the fallback state is `unregistered` rather
than `active`.

**Joined/left TIMES DO NOT EXIST.** `members` is a plain list on
`SynapseSession` (schemas.py:174), appended by `add_member` (store.py:95) and
removed by `remove_member` (store.py:100) — no timestamp, and store.py:104-113
records the deliberate reason membership is not in the log. The page renders
state, never a join or leave time.

| Field | Source |
|---|---|
| `last_seen_version` | `store.last_seen(sid, contributor)` (store.py:366-382) — **keyed by Contributor, not by conversation**, deliberately (decisions/001; CONTEXT.md:123-125). Repeated on each of that person's rows; the column header says so |
| `behind` | `memory_version - last_seen_version`. **Caveat to render honestly:** `last_seen` defaults to `0` for someone who has never queried, which is indistinguishable from someone genuinely at v0. Show `—` when the participant has no `last_query_iso` at all, not `behind: 7` |
| `last_query_iso` | `feed.snapshot(tag="query", session=sid)` (debug.py:57-63), written by api.py:744-769. Match on `detail.asked_by_session` when present → `last_query_scope: "conversation"`; else match `detail.asked_by == contributor` → `"contributor"`; else `null` → `"none"` |

**`asked_by_session` needs one line.** api.py:760 records
`asked_by=contributor`; `asking_session` is already in scope at api.py:679 and
is what suppression keyed on for that very request. Adding
`asked_by_session=asking_session,` to the same `feed.event(...)` call makes the
column per-conversation. Without it the page still works and labels the column
per-person — that is what `last_query_scope` is for, and the UI must render the
distinction rather than hide it.

**What is NOT available, at all:**

- **Health / "connected".** There is no heartbeat, no connection registry, no
  liveness ping anywhere in the service. `/watermark` (api.py:600) is the
  closest thing to a poll and it records **nothing** — no feed event, no
  timestamp — and it is called on MCP arrival and by two lifecycle probes
  (server.py:835, 1011; briefing.py:84-105), not on a timer. The page therefore
  computes **recency of last observed activity**, client-side, from
  `max(last_contribution_iso, last_query_iso)`, with stated thresholds:
  `< 2 min` active · `< 15 min` idle · older quiet · never seen `—`. The legend
  says *"derived from last observed activity — the service holds no heartbeat"*.
  The word **connected** does not appear on the page.
- **"Where they are in their session"** in the transcript sense (which turn,
  what file). The service never sees a transcript; only the worker does, and the
  worker never talks to the service (orchestrator is the single egress,
  CONTEXT.md:47-49). The nearest true reading is *where they are in the
  **memory***, which is `last_seen_version` / `behind`, and that is what the
  column is called.
- **Which machine / transcript a participant is on.** `SessionBinding` carries
  `transcript_path` and `pinned_at` (binding.py:54-63) but it is **local, on
  disk, on each teammate's laptop**. The service has no access to it and must
  not pretend otherwise.

### 2.4 Working memory

| Field | Source |
|---|---|
| `text` | `ctx.working_memory` (schemas.py:252) — already on the existing page as a collapsed `<details>` (debug.py:414-418) |
| `words` | `len(text.split())`. Cap for context: `SynthesisBudget.working_memory_words`, ≤ `MAX_WM_WORDS = 500` (synthesis.py:82,106,120) |
| `version` | `ctx.memory_version` |
| `updated_iso`, `revisions` | **DOES NOT EXIST TODAY.** New `WorkingMemoryLog`, §3 |

### 2.5 `recent` — structured rows (author · text · contributed vs listened)

Last **12** `FindingAppended` entries, newest first, from `store.log_entries(sid)`.

| Field | Source |
|---|---|
| `text`, `type`, `ts_iso`, `id` | `entry.finding` (schemas.py:145-149) |
| `authors` | ordered-unique `a.contributor for a in finding.attributions` — every attribution, never `attributions[0]` (schemas.py:141-143; the same rule server.py:611 enforces for `query`) |
| `provenance` | `finding.provenance` (schemas.py:81-86,151) |
| `status`, `merged_into` | project through `store.get(sid, finding.id)` (store.py:238) so tombstone/trivial state is the folded truth, not the producer's default (adr/0004; store.py:179-192) |

**"contributed vs listened" maps to `Provenance`, and it already exists:**
`CONTRIBUTED` = the agent called the `contribute` tool (server.py:686);
`DISTILLED` = the Edge Worker read the transcript — i.e. *listened*;
`SYNTHESIZED` = the service wrote it during a merge. Three badges, not two, and
the third is labelled `merged` because calling it either of the other two would
be false.

---

## 3. `WorkingMemoryLog` — the one piece of new retention

```python
class WorkingMemoryLog:
    """Bounded history of Working Memory rewrites, per Shared Session.

    Debug-only, like Feed and CallLog: constructed in build_app when debug is
    on, None when it is off, and no product path reads it. The rewrites already
    happen (synthesis.py:469 is the only writer of ctx.working_memory in the
    tree); nothing here creates a fact, it retains one that is otherwise
    discarded the instant the next merge lands.
    """
    def record(self, sid: str, text: str, version: int) -> None: ...
    def snapshot(self, sid: str) -> list[dict]: ...   # newest first
```

- `deque(maxlen=10)` per `sid`, in a dict.
- **Dedup:** append nothing when `text` equals the current head's `text`. A
  verdict may omit the rewrite entirely — `store.set_context` treats `None` as
  "leave alone" (store.py:326-336) and `SynthesisVerdicts.working_memory` is
  optional (synthesis.py:212-219) — so a version bump with unchanged prose is a
  real and common case, and it is not a revision.
- Each record: `{version, ts_iso (server clock, at record time), words, text}`;
  `delta_words` computed against the previous record on the way out.
- **One call site:** `api._record_synthesis_feed` (api.py:295-317), which
  already reads `ctx = store.get_context(sid)` after the merge and is already
  called from *both* merge paths (`push_findings` api.py:543 and `synthesize`
  api.py:594). `set_context` then `bump_version` (synthesis.py:469-471), so the
  ctx read there carries the new text *and* the new version together.
- Guarded by the same `if feed is None: return` that opens that function.

**Stated limitation the page must show:** this history lives in the process. It
is empty after a service restart, and empty for merges that happened elsewhere.
When `revisions == []` the panel renders the current text plus *"no rewrite
observed since this service started — revision history is not persisted"*, never
an empty list that reads as "the memory has never changed".

---

## 4. Page structure (`/debug`)

Same document shape as the existing page: one `<style>`, one `<script>`, `<link
rel="icon" href="data:,">`, no external anything. Reuse the CSS custom
properties verbatim (debug.py:180-200) — `--bg #0e1416`, `--panel #131c1e`,
`--teal #5fc6cc`, mono stack, `font-variant-numeric: tabular-nums`, the
`#banner` unreachable strip, and `.rail`/`.node` for the vitals row.

```
┌ header ────────────────────────────────────────────────────────────────┐
│ ● synapse-service · cloud        BRAIN | LOG | (memory)   [session ▾]   │
├ identity strip ────────────────────────────────────────────────────────┤
│ PURPOSE   "ship the retrieval fix before Friday"                        │
│ sh-1a2b3c4d · created by sid · ●ACTIVE · memory v7                      │
├ vitals rail (.rail) ───────────────────────────────────────────────────┤
│ CONTRIBUTORS 2 │ CONVERSATIONS 4 │ ★MEMORY 18 visible · 6 sup · 2 triv │
│ in this session │ agent sessions  │  the fold          │ CONFLICTS 1    │
├ WORKING MEMORY (hero panel, always open) ──────────────────────────────┤
│ v7 · rewritten 04:11:09 (2m ago) · 212 words                            │
│ <prose, max 72ch, pre-wrap>                                             │
│ ─ revisions ────────────────────────────────────────────────────────── │
│ v7 04:11:09  212w  +18  ▸        (click → full text of that revision)   │
│ v6 04:07:44  194w  −5   ▸                                               │
│ … up to 10, newest first                                                │
├ PARTICIPANTS ──────────────────────────────────────────────────────────┤
│ ● contributor  agent        conversation  contrib  last contrib  last  │
│                                                                  query │
│                                        memory position   state          │
│ ● sid          claude-code  0f3c…a91        6      04:10:52   04:11:02 │
│                                        v6 of v7 · 1 behind   active     │
│ ○ sid          claude-code  7b21…4de        2      03:58:01   —        │
│                                        v6 of v7 · 1 behind   active     │
│ ◌ aditya       —            —                0      —          —        │
│                                        —              listening         │
├ LATEST INTO MEMORY (12 rows) ──────────────────────────────────────────┤
│ 04:10:52  sid       [learning]  text…                    ⬤ listened     │
│ 04:09:31  aditya    [decision]  text…                    ⬤ contributed  │
│ 04:08:02  sid+adi   [learning]  text…                    ⬤ merged       │
├ footnote ──────────────────────────────────────────────────────────────┤
│ Activity dots are derived from the last observed contribution or query;  │
│ the service holds no heartbeat. Join and leave times are not recorded.   │
│ Revision history is kept in this process only.                          │
└────────────────────────────────────────────────────────────────────────┘
```

Interaction rules, all inherited from the existing page so there is one pattern:

- 1s poll of `/debug/brain.json`; `#banner` on failure (debug.py:390, 620-635).
- Session `<select>` change → clear expansion state, refetch (debug.py:457-461).
- Expanded rows survive the poll's `innerHTML` rebuild via an `expandedKeys`
  set keyed on something stable — **use the finding `id` and the revision
  `version`, not `tag|ts`** (the existing page's key, debug.py:442-444) — and
  prune keys whose rows left the snapshot (debug.py:511-514).
- Every value escaped through the existing `esc()` (debug.py:446-450). Finding
  text and working memory are model output and must never reach `innerHTML`
  unescaped.
- `hhmmss()` for absolute times (debug.py:452-455) plus a relative `(2m ago)`
  computed client-side.
- Responsive: the vitals rail stacks under 760px (debug.py:383-386); the
  participants table gets `overflow-x: auto` rather than wrapping.
- Accessibility: keep `:focus-visible` outlines (debug.py:239,298,381), give the
  table a `<caption class="sr-only">` and real `<th scope="col">`, and mark the
  status pill with text, not colour alone.

**Empty states, each specific — no shared "no data":**
no sessions → *"the service holds no Shared Session yet"*; empty working memory
→ *"(empty — no synthesis round has written one yet)"*; empty roster → *"nobody
has joined or contributed to this session yet"*; empty recent → *"no findings
have reached this session"*.

---

## 5. `stats.json` additive fields

In `_session_payload` (debug.py:105-139), where `ctx` and `session` are already
in hand:

```python
"status": ctx.status.value,          # active | ended  — the routing-note field
"created_by": session.created_by if session is not None else None,
```

Two lines, nothing renamed, nothing removed. `demo_say.py:73`,
`demo_local.py:286,359` and `rehearse_demo.py:285,310` read `sessions[].shared_id`,
`session.view`, `session.log_tail` and `session.llm` and are unaffected.

Optional 2-line bonus while in the file: `_summarize` (debug.py:66-79) has no
`SessionEnded` branch, so a closed session's log tail shows a raw dataclass repr
(`str(entry)`, debug.py:79). `return f"ended by {entry.ended_by}"` fixes it.

---

## 6. Test list

New file `packages/service/tests/test_debug_brain.py`, ASGI-only, matching
`test_debug.py`'s style (no sockets, `FakeProvider`).

**Routing / mounting**
1. `GET /debug/brain.json` lists sessions and defaults to the first when
   `?session` is absent or names an unknown id (mirrors debug.py:150).
2. `POST /debug/brain.json` → 405 (GET-only mounting, sibling of
   `test_debug_rejects_writes`).
3. With `build_app(debug=False)`: `/debug`, `/debug/log`, `/debug/brain` and
   `/debug/brain.json` all 404, and no `WorkingMemoryLog` object exists on the
   app at all (sibling of `test_no_call_log_exists_at_all_when_debug_is_disabled`).
4. `GET /debug` is 200 HTML containing `id="session-select"`, `id="wm-body"`,
   `id="revisions"`, `id="participants"`, `id="recent"`.
5. `GET /debug/log` is 200 HTML and still contains `id="log-tail"` and
   `id="feed"` — the moved page is intact.
6. The brain page makes **zero external requests**: no `<script src=`, no
   `<link href=` other than `data:,`, no `http://` or `https://` literal in the
   served HTML.

**Roster truth — the W2 properties**
7. Two Agent Sessions of ONE Contributor produce **two** participant rows,
   `counts.contributors == 1`, `counts.conversations == 2`. (Pre-W2 this state
   was unreachable; it is the reason this page exists now.)
8. A merge does not inflate contributions: push two findings from two authors,
   run a scripted verdict that merges them, assert each author's
   `contributions` is unchanged and no third participant appeared.
9. A member who has pushed nothing is present with `state == "listening"`,
   `contributions == 0`, `agent_session is None`.
10. `DELETE /v1/sessions/{sid}/members/{c}` for someone with findings in the log
    leaves their row present with `state == "left"` — the roster does not drop
    them and their findings stay attributed.
11. `behind` reflects `store.last_seen`: after a query by `sid`, `behind == 0`;
    after another synthesis round, `behind == 1`.
12. A participant who has never queried reports `last_query_iso is None` and
    `behind is None` (never `memory_version`, which would read as maximally
    stale rather than unknown).
13. `last_query_scope == "conversation"` when the query named an
    `agent_session`, `"contributor"` when it named only a contributor. *(Needs
    the one-line `asked_by_session` addition; if it is not taken, assert
    `"contributor"` in both cases and delete this test's first arm.)*

**Working memory**
14. A synthesis round that rewrites the prose appends one revision carrying the
    new `version`, a `ts_iso`, and the word count; `updated_iso` equals its ts.
15. A round whose verdict omits the rewrite (`working_memory: None`) bumps the
    version and appends **no** revision.
16. The ring is capped: 12 distinct rewrites leave 10 revisions, newest first,
    with the two oldest gone.
17. Before any synthesis: `revisions == []` and `updated_iso is None`.

**Session identity**
18. `status` is `"active"`, and `"ended"` after `POST /v1/sessions/{sid}/end` —
    on **both** `/debug/brain.json` and `/debug/stats.json` (the routing-note
    field, asserted where it was missing).
19. `created_by` is passed through, including `null` for a session recreated
    with `created_by: null` (schemas.py:180-203).

**Rows**
20. `recent` labels a `CONTRIBUTED` finding contributed, a `DISTILLED` one
    listened, and a synthesized merge result merged.
21. `recent` carries **every** attribution of a synthesized finding, not just
    the first (the same rule server.py:611 enforces).

**Read-only guarantee**
22. Sibling of `test_debug_stats_json_is_read_only_and_touches_no_provider`:
    polling `/debug/brain.json` repeatedly changes no `memory_version`, appends
    no log entry, and makes no provider call.

**Edited existing tests (2 lines total)**
- `test_debug.py::test_debug_page_has_required_ids` — `/debug` → `/debug/log`.
- `test_service_debug_page_js.py:64` — `/debug` → `/debug/log`.

**Should-have, if time allows:** a Node driver test for the brain page in the
style of `test_service_debug_page_js.py` — an expanded revision stays expanded
across the 1s poll's `innerHTML` rebuild. That is the exact defect that test
exists for on the other page, and the brain page reuses the pattern.

---

## 7. Summary of code touched

| File | Change | Size |
|---|---|---|
| `packages/service/src/synapse_service/debug.py` | `WorkingMemoryLog`; roster/recent/WM derivation helpers; `_brain_payload`; `brain.json` + `/debug` + `/debug/log` routes; `_BRAIN_PAGE`; two additive `stats.json` fields; optional `SessionEnded` summary | the bulk |
| `packages/service/src/synapse_service/api.py` | construct `WorkingMemoryLog` beside `Feed`; pass to `debug_routes`; one `wm_log.record(...)` in `_record_synthesis_feed`; one `asked_by_session=` kwarg on the query feed event | ~5 lines |
| `packages/service/tests/test_debug_brain.py` | new | §6 |
| `packages/service/tests/test_debug.py`, `test_service_debug_page_js.py` | one URL each | 2 lines |

Nothing in `store.py`, `memory.py`, `log.py`, `fold.py`, `synthesis.py`,
`retrieval.py`, or any contract. No product route changes shape. No new process,
no build step, no external asset.

---

## 8. As built — the four deviations

Recorded 2026-08-06, after implementation. Suite 1107 green.

**1. `recent` walks `Merged` entries too, not `FindingAppended` alone (§2.5).**
This spec was wrong, and the test list contradicted it: `SharedMemory.merge`
appends only a `Merged` entry, so a Synthesized Finding is **never** a
`FindingAppended`. Walking appends alone would have made test 20's `merged`
badge unreachable and left the one thing synthesis does out of "latest into
memory". Both kinds are walked, deduplicated by finding id so a resend is not a
second arrival. **The roster's exclusion of `Merged` (§2.3) is unchanged and
still deliberate** — the two lists answer different questions, and the code
says so at both sites.

**2. `behind` is reported when `last_seen > 0`, not only when a query was
observed (§2.3).** The spec's rule would hide a real watermark once the query
that set it aged out of the 200-event `Feed`. A non-zero `last_seen` is
positive evidence that someone read; the `0` default is the only ambiguous
case, and that is the one that renders `—`. Strictly more truthful, and it
never invents. `last_seen_version` is nulled alongside `behind` so the two
cannot disagree.

**3. The nav strip is on the brain page only (§1).** The spec offered the
choice and preferred byte-identical; taken. `/debug/log` is the `_PAGE`
constant unchanged, so the moved page cannot regress — its two tests pass
against content that did not change. The brain page links to it.

**4. `asked_by_session` was taken (§2.3), so `last_query_scope` has both
arms.** Live-checked: a query naming its Agent Session reports
`"conversation"`, and the same person's other window reports `"contributor"`
against the same timestamp, with the page labelling which it got.

**Beyond the spec, one addition:** a Node driver test for the brain page
(`test_service_brain_page_js.py` + `support/brain_page_driver.js`), the §6
"should-have". It pins the expanded-row-survives-the-poll behaviour on both
rebuilt lists and asserts the em-dashes in the RENDERED roster, which no
assertion on the served HTML can see. `support/minidom.js` grew one line so its
`innerHTML` getter returns the last-written markup — without it a `<table>` is
invisible to any driver, and the roster is the element most worth asserting on.

**Verified live**, seeded service on port 14899, browser: three revisions with
word deltas, five participant rows across two Contributors and four Agent
Sessions including one `left` and one `listening`, tombstones struck through,
`merged`/`contributed`/`listened` badges distinct, session switching, and the
empty-session case rendering all four specific empty states. Zero console
errors.

---

### 8.1 As reviewed — five further changes (2026-08-06, adversarial pass)

Suite 1113 green.

**5. `left` split into `left` and `unregistered` (§2.3).** The blocking one.
`∉ members` was rendered as `left` unconditionally, so a contributor pushed in
by raw `curl` — the demo script's own Beats 1–6 — appeared on camera as having
*left*, directly beneath a Contributors tile reading `0`. `store.remove_member`
now records the observed DELETE (`_departed`, retracted by `add_member`) and
only that produces `left`; everything else renders **not a member** with the
caveat in its `title` and in the footnote. Proved against the branch before the
fix (`state: "left"`, `contributors: 0`, `contributions: 1`) and after
(`state: "unregistered"`).

**6. The Contributors tile shows both numbers.** `contributors_in_log` was in
the payload and rendered nowhere, which is what let a `0` sit above a populated
roster. The sub-line now reads `registered · N in the log`.

**7. `escAttr` for every attribute (§4).** The page's `esc` is
`textContent`→`innerHTML`, and that serializer escapes `& < >` but **not
quotes**. `Finding.id`, `Attribution.agent_session` and `shared_id` are bare
`str` in contracts/schemas.py, written by another teammate's machine across the
device boundary, and all three reach an attribute. Both pages now have
`escAttr`; the driver test pushes `f-x" onmouseover="alert(1)` through the real
renderer and asserts it comes out inert.

**8. `updated_iso` is guarded on the text matching, not on the list being
non-empty (§2.4).** Otherwise a rewrite reaching `store.set_context` by any
path other than `_record_synthesis_feed` would date the prose on screen by an
observation of *different* prose. Latent (both merge call sites do run the
recorder); pinned so it stays latent.

**9. Test honesty.** `assert row["last_seen_version"] == row["last_seen_version"]`
pinned nothing and now pins the watermark. The JS fixture could drift from the
server silently and now echoes its own key names back for comparison against a
real `_brain_payload`. The footnote gained the 200-event `Feed` bound, so a
`—` in the last-query cell reads as "not in the window", never "never asked".

**Docs corrected:** `docs/demo-script.md` and `docs/NPU-RUNBOOK.md` still sent
the presenter to `/debug` for the log tail, which moved to `/debug/log` in
`78387d3`; `docs/STATE.md:74` said the same. All three now name the right
route, and the demo script carries the raw-`curl`/`not a member` note.
