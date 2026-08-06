# W7 — the full lifecycle arc, driven end to end against the real stack

Written 2026-08-06 overnight (W7). Driver: `scripts/_w7_drive.py`, a real MCP
client (pinned `mcp==1.9.4`, streamable-http) against a real
`synapse-orchestrator` on **:8787**, a real `synapse-service` on **:8899**, and
the model seam on **:18181**. Nothing is mocked, stubbed or in-process.

Reproduce:

```
uv run python scripts/serve_local.py --purpose "w7 overnight live evidence" \
    --distiller claude-cli --claude-model haiku --contributor sid
uv run python scripts/_w7_drive.py --contributor sid --json-out /tmp/w7.json
```

**Headline: the whole arc works. `create_session` → `join_session` (second
conversation) → `contribute` → `query` (attributed) → `leave_session` →
REJOIN under a new agent session id → `end_session` ran clean, twice, with no
code changes and no retries.** Every lifecycle tool answered truthfully,
including the two negative beats (`query` as the author is suppressed;
`query` after the end says not-joined).

**What is NOT clean is what the arc carried.** One paragraph of contributed
prose came back as 8 findings on run 1 and 9 on run 2, and **6 of them, both
times, are the distiller prompt pack's own few-shot examples echoed back as if
they had been extracted from the segment** (§5, F1). That is the finding of the
night and it is not a lifecycle bug — the lifecycle faithfully delivered
contaminated content to every reader.

Ran twice, ~13 minutes apart, with three fresh conversation ids each time. Every
number and every quoted string below is from **run 2** unless it says otherwise;
run 1 is quoted where the two differ. Raw transcripts: `/tmp/w7_drive.json`,
`/tmp/w7_drive2.json` (not committed — they hold nothing the tables below omit).

---

## 1. The stack, as booted

Ports checked free before boot (`lsof -nP -iTCP:<port> -sTCP:LISTEN`, one
command per port): 8899 → no listener, 8787 → no listener, 18181 → no listener.

Boot banner, verbatim:

```
model      stand-in on http://127.0.0.1:18181/v1 (replays this repo's corpus; not a model, not the NPU — retrieval ranking is identity)
service    http://127.0.0.1:8899  · dashboard http://127.0.0.1:8899/debug
           reachable on the LAN at http://192.168.4.44:8899 — no auth, so anyone who can reach this port can read and write the team's memory
distiller  haiku via the local `claude` binary on your SUBSCRIPTION — no API key spent at all. Only synthesis still spends the Cirrascale budget.
orchestr.  http://127.0.0.1:8787/mcp  <- point Claude Code here
session    sh-b0671df5  (contributor: sid)
```

**Model arm.** `--distiller anthropic` was not used: `secrets.jsonc`'s
`anthropic.api_key` is present but an empty string, exactly as W6 recorded. The
`claude-cli` arm needs no credential, so `secrets.jsonc` was never copied into
this worktree at all — nothing in the non-`--live` path reads it
(`scripts/local_model_server.py` reads it only for `--mode proxy`).

**Two model seams, not one.** `claude-cli`/haiku does the *distilling* (real
subprocess, real subscription). The *service's* synthesizer and retriever are
`aic100` pointed at the local stand-in on :18181, which replays this repo's
corpus and ranks by identity. So "query returned these in this order" is a
statement about plumbing, not about ranking quality. From the service's own
`/debug/stats.json` during run 1:

```
"component":"retrieval","provider_id":"aic100", ... "output_preview":"{'ranked': [0, 1, 2, 3, 4, 5, 6, 7]}"
"component":"synthesis","provider_id":"aic100", ... "output_preview":"{'working_memory': \"8 findings under review; 0 merged by the replay stub's corpus rule.\", 'merges': [], 'trivial_ids': [], 'conflicts': []}"
```

---

## 2. How `agent_session_id` was made real

`agent_session_id` is not taken on trust. `server._bind` resolves it through
`synapse_worker.discovery.find_transcript_by_session_id`, which searches every
project slug under `~/.claude/projects` for an exact id match and **refuses
rather than falling back to mtime** when nothing matches. So three conversations
means three real transcript files.

