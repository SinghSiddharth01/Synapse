# Demo script — Aug 7

**Two sections, same corpus, same three queries.** §A is the fallback (`demo-fallback`, plain `main`).
§B is the integrated demo (the cut, `feat/brain-integration`). Both are driven by the same three
push payloads, built once by the snippet below and cached at `.measurements/demo-push{1,2,3}.json`
(gitignored — regenerate rather than commit).

## The corpus

| Push | Contributor | Contents | Running total |
|---|---|---|---|
| 1 | aditya | `seg-005a` (1) + `seg-001` (4) + 5 distractors | 10 |
| 2 | akhil | `seg-002` (2) + `seg-003` (2) + `seg-006` (1) + `seg-007` (1) + 8 distractors | **24** |
| 3 | aditya | `seg-005b` (1) + 1 distractor | **26** |

Three properties, each load-bearing for a task in the cut:

1. **`seg-005a` is the first finding of push 1; `seg-005b` is in push 3, 24 findings later.**
   `synthesis.py`'s `others = [...][-CANDIDATE_WINDOW:]` is the last **20** non-new findings —
   `seg-005a` sits at position 1, outside that window. On `main` the pair **cannot** merge, by
   construction. On the branch the symbol and lexical lanes surface it regardless of recency.
2. **24 findings are visible when the queries run** (after push 2), above `TOP_K = 14`, so the
   small-session bypass does not fire and the lane-selection path is the one actually exercised.
3. **Two contributors across three pushes** gives the topic index more than one cluster, so the
   arrival briefing (§B) renders more than one label.

Build the payloads (idempotent — re-run any time; `.measurements/` is gitignored):

```bash
export PATH="/opt/homebrew/bin:$PATH"
cd /Users/siddharthsingh/Dev/synapse-exec/e5
python3 - <<'PY'
import json, pathlib
ROOT = pathlib.Path("/Users/siddharthsingh/Dev/synapse-exec/e5")
FIX  = ROOT / "fixtures" / "findings"
OUT  = ROOT / ".measurements"; OUT.mkdir(exist_ok=True)

def load(stem):                       # every fixture is a BARE LIST, not {"findings": …}
    return json.loads((FIX / f"{stem}.findings.json").read_text())

def filler(prefix, n, contributor, agent_session, text):
    return [{"id": f"f-{prefix}-{i:02d}", "type": "learning",
             "text": text.format(i=i),
             "attributions": [{"contributor": contributor,
                               "agent_session": agent_session, "agent": "claude-code"}],
             "ts": "2026-08-05T09:00:00Z", "refs": [],
             "provenance": "distilled", "status": "kept",
             "merged_from": [], "merged_into": None}
            for i in range(n)]

push1 = load("seg-005a") + load("seg-001") + filler(
    "a", 5, "aditya", "as-demo-aditya", "The build script re-exports flag {i} on every run.")
push2 = (load("seg-002") + load("seg-003") + load("seg-006") + load("seg-007")
         + filler("b", 8, "akhil", "as-demo-akhil",
                  "Allocation attempt {i} for the context binary trips the pool ceiling."))
push3 = load("seg-005b") + filler(
    "c", 1, "aditya", "as-demo-aditya", "The tokenizer cache is rebuilt on cold start ({i}).")

for name, batch in (("push1", push1), ("push2", push2), ("push3", push3)):
    (OUT / f"demo-{name}.json").write_text(json.dumps({"findings": batch}, indent=1))
    print(name, len(batch))
print("cumulative:", len(push1), len(push1)+len(push2), len(push1)+len(push2)+len(push3))
PY
```

Expected: `push1 10` / `push2 14` / `push3 2` / `cumulative: 10 24 26`. If push 1 is not 10 or the
cumulative before push 3 is not 24, the corpus has lost property 1 — fix the filler counts before
going on.

The three queries, run from `as-observer` after push 2 and again after push 3:

```
"what do we know about timing"
"why does the decode fail"
"what should I avoid touching"
```

---

## §A — the fallback, on `demo-fallback` (pre-brain `main`, cut at `dee49e4` if a fallback branch is wanted)

**Rehearsed first, on the evening of Aug 6** — it is the script already known to work.

