# Implementation report — 2026-08-05 · Shared Memory

**Package:** `packages/service` (`synapse_service`)
**Covers:** Plan C Task C.2 (storage seam) and the retrieval half of C.4/C.5
**Branch:** `feat/shared-memory-store`
**Tests:** 75 new, 266 total green offline. No model, no network, no key.

---

## TL;DR

The storage seam and candidate selection exist. Nothing in this package calls a model — deliberately, because the parts that fail *silently* are deterministic and testable, and they come first. The merge call sits on top once recall is known.

The log is append-only and current state is derived by folding it ([ADR 0004](./adr/0004-the-log-is-append-only-and-state-is-a-fold.md)). Candidate selection is five lanes unioned, and the prompt it feeds stays a fixed size no matter how large the log gets — measured identical at 422 and 2,022 findings.

Two design flaws were found by measuring rather than by thinking. The topic lane, which the design leaned on hardest, currently contributes **nothing**.

---

## Part 1 — What was built

```
log.py       append-only entries: FindingAppended · Merged · TopicAssigned · TopicSplit
fold.py      derives the current view; RETRIEVABLE defined once, here
symbols.py   exact lane — numbers with units, identifiers, paths, config keys
lexical.py   BM25 over an inverted index
semantic.py  Embedder protocol · offline HashingEmbedder · vectors · topic centroids
lanes.py     five lanes, RRF fusion, reserved floors, lane provenance per candidate
store.py     the seam — append · merge · candidates · rebuild
corpus.py    synthetic duplicate pairs in four difficulty bands
recall.py    the harness
```

Plus `scripts/measure_recall.py`.

### The seam

Plan C.2 asked for three operations. Two signatures differ from the plan, both to keep an option open the original shape closed:

```
append(finding)            -> Appended        was: store_findings(shared_id, Finding[])
merge(result, sources)     -> Appended        new: merging is a write, not a mutation
view()                     -> View            was: get_context(shared_id)
candidates(text, top_k=…)  -> CandidateSet    was: query_candidates(shared_id)
```

`candidates()` takes **the text to search for**. That is the substantive change. Plan C.4's merge prompt sees "the bounded Working Memory plus the new findings", which means two findings only ever merge if they land in the same batch — Aditya pushing at 14:02 and Akhil at 14:09 never meet. One lookup, called with an incoming finding by synthesis and with a question by retrieval, is what makes cross-contributor merging possible at all.

It also takes `top_k`, so swapping the in-memory lanes for an approximate index later changes no caller.

### Candidate selection

Five lanes, **unioned, never intersected**, because this is a recall problem: a missed candidate is a merge that never happens and nothing reports it, while a spurious one costs ~50 tokens and a "no".

| Lane | Mechanism | Scans? | Catches |
|---|---|---|---|
| `symbols` | exact index over extracted marks | no | `40 ms` on both sides |
| `lexical` | BM25, inverted index | no | reworded, same vocabulary |
| `vector` | cosine over stored embeddings | yes, one pass | paraphrase, no shared words |
| `topic` | nearest centroid's members | no | a decision that *governs* a finding |
| `recent` | the last M, unconditionally | no | too new to have settled |

Fused with Reciprocal Rank Fusion. The lanes produce scores on incomparable scales; normalising per lane makes a lane that found nothing look as confident as one that found the answer, and hand-weighting bakes in an assumption before anything is measured. RRF fuses on rank, needs no tuning, and rewards agreement across lanes.

Every candidate carries **which lanes surfaced it**. That is not decoration — it makes lane yield measurable, which is the only honest answer to whether a lane earns its cost.

### Topics: geometry decides, a model only names

Membership is cosine against centroids — deterministic, drift-free, no model. Naming is one call when a cluster is *born*, not per finding, and rides on the merge call. **Retrieval uses centroids, not names**, so a bad label is cosmetic.

This replaced an earlier design that had the on-device 4B emitting a label per finding. That was wrong for the reason ADR 0003 already documents: it is the least reliable component in the system, it sees one segment at a time with no view of the session's vocabulary, and it would fragment `pooling` / `conn pool` / `connection pooling` within an hour.

---

## Part 2 — Measurements

`uv run python scripts/measure_recall.py --distractors 2000`

```
candidate recall @ K=14        422 findings    2,022 findings
  overall                          86.4%           86.4%
  symbol band                       100%            100%
  lexical band                      100%            100%
  paraphrase band                   100%            100%
  governing band                     25%             25%
  partners from the topic lane         0               0
```

**Recall does not move as the corpus grows 5×.** That is the one genuinely encouraging line, and it is the property the whole design was for.

**Prompt size is flat in N.** The only thing in it that scales with the log is the number of digits in the corpus count.

### Read these numbers correctly

They are a **regression guard, not evidence**. Two reasons, both disqualifying on their own:

1. **The corpus is synthetic and was authored alongside the lanes it measures.** This is trap #3 from 2026-08-03 — *do not let one person write both the prompt and the eval target* — and it applies here in full.
2. **The offline embedder is a token hash with no paraphrase signal.** The two lanes that exist to catch paraphrase are measured with that capability switched off. `HashingEmbedder` tests plumbing; it does not test the lane.