The driver writes them under a throwaway project slug whose "cwd" is a temp
directory created for the run only — the same pattern, and the same real
`~/.claude/projects`, as `scripts/verify_orchestrator.py` — and removes both on
the way out, success or failure. Both runs cleaned up; verified after the fact
that neither slug directory survives.

Run 2's conversations:

| Role | agent_session_id |
|---|---|
| A — creates the session, later asks | `33a90844-c815-4573-ba0a-ec8e9c980ea1` |
| B — joins, contributes | `79e2ca77-71d4-46cd-8088-1e9288928971` |
| C — the REJOIN, a new conversation for the same human | `757d7ccb-9ae3-4a5b-a1c6-5c01c8d33271` |

---

## 3. The arc, beat by beat

Timings are wall-clock for the tool call itself, measured client-side.

| # | Beat | Took | Outcome |
|---|---|---|---|
| 1 | `initialize()` — first arrival | 0.027s | 6 tools; briefing = the default unbound text (see F2) |
| 2 | `create_session(purpose, as A)` | 0.010s | `sh-d5397a07` created, A bound |
| 3 | `join_session(sh-d5397a07, as B)` | 0.007s | B bound — its own file, A untouched |
| 4 | `contribute(prose, as B)` | **46.810s** | "9 finding(s) shared with the team." |
| 5 | `query(q, as A)` | 0.012s | 9 findings, every one attributed `— sid` |
| 6 | `query(q, as B)` | 0.007s | "nothing relevant" — B's own work suppressed from B |
| 7 | `GET /watermark?contributor=sid` | <0.01s | `by_type: {}` — correct, see F7 |
| 8 | `GET /watermark?…&agent_session=C` | <0.01s | `by_type` with all 9 |
| 9 | `leave_session(as B)` | 0.006s | B detached; "1 other conversation(s) … still bound" |
| 10 | `join_session(sh-d5397a07, as C)` | 0.008s | REJOIN, new id, same contributor |
| 11 | `initialize()` — arrival after rejoin | 0.025s | the briefing, quoted in §4 |
| 12 | `end_session()` | 0.011s | closed for everyone, 3 bindings cleared |
| 13 | `query(q, as A)` | 0.002s | "Not joined to a Shared Session yet" |

Whole arc: **58.95s**, of which 46.8s is the one `claude -p` call and 12.0s is a
deliberate wait for one briefing-refresh interval (step 11 cannot be read sooner
— see §4). Every other call in the arc is **single-digit milliseconds**. Run 1
was the same shape: 48.0s total, 35.9s of it the distiller call.

### The load-bearing responses, verbatim

**create_session** —

> Created Shared Session sh-d5397a07 (purpose: 'w7 overnight live evidence — full lifecycle arc on real ports') as sid, and bound claude-code conversation 33a90844-c815-4573-ba0a-ec8e9c980ea1 (/Users/siddharthsingh/.claude/projects/-private-var-folders-…-synapse-w7-drive-t8sceoja/33a90844-c815-4573-ba0a-ec8e9c980ea1.jsonl) to it. Tell the user that id — it is what teammates pass to `join_session`. Findings from this conversation now go to sh-d5397a07 and nowhere else.

**join_session (B)** —

> Joined Shared Session sh-d5397a07 as sid, and bound claude-code conversation 79e2ca77-71d4-46cd-8088-1e9288928971 (…/79e2ca77-….jsonl) to it. Findings from this conversation now go to sh-d5397a07. Call `query` before investigating anything — the team may already know it.

**query as A** — the attribution beat. The whole response, verbatim, all nine
lines in the order returned. Quoting only the first two (an earlier draft of this
file did) hid the split that is the night's actual finding, and left one line
unaccounted for in every count below:

> What the team already knows about this — each line is one teammate's finding, and who found it:
> - [decision] Use the local command-line distiller as a workaround on this machine instead of the primary distiller. — sid
> - [learning] Authentication credential field can be present but contain an empty string, causing silent authentication failures when the credential provider does not validate the field and defers to the SDK, which reports a generic error. — sid
> - [decision] Message queue was moved to a durable broker so messages are not lost under load. — sid
> - [learning] An alternative distiller implementation that shells out to a local command-line tool requires no separate credential configuration. — sid
> - [open_question] The impact of the added network round trip on overall page latency has not been measured. — sid
> - [dead_end] Caching rendered templates in each worker's local memory fails across multiple workers: copies drift after deploys, and users get stale pages depending on which worker serves them. — sid
> - [learning] The durable broker's throughput is roughly one-third that of the in-memory queue, because each message waits for a disk write before being acknowledged. — sid
> - [decision] Template cache was moved to a shared store that every worker reads from, replacing per-worker in-memory caching. — sid
> - [learning] Accessing the template cache from a shared store adds a network round trip to every render. — sid
>
> If one of these explains the problem in front of you, tell the user now and credit whoever found it, rather than re-deriving it. Then verify only what your particular situation could change, and say what you are checking and why.

Nine lines, and they are **not** nine of a kind. Classified — the numbering is
this document's, the response carried no labels:

| # | Type | Provenance in truth |
|---|---|---|
| 1 | `decision` | **Genuine** — the contributed paragraph's disposition |
| 2 | `learning` | **Genuine** — the empty-string credential, de-identified |
| 3 | `decision` | Pack echo — few-shot 1 (F1) |
| 4 | `learning` | **Genuine** — why the claude-cli arm needs no credential |
| 5 | `open_question` | Pack echo — few-shot 2 (F1) |
| 6 | `dead_end` | Pack echo — few-shot 2 (F1) |
| 7 | `learning` | Pack echo — few-shot 1 (F1) |
| 8 | `decision` | Pack echo — few-shot 2 (F1) |
| 9 | `learning` | Pack echo — few-shot 2 (F1) |

**Three genuine, six echoes.** That is the arithmetic §5's F1 rests on, and it is
now stated in one place instead of implied across two. The three genuine ones are
what the contributed paragraph actually said, distilled and de-identified (note
the pack's no-identifiers rule doing its job — "the credential provider", not
"secrets.jsonc"). Attribution is correct and present on every line, echo or not
— which is precisely why F1 matters. The type counts match the watermark's
`by_type` in §7 exactly: `decision: 3, learning: 4, dead_end: 1, open_question: 1`.

**query as B** — the suppression beat, and the one that proves attribution is
per-*conversation* and not per-person:

> Team memory has nothing relevant to that. (Checked — not skipped.)

B wrote those findings, so B is handed none of them back. A, a different
conversation of the same human, gets all nine. That is `decisions/001` working
live.

**leave_session (B)** —

> Left Shared Session sh-d5397a07 (was following …/79e2ca77-….jsonl, …/79e2ca77-….jsonl). This conversation is no longer bound to it and nothing more from here will reach sh-d5397a07. `query` and `contribute` will say you are not joined until you join another. 1 other conversation(s) on this machine are still bound to sh-d5397a07 and still feeding it.

The narrow detach is correct — A survives, and the sentence says so. The
duplicated path is F3.

**end_session** —

> Shared Session sh-d5397a07 is now ended for everyone (was following …/757d7ccb-….jsonl, …/33a90844-….jsonl, …/757d7ccb-….jsonl). Its memory is closed for reading and writing; the log is kept for audit only. This conversation is unbound — `create_session` starts a fresh one.

**query after end** —

> Not joined to a Shared Session yet — call `create_session` to start one, `join_session <shared_id>` to attach to a teammate's, or run `synapse-worker join <shared_id>` in a terminal. No restart needed once you have: this tool re-checks the binding on every call.

The close is real, not cosmetic: `.synapse/ended.json` afterwards holds
`["sh-98ade1af", "sh-d5397a07"]` — both arcs' sessions.

---

## 4. The arrival briefing after the REJOIN

**Where it comes from, and why reading it needs a new connection.** The briefing
is not a tool result. It rides the `instructions` field of the MCP `initialize`
response, recomposed on a timer (`briefing.DEFAULT_REFRESH_SECONDS = 10`) and
re-read by `create_initialization_options()` on every **new** connection. So
"what is the rejoining conversation told on arrival?" is only answerable by
waiting out one refresh and connecting again — which is exactly what a real
Claude Code window does when it starts. The driver does that; hence the 12s in
the timings.