What §A explicitly **does not** show, and what the narrator must not claim: **no topic labels in
the arrival briefing** (`/watermark` has no `topics` field on `main`), and **`seg-005a`/`seg-005b`
do not merge** (they are 24 apart and `main`'s merge candidates are a 20-deep recency slice). What
it does show: one shared memory, two agents, semantic retrieval over the whole log.

```bash
export PATH="/opt/homebrew/bin:$PATH"
cd /Users/siddharthsingh/Dev/synapse-exec/e5
git switch demo-fallback && uv sync
uv run synapse-service &
sleep 2
```

**Beat 1 — create the session.**

```bash
SID=$(curl -s -X POST localhost:8899/v1/sessions \
      -H 'content-type: application/json' \
      -d '{"purpose":"fec decode on the NPU","created_by":"siddsing"}' \
      | python3 -c 'import sys,json;print(json.load(sys.stdin)["shared_id"])')
echo "$SID"
```
Expected: `201`, a `shared_id` string.

**Beat 2 — push 1 (aditya).**

```bash
curl -s -w '\nHTTP %{http_code}\n' -X POST localhost:8899/v1/sessions/$SID/findings \
     -H 'content-type: application/json' -d @.measurements/demo-push1.json
```
Expected: `HTTP 200`, body `{"accepted": 10, "memory_version": ..., "synthesized": ...}` —
`push_findings` (api.py) returns a bare `JSONResponse`, i.e. **200**, never `201`. Only
`POST /v1/sessions` (Beat 1) mints with `201`. `-w` is kept alongside the body, not instead of it
— `-o /dev/null` on this beat would discard the very `accepted` count the beat is checking.

**Beat 3 — a teammate connects: arrival briefing.**

```bash
curl -s "localhost:8899/v1/sessions/$SID/watermark?agent_session=as-observer"
```
Expected: `200`; a body with **counts and types**, no `topics` key.

**Beat 4 — push 2 (akhil), then the three queries from `as-observer`.**

```bash
curl -s -w '\nHTTP %{http_code}\n' -X POST localhost:8899/v1/sessions/$SID/findings \
     -H 'content-type: application/json' -d @.measurements/demo-push2.json
for Q in "what do we know about timing" "why does the decode fail" "what should I avoid touching"; do
  curl -s -X POST localhost:8899/v1/sessions/$SID/query \
       -H 'content-type: application/json' \
       -d "{\"query\":\"$Q\",\"agent_session\":\"as-observer\"}"
  echo
done
```
Expected: push `HTTP 200`, `accepted` `14` (running total 24, above `TOP_K` — the whole 24-finding
log is the candidate set on `main`, there is no lane selection to bypass here). Three query
responses, each a body of the shape `{"findings": [...]}` — `/query` (api.py) has no `answer` key
and no `sources` key; the ranked Findings themselves are the response. **Record the responses
verbatim in `docs/STATE.md` — they are the `main` half of Task 13's A/B.**

**Beat 5 — push 3, re-query.**

```bash
curl -s -w '\nHTTP %{http_code}\n' -X POST localhost:8899/v1/sessions/$SID/findings \
     -H 'content-type: application/json' -d @.measurements/demo-push3.json
curl -s "localhost:8899/v1/sessions/$SID/watermark?agent_session=as-observer" | python3 -m json.tool
```
Expected: `accepted` `2` (running total 26). `seg-005a` and `seg-005b` remain **two separate,
unmerged findings** — say so explicitly; it is the contrast §B exists to draw.

```bash
kill %1   # stop the service
```

---

## §B — the integrated demo, on `main` (the brain merged 2026-08-05)

Identical beats, plus the three the integration buys, each named against the task that ships it:

- the arrival briefing reads *"The team is working on: …"* with real medoid topic labels
  (**Task 10**);
- push 3 merges `seg-005b` into `seg-005a`'s lineage, 24 findings after the fact (**Task 8**) —
  the `/findings` response carries `synthesized: true` and the merged pair leaves `retrievable`;
- `/query` sends 14 candidates instead of 24 and the answers are at least as good (**Task 9**, and
  Task 13 Step 2 is where "at least as good" stops being an assumption);
- kill the service, contribute once more (through the orchestrator, not a raw push at the
  service), restart, `synapse-orchestrator resync`, re-query (**Task 11 Step 2**).

```bash
export PATH="/opt/homebrew/bin:$PATH"
cd /Users/siddharthsingh/Dev/synapse-exec/e5
git switch feat/brain-integration && uv sync
uv run synapse-service &
SVC_PID=$!
uv run synapse-orchestrator --port 8787 --service-url http://127.0.0.1:8899 --state-dir .synapse &
ORCH_PID=$!
sleep 2
```
The orchestrator is started here, alongside the service, even though nothing needs it before Beat
7 — its producer endpoint is what Beat 7 pushes through, and `register_tools`/`build_app` register
it unconditionally at boot (cli.py), so starting it early costs nothing and there is no second
process to remember to bring up mid-beat. `$SVC_PID`/`$ORCH_PID` (not `%1`/`%2`) track the two
background processes from here on — job numbers get confusing with two backgrounded servers, one
of which gets killed and restarted mid-script.

Right after these two boot steps, open **`http://127.0.0.1:8790/debug`** (`synapse-worker`) and
**`http://127.0.0.1:8899/debug`** (`synapse-service`, mounted on the same port the beats below
already curl) side by side — Beats 3–5 are *watchable*: NPU-now counts the distil seconds live
when a worker is condensing, and the `Merged` log-tail entry on the service page appears the
moment Beat 5's push lands, ahead of the narrator reading the JSON off the terminal. (This script's
own pushes go straight from `.measurements/demo-push*.json` to the service by `curl`, bypassing a
live worker entirely, so `/debug`'s NPU-now reads `idle` throughout unless a `synapse-worker run`
is also on stage — the service side is live regardless, since every push and merge above goes
through it either way.)

