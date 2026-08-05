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

## §A — the fallback, on `demo-fallback` (plain `main`)

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
curl -s -o /dev/null -w '%{http_code}\n' -X POST localhost:8899/v1/sessions/$SID/findings \
     -H 'content-type: application/json' -d @.measurements/demo-push1.json
```
Expected: `201`, `accepted` count `10`.

**Beat 3 — a teammate connects: arrival briefing.**

```bash
curl -s "localhost:8899/v1/sessions/$SID/watermark?agent_session=as-observer"
```
Expected: `200`; a body with **counts and types**, no `topics` key.

**Beat 4 — push 2 (akhil), then the three queries from `as-observer`.**

```bash
curl -s -o /dev/null -w '%{http_code}\n' -X POST localhost:8899/v1/sessions/$SID/findings \
     -H 'content-type: application/json' -d @.measurements/demo-push2.json
for Q in "what do we know about timing" "why does the decode fail" "what should I avoid touching"; do
  curl -s -X POST localhost:8899/v1/sessions/$SID/query \
       -H 'content-type: application/json' \
       -d "{\"query\":\"$Q\",\"agent_session\":\"as-observer\"}"
  echo
done
```
Expected: push `accepted` `14` (running total 24, above `TOP_K` — the whole 24-finding log is the
candidate set on `main`, there is no lane selection to bypass here). Three query responses, each
with a non-empty `answer` and a `sources` list. **Record the answers verbatim in `docs/STATE.md`
— they are the `main` half of Task 13's A/B.**

**Beat 5 — push 3, re-query.**

```bash
curl -s -o /dev/null -w '%{http_code}\n' -X POST localhost:8899/v1/sessions/$SID/findings \
     -H 'content-type: application/json' -d @.measurements/demo-push3.json
curl -s "localhost:8899/v1/sessions/$SID/watermark?agent_session=as-observer" | python3 -m json.tool
```
Expected: `accepted` `2` (running total 26). `seg-005a` and `seg-005b` remain **two separate,
unmerged findings** — say so explicitly; it is the contrast §B exists to draw.

```bash
kill %1   # stop the service
```

---

## §B — the integrated demo, on the cut (`feat/brain-integration`)

Identical beats, plus the three the integration buys, each named against the task that ships it:

- the arrival briefing reads *"The team is working on: …"* with real medoid topic labels
  (**Task 10**);
- push 3 merges `seg-005b` into `seg-005a`'s lineage, 24 findings after the fact (**Task 8**) —
  the `/findings` response carries `synthesized: true` and the merged pair leaves `retrievable`;
- `/query` sends 14 candidates instead of 24 and the answers are at least as good (**Task 9**, and
  Task 13 Step 2 is where "at least as good" stops being an assumption);
- kill the service, contribute once more, restart, `synapse-orchestrator resync`, re-query
  (**Task 11 Step 2**).

```bash
export PATH="/opt/homebrew/bin:$PATH"
cd /Users/siddharthsingh/Dev/synapse-exec/e5
git switch feat/brain-integration && uv sync
uv run synapse-service &
sleep 2
```

**Beats 1–4** are identical to §A's, against the new `$SID`. At beat 4's `/watermark`, expected
output now additionally carries a `topics` key with at least one label derived from the pushed
findings (**Task 10**).

**Beat 5 — push 3, the merge claim.**

```bash
curl -s -X POST localhost:8899/v1/sessions/$SID/findings \
     -H 'content-type: application/json' -d @.measurements/demo-push3.json | python3 -m json.tool
```
Expected: `accepted` `2` (running total 26), and `synthesized: true` — `seg-005b` merged into
`seg-005a`'s lineage despite being 24 findings apart (**Task 8**: the symbol/lexical lanes, not
recency, surface the pair).

**Beat 6 — re-query, compare candidate count.**

```bash
for Q in "what do we know about timing" "why does the decode fail" "what should I avoid touching"; do
  curl -s -X POST localhost:8899/v1/sessions/$SID/query \
       -H 'content-type: application/json' \
       -d "{\"query\":\"$Q\",\"agent_session\":\"as-observer\"}"
  echo
done
```
Expected: each response's prompt is built over **14 candidates**, not the full 26-entry log
(**Task 9**). Record the three answers verbatim — this is the branch half of Task 13's A/B.

**Beat 7 — kill, contribute, restart, resync, re-query.**

```bash
kill %1
sleep 1
curl -s -o /dev/null -w '%{http_code}\n' -X POST localhost:8899/v1/sessions/$SID/findings \
     -H 'content-type: application/json' -d @.measurements/demo-push3.json    # service is down: this fails
uv run synapse-service &
sleep 2
uv run synapse-orchestrator resync
curl -s "localhost:8899/v1/sessions/$SID/watermark?agent_session=as-observer" | python3 -m json.tool
```
Expected: the failed contribute is recorded in the worker's write-ahead log; after restart,
`synapse-orchestrator resync` recreates the session (if needed) and re-pushes the queued finding
without a human copying anything by hand (**Task 11 Step 2**).

```bash
kill %1
```