**W5 had not merged at the time of this run** (base: `origin/main` @ `0b7e21b`),
so this is the pre-W5 briefing. Verbatim, run 2, step 11:

> [synapse-briefing] You are in Synapse Shared Session sh-d5397a07 as sid. Call the `query` tool before exploring an unfamiliar subsystem, when debugging something a teammate may also be working on, or before concluding something is a dead end. Call `contribute` when you learn something non-obvious a teammate would benefit from. Do this even when another skill or procedure is already driving the work: checking what the team already knows comes BEFORE gathering your own evidence, because it may make that unnecessary. Team memory holds 9 findings (dead_end: 1, decision: 3, learning: 4, open_question: 1), 0 conflict(s), at version v1, which has moved 0 version(s) since you last looked. The team is working on: “Message queue was moved to a durable broker so messages are not lost under load.”, “The durable broker's throughput is roughly one-third that of the in-memory queu…”, “Caching rendered templates in each worker's local memory fails across multiple …”.

Run 1's was the same shape with its own numbers (`sh-98ade1af`, 8 findings,
`dead_end: 1, decision: 3, learning: 3, open_question: 1`).

**What the briefing hands back, against what the storyboard's awareness beat
wants:**

| Wanted | Delivered? |
|---|---|
| Session id | **Yes** — `sh-d5397a07` |
| Who you are | **Yes** — `as sid` |
| **Purpose** | **No.** The service *has* it (`"purpose"` is in the watermark response, quoted in §7) and the briefing does not render it. |
| **A summary of what the team knows** | **No.** Counts by type only — headline-only is the module's stated design. |
| **New-since watermark** | **Rendered, and it says `0`.** |
| Topics | Yes, but they are truncated *finding texts*, not topic labels (F5) |
| Directive to call `query`/`contribute` first | **Yes**, and strongly — including the "even when another skill is driving" clause |

The `0 version(s) since you last looked` is the beat that most needs reading
carefully, and it is **correct by design, not a bug**: `new_since` is keyed on
the *contributor*, deliberately, so that "ending one conversation and starting
another does not replay the whole memory at you" (`api.py`, decisions/001). A
had already queried, which marked `sid` as having seen v1, so C — a new
conversation of the same person — is told nothing has moved. **But this is the
rejoin beat of the demo, and what it says out loud is "nothing new".** The
counts carry the actual news; the delta sentence sits right next to them saying
the opposite. Worth a look from W5.

---

## 5. Findings

### F1 — the distiller returns its own few-shot examples as findings *(the big one)*

One paragraph in. Nine findings out. **Six of them are `config/prompts/v4-condense.toml`'s
few-shot example outputs, paraphrased and returned as if extracted from the
segment.** Reproduced on both runs (6 of 8 on run 1, 6 of 9 on run 2).

The pack's few-shot 1 output contains:

> {"type": "decision", "text": "The message queue was switched to the durable broker so that messages are not lost under load."}, {"type": "learning", "text": "The durable broker's throughput is roughly a third of the in-memory queue's, because each message waits for a disk write before being acknowledged. …"}

The live `query` returned:

> - [decision] Message queue was moved to a durable broker so messages are not lost under load. — sid
> - [learning] The durable broker's throughput is roughly one-third that of the in-memory queue, because each message waits for a disk write before being acknowledged. — sid

Few-shot 2's four outputs (the template-cache set: `dead_end`, `decision`,
`learning`, `open_question`) come back the same way. `grep -rl "durable broker"`
over the whole worktree returns exactly one file: `config/prompts/v4-condense.toml`.
Nothing in the contributed prose mentions queues, brokers, templates or workers.

Why it matters more than it looks:

- These land as **`Provenance.CONTRIBUTED`, attributed to a real human**, in a
  memory whose entire value proposition is "a result is a teammate's verified
  experience, not a hypothesis" (the `query` tool's own description). Six of
  nine lines a teammate reads are a prompt file's fiction wearing a colleague's
  name.
- They pass every guard we have. `guards.assert_prompt_conditioned` checks the
  model was conditioned at all; `test_fixture_contamination.py` checks the
  *inverse* direction (no fixture may share a six-gram with a pack) and is
  green. **There is no runtime check that a returned finding is not the pack's
  own few-shot output** — and a six-gram check in that direction would have
  caught this, since the returned text is near-verbatim.
