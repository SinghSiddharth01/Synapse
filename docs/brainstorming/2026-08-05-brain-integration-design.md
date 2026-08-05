# Brain integration design — one service, main's surface on the branch's core

**Status:** Proposed 2026-08-05. Decides every open question in §4–§9; the only thing left open by choice is the topic lane's flag, which a two-second harness run closes (§6.4).
**Integrates:** `feat/shared-memory-store` (HEAD `d491956`, branched from `8695eed` = pre-E3 main) into `main` (`dee49e4`, 387 green).
**Amends:** [`adr/0004`](../adr/0004-the-log-is-append-only-and-state-is-a-fold.md) — three amendments in §3.3, its Option A closed in §4.3 · Plan C.2's storage seam (§4.1) · Plan C.4's `CANDIDATE_WINDOW` mechanism (§6.1).
**Companions:** `/CONTEXT.md` (vocabulary) · [`docs/plans/README.md`](../plans/README.md) (six invariants) · `adr/0001`–`0003` · [`docs/STATE.md`](../STATE.md).
**Deadline:** demo Aug 7. Every decision below was made against that, and where the deadline is the reason, it says so.

---

## 1. Context

`main` runs the loop end to end: transcript → segmented → triaged → distilled on the NPU → relayed → synthesized → retrieved. 387 tests green, offline. That is the thing that must not break.

The branch adds a complete, model-free retrieval core to the same package: an append-only log, a pure fold that derives visibility, five candidate-selection lanes fused by RRF, deterministic centroid topics, and a recall harness with a synthetic corpus. 266 tests green in 1.9 s, no keys, no network, no model. Contracts are byte-identical between the two checkouts, so the branch's code already operates on main's exact `Finding`.

Nine files collide. Two of them are `packages/service/src/synapse_service/{store.py,__init__.py}` — two different service cores at one module path. That collision is the whole problem, and it is a false one: the two are not competitors. Main's `InMemoryStore` is a **multi-session registry**; the branch's `Store` is **one Shared Session's memory**. A registry holding N of the other is the integrated system, and almost everything else follows from saying it that way.

The design goal, stated once so the rest can be checked against it:

> Keep every externally visible property `main` has proven — six routes, synthesis semantics, suppression, watermark, self-heal, producer contracts, all six invariants — and replace the *mechanism* underneath with the branch's log + fold + lanes wherever the mechanism is genuinely better. Nothing in the demo path changes shape; two things in it change from "grows without bound" to "fixed cost."

---

## 2. What the two reviews found

### 2.1 The branch review

The retrieval half is the genuine win and adoptable behind main's existing seam with almost no risk. `symbols.py`, `lexical.py`, `semantic.py`'s `Embedder`/`VectorIndex` are stdlib-only, self-contained, and land as new filenames main does not have. The recall harness is a live regression guard, not decoration — removing the symbols reserved floor drops the symbol band 100% → 87.5% and fails a named test. The documentation is calibrated to an unusual degree: the branch labels its own corpus as non-evidence in three places and names its topic lane as the weakest thing it built.

Four defects that must be fixed before or during wiring, all confirmed by mutation or probe:

| # | Defect | Consequence if carried in |
|---|---|---|
| 1 | `Store.append`'s duplicate guard is pinned by no test (delete it → 75 pass) | a resend re-invokes the model, defeating `test_replayed_push_is_a_noop_and_skips_the_model` |
| 2 | Reserved floors waste result slots instead of back-filling (`top_k=14` → 12 candidates) | fewer candidates exactly when the shared symbol is common — the case breadth matters most |
| 3 | No suppression anywhere in the package; `exclude=` is the right seam, nothing populates it | wiring `candidates()` into `/query` as-is silently violates invariant 3 |
| 4 | Topic membership is never pruned on merge; `unhealthy_topics()` counts invisible findings | "collapse looks like working" — the exact shape `semantic.py:183` warns about |

Plus: `CandidateSet.searched` does not subtract `blocked`, so `coverage_line()` over-reports once suppression is wired; three load-bearing docstring arguments (IDF, the `0.45` topic threshold, the signed-bucket trick) are unpinned; two tests are vacuous; `DEFAULT_RECENT = 8` is dead (the lane contributes 2).

### 2.2 The merged-service review

The "storage seam" main advertises **is not a seam**. Synthesis applies every verdict by mutating objects the store handed back — `s.merged_into = synthesized.id` (`synthesis.py:228`), `finding.status = TRIVIAL` (`:236`), `ctx.conflicts = resolved` (`:269`), `ctx.working_memory = ...` (`:272`). `InMemoryStore` has **no write path for any verdict field**; `bump_version` is its only verdict-writing method. Any replacement store that returns copies, frozen records, or a derived view silently discards every merge, tombstone, trivia mark and conflict *while the API-level tests still pass* — and `test_store.py:35` blesses the pattern by mutating through `store.get(...)`.

