# Plan E — Brain integration

**Track:** the Synapse Service's retrieval core — one service, `main`'s surface on the branch's mechanism.
**Suggested owner:** Siddsing (service), with the branch author on `docs/adr/0004` and `CONTEXT.md`.
**Depends on:** Plan C (built and merged, E3) · Plan D (built and merged, E4) · `feat/shared-memory-store` (`d491956`, unmerged).
**Design:** [`docs/brainstorming/2026-08-05-brain-integration-design.md`](../brainstorming/2026-08-05-brain-integration-design.md) — every decision below is made there, with its reasoning. This plan is the executable form and adds nothing new.
**Deadline:** demo **Aug 7**. Two days. Where the deadline is the reason for a call, this plan says so.

**Goal:** keep every externally visible property `main` has proven — six routes, synthesis semantics, suppression, watermark, self-heal, producer contracts, all six invariants — and replace the *mechanism* underneath with the branch's append-only log, pure fold and five-lane candidate selection wherever the mechanism is genuinely better.

> **⟨STATUS 2026-08-05⟩ Nothing in this plan is built.** `main` is `cc58619`, 387 tests green, offline, and runs the loop end to end: transcript → segmented → triaged → distilled on the NPU → relayed → synthesized → retrieved. **That is the thing that must not break.** The branch is `d491956` (two commits off merge-base `8695eed` = pre-E3 `main`), 266 tests green in 1.9 s, no keys, no network, no model. Contracts are byte-identical across the two checkouts, so the branch's code already operates on `main`'s exact `Finding`.
>
> **Nine files collide**, verified by `git diff --name-only 8695eed {main,d491956}`: `CONTEXT.md`, `docs/STATE.md`, `docs/plans/README.md`, `pyproject.toml`, `uv.lock`, `packages/service/pyproject.toml`, and `packages/service/src/synapse_service/{store.py,__init__.py}` plus `packages/service/tests/test_store.py`. Two of those are **two different service cores at one module path**, and that collision is a false one: `main`'s `InMemoryStore` is a **multi-session registry**; the branch's `Store` is **one Shared Session's memory**. A registry holding N of the other is the integrated system, and almost everything in this plan follows from saying it that way. Task E.2 names the resolutions; Task E.6 does the swap.
>
> **Two arrows are the product claim.** `synthesis.merge` merging a near-duplicate that arrived forty findings ago is impossible on `main` today. `/query` sending fourteen findings instead of the entire visible log is what keeps an 8B usable as the session grows. Everything else in this plan is plumbing that must not move.

**The six invariants** ([`README.md`](./README.md)) are restated inside every task that touches one. Their audit for this integration is in the design memo §9; the short version: **1 strengthened, 2 held and improved, 3 held by construction at a new seam and most at risk, 4 honestly better but still not solved, 5 intent preserved and mechanism changed, 6 untouched and explicitly preserved.**

---

## Task E.1 — The storage seam, made real

**Lands on `main` first, before any merge, with the 387 green as the guard.** Nothing else in this plan may start until this is done.

Plan C.2 promised "a narrow interface." What shipped is a concrete class with eleven methods, constructed internally by `build_app`, type-hinted by name in `synthesis.py:129`, and — decisively — **bypassed for every verdict write**. Synthesis applies every verdict by mutating objects the store handed back:

| Site | Statement | Field |
|---|---|---|
| `synthesis.py:228` | `s.merged_into = synthesized.id` | tombstone |
| `synthesis.py:236` | `finding.status = FindingStatus.TRIVIAL` | trivia |
| `synthesis.py:269` | `ctx.conflicts = resolved` | conflicts |
| `synthesis.py:272` | `ctx.working_memory = verdicts.working_memory` | Working Memory |

`InMemoryStore` has **no write path for any of them**; `bump_version` is its only verdict-writing method. `test_store.py:35` blesses the pattern by mutating through `store.get(...)`.

> **This is the single mechanical blocker for any store swap.** A replacement that returns copies, frozen records, or a derived view silently discards every merge, tombstone, trivia mark and conflict **while every API-level test still passes**. That is the worst possible failure shape two days out, and it is why the seam is fixed against today's mutable store, where 387 tests can prove the refactor lossless, rather than during the swap.

Add to `InMemoryStore` (`packages/service/src/synapse_service/store.py`):

```python
def supersede(self, shared_id: str, sources: list[FindingId], result: Finding) -> None
def mark_trivial(self, shared_id: str, finding_ids: list[FindingId]) -> None
def set_context(self, shared_id: str, *, working_memory: str | None = None,
                conflicts: list[Conflict] | None = None) -> None
```

`supersede` upserts `result`, then records that each live source is superseded by it. `mark_trivial` marks only findings that are not already superseded (today: `if finding.merged_into is None`). `set_context` writes only the keyword arguments it is given — `None` means "leave alone", which is what preserves `synthesis.py`'s existing behaviour when a verdict omits `working_memory`.

`set_context` is not strictly required — `SessionContext` is registry-owned mutable state, not fold-derived — but it is one method, it removes the last mutation-through-reference, and it is what lets a durable context land later without touching `synthesis.py` again.