- It inflates the counts the briefing reports, so the awareness beat is
  overstated too: "Team memory holds 9 findings" is really three.

Not fixed here — it is prompt/guard work, not lifecycle work, and the rule for
tonight is record-and-move-on for anything structural. **This is the single
highest-value thing to fix before the demo**, because it is visible in the one
screen the demo spends the most time on.

Unknown, and worth measuring: whether the NPU (`Qwen3-4B`) and Anthropic arms do
this too, or whether it is specific to `claude-cli`/haiku with `--max-turns 1`.
The pack's calibration line says it was measured against
`qualcomm/Qwen3-4B-Instruct-2507:W4A16` — a different model from the one that
ran here.

### F2 — `end_session` deletes the machine-scope binding, orphaning the boot session

Run 2's **first** `initialize()` returned the *default unbound* briefing:

> [synapse-briefing] Synapse passively distils this coding session into shared team memory. No session is bound yet — run `synapse-worker join <shared_id>` in a terminal to connect one.

Run 1's, on the same still-running stack, had returned
`You are in Synapse Shared Session sh-b0671df5 as sid`.

The chain: `serve_local.py` writes its machine-scope binding to the **legacy**
path `bindings/claude-code.json`. `create_session`/`join_session` write the
per-session file *and* refresh that same legacy mirror, so the machine binding
is overwritten by the first lifecycle call. `end_session` then clears every
binding file pointing at its session — including the mirror. Afterwards
`.synapse/bindings/` does not exist at all:

```
$ find .synapse -type f
.synapse/ended.json
.synapse/logs/{model,orchestrator,service}.log
.synapse/relay/{findings,sent}.jsonl
.synapse/scratch-transcript.jsonl
.synapse/sessions.json
```

The still-running stack now belongs to no session, and `sh-b0671df5` — the
session the banner told the operator to hand teammates — is orphaned but still
live in the service. Nothing errors. **Demo consequence: do not `end_session`
mid-demo and expect the banner's session to still be yours.** A fresh
`create_session` recovers fully (run 2 proved it), so this is a sequencing trap,
not a dead end.

### F3 — `leave_session` and `end_session` name the same transcript twice

> Left Shared Session sh-d5397a07 (was following …/79e2ca77-….jsonl, …/79e2ca77-….jsonl)

The legacy mirror and the per-session file are two *files* holding one
*transcript path*, and `server.py:962` (and `:1076`) joins `b.transcript_path`
over both without deduplicating. `end_session` shows it worse — three entries,
two of them identical. Cosmetic, but it is in the sentence whose entire job is
to let a user verify the right conversation was bound, so a duplicate reads as a
bug in exactly the place the text is asking to be trusted.

Deliberately **not** fixed here: it is a one-line dedup in `server.py`, and
`server.py` is where W5's briefing work also lands. Not worth a merge conflict
at 15:40 for a display string. Left for the morning.

### F4 — "a second contributor" is not reachable through one orchestrator

The brief asked for a second *contributor*. What the arc drives is a second
*participant*: a second Agent Session, its own `agent_session_id`, its own
binding file, its own suppression key — but the same contributor name.

That is architecture, not a shortcut. `server._identity()` reads
`binding.contributor`, and one orchestrator holds one identity;
`serve_local.py` prints the reason itself — *"They must NOT point Claude Code at
your :8787 — one orchestrator per laptop, or their findings get stamped as
yours."* A genuinely second contributor needs a second orchestrator on a second
port, and tonight's authorization covers 8787 only. So every finding in this
evidence is attributed `— sid`, and the multi-*conversation* half of W2 is what
got exercised. It is the half that was broken before W2, and it is provably
fixed: step 6 (B suppressed from itself) and step 9 (B leaves, A survives) both
depend on it.

### F5 — the briefing's "topics" are truncated finding texts

> The team is working on: “Message queue was moved to a durable broker so messages are not lost under load.”, “The durable broker's throughput is roughly one-third that of the in-memory queu…”, …

Three topics, `size: 1` each, labels that are finding bodies cut at ~80
characters mid-word. Nine findings produced three one-member "topics". Reads as
a bug even though it is the stand-in's clustering being trivial. Compounded by
F1 — all three topics shown are few-shot fiction.