That is the single mechanical blocker for any swap, and it decides the order of work (§8).

Two more that bear directly on the integration: `memory_version` means **verdict rounds applied** ⟨CORRECTION, corrected 2026-08-05: this sentence previously said "merges completed"; `synthesis.py:273` bumps unconditionally, merges or not, and `test_full_flow_push_watermark_query` pins it⟩, is asserted as such in a dozen places, and collides head-on with `Log.version = len(entries)`, which advances on every `TopicAssigned` and every inert resend. And — independent of the branch entirely — the documented restart-recovery path is *impossible today*: `POST /v1/sessions` mints a server-side random `sh-…`, so after a restart every resync POST 404s forever while the Relay logs "Service unavailable" (`relay.py:192` catches `httpx.HTTPError`, which includes `HTTPStatusError`) and retries forever.

### 2.3 ADR 0004's bug — adjudicated

Both reviews ran the code rather than reading it, independently, and agree.

**The bug as stated is FALSE against main.** ADR 0004's Context dramatizes a resync re-writing `#41` with `merged_into=None` and resurrecting it. That is true of a whole-object upsert (`table[f.id] = f`). Main never shipped one: `store.py:58` is `if finding.id not in table`, the module docstring names FIRST-WRITE-WINS and gives exactly this scenario as its reason, and it is pinned three ways — `test_upsert_is_first_write_wins`, `test_replayed_original_never_clobbers_a_tombstone`, and at the route by `test_replayed_push_is_a_noop_and_skips_the_model` (`accepted == 0` means `merge()` is never called, so a replay does not even reach the model). Re-run: 387 passed.

**The bug that IS real is not fixed by ADR 0004 either.** A service restart empties `InMemoryStore`. The synthesized finding was minted inside the service (`syn-<uuid4>`, `provenance=SYNTHESIZED`) and was never sent to any orchestrator, so it lives in no durable log anywhere. Verified on both implementations: after resync into a fresh store, main gives back `['f-41','f-58']` with `working_memory=''`, `conflicts=[]`, `memory_version=0`; the branch's own `Store` gives back `visible_ids == ('f-41','f-58')` — and worse, the branch has no synthesis and no `/synthesize`, so nothing re-merges them. `store.py`'s own docstring concedes it: *"Persistence is deliberately absent."*

**We adopt the decision anyway**, for three reasons that survive the motivating bug being false:

1. **Property beats discipline, and we are about to add write paths.** First-write-wins is a rule someone must re-apply at every future write path, and its failure mode is silent. This week adds `supersede`, `mark_trivial` and a projection; next week adds persistence. "A rule people have to follow became a property the structure guarantees" is a real improvement in kind — it is just not a bug fix, and must not be sold to the team as one.
2. **It closes the producer-forged-verdict hole for free.** Today `api.push_findings` runs only `Finding.model_validate`, and the Relay POSTs to the service directly. A first push carrying `merged_into="x"` or `status=trivial` lands, is excluded from retrieval forever, and cannot be corrected by any later push because upsert ignores known ids. Under the fold, **visibility stops reading producer-writable fields at all** — it reads entries. The forged field is inert. Neither review claimed this; it is the strongest argument the ADR has and it is not in the ADR.
3. **It is the enabling step for durability.** A log is `for entry in entries: write(json)`. Mutable cross-referenced state is not. `Store.rebuild()` already proves replay is sufficient. That makes the real fix (§5) a ~20-line change *after* the demo instead of a project.

### 2.4 Three amendments ADR 0004 needs

Written as an **Amendment** section appended to the ADR during the merge, not as a rewrite of the teammate's text.

- **A1 — retarget the Context.** State that main's first-write-wins upsert already closes the replay-while-alive case, cite `test_replayed_original_never_clobbers_a_tombstone`, and retarget the motivation at the restart case + the enabling argument above. An hour's work that makes the decision defensible; leaving it as written invites someone to merge a synthesis rewrite two days before the demo to fix a bug that is not there.
- **A2 — the order argument is wrong, and the property is stronger than claimed.** The ADR says the resync is inert because "the `Merged` entry at #59 is still in the log and still **earlier**." Order is irrelevant: `fold` accumulates `superseded_by` across all entries and filters at the end (`fold.py:107-114`), so a `Merged` entry appearing *after* the re-append suppresses just as well. Correct the reasoning; keep the conclusion.
- **A3 — a fifth entry kind, `MarkedTrivial`.** The four kinds cannot express the trivia verdict, yet `fold.py:113` reads `findings[fid].status is FindingStatus.KEPT` — a field nothing in the branch ever writes, and the same field the ADR tells readers to treat as undefined. Per `adr/0003` the trivia filter is load-bearing (triage is upstream, the distiller no longer judges), so adopting the branch as-is deletes one of the two homes durability judgment has. §4.2 builds it.

---

## 3. The integrated architecture