**Beats 1–4** are identical to §A's, against the new `$SID`. At beat 4's `/watermark`, expected
output now additionally carries a `topics` key with at least one label derived from the pushed
findings (**Task 10**).

**Beat 5 — push 3, the merge claim.**

```bash
curl -s -X POST localhost:8899/v1/sessions/$SID/findings \
     -H 'content-type: application/json' -d @.measurements/demo-push3.json | python3 -m json.tool
curl -s "localhost:8899/v1/sessions/$SID/watermark?agent_session=as-observer" | python3 -m json.tool
```
Expected: `HTTP 200`, `accepted` `2` (running total 26), and `synthesized: true`.

`synthesized: true` on its own is **not** the evidence the merge happened — `api.py` computes it
as `memory_version > version_before`, and `synthesis.merge` bumps `memory_version` unconditionally
on every structurally-valid verdict, merges or not (`adr/0004`'s Amendment, 2026-08-05: it counts
**verdict rounds applied**, not merges — see the Amendment for the corrected gloss). A verdict with
`"merges": []` renders the exact same `synthesized: true` as the flagship merge. The `/watermark`
call above is the actual observation: sum its `by_type` counts and compare against §A's — **25**
here (26 pushed findings minus 2 merge sources plus 1 synthesized result), against **26** in §A,
where `seg-005a`/`seg-005b` never merge. If the sum reads 26 here too, the merge did not happen —
say so, do not claim it off `synthesized: true` alone. `seg-005b` merged into `seg-005a`'s lineage
despite being 24 findings apart (**Task 8**: the symbol/lexical lanes, not recency, surface the
pair).

**Beat 6 — re-query, compare candidate count.**

```bash
for Q in "what do we know about timing" "why does the decode fail" "what should I avoid touching"; do
  curl -s -X POST localhost:8899/v1/sessions/$SID/query \
       -H 'content-type: application/json' \
       -d "{\"query\":\"$Q\",\"agent_session\":\"as-observer\"}"
  echo
done
```
Expected: each response is `{"findings": [...]}` (no `answer`/`sources` keys — see Beat 4), built
over **14 candidates**, not the full 26-entry log (**Task 9**). Record the three responses
verbatim — this is the branch half of Task 13's A/B.

**Beat 7 — kill, contribute (through the orchestrator), restart, resync, re-query.**