### F6 — for the record: a watermark read without `agent_session` looks empty

Run 1's corroborating `GET /watermark?contributor=sid` returned `by_type: {}`
while the briefing on the same data showed all 8. That is **not** a
discrepancy: `by_type`/`topics` are CONTENT fields and suppress by the asking
*conversation*, so a read naming only the contributor — every finding of which
is that contributor's — correctly sees nothing. The briefing sends both
parameters and sees everything. Run 2 issues both spellings side by side (steps
7 and 8) so the evidence carries the proof rather than the puzzle. Recorded
because the first read is the kind of thing that gets screenshotted as a bug.

---

## 6. What this does and does not prove

Proves, live, on the real ports:

- All six MCP tools register at boot and are callable before anything is bound.
- `create_session` creates, binds, names both the id and the transcript, and
  registers the creator as a member.
- `join_session` binds a *second* conversation without disturbing the first.
- `contribute` round-trips real prose through a real `claude -p` subprocess and
  reaches the service (`POST /v1/sessions/…/findings` → 200 in the log).
- `query` returns attributed findings to a different conversation, and returns
  *nothing* to the authoring one.
- `leave_session(agent_session_id=…)` detaches exactly one conversation and says
  how many remain.
- Rejoining under a new id works, and the new connection's briefing reflects the
  new binding.
- `end_session` closes for everyone; the next `query` says not-joined; the id is
  retained in `ended.json`.

Does not prove:

- Anything about **retrieval ranking**. The stand-in ranks by identity
  (`{'ranked': [0..8]}`). Order in the `query` output is log order.
- Anything about **synthesis or merging**. The stand-in merged nothing.
- Anything about a **second machine or a second contributor** (F4).
- Anything about the **NPU or Anthropic arms** — only `claude-cli`/haiku ran.
- Anything about **W5's briefing**, which had not merged at this base.

---

## 7. Raw payloads worth keeping