One service process. Main's registry on top; the branch's Shared Memory underneath, one per Shared Session.

```
                         Synapse Service  (one process, one provider)
 ┌────────────────────────────────────────────────────────────────────────────┐
 │ api.py   POST /v1/sessions            (create-or-return: accepts shared_id) │
 │          POST …/members  …/findings  …/synthesize   GET …/watermark         │
 │          POST …/query                                                       │
 │              │                                              │              │
 │              │  invariant 3 computed here (visible_to)      │              │
 │              │  exclude= ─────────────────┐                 │              │
 │ ┌────────────▼────────────────────────────┼─────────────────▼────────────┐ │
 │ │  InMemoryStore — the registry            │        KEPT FROM main       │ │
 │ │    sessions · members · SessionContext · last_seen                     │ │
 │ │    memory_version = verdict rounds applied  ⟨CORRECTION, corrected 2026-08-05: not merges completed⟩ │ │
 │ │      (NOT log length — §4.4)                                            │ │
 │ │    supersede() · mark_trivial() · set_context()          NEW (§4.1)    │ │
 │ │    get/all_findings/retrievable → Option A projection    NEW (§4.3)    │ │
 │ │                                          │                             │ │
 │ │   per shared_id ──►  SharedMemory  (memory.py)      ADOPTED from branch│ │
 │ │     ┌────────────────────────────────────┼───────────────────────────┐ │ │
 │ │     │  Log   append-only · five kinds    │                           │ │ │
 │ │     │    FindingAppended · Merged · MarkedTrivial(new)               │ │ │
 │ │     │    TopicAssigned · TopicSplit (kept, never called)             │ │ │
 │ │     │            │  fold()  pure · cached on log.version             │ │ │
 │ │     │            ▼                                                   │ │ │
 │ │     │  View   visible_ids · superseded_by · trivial · topic_of        │ │ │
 │ │     │         members_of  (visible-only — already correct)           │ │ │
 │ │     │            │                                                   │ │ │
 │ │     │  Indexes  symbols · lexical(BM25) · vectors(Embedder seam)      │ │ │
 │ │     │           topics  (index kept; LANE flagged off — §6.4)        │ │ │
 │ │     │            └──► select(text, exclude=) ──► CandidateSet(top-K) │ │ │
 │ │     └────────────────────────────────────────────────────────────────┘ │ │
 │ └──────────┬──────────────────────────────────────────┬─────────────────┘ │
 │            │ candidates(text, top_k, exclude)         │                   │
 │   synthesis.merge                              retrieval.query_findings    │
 │    pushed ∪ top-K others   (was: last 20 by arrival)   top-K, then         │
 │    fixed-cost prompt                                   visible_to  (was:   │
 │                                                        the ENTIRE log)     │
 └────────────────────────────────────────────────────────────────────────────┘
```

Two arrows in that picture are the product claim. `synthesis.merge` merging a near-duplicate that arrived forty findings ago is impossible on main today. `/query` sending fourteen findings instead of the whole log is what keeps an 8B working as the session grows. Everything else is plumbing that must not move.

### 3.1 Naming, so the collision stops being one

| Branch name | Integrated name | Why |
|---|---|---|
| `store.py` / `Store` | `memory.py` / `SharedMemory` | It is one Shared Session's Finding Log plus its derived indexes. `InMemoryStore` is the registry that pairs it with the Working Memory prose on `SessionContext` — together, `CONTEXT.md`'s "Shared Memory". The umbrella term finally has a home in code, and the filename conflict evaporates. |
| `tests/test_store.py` | `tests/test_memory.py` | main's `test_store.py` keeps its path and tests the registry. |
| `log.py` / `Log` | unchanged | Raw entry sequence. Teammate's file lands untouched. |

---

## 4. The storage seam — one revision, and it is not optional

### 4.1 The seam is fiction until verdict application is explicit

Plan C.2 promised "a narrow interface." What shipped is a concrete class with eleven methods, constructed internally by `build_app`, type-hinted by name in `synthesis.py:129` and `:111` — and, decisively, **bypassed** for every verdict write. `merged_into`, `status`, `conflicts` and `working_memory` reach storage by mutating returned references.

> **The seam revision:** verdict application becomes explicit store calls. Three methods, added to today's mutable `InMemoryStore` **first**, with the 387 green as the guard, *before* any branch code is merged.

```
supersede(shared_id, sources: list[FindingId], result: Finding) -> None
mark_trivial(shared_id, finding_ids: list[FindingId])          -> None
set_context(shared_id, *, working_memory=None, conflicts=None) -> None
```

`set_context` is not strictly required — `SessionContext` is registry-owned mutable state, not fold-derived — but it is one method and it removes the last mutation-through-reference, which is what lets a future durable context land without touching `synthesis.py` again.

This is the smallest change that makes the two implementations comparable at all. Do it first (§8, step 0). Until it exists, a fold-based store returns a view whose mutations go nowhere **and every API-level test still passes** — the worst possible failure shape two days out.