Every push in Beats 1–6 above is a raw `curl` straight at the service (`localhost:8899`) — that is
correct for demonstrating ingest/synthesis/retrieval, but it means nothing has yet gone through the
orchestrator's `Relay`, and this beat is the one that specifically needs it: Task 11 Step 2 is
recoverable-after-restart, and what makes it recoverable is the Finding sitting durably in
`.synapse/relay/findings.jsonl`, not in the (now-dead) service's memory. A raw `curl` at the
service, issued while the service is down, writes nothing anywhere and leaves `resync` with an
empty log to replay — see Task 11's own named fail condition ("`resync` prints `re-pushed 0`").

`localhost:8787` below is the **orchestrator's** producer endpoint (Plan D.1), not the service.
Reaching it requires a `LocalBinding` on disk for the agent the Finding claims
(`bindings/claude-code.json` under `--state-dir .synapse`) — ordinarily written by
`synapse-worker join $SID` from inside a live coding-agent transcript. The one-off snippet below
writes the identical `SessionBinding` shape directly (same fields `join_session` produces —
`synapse_contracts.binding.write_binding`, matching `scripts/verify_orchestrator.py`'s own pattern
for making `join` deterministic in a rehearsal) so this beat does not depend on the narrator's
terminal happening to be inside a live, freshly-touched transcript at demo time:

```bash
python3 - <<PY
from synapse_contracts.binding import write_binding, SessionBinding
from datetime import datetime, timezone
write_binding(".synapse/bindings/claude-code.json", SessionBinding(
    agent_session_id="as-demo-aditya", shared_id="$SID", contributor="aditya",
    agent="claude-code", transcript_path="(demo — bound directly, no live transcript needed)",
    pinned_at=datetime.now(timezone.utc)))
PY

kill $SVC_PID        # service only — the orchestrator stays up
sleep 1
curl -s -w '\nHTTP %{http_code}\n' -X POST localhost:8787/producer/findings \
     -H 'content-type: application/json' -d '{"findings": [{
       "id": "f-demo-recovery-01", "type": "learning",
       "text": "The resync recreate pass must run before the retry loop, or queued findings never flush.",
       "attributions": [{"contributor": "aditya", "agent_session": "as-demo-aditya", "agent": "claude-code"}],
       "ts": "2026-08-05T09:05:00Z", "refs": [], "provenance": "contributed", "status": "kept",
       "merged_from": [], "merged_into": null}]}'
```
Expected: `HTTP 200`, body `{"accepted": 1, "sent": false}` — the Finding is durably recorded in
`.synapse/relay/findings.jsonl` (`app.py`'s `producer_findings` calls `relay.record()` before
attempting `relay.flush()`), then `flush()`'s `_post` gets a connection error against the dead
service and returns `"retry"`, so nothing is written to `sent.jsonl`. This is what "the failed
contribute is recorded in the orchestrator's Relay write-ahead log" concretely means — check
`cat .synapse/relay/findings.jsonl` on stage if there is time.

```bash
uv run synapse-service &
SVC_PID=$!
sleep 2
uv run synapse-orchestrator resync --state-dir .synapse --service-url http://127.0.0.1:8899
curl -s "localhost:8899/v1/sessions/$SID/watermark?agent_session=as-observer" | python3 -m json.tool
```
Expected: the restarted service has a fresh, empty `InMemoryStore` — `$SID` is momentarily unknown
to it. `resync` prints `resync: re-pushed 1 finding(s) across 1 session(s) (current session:
'sh-...'; synthesized: [...])` and exits `0` — NOT `re-pushed 0`, which would mean the recreate
pass never ran (Task 11's named fail condition). Under the hood: `cmd_resync` Step 1 first
`POST /v1/sessions`s `$SID` back into existence (create-or-return, invariant 4), THEN re-pushes the
one queued finding to it, THEN calls `/synthesize`. The closing `/watermark` is `200`, not `404`,
with `by_type: {"learning": 1}` reflecting the recovered finding and `purpose: "(recovered by
resync)"` — the retained log does not carry the original purpose; that is `docs/STATE.md`'s
documented, honest gap, not a bug in this beat.

```bash
kill $SVC_PID $ORCH_PID
```