`synthesis.py` then calls these three instead of assigning through references. Nothing else changes: order of operations stays `upsert → one model call → apply verdicts → bump once`, `_resolve_forward` keeps reading `store.get(...).merged_into`, and the whole-verdict `_SynthesisVerdicts` validation stays exactly where it is (no partial application, ever — invariant 5's premise that a merge is the only irreversible action in the system).

**First failing tests:** `supersede` sets `merged_into` on every live source and leaves an already-superseded source pointing at its original successor; `mark_trivial` skips a source that `supersede` already tombstoned (the `merged_into is None` guard, preserved); `set_context(working_memory=None, conflicts=[...])` leaves `working_memory` untouched; **grep-level: no assignment to `.merged_into`, `.status`, `.conflicts` or `.working_memory` survives anywhere outside `store.py`** — write it as a test that reads `synthesis.py`'s source, because the failure mode is a line someone adds back later.

**Exit:** `uv run pytest` is 387 green, unchanged, and the seam exists.

## Task E.2 — Migration mechanics

**Merge, do not cherry-pick.** The two checkouts share one object store — `synapse-exec/brain` is a worktree of this repo, merge-base `8695eed`, branch head `d491956`, two commits — so a real merge keeps the teammate's commits and authorship in history while git hands over the nine collisions as a checklist instead of us reconstructing them by hand.

```
git switch -c feat/brain-integration main       # AFTER Task E.1 has landed on main
git merge --no-ff origin/feat/shared-memory-store
```

`synapse-exec/brain` is **read-only** for the duration. Never modify it, never push anything, anywhere.

### The nine, with their resolutions

| File | Resolution |
|---|---|
| `CONTEXT.md` | **Both.** Take the branch's new "Storage and retrieval" section verbatim. **Revert what taking theirs would delete** — the branch's base predates E2, so it has no **Triage** entry, no **Distiller** entry, and no "triages" in the Edge Worker definition. Task E.4 owns the finished file. |
| `docs/STATE.md` | **Ours**, plus the branch's "The topic lane is on notice" section folded in verbatim. That honesty is worth keeping and Task E.10 writes into it. |
| `docs/plans/README.md` | **Ours** — six invariants; the branch's copy predates E2 and has five. |
| `pyproject.toml`, `uv.lock` | **Ours.** Re-run `uv lock` after the merge (`export PATH="/opt/homebrew/bin:$PATH"`). |
| `packages/service/pyproject.toml` | **Ours, unconditionally.** Theirs declares only `synapse-contracts`, drops `starlette`/`uvicorn`/`httpx`, and drops the `[project.scripts] synapse-service = "synapse_service.cli:main"` console script. The imports keep working by accident (`mcp==1.9.4` pulls starlette/uvicorn/httpx into the shared workspace venv); the vanished entry point breaks the demo launch immediately and silently. |
| `packages/service/src/synapse_service/store.py` | **Ours.** Theirs lands at the new path `memory.py` — `git checkout --theirs` into `memory.py`, then resolve the conflict at the old path in favour of ours. |
| `packages/service/src/synapse_service/__init__.py` | **Union.** 30 branch names + `InMemoryStore`/`Synthesizer`; no clashes; `Store` → `SharedMemory`, `Appended` kept. |
| `packages/service/tests/test_store.py` | **Ours** at that path — it tests the registry. Theirs → `packages/service/tests/test_memory.py`. |
| `docs/adr/0002-semantic-merge-and-tombstones.md` | Take the branch's superseded-by header. Not really a conflict: `main` has not touched the file since the fork, so it shows as theirs-only. |

### Renaming, so the collision stops being one

| Branch name | Integrated name | Why |
|---|---|---|
| `store.py` / `Store` | `memory.py` / `SharedMemory` | It is one Shared Session's Finding Log plus its derived indexes. `InMemoryStore` is the registry that pairs it with the Working Memory prose on `SessionContext` — together, `CONTEXT.md`'s **Shared Memory**. The umbrella term finally has a home in code and the filename conflict evaporates. |
| `tests/test_store.py` | `tests/test_memory.py` | `main`'s keeps its path and its subject. |
| `log.py` / `Log`, `fold.py`, `lanes.py`, `lexical.py`, `semantic.py`, `symbols.py`, `corpus.py`, `recall.py` | unchanged | New filenames `main` does not have. They land clean. |

Everything else the branch adds is a new filename and lands clean: eight test files, `scripts/measure_recall.py`, `docs/adr/0004-the-log-is-append-only-and-state-is-a-fold.md`, `docs/2026-08-05-service-implementation-report.md`.

**First failing tests:** none of its own — this task is proven by collection, not by assertion. `uv run pytest` must **collect** both suites (387 + 266) with no import error and no duplicate module basename; `uv run synapse-service` must start (the entry point the branch's `pyproject.toml` would have removed); `python -c "from synapse_service import SharedMemory, InMemoryStore"` must succeed.

**Exit:** one merge commit on `feat/brain-integration`, both suites collected and green, teammate's two commits present in `git log` with their authorship intact. **Nothing pushed.**

## Task E.3 — ADR 0004 lands on `main`, with a dated amendment

ADR 0004 exists **only on the branch** today. It lands on `main` at `docs/adr/0004-the-log-is-append-only-and-state-is-a-fold.md` **as written, unedited**, through the merge in Task E.2 (it is a theirs-only file). Its status is `Accepted (2026-08-05)` and stays that way. The corrections go in an appended, dated, separately-attributed **Amendment (2026-08-05)** section — the teammate's argument stays theirs.

**Why it is adopted at all, given that its motivating bug is false.** Both reviews ran the code rather than reading it, independently, and agree: the resync-resurrects-a-merged-finding scenario is true of a whole-object upsert (`table[f.id] = f`) and `main` never shipped one. `store.py:58` is `if finding.id not in table`, the module docstring names FIRST-WRITE-WINS and gives exactly that scenario as its reason, and it is pinned three ways — `test_upsert_is_first_write_wins`, `test_replayed_original_never_clobbers_a_tombstone`, and at the route by `test_replayed_push_is_a_noop_and_skips_the_model` (`accepted == 0` means `merge()` is never called, so a replay does not even reach the model). Adopted anyway, for three reasons the ADR does not currently give:

1. **Property beats discipline, and we are about to add write paths.** First-write-wins is a rule someone must re-apply at every future write path, and its failure mode is silent. This week adds `supersede`, `mark_trivial` and a projection; next week adds persistence.
2. **It closes the producer-forged-verdict hole for free.** Today `api.push_findings` runs only `Finding.model_validate`, and the Relay POSTs to the service directly. A first push carrying `merged_into="x"` or `status=trivial` lands, is excluded from retrieval forever, and cannot be corrected by any later push because upsert ignores known ids. Under the fold, **visibility stops reading producer-writable fields at all**. Neither review claimed this; it is the strongest argument the ADR has and it is not in the ADR.
3. **It is the enabling step for durability.** A log is `for entry in entries: write(json)`. Mutable cross-referenced state is not. `rebuild()` already proves replay is sufficient.

> **This must not be sold to the team as a bug fix.** Leaving the Context as written invites someone to merge a synthesis rewrite two days before the demo to close a hole that is not open.

### The amendment, three parts plus one closure

- **A1 — retarget the Context.** State that `main`'s first-write-wins upsert already closes the replay-while-alive case; cite `store.py:58` and `test_replayed_original_never_clobbers_a_tombstone`; retarget the motivation at the restart case (Task E.9) and the three arguments above.
- **A2 — the order argument is wrong, and the property is stronger than claimed.** The ADR says the resync is inert because "the `Merged` entry at #59 is still in the log and still **earlier**." Order is irrelevant: `fold` accumulates `superseded_by` across all entries and filters at the end (`fold.py:107-114`), so a `Merged` entry appearing *after* the re-append suppresses just as well. Correct the reasoning; keep the conclusion.
- **A3 — a fifth entry kind, `MarkedTrivial`.** The four kinds cannot express the trivia verdict, yet `fold.py:113` reads `findings[fid].status is FindingStatus.KEPT` — a field nothing in the branch ever writes, and the same field the ADR tells readers to treat as undefined. Task E.5 builds it.
- **Option A, closed.** The ADR leaves Option A (project on egress) vs Option B (drop the fields from the contract) open, calling it "a team decision, not a resolved one." **Decided: Option A.** Option B is a three-track contract break two days out, for no gain. The ADR's Follow-up asks for the projection "at the ingest boundary rather than inside the store"; **Task E.6 deviates deliberately** and the amendment records why.

Also record, in the ADR's Consequences, the one property it genuinely buys today and does not claim: **visibility no longer reads producer-writable fields, so a forged verdict on ingest is inert.**

**First failing tests:** documentation, so the tests are elsewhere and named here on purpose — A3 is pinned by `test_marked_trivial_round_trips_through_rebuild` (Task E.5) and the Option-A closure by `test_a_forged_verdict_on_ingest_has_no_effect_on_visibility` (Task E.6). An ADR whose claims no test pins is how the false Context got written in the first place.

**Exit:** `docs/adr/0004-*.md` on `feat/brain-integration` with the branch's text intact plus an `## Amendment (2026-08-05)` section carrying A1/A2/A3 and the Option A closure, each dated and attributed. `docs/STATE.md`'s "Open, unchanged" entry on `merged_into`/`status` is updated from open to **decided: Option A**.

## Task E.4 — `CONTEXT.md` vocabulary

The vocabulary is the one document every plan is written against, and this integration introduces three ideas it has no words for. Take the branch's **Storage and retrieval** section verbatim — **View**, **Lane**, **Candidate**, **Lane yield** — and add three more so the terms this plan uses in prose exist in the language:

**Fold**:
The pure function that replays the Finding Log in order and produces the View. Deterministic, no model, cached on log version and discardable. A fold is the *only* way current state is obtained; nothing derives visibility any other way.
_Avoid_: reduce, replay (the recovery path is a resync, not a fold), rebuild (that is re-deriving the indexes), projection

**Topic**:
A cluster of Findings grouped by cosine against a centroid — geometry decides membership, and a label only ever describes it. Topics exist to reach a decision that *governs* a Finding it shares no vocabulary with. A Topic is never an input to what is durable.
_Avoid_: cluster, category, tag, label (a label is a Topic's name, not the Topic), theme

**Tombstone** (revise the existing entry, do not replace it): everything the term means is unchanged — text and Attribution retained, excluded from retrieval, reachable by id, reversible — but it is now a **derived condition, not a written field**. A Finding leaves the View because a later `Merged` entry names it as a source; nothing is written onto the original. Keep the `_Avoid_` line as it stands.

Take both of the branch's new Notes bullets (the Tombstone-is-derived note and the append-only note), **amended**: the second half of the Tombstone bullet says the projection question is undecided. It is decided — Option A, Task E.3 — so it reads *"`Finding.merged_into` and `Finding.status` are projected onto egress from the View (`adr/0004`, Option A, closed 2026-08-05); they are never written by a producer and never read to decide visibility."*

**Revert what taking the branch wholesale would delete:** the **Triage** entry, the **Distiller** entry, and "triages" in the Edge Worker definition. They predate E2's merge on the branch's base; deleting them would silently un-say `adr/0003`, which is the reason triage exists.

**First failing tests:** a one-file `tests/test_vocabulary.py` at the repo root asserts that **Triage**, **Distiller**, **View**, **Lane**, **Candidate**, **Lane yield**, **Fold** and **Topic** each appear as a bolded defined term in `CONTEXT.md`, and that the string "team decision, not a resolved one" no longer describes the `merged_into`/`status` question anywhere in `docs/`. Cheap, and it is the only thing standing between a merge resolution and a silently deleted definition.

**Exit:** `CONTEXT.md` carries the branch's section, the three additions, the revised Tombstone entry, both amended Notes bullets, and every E2-era definition still present.

## Task E.5 — The fold gains a fifth entry kind

Pure package work: no route, no store, no model. It is separated from the swap (Task E.6) precisely so it can be proven against the branch's own 266 tests before anything on the demo path moves.

**`log.py`** gains a fifth kind and extends the union:

```python
@dataclass(frozen=True)
class MarkedTrivial:
    finding_ids: tuple[FindingId, ...]
    kind: Literal["marked_trivial"] = "marked_trivial"

Entry = Union[FindingAppended, Merged, MarkedTrivial, TopicAssigned, TopicSplit]
```

**`fold.py`** accumulates `trivial: set[FindingId]`, adds it to `View` as `trivial: frozenset[FindingId]`, and visibility becomes:

```
RETRIEVABLE  ==  fid not in superseded_by  and  fid not in trivial
```

> **Note what leaves: the fold stops reading `findings[fid].status` entirely.** That is the point. Both service-written fields are now derived from entries, neither is read from a producer-supplied record, and the forged-verdict hole closes as a side effect rather than as a feature someone has to build.

**Invariant 2 — retrieval reads the Finding Log, not the Working Memory** — is restated here because this task moves where `RETRIEVABLE` is defined: it moves from `store.is_retrievable` to `fold.py`, and stays defined in **exactly one place**. No consumer outside `fold.py` is given the raw entry list, because a predicate re-implemented at a call site is one that eventually gets it subtly wrong and surfaces something that was merged away.

**Invariant 6 — the Distiller compresses; it does not judge** — is restated here because this is the task that could quietly break it. Per `adr/0003`, durability judgment has exactly two homes: triage upstream (Plan A.5b, built in E2) and synthesis's trivia verdict downstream. `MarkedTrivial` exists so adopting the branch as-is does not delete one of them.

**`Log.version`'s docstring is corrected here** (see Task E.6 for why): it is the entry count and the fold-cache key. It is **not** `memory_version` and never leaves the store.

**First failing tests:** a `MarkedTrivial` entry removes its findings from `view.visible_ids` while leaving them in `view.findings`; **`test_marked_trivial_round_trips_through_rebuild`** — fold, `rebuild()`, assert the two views are identical (this is the pin on ADR 0004's "the log is the only thing that has to survive"); a finding both merged and marked trivial is absent once, not counted twice; a producer-supplied `status=TRIVIAL` on an appended finding **does not** remove it from `visible_ids` (the fold no longer reads that field); the existing `test_superseded_findings_are_never_candidates` still passes untouched.

**Exit:** the branch's 266 green with `fold.py` no longer importing `FindingStatus` for the visibility decision.

## Task E.6 — `SharedMemory` under the registry, and the Option A projection

The swap. This is the task that touches the demo path, and it is guarded by the ~380 tests that must pass **unchanged**.

`InMemoryStore` keeps everything it has — `create_session`, `get_session`, `add_member`, `get_context`, `bump_version`, `last_seen`, `mark_seen` have no counterpart in the branch and are load-bearing — and replaces its `dict[str, dict[str, Finding]]` finding half with `dict[str, SharedMemory]`, one per `shared_id`.

### `memory.py` — `SharedMemory`, per Shared Session

```python
SharedMemory(shared_id: str, purpose: str = "", embedder: Embedder = HashingEmbedder())

  append(finding: Finding) -> Appended
  merge(result: Finding, sources: tuple[FindingId, ...]) -> Appended
  mark_trivial(finding_ids: tuple[FindingId, ...]) -> None        # NEW, appends MarkedTrivial
  view() -> View
  candidates(text: str, *, top_k: int = DEFAULT_TOP_K,
             recent: int = DEFAULT_RECENT,
             exclude: frozenset[FindingId] = frozenset()) -> CandidateSet
  rebuild() -> None
  split_topic(topic_id: TopicId) -> tuple[TopicId, TopicId]        # kept, never called
  unhealthy_topics() -> list[TopicId]                              # kept, never called
```

**`append` decides "new" from the view, not from a cache.** Today it reads `if finding.id in self.indexes.vectors.vectors` (`store.py:100`) — an index, i.e. a cache, standing in for the authority. Change it to `finding.id in self.view().findings`. That is what finally makes the branch's duplicate guard testable: deleting the guard today leaves 75 tests green, and after this change `accepted == 0` on a resend is the assertion that fails.

### `InMemoryStore` — the registry, revised

```python
upsert(shared_id, findings: list[Finding]) -> int          # ids NOT PREVIOUSLY SEEN
get(shared_id, finding_id) -> Finding | None               # projected
all_findings(shared_id) -> list[Finding]                   # projected
retrievable(shared_id) -> list[Finding]                    # projected, view.visible()
supersede(shared_id, sources: list[FindingId], result: Finding) -> None
mark_trivial(shared_id, finding_ids: list[FindingId]) -> None
set_context(shared_id, *, working_memory=None, conflicts=None) -> None
candidates(shared_id, text: str, *, top_k: int = DEFAULT_TOP_K,
           exclude: frozenset[FindingId] = frozenset()) -> CandidateSet
```

> **`upsert`'s return value is a behavioural contract, not a count of writes.** It returns **ids not previously seen** — `api.py:74`'s `if accepted:` is the only thing keeping a replayed POST off the provider. Do not change it to "entries appended"; the log records the resend (it happened) while `accepted` stays 0.

`supersede` becomes `memory.merge(result, tuple(sources))`. `mark_trivial` becomes `memory.mark_trivial(tuple(ids))`. `set_context` still writes `SessionContext`, which is registry-owned mutable state and deliberately not in the log.

### Option A: the projection lives in the read accessors

`get`, `all_findings` and `retrievable` return findings **copied** with `merged_into` and `status` filled from the `View`:

```python
def _project(self, view: View, f: Finding) -> Finding:
    return f.model_copy(update={
        "merged_into": view.superseded_by.get(f.id),
        "status": FindingStatus.TRIVIAL if f.id in view.trivial else FindingStatus.KEPT,
    })
```

ADR 0004's Follow-up asks for this "at the ingest boundary rather than inside the store." **We deviate, deliberately, for three reasons**, recorded in the amendment (Task E.3):

- The store is the only component holding the View, so it is the narrowest place the projection can live **where no caller can forget it**. `retrieval.py` never touches a store and must stay that way; `api.py` serialises whatever it is handed.
- It is what lets ~380 existing tests keep passing unchanged, including every `test_synthesis.py` assertion of the form `f.merged_into == syn.id`. That is not a convenience — it is the regression guard for the whole swap.
- The store keeps exactly one internal representation of supersession, which is what the Follow-up actually cares about.

> **Free consequence worth naming: a projected copy is a *copy*.** Any surviving mutation-through-reference now fails loudly instead of silently. The blocker Task E.1 fixed becomes a red test rather than a lost merge if anyone reintroduces it.

### Two version counters, two jobs, never the same number

| Counter | Meaning | Who reads it |
|---|---|---|
| `SessionContext.memory_version` | **merges completed** — bumped once, at the end of a fully-applied verdict | `/findings`'s `synthesized`, `/synthesize`'s `synthesized`, `/watermark`'s `version` and `new_since`, `last_seen` |
| `Log.version` | entry count | fold-cache invalidation inside `SharedMemory`. **Never leaves the store.** |

`Log.version`'s docstring claims it *is* `memory_version` for the watermark. Taking that would make `synthesized` True on every push including a pure replay, and turn `new_since` into a count of log entries (2+ per finding). `bump_version` stays exactly as `main` has it.

**Invariant 5 — a merged Finding is a new record; originals become tombstones, never deletions** — restated because this task changes its mechanism and nothing else. Originals stay readable in `view.findings`; a bad merge is reversible; Conflicts follow supersession forward (`View.resolve()`, depth-capped at `MAX_SUPERSESSION_DEPTH = 64`, raising `SupersessionCycleError` rather than hanging the service). The tombstone stops being a written field and becomes a derived condition, projected back onto egress so `Finding.merged_into` still means what every consumer thinks it means.

### Which tests transfer, and which encode the old mechanism

**Transfer unchanged:** `test_upsert_is_first_write_wins` (still true; new mechanism is `fold._record`), `test_context_versioning_and_members`, `test_last_seen_tracking`, `test_unknown_session_is_none_not_keyerror`, and — because of the projection — every `test_synthesis.py` assertion on `f.merged_into`/`f.status`, all of `test_api.py`, all of `test_retrieval.py`, and the three-package closed-loop test.

**Encode the old mechanism, rewritten:**

- **`test_replayed_original_never_clobbers_a_tombstone` (`test_store.py:32`) is rewritten FIRST and is the stop-gate for the whole swap.** It sets `merged_into` through a returned reference. Rewrite: `store.supersede(sid, ["f-1"], syn)`, re-upsert `f-1`, assert it is absent from `retrievable(sid)` and that `get(sid, "f-1").merged_into == syn.id`. It is the pin on the exact property ADR 0004 claims to guarantee by construction. **If it cannot be made to pass through the new write path, the swap stops and `main` is untouched.**
- `test_retrievable_excludes_tombstones_and_trivia` (`test_store.py:40`) — same shape; rewrite through `supersede` + `mark_trivial`. What is asserted genuinely changes: from "the store hands back a mutable reference" to "the store records a verdict."

**Retired as vacuous** (branch, `test_recall.py`): `test_a_larger_corpus_does_not_change_the_prompt_size` (asserts the same literal `14` against itself and does arithmetic on its own build parameters — a mutation removing `[:budget]` entirely left it green) and `test_recall_is_reported_per_band_and_per_lane` (asserts what `by_lane()` guarantees by construction). Delete them with a one-line commit message saying why; leaving a vacuous test is worse than having none.

**First failing tests:** the two rewrites above, in that order · **`test_a_forged_verdict_on_ingest_has_no_effect_on_visibility`** — a first push carrying `status=trivial` and `merged_into="whatever"` is retrievable anyway · **`test_a_resend_is_accepted_zero_and_changes_no_topic_membership`** — pins the branch's duplicate guard through `upsert`'s contract *and* asserts `view.topic_of` is unchanged · `store.get(...)` returns a copy, so mutating it changes nothing (the loud-failure property) · `all_findings` still returns superseded and trivial findings (nothing was deleted).

**Exit:** 387 green minus the two rewrites (which are green in their new form), plus the branch's 266 minus the two retirements. **The closed-loop test `packages/orchestrator/tests/test_end_to_end.py` is green unchanged** — if it needed editing, the surface moved and something here was wrong.

## Task E.7 — Lanes replace recency, at both call sites

The product claim. Both call sites currently select candidates in a way that is either wrong as the session grows, or unbounded.

### E.7a — Synthesis

`synthesis.py:149` is a pure recency slice over dict insertion order:

```python
others = [f for f in retrievable if f.id not in new_ids][-CANDIDATE_WINDOW:]
```

So in a 40-finding session, findings 1–20 are permanently unmergeable against anything new. The ADR 0002 merge simply stops happening as the session grows; `seg-005`'s pairing only works because both halves land in the same push.

**`CANDIDATE_WINDOW = 20` keeps its name and its meaning as a budget; only the selection rule changes.**

```
pushed     = every finding accepted in THIS call            (unconditional — the E3 starvation fix)
others     = ⋃ over pushed f of  store.candidates(sid, f.text, exclude=new_ids)
             deduped, capped at CANDIDATE_WINDOW
candidates = pushed + others
```

Plan C.4's fixed-cost property is preserved exactly, and both existing pins transfer with minimal edits: `test_a_push_larger_than_the_candidate_window_is_not_starved` (`test_synthesis.py:326`), `test_the_window_still_bounds_established_candidates_not_in_this_push` (`:352`), and `test_push_larger_than_candidate_window_reaches_the_merge_prompt_via_the_route` (`test_api.py:202`, whose function-local `from synapse_service.synthesis import CANDIDATE_WINDOW` must keep resolving).

### E.7b — Retrieval, and invariant 3 at the lanes seam

`api.py:173` passes `candidates=store.retrievable(sid)` — the **entire** visible log — into one model prompt, uncapped, growing linearly.

```python
visible    = store.retrievable(sid)
suppressed = {f.id for f in visible} - {f.id for f in visible_to(visible, agent_session)}
cands      = store.candidates(sid, body["query"], top_k=TOP_K,
                              exclude=frozenset(suppressed))
ranked     = await query_findings(provider, context=store.get_context(sid),
                                  candidates=[c.finding for c in cands.candidates],
                                  query=body["query"],
                                  asking_agent_session=agent_session)
```

> **Invariant 3 — suppress a Finding only when *every* Attribution is the asking agent's own Agent Session.** This is **the invariant most at risk in this integration**: the branch has no suppression anywhere, and `lanes.select`'s `exclude=` parameter is the right seam with nothing populating it. `visible_to(candidates: list[Finding], asking_agent_session: str) -> list[Finding]` in `retrieval.py` stays the **one** definition — `f.attributions and all(a.agent_session == asking_agent_session for a in f.attributions)`, empty-attributions guard intact, because `all(...)` over an empty list is vacuously True and would suppress a zero-attribution Finding from every possible asker. It is still applied inside `query_findings`; the belt is the definition and applying it twice is idempotent.
>
> Suppression is a pure predicate over a Finding, so computing it across the whole visible log is an O(N) Python loop with no model and no prompt cost. **The thing that must be bounded is the prompt, not the loop.**

`CandidateSet.searched` becomes `len(visible − blocked)` (today `lanes.py:262` is `len(visible)`), so `coverage_line()` — whose stated job is making "I found no match" calibrated rather than confident — stops over-reporting once suppression is wired.

### E.7c — The reserved-floor under-fill

Fixed **before** wiring, not after. In `lanes.select` (`lanes.py:226-244`), `budget` deducts a slot for each reserved id, but a reserved id already in `chosen` hits `continue` and nothing takes its place — `top_k=14` returns 12, in a module whose stated thesis is that every knob is set toward returning more, and precisely when the shared symbol is common and breadth matters most. Back-fill from the fusion remainder up to `top_k`. ~6 lines.

The existing `test_top_k_bounds_the_result` uses a symbol-free query, so the reservation is empty and the count comes out right by accident; the new test must use a symbol-bearing query.

`DEFAULT_RECENT = 8` is corrected in the **docstring**, not in behaviour — the lane contributes `max(1, top_k // RESERVE_DIVISOR)` = 2, and raising it costs fusion slots the branch already measured as harmful (its top entry scores identically to a symbol lane's top entry under RRF, so eight unranked recency picks displaced genuine matches). Whether 2 or 8 is right is a harness question, not an opinion — Task E.10.

**First failing tests:** **an old near-duplicate outside the last twenty by arrival is selected as a merge candidate** (impossible on `main` today — this test *is* the product claim) · back-fill returns exactly `top_k` candidates on a symbol-bearing query · `exclude=` is honoured and `searched` subtracts it · the IDF factor is isolated in `lexical.py` (dropping `idf *` today leaves 75 green, so the docstring's argument for BM25 over plain overlap is currently unpinned) · `/query` sends at most `TOP_K` findings into the prompt at 100 findings · **a Finding whose every Attribution is the asker's own Agent Session is absent from both `exclude`'s complement and the ranked result** · the two `CANDIDATE_WINDOW` pins, edited but still asserting the same properties.

**Exit:** both call sites bounded, invariant 3 pinned at the new seam, closed-loop test still green unchanged.

## Task E.8 — Topics: index in, lane out, labels in the briefing

The branch's own measurement: the topic lane surfaced **0 partners and 0 uniquely** at 422 findings and at 2,022; `docs/STATE.md` on the branch has a section titled "The topic lane is on notice." A lane that returns a whole 40-member cluster into an RRF fusion is not free — those members take rank credit that can outvote real matches — and it has never supplied one.

| Piece | Call |
|---|---|
| `TopicIndex`, `TopicAssigned` | **Adopt.** Cheap, deterministic, no model in the decision path, and recording the assignment is what lets a rebuild reproduce arrival-order-dependent centroids. |
| Topic **lane** in `select()` | **Flag, default OFF** (`select(..., topic_lane: bool = False)`). Task E.10 sets it from a measurement. |
| `unhealthy_topics()` / `split_topic()` | **Defer, never called.** Their entry condition is the un-pruned-membership bug: topic membership is never pruned on merge, so 70 findings in a topic with 69 merged away still reports `size=70, share=0.986` — "collapse looks like working", the exact shape `TopicHealth.is_collapsed` (`semantic.py:183`) exists to warn about. |
| `TopicSplit` entry kind | Kept, unused, documented as unused. Removing it is churn on the teammate's code for no gain. |

### Amendment F's topic labels reach the briefing, deterministically

`GET /v1/sessions/{sid}/watermark` gains `topics`, plus the two fields Plan C.6 asked for and never shipped:

```python
@dataclass(frozen=True)
class TopicSummary:
    topic_id: TopicId
    size: int
    label: str          # medoid member's text, truncated — highest cosine to the centroid

InMemoryStore.topic_summaries(shared_id: str, *, limit: int = 3) -> list[TopicSummary]
```

Response shape becomes `{version, new_since, by_type, conflicts, topics: [{id, size, label}], purpose, members}`.

> **No model call, and it rebuilds from the log.** `label` is read from **`View.members_of`**, which the fold already restricts to visible ids. That is what makes the sizes honest despite the un-pruned-membership bug: the bug lives in `TopicIndex.topics[].members`, which only `health()` and `split()` read, and we call neither. This routes around the bug rather than fixing it — a deliberate two-days-out call, recorded as such.

`briefing.py` renders "the team is working on: *<label>*, *<label>*" alongside the counts it already shows. Two constraints are non-negotiable there: the composed string stays under `_MAX_BRIEFING_CHARS = 1200` (headlines only — bodies grow with session length, headlines do not), and every service-supplied value goes through `_clean()` before interpolation, because `instructions` is the highest-trust text surface a connecting agent sees and a label containing newlines could read like a new instruction block.

**The watermark's content/change split is preserved exactly**: `by_type` and `conflicts` are content fields and run through the same all-attributions suppression rule as `/query` (invariant 3); `version` and `new_since` are change fields and stay global. `topics`, `purpose` and `members` are content fields — a topic whose every visible member is the asker's own contributes nothing to the briefing.

**First failing tests:** `/watermark` returns `topics` sorted by size with a deterministic medoid label and no provider call (assert the `FakeProvider` was never invoked) · the label is stable across a `rebuild()` · a topic whose members are all merged away reports `size` from `View.members_of`, not from `TopicIndex` · `build_briefing` **fails open to `_DEFAULT_INSTRUCTIONS`** on the new response shape when `topics` is missing, is not a list, or holds a non-dict — the whole point of that guard is that it runs in `cli.main` before `uvicorn.serve`, so an exception there takes the orchestrator process down · a label containing `\n` is `_clean`ed before interpolation · the composed briefing with three topics stays under the cap.

**Exit:** briefing renders topic labels, `/watermark` still touches no provider, `briefing.py`'s fail-open guard proven against the new shape.

## Task E.9 — The recovery path

> **The service-side log does NOT fix the restart case.** Both reviews measured it on both implementations. The branch's `Log` is in-memory and dies with the process, and `Merged` is a service-authored entry (`syn-<uuid4>`, `provenance=SYNTHESIZED`) that was never sent to any orchestrator and lives in no durable log anywhere. Verified: after resync into a fresh store, `main` gives back `['f-41','f-58']` with `working_memory=''`, `conflicts=[]`, `memory_version=0`; the branch's own `Store` gives back `visible_ids == ('f-41','f-58')` and has no synthesis at all. Append-only changes the *mechanism* of the replay-while-alive case (which `main` already handled correctly) and changes **nothing** about restart.

**Invariant 4 — Findings are durable the moment they are produced, before any send, and retained after sending.** The producer side is unchanged and correct. The service side is honestly worse than the docs claimed. What ships for Aug 7 is the ~15 lines that make the *documented* recovery path possible for the first time — orthogonal to which store wins:

1. **`POST /v1/sessions` accepts an optional `shared_id`: create-or-return.**
   `create_session(self, purpose: str, created_by: str, *, shared_id: str | None = None) -> SynapseSession` — mint `sh-{uuid4().hex[:8]}` when absent; return the existing session unchanged when the id is already known; create with that exact id when it is not. Today the id is minted server-side only, so after a restart the old `sh-…` 404s and **cannot be recreated by construction** — every teammate must re-join a brand-new session mid-demo. The branch inherits this hole verbatim; it has no session registry at all.
2. **The Relay treats 4xx as terminal-and-loud, 5xx as retryable.**
   `relay.py:192` catches `except (httpx.HTTPError, OSError)`, and `httpx.HTTPError` includes `HTTPStatusError`, so a permanent 404 is indistinguishable from a transient outage and loops forever while logging "Service unavailable". Split the handler: a 4xx logs at `warning` with the status and the URL, and the findings are dropped from the retry queue for that session rather than re-attempted; everything else keeps today's behaviour.
3. **`cmd_resync` calls `/synthesize`.**
   `push_findings` gates the model on `accepted > 0`, so a full resync into a store that already holds those findings never re-synthesizes. `cmd_resync` POSTs `/v1/sessions/{sid}/synthesize` once after a successful resync and folds the result into its printed line.

Recovery then reads, honestly:

> A service restart loses the in-memory log. Every orchestrator resyncs its retained durable log into the **same** `shared_id`; findings land (first write of an id wins, by construction now); one `POST /v1/sessions/{sid}/synthesize` re-derives Working Memory, conflicts and merges. **What is recomputed, not restored:** synthesized findings get new ids, Working Memory and Conflicts are re-derived by a fresh 8B call and may differ, and any contributor who does not resync is gone entirely.

**First failing tests:** `POST /v1/sessions` with a known `shared_id` returns 200 and the same session, not a second one; with an unknown `shared_id` creates it with that exact id; without one mints as today (201) · a 404 from `_post` is terminal — the Relay does not re-attempt it on the next `flush()` and logs it at warning · a 503 is still retried · `cmd_resync` into a fresh service converges to a synthesized state, asserted by `memory_version > 0` and the merged pair being absent from `retrievable` · **`test_full_log_replay_into_a_fresh_store_converges_with_the_original_stream` (`test_api.py:268`) still passes unchanged.**

**Exit:** the recovery path in `docs/STATE.md` and `relay.py`'s docstring say what the code actually does, including what is recomputed rather than restored.

## Task E.10 — The recall gate, and the topic-lane flag

`scripts/measure_recall.py` is the only quality signal in the system that needs no model, no key and no network, and it is a demonstrably working gate: removing the symbols reserved floor drops the symbol band 100% → 87.5% and fails a named test.

```
export PATH="/opt/homebrew/bin:$PATH"
uv run python scripts/measure_recall.py                       # topic lane OFF
uv run python scripts/measure_recall.py --topic-lane          # topic lane ON
```

Keep whichever overall number is higher. **Record both** in `docs/STATE.md`, with the flag's resulting default. A two-second decision the harness was built to make; it is left to a measurement rather than an opinion on purpose.

While the harness is open, the same run answers `DEFAULT_RECENT`: 2 (its actual behaviour, docstring corrected in Task E.7) or 8 (its stated behaviour, at the cost of fusion slots the branch measured as harmful). Same rule — record both, keep the higher.

> **No number from this harness leaves the repo.** The corpus is synthetic and was written by the same author as the lanes it measures — the team's own 2026-08-03 trap #3, which `corpus.py` cites against itself in three places — and `HashingEmbedder` has no paraphrase signal at all, so the two lanes that exist to catch paraphrase are measured with the capability removed. It tells you a change made recall worse. It is **not** evidence the lanes work, and no number from it belongs in a demo script or a README. Keep every "regression guard, not evidence" label that ships with the code.

**First failing tests:** the harness runs offline in CI in under 5 s and its symbol-band assertion still fails when the reserved floor is removed (the guard's own guard); the `topic_lane` flag is read from one place and the lane contributes nothing when off (`Lane.TOPIC not in candidate.lanes` for every candidate).

**Exit:** both numbers in `docs/STATE.md`, the flag set from them, the labels intact.

---

## Exit criteria

1. `uv run pytest` — both suites collected, green, **offline**, with no network and no key.
2. **`packages/orchestrator/tests/test_end_to_end.py` green *unchanged*.** If it needed editing, the surface moved and something in this plan was wrong. This is the strongest single signal available.
3. `test_replayed_original_never_clobbers_a_tombstone` green in its rewritten form, through `supersede`.
4. A producer-forged `status=trivial` / `merged_into` push has no effect on visibility.
5. An old near-duplicate outside the last twenty by arrival is selected as a merge candidate.
6. `/query` sends at most `TOP_K` findings into one prompt at 100 findings, with invariant 3 applied at both the `exclude=` seam and inside `query_findings`.
7. `scripts/measure_recall.py` run with the topic lane on and off, both numbers in `docs/STATE.md`, the flag set from them, the "regression guard, not evidence" label intact.
8. `uv run synapse-service` starts — the entry point the branch's `pyproject.toml` would have silently removed.
9. `docs/adr/0004` on `main` with its Amendment (2026-08-05) section; `CONTEXT.md` carrying View / Lane / Candidate / Lane yield / Fold / Topic **and** Triage / Distiller.
10. **Nothing pushed.** `main` moves only when 1–9 are all true.

## Order of work

Each step is independently revertable, and the demo loop is only touched at E.6.

| Step | Task | Guard |
|---|---|---|
| 0 | E.1 storage seam, on `main` before any merge | 387 stay green. Now the seam is real. |
| 1 | E.2 merge · E.3 ADR · E.4 `CONTEXT.md` | Both suites collected; branch tests renamed; entry point starts. |
| 2 | E.5 fifth entry kind (pure package) | Branch's 266 green; fold no longer reads `status`. |
| 3 | E.6 swap + projection | 387 minus the two rewrites; **stop-gate: the tombstone test passes through `supersede` or the swap stops**. |
| 4 | E.7 lanes at both call sites | The two product-claim tests; closed-loop test unchanged. |
| 5 | E.8 topics + watermark + briefing · E.9 recovery path | `briefing.py` still fails open on the new shape. |
| 6 | E.10 harness | The number, not an opinion. |

## Scope / YAGNI

**In:** the storage seam as explicit verdict calls; the branch merged by `git merge` with its authorship; `MarkedTrivial`; `SharedMemory` per Shared Session under `main`'s registry; the Option A projection; lanes at both call sites with `exclude=` suppression and the back-fill fix; topic index and deterministic medoid labels; `/watermark` gaining `topics`/`purpose`/`members`; create-or-return session ids, terminal 4xx in the Relay, `cmd_resync → /synthesize`; ADR 0004 with its amendment; `CONTEXT.md` vocabulary; the recall gate.

**Out, and each for a stated reason:**

- **Service-side log persistence to disk.** The actual restart fix. First item after the demo, ~20 lines *because of* ADR 0004 (`rebuild()` already proves replay is sufficient), and half a story on its own — Working Memory and Conflicts are not in the log, so even after it a restart still recomputes rather than restores them. Half a story is not worth a day of the two we have.
- **`unhealthy_topics()` / `split_topic()`.** Blocked on pruning topic membership when a finding is merged away. Not called, so not blocking.
- **The topic lane on by default.** Measured at 0 partners and 0 uniquely. Task E.10 may turn it on; nothing else may.
- **Model-emitted topic names.** The branch rejected them and was right; deterministic medoid labels ship instead.
- **Option B** for `merged_into`/`status` — a three-track contract break, two days out.
- **Snapshots / event-sourcing compaction.** ADR 0004 already records this as the scaling move deliberately not taken. Fold is microseconds at demo scale (2.5 ms per `candidates()` at 422 findings, 57 ms at 10k).
- **Swapping `HashingEmbedder` for Cirrascale bge.** The `Embedder` protocol is the seam and it is ready; flipping it needs the recall harness re-run *and* the live Cirrascale flip, both still open.
- **Auth / the producer trust boundary at the service.** The forged-verdict half closes for free; a shared token is out.
- **Lane yield on a real corpus.** The only honest test of whether a lane earns its cost, and blocked on the fixture co-sign that is still open (`docs/STATE.md`, "What remains").
- **Anything in `packages/worker` or `packages/distiller`.** Not touched by this integration at all.
- **The worker-side WAL re-join gap** (`docs/STATE.md` trap #8). Untouched here and still needs a prioritization call.

## Risks

| Risk | Mitigation |
|---|---|
| **The store swap silently discards verdicts while every API test passes** | Task E.1 lands the explicit write path **first**, against the mutable store, with 387 green as the guard. The Option A projection then returns copies, so any surviving mutation-through-reference fails loudly. This is the risk the whole task order exists to defuse |
| **Invariant 3 breaks at the new seam** — the branch has no suppression anywhere and `exclude=` is empty | `visible_to` stays the single definition and is still applied inside `query_findings`; it also computes `exclude=`; `searched` subtracts `blocked` so `coverage_line()` stays calibrated. More new tests than anything else in this plan |
| Someone reads ADR 0004's Context and rewrites synthesis to fix a bug that is not there | Amendment A1, dated and attributed, retargets the Context at the restart case and cites the three tests that already pin first-write-wins. Written **during** the merge, not after |
| Two version counters converge into one number | `Log.version` never leaves the store; its docstring is corrected in Task E.5. Taking it as the watermark makes `synthesized` True on every replay and `new_since` a count of log entries |
| The demo loop breaks two days out | The demo path is touched only at E.6, E.7 and E.8; every step is independently revertable; the closed-loop test must stay green **unchanged** at every one of them |
| Topic membership is never pruned, so sizes and health lie | `health()`/`split()` are never called; labels and sizes read `View.members_of`, which the fold already restricts to visible ids. Routed around, not fixed — recorded as a deliberate call, not an oversight |
| A recall number from a synthetic, self-authored corpus under a paraphrase-blind embedder escapes into the demo narrative | Every "regression guard, not evidence" label ships with the code; Task E.10's exit criterion names it; the fixture co-sign is still open and gates lane-yield entirely |
| `git merge` resolution silently drops the console script or an E2-era definition | The nine are enumerated with resolutions in E.2; `uv run synapse-service` and the `CONTEXT.md` grep test are exit criteria, not habits |
| `packages/orchestrator` imports `synapse_service` in `test_end_to_end.py` and resolves only via the shared workspace venv | Noted, not addressed. Harmless until someone installs a package standalone; a one-line dependency declaration whenever that day comes |