### 4.2 The fifth entry kind

```python
@dataclass(frozen=True)
class MarkedTrivial:
    finding_ids: tuple[FindingId, ...]
    kind: Literal["marked_trivial"] = "marked_trivial"
```

`fold` accumulates `trivial: set[FindingId]` and visibility becomes:

```
RETRIEVABLE  ==  fid not in superseded_by  and  fid not in trivial
```

Note what leaves: the fold no longer reads `findings[fid].status` at all. That is the point. Both service-written fields are now derived from entries, neither is read from a producer-supplied record, and the forged-verdict hole in §2.3(2) closes as a side effect rather than as a feature someone has to build. `RETRIEVABLE` stays defined in exactly one place (`fold.py`), which is invariant-2 hygiene main also has.

### 4.3 Option A, closed — and where the projection lives

ADR 0004 leaves Option A (project on egress) vs Option B (drop the fields from the contract) open, calling it "a team decision, not a resolved one." **Decided: Option A.** Option B is a three-track contract break two days out, for no gain.

The ADR's Follow-up asks for the projection "at the ingest boundary rather than inside the store." **We deviate, deliberately.** The projection lives in `InMemoryStore`'s read accessors — `get`, `all_findings`, `retrievable` return findings copied with `merged_into` and `status` filled from the `View`:

- The store is the only component holding the View, so it is the narrowest place the projection can live **where no caller can forget it**. `retrieval.py` never touches a store and must stay that way; `api.py` serialises whatever it is handed.
- It is what lets ~380 existing tests keep passing unchanged, including every `test_synthesis.py` assertion of the form `f.merged_into == syn.id`. That is not a convenience — it is the regression guard for the whole swap.
- The store keeps exactly one internal representation of supersession, which is what the ADR's Follow-up actually cares about.

Free consequence worth naming: a projected copy is a *copy*, so any surviving mutation-through-reference now fails loudly instead of silently. The blocker in §2.2 becomes a red test rather than a lost merge.

### 4.4 Two version counters, two jobs, never the same number

| Counter | Meaning | Who reads it |
|---|---|---|
| `SessionContext.memory_version` | **verdict rounds applied** — bumped once at the end of every structurally-valid verdict, merges or not ⟨CORRECTION, corrected 2026-08-05: this row previously read "merges completed"⟩ | `/findings`'s `synthesized`, `/synthesize`'s `synthesized`, `/watermark`'s `version` and `new_since`, `last_seen` |
| `Log.version` | entry count | fold-cache invalidation inside `SharedMemory`. **Never leaves the store.** |

`Log.version`'s docstring claims it *is* `memory_version` for the watermark. It is not, and taking that would make `synthesized` True on every push including a pure replay and turn `new_since` into a count of log entries (2+ per finding). Correct the docstring during the merge; keep `bump_version` exactly as main has it.

### 4.5 `upsert` keeps its behavioural contract

`upsert` returns **ids not previously seen**, not entries appended — `api.py:74`'s `if accepted:` is what keeps a replayed POST off the provider. `SharedMemory.append` decides "new" by `finding.id in self.view().findings` (the authority) rather than `in self.indexes.vectors.vectors` (a cache). That change is what finally makes the branch's duplicate guard testable: `accepted == 0` on a resend is the assertion, and it fails if the guard is deleted. Fixes branch-review defect #1.

---

## 5. Durability — the honest recovery story

**No, the service-side log does not fix the restart case.** Both reviews measured it on both implementations. The branch's `Log` is in-memory and dies with the process, and `Merged` is a service-authored entry that lives in no orchestrator's durable log. Append-only changes the *mechanism* of the replay-while-alive case (which main already handled correctly) and changes **nothing** about restart.

What we actually ship for Aug 7 is the ~15 lines that turn an unrecoverable demo failure into a recoverable one, and they are orthogonal to which store wins:

1. **`POST /v1/sessions` accepts an optional `shared_id`: create-or-return.** Today the id is minted server-side, so after a restart the old `sh-…` 404s and cannot be recreated by construction — every teammate must re-join a brand-new session mid-demo. The branch inherits this hole verbatim; it has no session registry at all.
2. **The Relay treats 4xx as terminal-and-loud, 5xx as retryable.** `relay.py:192` catches `httpx.HTTPError`, which includes `HTTPStatusError`, so a permanent 404 is indistinguishable from a transient outage and loops forever.

Recovery then reads, honestly:

> A service restart loses the in-memory log. Every orchestrator resyncs its retained durable log into the **same** `shared_id`; findings land (first write of an id wins, by construction now); one `POST /v1/sessions/{sid}/synthesize` re-derives Working Memory, conflicts and merges. **What is recomputed, not restored:** synthesized findings get new ids, Working Memory and Conflicts are re-derived by a fresh 8B call and may differ, and any contributor who does not resync is gone entirely. **What is not covered at all:** `/synthesize` has no caller in the product today — `cli.cmd_resync` only POSTs findings, and `push_findings` gates the model on `accepted > 0`, so a full resync into a store that already holds those findings never re-synthesizes. Wire `cmd_resync` to call it. That is the third small change.