The watermark read with C's id as the asking conversation (run 2, step 8).
**Read the ordering carefully, because an earlier draft of this caption claimed
more than the payload supports.** This GET is issued at step 8 — *before* C joins
(step 10) and *before* the briefing is read (step 11). So it is a corroborating
proxy read against an id not yet bound to the session, not the input the step-11
briefing was composed from. Its numbers do agree with that briefing (9 findings,
`new_since: 0`), which is the reason it is worth keeping; it is evidence *for*
the briefing, not the briefing's own source. The driver carries the same
overstatement in its own recorded note (`scripts/_w7_drive.py:299`, "what the
rejoining conversation's briefing is composed from") and that line is
deliberately **left unedited**, so the committed script stays byte-identical to
the one that produced these payloads.

```json
{"version": 1, "new_since": 0,
 "by_type": {"decision": 3, "learning": 4, "dead_end": 1, "open_question": 1},
 "conflicts": 0,
 "topics": [{"id": "t0001", "size": 1, "label": "Message queue was moved to a durable broker so messages are not lost under load."},
            {"id": "t0002", "size": 1, "label": "The durable broker's throughput is roughly one-third that of the in-memory queu…"},
            {"id": "t0003", "size": 1, "label": "Caching rendered templates in each worker's local memory fails across multiple …"}],
 "purpose": "w7 overnight live evidence — full lifecycle arc on real ports",
 "members": ["sid"]}
```

The same route without `agent_session` (run 2, step 7) — see F6:

```json
{"version": 1, "new_since": 0, "by_type": {}, "conflicts": 0, "topics": [],
 "purpose": "w7 overnight live evidence — full lifecycle arc on real ports",
 "members": ["sid"]}
```

`.synapse/sessions.json` after both runs — every session recorded with its real
creator, which is the fix `serve_local.py`'s `record_session` call exists for:

```json
{"sh-98ade1af": {"created_by": "sid", "purpose": "w7 overnight live evidence — full lifecycle arc on real ports"},
 "sh-b0671df5": {"created_by": "sid", "purpose": "w7 overnight live evidence"},
 "sh-d5397a07": {"created_by": "sid", "purpose": "w7 overnight live evidence — full lifecycle arc on real ports"}}
```

---

## 8. Suite

```
$ uv run pytest -q
1109 passed, 1 warning in 35.43s
```

Green at the base commit and green after adding `scripts/_w7_drive.py`. No
production code was changed by this workstream — F3 was the only candidate and
it was deliberately left (see F3).

---

## 9. Process hygiene

**Before boot** — all three ports verified free, one `lsof` per port:

| Port | `lsof -nP -iTCP:<port> -sTCP:LISTEN` |
|---|---|
| 8899 | exit 1, no listener |
| 8787 | exit 1, no listener |
| 18181 | exit 1, no listener |

**After finishing** — each `pkill` as its own command, in the required order:
`pkill -f serve_local` (exit 0), `pkill -f synapse-service` (exit 0),
`pkill -f synapse-orchestrator` (exit 0), `pkill -f local_model_server`
(exit 0), `pkill -f synapse-worker` (exit 1 — no worker was ever started;
`serve_local.py` does not spawn one).

**Ports re-verified free after cleanup:** 8899 exit 1, 8787 exit 1, 18181 exit
1. **All three ports are free. Nothing from this workstream is left running,
and nothing that this workstream did not start was killed.**

The throwaway `~/.claude/projects/-private-var-folders-…-synapse-w7-drive-*`
slug directories from both runs were removed by the driver's `finally` block and
verified gone. No secrets were read, copied, printed or committed:
`secrets.jsonc` was never brought into this worktree.

---

## 10. Verification pass (after the fact)

This file was reviewed against the code and against the driver's own recorded
transcripts (`/tmp/w7_drive.json`, `/tmp/w7_drive2.json`) by a second pass that
did **not** re-run the arc — re-running is the orchestrator's call, not the
reviewer's. Two defects were found, both in *this document*, neither in the
stack, and both are fixed above:

1. **An accounting gap in §3.** The old text quoted 2 of the 9 returned findings
   and said "the other seven are in §5", while §5 accounts for six and its own
   closing line implies three genuine. Six echoes + three genuine = nine
   reconciles, but the third genuine finding was never quoted or classified
   anywhere. §3 now quotes all nine verbatim from the recorded run-2 payload and
   classifies each.
2. **A mislabelled payload in §7.** The first block was captioned as what the
   rejoining conversation's briefing sees. Per `scripts/_w7_drive.py:294-299`
   that GET is issued at step 8, before the rejoin (step 10) and before the
   briefing read (step 11). The caption now says what it actually is.

Independently re-checked and **confirmed**, no change needed: the rejoin is a
genuinely new `agent_session_id` (`_w7_drive.py:347` mints three independent
`uuid4`s; `_bind` resolves each through `find_transcript_by_session_id`, which
refuses rather than falling back to mtime); the two participants are proven by
*asymmetry* rather than relabelling (A gets 9 attributed findings, B gets
"nothing relevant" for the identical question, `leave_session(B)` detaches B
while A stays bound); `claude_cli_provider.py` has **no** canned or fixture path,
so F1 is the model echoing its own few-shots and not a stub replaying a corpus;
the diff against `origin/main` is exactly these two files with **zero**
production-code changes, so nothing structural was smuggled in as a "small fix";
`server._identity()` does read `binding.contributor`, so F4's single-contributor
ceiling is architectural and correctly disclosed; `api.py:662` does compute
`new_since` contributor-keyed, so §4's `0` is by design; the quoted briefing
matches `briefing.py` word for word, and the boot banner matches
`serve_local.py`. `git merge-base --is-ancestor 0b7e21b origin/main` exits 0 and
no commit has touched `briefing.py` on `main` since that base, so "pre-W5
briefing" still holds.

Ports re-verified free at review time, one `lsof` per port: 8899, 8787 and 18181
all exit 1, and
`pgrep -fl 'serve_local|synapse-service|synapse-orchestrator|local_model_server|synapse-worker'`
exits 1. Nothing from this workstream is running. `secrets.jsonc` is absent from
the worktree and absent from the commit. Suite re-run at the reviewed HEAD:
`1109 passed, 1 warning in 36.18s`.

No section of this file is disputed: neither defect invalidates a live result.