No number above belongs in a demo.

---

## Part 3 — Two flaws found by measuring

Both were invisible to design review and appeared immediately under measurement.

### 1. Rank fusion assumes its lanes are independent. Two of them are not.

A pair sharing `1.9.4` — a symbol held by exactly two findings in 422 — was pushed out of the top 14 by twelve distractors that each scored on `lexical` **and** `vector`.

A bag-of-words hash embedder is a lossy BM25. Those two lanes read the same surface form, so their agreement double-counts one signal and outvotes genuine evidence. RRF's core assumption quietly did not hold.

Fixed with a **reserved floor** for the symbols lane rather than a tuned weight — a weight fitted against a synthetic corpus is fitted to noise, whereas a floor is a guarantee that the highest-precision lane is never shut out. Symbol band went 87.5% → 100%.

**This must be re-checked when a real embedder lands.** A real embedding model is genuinely independent of BM25, which changes the relationship the fix was built around.

### 2. The recency hedge was crowding out what it hedges for.

`recent` contributed 8 unranked picks into a 14-slot result, each scoring identically under RRF to a symbol lane's top hit. Noise reached rank 4 of 14.

It is not a ranking, so it no longer competes on rank; it gets a small reserved floor instead. A hedge that displaces the thing it is hedging for is worse than no hedge.

---

## Part 4 — Gaps

### 1. The topic lane surfaced zero partners

Not "unverified" any more. **Zero**, at both corpus sizes, and zero uniquely. The governing band it exists for sits at 25%.

Part of that is the offline embedder having no paraphrase signal, and part is a corpus artifact — the distractor templates cluster artificially, which is the generated-corpus failure this report's own warning describes. But a lane at exactly zero has to justify itself on a real corpus or be deleted, and the design is arranged so that deleting it changes nothing else.

This is the lane the design leaned on hardest. It is currently the weakest thing in it.

### 2. The corpus is the blocking dependency, still

Unchanged from 2026-08-04 and now blocking more. Real two-people-found-the-same-thing pairs from actual sessions, not written by whoever owns the merge logic. Every number in Part 2 is waiting on it.

### 3. No real embedder is wired

The `Embedder` protocol exists and `HashingEmbedder` satisfies it offline. The Cirrascale key has `bge` embeddings (verified live 2026-08-03) and the existing OpenAI-compatible adapter should reach it. Until it does, the vector and topic lanes have not been evaluated — only their plumbing has.

### 4. `Finding.merged_into` / `Finding.status` are not written

See ADR 0004's Consequences. The service derives supersession; it does not write those fields. Treat them as **undefined on anything the service returns** until the team picks Option A (project on egress) or Option B (drop from the contract). Option A is the lower-risk default.

### 5. Not built

No ingest API, no synthesis call, no watermark endpoint, no awareness support, no persistence. `K` is fixed at 14 with no measured basis for how it should grow with N. There is no alerting on topic health — collapse and fragmentation are both *detectable*, but detection assumes something is watching, and nothing is.

---

## Part 5 — Integration notes

**Nothing here calls a model or the network.** The package depends only on `synapse-contracts`. It runs in CI with no keys.

```python
from synapse_service import Store

store = Store(shared_id="checkout-timeouts", purpose="fix flaky timeouts")
appended = store.append(finding)          # appended.topic_founded -> owes a naming call
result = store.candidates(new_finding.text, exclude=frozenset({new_finding.id}))
print(result.coverage_line())             # "searched 2847 findings · 5 lanes · showing top 14"
for candidate in result.candidates:
    print(candidate.render())             # "#1044 … [symbols · vector: 40ms]"
```

To swap the embedder, pass anything satisfying `Embedder` to `Store(embedder=…)`. Nothing else changes.

**For whoever builds synthesis:** `candidates()` is the input to the merge prompt, and `merge(result, sources)` is how its output is recorded. Do not write `merged_into` — appending the `Merged` entry is what makes the sources disappear from the view.

**One invariant to preserve:** `symbols.py` has no entry point that takes a `Segment`, and a test asserts it never grows one. Extracting symbols from raw transcript content instead of the abstracted finding text would carry redacted material across the device boundary in tag form, while `verbatim_overlap` still reported clean — the exact shape of the `default_pool_size=25` leak that scored 0.00.

---

## Part 6 — What changed in the plans

| Plan item | Before | Now |
|---|---|---|
| Plan C.2 seam | `query_candidates(shared_id)` | `candidates(text, top_k=…)` — one lookup, two callers |
| Plan C.2 storage | "idempotent upsert by id" | append-only; the fold takes the first write of an id — ADR 0004 |
| Plan C.4 merge input | Working Memory + new findings | + retrieved candidates, or cross-contributor merges only happen inside a batch |
| ADR 0002 tombstones | `merged_into` set on the original | derived from a later `Merged` entry; intent unchanged, mechanism superseded |
| Plan C.4/C.6 topics | implied model-assigned | centroid membership; a model names a cluster once |
| Plan D.4 `resync` | safe because ingest upserts by id | safe by construction — a replayed append is inert |