Persisting the log to disk is the real fix, it is ~20 lines *because of* ADR 0004 (`rebuild()` already proves replay is sufficient), and it is **out of scope** — see §7. It would restore findings, merges and topics exactly; it would still not restore Working Memory or conflicts, which are not in the log. Half a story is not worth a day of the two we have.

---

## 6. Candidates: lanes replace recency, at both call sites

### 6.1 Synthesis

Main's candidate selection is a pure recency slice over dict insertion order — `others = [...][-CANDIDATE_WINDOW:]` — so in a 40-finding session, findings 1–20 are permanently unmergeable against anything new. The ADR 0002 merge simply stops happening as the session grows. The seg-005 pairing only works because both halves land in the same push.

**`CANDIDATE_WINDOW = 20` keeps its name and its meaning as a budget; only the selection rule changes.**

```
pushed     = every finding accepted in THIS call        (unconditional — the E3 starvation fix)
others     = ⋃ over pushed f of  store.candidates(sid, f.text, exclude=new_ids)
             deduped, capped at CANDIDATE_WINDOW
candidates = pushed + others
```

Plan C.4's fixed-cost property is preserved exactly. Both existing pins transfer with minimal edits (`test_synthesis.py:326`, `test_api.py:202`, plus `test_api.py:212`'s function-local `from synapse_service.synthesis import CANDIDATE_WINDOW`). One new test states the win: *an old near-duplicate outside the last twenty by arrival is still selected* — impossible today, and the substantive departure from Plan C.2 the branch was right about.

### 6.2 Retrieval, and invariant 3 at the lanes seam

`api.query` passes `candidates=store.retrievable(sid)` — the **entire** visible log — into one model prompt, uncapped, growing linearly. Replace with a bounded top-K, and populate the `exclude=` seam the branch left empty:

```python
visible    = store.retrievable(sid)
suppressed = {f.id for f in visible} - {f.id for f in visible_to(visible, agent_session)}
cands      = store.candidates(sid, body["query"], top_k=TOP_K, exclude=frozenset(suppressed))
ranked     = await query_findings(provider, context=..., query=...,
                                  candidates=[c.finding for c in cands.candidates],
                                  asking_agent_session=agent_session)
```

`visible_to` stays the **one** definition of invariant 3 and `query_findings` keeps applying it internally — idempotent, and the belt is the definition. Suppression is a pure predicate over a Finding: computing it across the whole visible log is an O(N) Python loop with no model and no prompt cost. The thing that must be bounded is the *prompt*, not the loop.

`CandidateSet.searched` becomes `len(visible − blocked)` so `coverage_line()` — whose stated job is making "I found no match" calibrated — stops over-reporting. Fixes branch-review defect #3 and its `searched` rider.

### 6.3 The reserved-floor under-fill

Fixed before wiring, not after: a reserved id already in `chosen` hits `continue` and nothing takes its place, while `budget` has already deducted the slot — `top_k=14` returns 12, in a module whose stated thesis is that every knob is set toward returning more, and precisely when the shared symbol is common. Back-fill from the fusion remainder up to `top_k`; new test asserts an exact count on a symbol-bearing query (the existing `test_top_k_bounds_the_result` uses a symbol-free query, so the reservation is empty and the count comes out right by accident). ~6 lines. Fixes branch-review defect #2.

`DEFAULT_RECENT = 8` is corrected in the docstring rather than in behaviour — the lane contributes `max(1, top_k // 5)` = 2, and raising it costs fusion slots the branch already measured as harmful. The harness decides if 2 is right, later.

### 6.4 Topics: index in, lane out, labels in the briefing

The branch's own measurement: the topic lane surfaced **0 partners and 0 uniquely** at 422 findings; `docs/STATE.md` on the branch has a section titled "The topic lane is on notice." A lane that returns a whole 40-member cluster into an RRF fusion is not free — those members take rank credit that can outvote real matches — and it has never supplied one.

