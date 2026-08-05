# Plan C — Service

**Track:** the remote service — ingest, synthesis, shared memory, retrieval.
**Suggested owner:** Siddsing.
**Depends on:** Plan 0 (contracts, fixtures, `FakeProvider`).
**Deployment:** runs on any machine; for the demo, a teammate's laptop. It reaches Cloud AI 100 over HTTPS, so it is decoupled from the accelerator.

**Goal:** merge many Contributors' findings into one Shared Memory that a teammate's agent can usefully query.

> **⟨STATUS 2026-08-04⟩ Providers, storage, ingest, synthesis, and retrieval are implemented on branch `exec/e3`, pending merge to `main`.** `AIC100Provider` routes schema calls through `POST /completions` per the two verified Cirrascale gotchas (Task C.1); the storage seam, ingest API (create/join/push/watermark, idempotent upsert), incremental `Synthesizer.merge` (semantic merge, tombstones, trivia filter, cross-round conflict forward-resolution), and the LLM-as-retriever over the curated Log are all built and tested against `FakeProvider`. A fix-and-verify round is complete — verifier verdict **clean**, with three residual **major** findings still flagged. **First:** the API-level half of the `CANDIDATE_WINDOW` starvation fix (Finding #10) is unpinned by any test that goes through the route — reverting `api.py`'s `await synthesizer.merge(store, sid, findings)` back to the plan's `merge(store, sid, [])` survives the full service+synthesis suite (23 passed, 0 failed) even though it silently reintroduces the exact starvation the finding described; runtime correctness was verified live over ASGI (a 25-finding POST puts all 25 ids in the merge prompt), but nothing fails if that regresses. **Second:** the Task 5 post-review amendment is self-contradictory — it credits the restored extractor as "not hand-rolled brace-counting" against a code listing in the same document that still is one — and omits two shipped, tested deviations: `_satisfies_schema` gating `schema_valid=True`, and the retry sending a repair prompt at `temperature: 0.2` rather than an identical resend at `0.0`. **Third:** Finding #11's resync story is half-built — a failed push now correctly reports `synthesized: false` (a malformed verdict no longer masquerades as success), but there is still no endpoint to re-run synthesis independent of a later push, so a session whose last push failed cannot self-heal; this plan's own Task C.3 first-failing-test for a full-log resync converging to the same state still has no corresponding test. None of the three block the exit criteria below; all are considered real and worth closing before E4 integration leans on the recovery story.
>
> Three plan amendments recorded post-review on `docs/plans/exec/2026-08-04-e3-service.md`: end of Task 2 (whole-verdict pydantic validation with no partial application, the `CANDIDATE_WINDOW` union-with-current-push fix, cross-round conflict forward-resolution + dedup, and merge-of-one restored — a revert of a mid-review inversion, not a new evolution); end of Task 4 (the watermark's content/change field split, findings passed through to `merge()` instead of `[]`, the new `synthesized` response field, 422 validation added to the other three routes, and the still-unbuilt resync gap flagged as a known, undertaken-out-of-scope item); end of Task 5 (the extractor restored to the plan's standalone string-aware scanner after a mid-review deviation had aliased it to `openai_compat`'s private helper, plus new truncation logging on `finish_reason == "length"`, with `max_tokens=800` explicitly left unchanged).

---

## Task C.1 — Providers

- `ClaudeProvider` — Anthropic SDK. Quality baseline and judge.
- `OpenAICompatibleProvider` — one HTTP adapter; `OllamaProvider`, `AIC100Provider`, and Plan B's `NPUProvider` differ essentially by base URL.
- `AIC100Provider` — hosted Cirrascale, `Llama-3.1-8B`, `INFERENCE_CLOUD_API_KEY`.

> **⟨CORRECTION⟩ The key *is* entitled to 70B/32B — `GET /models` under-reports.** The earlier "8B only" conclusion was drawn from `/models` alone, which returns just `{"llm":["Llama-3.1-8B"]}`; the console lists `Llama-3.3-70B`, `DeepSeek-R1-Distill-Llama-70B` and `Qwen-QwQ-32B` as available with configured buckets. Re-probed by invocation, which is the only authoritative test: an *unentitled* model is rejected immediately with a distinct `429` naming the missing rate-limit config, while the 70B/32B models are **accepted by the router and then block on backend capacity** (500 after 60 s, or client timeout). Entitlement present, capacity absent — a different failure with different consequences.
>
> **But no successful generation above 8B has ever returned.** 70B synthesis quality is unmeasured, not merely unavailable. Do not plan around it until one completion actually lands. Treat `/models` as a lower bound on the catalog, never as the entitlement list — a lesson that generalises past this provider.

Two verified gotchas, both probed live:
1. `response_format: json_schema` is **silently ignored** → `native_structured_output = False`.
2. `/chat/completions` **eats JSON output** — any prompt leading the model to emit `{...}` trips the server's tool-call parser, returning empty content plus an empty `tool_calls` entry. Schema calls must route via **`POST /completions`** with a flattened prompt and a tolerant first-balanced-object extractor.

Bound `max_tokens` on every call — the credit pool is shared.

**First failing tests:** provider conformance (same input → schema-valid output, `usage`/`latency` populated, capability flag honoured); a schema request to `AIC100Provider` hits `/completions`, never `/chat/completions`; a malformed response is tolerantly extracted, then retried once.

## Task C.2 — Storage seam

A narrow interface, because **this shape is a deliberate first pass and is where most of the system's future value lives**. Do not over-freeze it.

```
store_findings(shared_id, Finding[])    idempotent upsert by id
get_context(shared_id)                  -> SessionContext
query_candidates(shared_id)             -> retrievable Findings
```

First implementation is in-memory. Vector RAG, a findings graph, or a purpose→topic hierarchy are candidate backends chosen later on evidence.

**Retrievable is defined once, here:** `merged_into is None and status is KEPT`.

**First failing tests:** the same `Finding` stored twice yields one row (idempotent by id); a tombstone is stored but excluded from `query_candidates`; a trivial finding likewise.

## Task C.3 — Ingest API

Receives findings from orchestrators, plus Shared Session create and join.

- `POST /v1/sessions` → create with purpose
- `POST /v1/sessions/{id}/members` → register a Contributor
- `POST /v1/sessions/{id}/findings` → upsert by id
- `GET  /v1/sessions/{id}/watermark` → `{version, new_since, topics, conflicts}` — precomputed, **no model call**, safe to hit every turn

**First failing tests:** findings POST and land; **a replayed POST with identical ids is a no-op** (this is the retry path, exercised for real); a full `resync` of a machine's entire log after a service restart converges to the same state as the original stream; an unknown session errors cleanly; the watermark endpoint responds without touching a provider.

> Idempotent upsert is what makes the orchestrators' durable logs safe to replay wholesale. Since Shared Memory is in-memory and a restart wipes it, that replay is the recovery path — test it deliberately rather than assuming it.

## Task C.4 — Synthesis

Incremental merge: `(SessionContext, new Finding[]) -> SessionContext`. The prompt sees the bounded Working Memory plus the new findings, so cost stays flat as a session grows.

**Three jobs, and the second is the one that is easy to get wrong:**

**a. Working Memory** — rewrite the bounded prose (~500 words). Read only by the next merge.

**b. Semantic merge.** Two Findings that *mean the same thing* produce a **new Synthesized Finding** capturing the essence of both, carrying **every** source's Attribution and `merged_from` lineage. The originals become **tombstones**: `merged_into` set, text and Attribution retained, excluded from retrieval.

> Never discard-one. *"the timing window is 40 ms"* + *"fails when delay exceeds ~40 ms **under load**"* must not silently lose "under load" — pooling those halves is the entire product.
>
> Never rewrite an original in place either, or its id points at text its author never wrote — and `Conflict` holds ids.
>
> Tombstones rather than deletes, for correctness: ingest upserts by id so a retry must find a known id; conflicts must follow `merged_into` forward; and this merge is an 8B model's judgement performing the only irreversible action in the system.

**c. Conflicts and trivia.** Contradictory pairs become `Conflict{finding_a: FindingId, finding_b: FindingId, description}` — surfaced, never silently resolved. And one prompt instruction: *drop findings that merely restate actions without insight* → `status = TRIVIAL`.

> **⟨2026-08-04⟩ This filter got promoted from backstop to load-bearing.** `adr/0003` removed durability judgment from the distiller because a 4B invented findings from all-noise segments in 6 of 6 configurations. That judgment now has exactly two homes: triage upstream (Plan A.5b, **does not exist**) and this instruction. Today neither is built, and trivia demonstrably reaches the sink.
>
> Two things follow. First, **this is no longer a nice-to-have prompt line** — write a test for it, not just an instruction. Second, it runs on `Llama-3.1-8B`, whose quality at this job is unvalidated, and the 70B that might do it better has never returned a completion. Do not assume the downstream half of `adr/0003` is free.

⟨CORRECTION, corrected 2026-08-05⟩ Bump `memory_version` once per verdict round applied, not once per merge — `synthesis.merge` calls `bump_version` at the end of every structurally-valid verdict, `"merges": []` included (see `adr/0004`'s Amendment for the full correction and why it matters for `/findings`'s `synthesized` field).

**First failing tests:** two Contributors' findings merge; **`seg-005`'s near-duplicates produce one Synthesized Finding carrying both Attributions, with both originals tombstoned and neither retrievable**; a contradictory pair yields a `Conflict` referencing ids; a trivial finding is marked, not deleted; ⟨CORRECTION, corrected 2026-08-05⟩ `memory_version` increments exactly once per verdict round applied, merges or not; incremental merge cost stays bounded as findings accumulate.

**The prompt is explicitly first-pass** — Claude-drafted, tuned by Plan B's eval loop, not a contract.

## Task C.5 — Retrieval

LLM-as-retriever: query + purpose + Working Memory as context + **candidate findings from `query_candidates`** → ranked findings. No embeddings at this scale.

> **This must rank over the Finding Log, not the Working Memory.** The prose is bounded and read only by the next merge. If retrieval ranks raw pushed findings instead of curated candidates, synthesis's dedup and trivia filter protect nothing a teammate ever sees — which was the whole point of doing them.

**First failing tests:** relevant findings rank above irrelevant; tombstoned and trivial findings **never** appear; an empty log returns empty rather than hallucinating; a nonsense query returns nothing rather than everything.

## Task C.6 — Awareness support

The service half of the awareness layer (Plan D owns delivery).

- `memory_version` on `SessionContext`, compared against each member's `last_seen_version`, advanced when that member queries.
- Arrival briefing content: purpose, members, counts by type, topic labels, conflict count, current version. **Hard-capped near 200 tokens** — headlines and topics only, never finding bodies. Bodies grow with session length; headlines do not.
- Suppression happens at retrieval: exclude a Finding only when **every** Attribution is the asking agent's own Agent Session.

**First failing tests:** a briefing for a session with 40 findings stays within budget; querying advances that member's `last_seen_version`; a Synthesized Finding carrying a teammate's Attribution is **not** suppressed from the co-author's agent.

---

## Exit criteria

1. Provider conformance green, including the `/completions` schema path.
2. Idempotent ingest proven with a replayed POST.
3. `seg-005` merges into one Synthesized Finding with both attributions and two tombstones.
4. Retrieval excludes tombstoned and trivial findings.
5. Whole track green offline against `FakeProvider`; then the same suite against `aic100`.

## Scope / YAGNI

**In:** providers, storage seam, ingest, synthesis with semantic merge, retrieval, awareness support.
**Out (stretch):** a retrieval-optimised backend (RAG / graph / hierarchy); cross-session persistence (restart wipes — acceptable here); auth beyond opt-in join (**a named gap, not an oversight**); streaming synthesis; dynamically surfacing tombstoned text when merge confidence is low.

## Risks

| Risk | Mitigation |
|---|---|
| Retrieval reads Working Memory instead of the Log | Explicit test that a trivial finding never surfaces. This is the single easiest mistake to make in this plan |
| Bad merge by the 8B destroys an insight | Tombstones make it recoverable and inspectable; the merge prompt is conservative by default |
| Synthesis quality on a single 8B | Tight Working Memory bound, simple instructions, tolerant parse. The Claude-vs-8B delta *is* the demo narrative. ⟨2026-08-04⟩ 70B/32B **are entitled** but capacity-blocked — no completion above 8B has ever returned, so this is now a capacity question to raise at office hours rather than an entitlement one |
| **`seg-005` does not exist, so semantic merge is entirely unexercised** ⟨2026-08-04⟩ | ADR 0002 is the most intricate decision in the design and nothing tests it. This plan's exit criteria cannot be met until Plan 0's fixture work lands — a hard cross-track dependency, not a nice-to-have |
| Shared credit pool / unknown rate limits | Bound `max_tokens` everywhere; dev loops on Ollama; track spend via `ModelResult.usage` |
| Cirrascale chat endpoint mangles JSON | Route schema calls via `/completions` with tolerant extraction — probed and verified |
| Cross-machine ordering | Wall-clock timestamps assumed sufficient at this scale. A named limitation |