| Piece | Call |
|---|---|
| `TopicIndex`, `TopicAssigned` | **Adopt.** Cheap, deterministic, records assignment so a rebuild reproduces arrival-order-dependent centroids. |
| Topic **lane** in `select()` | **Flag, default OFF.** Gate: flip it, run `scripts/measure_recall.py`, keep whichever overall number is higher, record both. A two-second decision the harness is built to make. |
| `unhealthy_topics()` / `split_topic()` | **Defer, never called.** Their entry condition is the un-pruned-membership bug (branch-review defect #4): 70 findings in a topic, 69 merged away, `health()` still reports `size=70, share=0.986`. |
| `TopicSplit` entry kind | Kept, unused, documented as unused. Removing it is churn on the teammate's code for no gain. |

**Amendment F's topic labels reach the briefing, deterministically.** `/watermark` gains `topics: [{id, size, label}]`, top few by size, where `label` is the medoid member's text truncated — the member with highest cosine to the centroid. No model call, rebuilds from the log, and it reads from **`View.members_of`**, which the fold already restricts to visible ids. That is what makes the sizes honest despite defect #4: the pruning bug lives in `TopicIndex.topics[].members`, which only `health()`/`split()` read, and we call neither.

The briefing then renders "the team is working on: *<label>*, *<label>*" alongside the counts it already shows. While we are in `/watermark`: add `purpose` and `members`, which Plan C.6 asked for and which are the two things that would make `sh-0fe841b3` legible on a demo screen. Two lines, disproportionate value.

---

## 7. Decisions table

| Module | Call | Notes |
|---|---|---|
| `symbols.py` | **Adopt as-is** | Highest value per unit of risk in the branch. The flagship demo case — Aditya's "40 ms" meeting Akhil's "40 ms", seven minutes apart, two laptops — is entirely this module, and it *strengthens* invariant 1 rather than merely respecting it (no `Segment` entry point, deliberately, with a test). Strengthen that test to walk classes and annotations, not just `str(signature)` of module-level functions. |
| `lexical.py` | **Adopt as-is** + 1 test | Self-contained BM25, no scan, no deps. Add the test that isolates the IDF factor before trusting the docstring's argument for BM25 over plain overlap — dropping `idf *` today leaves 75 green. |
| `semantic.py` — `Embedder`/`HashingEmbedder`/`VectorIndex` | **Adopt as-is** | `Embedder` is the correct seam for dropping Cirrascale's bge in behind `SharedMemory(embedder=…)`. Add tests pinning the `0.45` threshold and the signed-bucket trick; both currently survive mutation. |
| `semantic.py` — `TopicIndex` | **Adapt** | Index adopted; lane flagged off; `health`/`split` not called. §6.4. |
| `lanes.py` | **Adapt** | Reserved-floor back-fill (§6.3); `searched` subtracts `blocked`; `exclude=` populated by the caller (§6.2). Single highest-leverage change available before the demo. |
| `log.py` | **Adopt + extend** | Add `MarkedTrivial` (§4.2). Correct `Log.version`'s docstring (§4.4). |
| `fold.py` | **Adopt + extend** | Add `trivial` accumulation; drop the `status` field read. Everything else unchanged — `resolve()`, the depth cap, `SupersessionCycleError` are all right. |
| `store.py` (branch) | **Adapt → `memory.py` / `SharedMemory`** | Becomes the per-session half of the registry. Drops nothing; gains `mark_trivial`. `append` decides "new" from the view (§4.5). |
| `store.py` (main) | **Keep, revise once** | Registry survives intact — `create_session`/`get_session`/`add_member`/`get_context`/`bump_version`/`last_seen`/`mark_seen` have no counterpart in the branch and are load-bearing. Gains `supersede`/`mark_trivial`/`set_context` and the Option A projection. |
| `corpus.py` + `recall.py` + `scripts/measure_recall.py` | **Adopt as-is** | Zero production coupling; the only quality signal in the system needing no model, no key, no network. Demonstrably a working gate. Keep every "regression guard, not evidence" label — these numbers do **not** enter the demo script. |
| `__init__.py` | **Union** | 30 branch names + 6 main names, no clashes, `Store` → `SharedMemory`. |
| `packages/service/pyproject.toml` | **Main's, unconditionally** | The branch's declares only `synapse-contracts` and drops the `synapse-service` console script. Imports keep working by accident (`mcp==1.9.4` pulls starlette/uvicorn/httpx into the shared venv); the vanished entry point breaks the demo launch immediately. |
| `docs/adr/0004` | **Adopt + amend** | Three amendments, §2.4. Option A closed, §4.3. |
| `CONTEXT.md` | **Merge both ways** | Take the branch's new "Storage and retrieval" section (View, Lane, Candidate, Lane yield). **Revert its deletions** — it removes **Triage** and **Distiller** and drops "triages" from Edge Worker, because it predates E2's merge. |

---

## 8. Order of work

Each step is independently revertable, and the demo loop is only touched at step 3.

| Step | Work | Guard |
|---|---|---|
| **0** | On `main`, before any merge: `supersede` / `mark_trivial` / `set_context` on today's mutable `InMemoryStore`; `synthesis.py` calls them instead of mutating references. | 387 stay green. Now the seam is real. |
| **1** | Merge the branch; resolve the nine (§9). | Both suites collected; branch tests renamed. |
| **2** | Swap `InMemoryStore`'s finding half to `SharedMemory`; add `MarkedTrivial`; Option A projection in the read accessors. | 387 green **minus** the mechanism tests rewritten in §8.1; branch's 266 green. |
| **3** | Wire lanes into `synthesis.merge` and `api.query`, with `exclude=` suppression and the back-fill fix. | The two new tests in §6.1/§6.2; closed-loop test unchanged. |
| **4** | `/watermark` gains `topics` + `purpose` + `members`; briefing renders them. `POST /v1/sessions` create-or-return; Relay 4xx terminal; `cmd_resync` calls `/synthesize`. | `briefing.py`'s fail-open guard must still fail open on the new shape. |
| **5** | Run `scripts/measure_recall.py` with the topic lane on and off; record both; set the flag. | The number, not an opinion. |

### 8.1 Which tests transfer, which encode the old mechanism

**Transfer unchanged** — `test_upsert_is_first_write_wins` (still true, new mechanism: `fold._record`), `test_context_versioning_and_members`, `test_last_seen_tracking`, `test_unknown_session_is_none_not_keyerror` (registry half, untouched), and — because of the projection — every `test_synthesis.py` assertion on `f.merged_into` / `f.status`, plus all of `test_api.py` and `test_retrieval.py` and the closed-loop test.

**Encode the old mechanism, rewritten:**

- `test_replayed_original_never_clobbers_a_tombstone` (`test_store.py:32`) — sets `merged_into` through a returned reference. Rewrite: `store.supersede(sid, ["f-1"], syn)`, re-upsert `f-1`, assert it is absent from `retrievable`. **This is the most important test in the repo and it is rewritten first**, because it is the pin on the exact property ADR 0004 claims to guarantee by construction. If it cannot be made to pass through the new write path, the swap stops.
- `test_retrievable_excludes_tombstones_and_trivia` (`:40-47`) — same shape; rewrite through `supersede` + `mark_trivial`. What is being asserted genuinely changes: from "the store hands back a mutable reference" to "the store records a verdict."

**Retired as vacuous** (branch): `test_a_larger_corpus_does_not_change_the_prompt_size` (asserts the same literal `14` against itself and does arithmetic on its own build parameters — a mutation removing `[:budget]` entirely left it green) and `test_recall_is_reported_per_band_and_per_lane` (asserts what `by_lane()` guarantees by construction).

**New, non-negotiable:** resend guard pinned by `accepted == 0` + topic membership unchanged · back-fill returns exactly `top_k` on a symbol-bearing query · `exclude=` honoured and `searched` subtracts it · IDF isolated · an old near-duplicate outside the last-20-by-arrival is selected · **a producer-forged `status=trivial` / `merged_into` push has no effect on visibility** · `MarkedTrivial` round-trips through `rebuild()`.

---

## 9. Invariants audit

| # | Invariant | Verdict |
|---|---|---|
| 1 | **Egress rule** — nothing reaches the Service that has not passed the distiller | **Strengthened.** `symbols.py` has no entry point taking a `Segment`, deliberately, with a test asserting it never grows one; its docstring names the exact failure it prevents (symbol extraction one stage earlier would carry `default_pool_size=25` across the device boundary in tag form while `verbatim_overlap` still reported 0.00). Nothing else in the integration touches the worker. |
| 2 | **Retrieval reads the Finding Log, not the Working Memory** | **Held and improved.** `candidates()` reads the folded view; nothing in the package touches Working Memory. `RETRIEVABLE` stays defined in exactly one place — it moves from `store.is_retrievable` to `fold.py`, and no consumer re-implements it. |
| 3 | **Suppress only when EVERY Attribution is the asker's Agent Session** | **Held by construction, at a new seam.** `visible_to` remains the one definition (`f.attributions and all(...)`, empty-attributions guard intact) and is still applied inside `query_findings`. New: it also computes the `exclude=` set the lanes filter on (§6.2), and `searched` subtracts it so `coverage_line()` stays calibrated. **This is the invariant most at risk in this integration** — the branch has no suppression anywhere — and it is the one with the most new tests. |
| 4 | **Durable the moment produced; retained after send** | **Unchanged on the producer side; the service side is honestly worse than the docs claimed and slightly better after this work.** The service-side log is still in-memory. §5 ships create-or-return session ids, terminal 4xx in the Relay, and `cmd_resync → /synthesize`, which makes the documented recovery path *possible* for the first time. Log persistence is out (§7) and is now a ~20-line change rather than a project. |
| 5 | **Merged Finding is a new record; originals become tombstones, never deletions** | **Intent preserved exactly; mechanism changed.** Originals stay readable in `view.findings`; a bad merge is reversible; Conflicts follow supersession forward (`View.resolve()`, depth-capped at 64, `SupersessionCycleError` rather than a hung service). The tombstone stops being a written field and becomes a derived condition — projected back onto egress by Option A so `Finding.merged_into` still means what every consumer thinks it means. |
| 6 | **The Distiller compresses; it does not judge** | **Untouched**, and the trivia half of durability judgment is explicitly preserved by `MarkedTrivial` (§4.2) rather than quietly dropped, which adopting the branch as-is would have done. |
| 0004 | **The log is append-only; state is a fold** | **Adopted, with three amendments (§2.4) and Option A closed (§4.3).** Its motivating bug is false against main; its decision is right for reasons the ADR does not currently give. The one property it genuinely buys today, unclaimed by the ADR: visibility no longer reads producer-writable fields, so a forged verdict on ingest is inert. |

---

## 10. Explicitly out of scope for Aug 7

- **Service-side log persistence to disk.** The actual restart fix. First item after the demo, cheap *because of* ADR 0004, and half a story on its own (Working Memory and Conflicts are not in the log).
- **`unhealthy_topics()` / `split_topic()`.** Blocked on pruning topic membership when a finding is merged away. Not called, so not blocking.
- **Model-emitted topic names.** The branch rejected them and it was right; deterministic medoid labels are what ship (§6.4).
- **Option B** for `merged_into` / `status` — a three-track contract break, two days out.
- **Snapshots / event-sourcing compaction.** ADR 0004 already records this as the scaling move deliberately not taken. Fold is microseconds at demo scale (2.5 ms per `candidates()` at 422 findings, 57 ms at 10k).
- **Swapping `HashingEmbedder` for Cirrascale bge.** The `Embedder` protocol is the seam and it is ready; flipping it needs the recall harness re-run and the live Cirrascale flip, both still open.
- **Auth / the producer trust boundary at the service.** The forged-verdict half closes for free (§2.3); a shared token is out.
- **Lane-yield on a real corpus.** Needs the fixture co-sign that is still open (`docs/STATE.md`, "What remains"). Until then no recall number leaves the repo — the branch's own `corpus.py` cites the team's 2026-08-03 trap #3 against itself, and that labelling is adopted with the code.
- **Anything in `packages/worker` or `packages/distiller`.** Not touched by this integration at all.

---

## 11. Migration mechanics

> **Merge the branch — `git merge` onto a `feat/brain-integration` branch off `main`, resolve the nine collisions by hand, land as one merge commit — rather than cherry-picking modules, because the two checkouts share one object store (`synapse-exec/brain` is a worktree of this repo, merge-base `8695eed`), so a real merge keeps the teammate's two commits and their authorship in history while git hands us the collisions as a checklist instead of us reconstructing them by hand.**

Step 0 of §8 lands on `main` first, so the merge happens against a seam that already exists.

### The nine, with their resolutions

| File | Resolution |
|---|---|
| `CONTEXT.md` | **Both.** Take the branch's "Storage and retrieval" section; revert its deletions of **Triage**, **Distiller**, and "triages" in the Edge Worker definition — they predate E2's merge. |
| `docs/STATE.md` | **Ours**, plus the branch's "The topic lane is on notice" note folded in. That honesty is worth keeping. |
| `docs/plans/README.md` | **Ours** — six invariants; the branch's predates E2. |
| `pyproject.toml`, `uv.lock` | **Ours**; re-run `uv lock` after (`export PATH="/opt/homebrew/bin:$PATH"`). |
| `packages/service/pyproject.toml` | **Ours, unconditionally.** §7. |
| `packages/service/src/synapse_service/store.py` | **Ours.** Theirs lands as `memory.py` (`git checkout --theirs` to the new path, then `git rm` the conflict at the old one). |
| `packages/service/src/synapse_service/__init__.py` | **Union.** No name clashes; `Store` → `SharedMemory`. |
| `packages/service/tests/test_store.py` | **Ours** at that path; theirs → `tests/test_memory.py`. |
| `docs/adr/0002-*.md` | Take the branch's superseded-by header. Not really a conflict — main has not touched the file since the fork. |

Everything else the branch adds is a new filename and lands clean: `log.py`, `fold.py`, `lanes.py`, `lexical.py`, `semantic.py`, `symbols.py`, `corpus.py`, `recall.py`, their eight test files, `scripts/measure_recall.py`, `docs/adr/0004-*.md`, `docs/2026-08-05-service-implementation-report.md`.

`docs/adr/0004` lands as written and gains an **Amendment (2026-08-05)** section — the teammate's argument stays theirs; the corrections are dated and attributed separately.

### Gates before the integration branch merges to `main`

1. `uv run pytest` — both suites collected, green, offline.
2. `scripts/measure_recall.py` run and its numbers recorded in `docs/STATE.md`, with the topic-lane flag set from that run and the "regression guard, not evidence" label intact.
3. The closed-loop test (`packages/orchestrator/tests/test_end_to_end.py`) green **unchanged** — if it needed editing, the surface moved and something in this design was wrong.
4. `uv run synapse-service` starts. The entry point is the thing the branch's `pyproject.toml` would have silently removed.

Nothing is pushed. `main` moves only when all four are true.
