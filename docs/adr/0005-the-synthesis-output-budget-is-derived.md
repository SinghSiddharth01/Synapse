# 5. The synthesis output budget is derived, and merge rate is governed by spend

**Status:** Accepted (2026-08-06).

## Context

On 2026-08-06 the Shared Memory stopped moving. `memory_version` froze while findings kept landing normally — 8 findings at 01:32, 135 by 02:10, and every one of them queryable. Only the *synthesized* memory was stuck. The service returned HTTP 200 to every push, and the sole trace was one log line per round:

```
Synthesis returned schema_valid=False data of type NoneType for sh-bbe76a56;
findings are landed, memory unchanged
```

**Root cause.** `SYNTH_SYSTEM` asked the model to rewrite the working memory "under 500 words". `AIC100Provider.__init__` carried `max_tokens: int = 800`. Measured against the live memory — 3065 chars / 477 words / ~766 tokens — those two numbers are arithmetically incompatible:

| output field | tokens |
|---|---|
| `working_memory` at its instructed size | 766 |
| JSON envelope (4 keys, braces, quoting) | 40 |
| **floor before a single verdict** | **806** |
| cap | 800 |
| room for `merges` / `trivial_ids` / `conflicts` | **−6** |

The model was obeying its instruction exactly. Every response was cut off mid-object, `extract_first_json_object` returned `None`, and `merge()` took its documented "landed, quality degraded" path. Neither number was wrong alone. They lived in different files owned by different concerns, and **nothing in the system ever compared them.**

Three things made it invisible for hours rather than minutes:

1. **The endpoint lies about truncation.** Probed live: a `/completions` call with `max_tokens=3000` returned `completion_tokens: 3000` and `finish_reason: "stop"`. The guard at `aic100.py` keyed on `finish_reason == "length"` and could never fire.
2. **The test that covered it fabricated its fixture.** `test_a_truncated_response_is_logged_distinctly_from_a_parse_failure` asserted against a hand-written `"finish_reason": "length"` the host never sends. Its docstring had *predicted this exact failure* — "under 500 words, ~650-700 tokens alone… all under max_tokens=800 by default" — and it passed throughout the outage.
3. **Working memory is double-charged.** It is the largest input segment *and* the entire output budget — echoed in, rewritten out. Once it saturates its word cap, every subsequent round is guaranteed to overflow. Failure went from intermittent (2 rounds) to total (12 rounds) as input climbed 2110 → 2413 → 2924.

`capability.py` has owned exactly this discipline for the distiller since the beginning: *"The budget is **never** a shared hard-coded constant: swapping the distiller model, or editing the prompt, must re-budget segmentation automatically."* Synthesis — the reduce half — was outside that system entirely, with no `[capability.*]` record at all.

## Decision

**1. The synthesis output budget is derived, and an impossible configuration is refused.**

`SynthesisBudget.derive(x)` splits the provider's output cap into a working-memory word allowance and verdict room. Verdicts are reserved **first**: the failure was the opposite order in effect, and a round that reports no merges did not do its job however good the prose is. A budget too small for a memory that can brief anyone raises `SynthesisBudgetError` — the mirror of `MIN_USABLE_SEGMENT_TOKENS`.

| x | WM cap | verdict room | max merges |
|---|---|---|---|
| 800 (what shipped) | **270 words** | 301 tok | 4 |
| 1600 (adopted) | **500 words** | 710 tok | 10 |

The top row is the bug stated as a number: at 800 the affordable memory was 270 words while the prompt demanded 500.

**2. The prompt states the budget; the budget is never typed into the prompt.** `SYNTH_SYSTEM` became `synth_system(words, max_merges)`, generated from the budget. Editing `max_tokens` or swapping the synthesis model re-states the cap automatically — the same rule `PromptPack` already follows by deriving overhead from its own text.

**3. Truncation is detected from usage, not `finish_reason`.** `completion_tokens >= max_tokens` is endpoint-independent: a model cannot emit more than the cap, so reaching it exactly means a cut-off response or one that ended precisely on the boundary. Those are indistinguishable from the client, and treating the second as complete is the unsafe direction.

**4. A truncation retry shrinks the ask; it does not echo the overflow.** The generic repair path appends the bad response plus "did not match the schema" — under a length failure that lengthens the input and gives the model no reason to emit less, guaranteeing attempt 1 fails exactly as attempt 0 did.

**5. Overflow sheds the working memory, oldest first — never a verdict, never a key.** Stated to the model as a hard requirement. This is safe *by construction*: **the working memory is a projection over the Log, not the record.** Evicted material is still on disk and rebuildable via `/synthesize`. An omitted merge, by contrast, simply does not happen that round.

**6. The budget bounds what the model REPORTS, not what it is SHOWN.** Candidate selection is untouched. See Rejected alternatives.

**7. Merge rate is two limits, not one.** `MERGE_MIN_INTERVAL_S = 60` is a *latency* floor and a product decision. A separate governor tracks real token spend over a rolling hour and defers when the next round will not fit. A clock is a poor proxy for a budget; one fixed interval forces a choice between "too slow when quiet" and "over budget when busy". The two deferral reasons are logged distinctly because their fixes differ — latency deferral says lower the interval, budget deferral says add keys and lowering the interval will not help.

The governor charges **actual** usage including failed rounds — a truncated verdict burns the same tokens as a good one, and not counting those is part of how the key ran dry unnoticed. It prices the next round at the most expensive of the last five rather than an average, because merge cost trends upward with the candidate listing and an average lags the trend.

**8. An identical resend is not a log entry.** `push_findings` upserts and then `merge()` upserts the same list again, so every finding was appended twice — visible as duplicate `FINDINGAPPENDED` rows in the dashboard's log tail, and multiplied further by a write-ahead log that retries by design. `upsert` now skips the append when the stored finding compares equal. Cost per push: 3N → 2N. **Only exact duplicates are dropped** — a resend whose content changed is still a real event and still appended, because the log is the record and discarding a changed finding because its id was seen before loses data rather than noise.

## Consequences

**Deployment is not optional and not free.** These are code changes to a service whose store is in-memory. Restarting drops the Shared Memory; `resync` recreates the session under the same `sh-` id, but each machine holds only its own relay, so every contributor must resync or their findings do not come back.

**One key cannot deliver 60-second latency.** At ~4,000 tokens per merge (input ~2,500 and climbing, output up to 1,600) against 25,000 tokens and 20 requests per hour:

| | needed at 60s sustained | available on 1 key |
|---|---|---|
| tokens/hour | 240,000 | 25,000 |
| requests/hour | 60–120 (retries double) | 20 |

Tokens bind ~10× over. **~10 keys** are required to hold 60s under continuous pushes; ~7 at the load actually observed. On one key the governor allows ~6 rounds/hour: quiet periods get their 60s, busy periods stretch. Findings stay queryable throughout — only the synthesized memory lags.

**Round-robin is currently dead code, at both layers.** `AIC100Provider._post_rotating` rotates on 429, but the service's pool holds one entry (`"local-stand-in"`) because it talks to the local proxy; and `scripts/local_model_server.py`, which holds the *real* key, has no rotation at all. Adding keys to `secrets.jsonc` today changes nothing. Multi-key requires either pointing the service straight at Cirrascale (losing the model seam) or teaching the proxy to rotate a pool. **Open.**

**`RecordingProvider` must be transparent to configuration, not only to results.** It forwarded `provider_id` and `capabilities` and dropped `max_tokens` — so `build_app(debug=True)`, the path that actually ships, budgeted every merge at the 800 default no matter what `INFERENCE_CLOUD_MAX_TOKENS` said. Observed with the env var at 1600: 270 words / 4 merges asked for, 500 / 10 paid for. A wrapper that is "transparent" for the two attributes someone happened to need is opaque for everything else.

**`800` was never an endpoint limit.** Probed live: 3000 is accepted. It was a bound on the shared credit pool, chosen for cost and then treated as physics.

**Raising `max_tokens` without raising the timeout moves the failure rather than fixing it.** `AIC100Provider`'s client timeout was 60.0s; two failing rounds measured 48.5s and 51.7s, and one later round hit 66.3s. A `ReadTimeout` is caught by `synthesis.py`'s `except Exception` and reported as "findings are landed, memory unchanged" — the identical symptom from a different cause. `INFERENCE_CLOUD_TIMEOUT` travels with `INFERENCE_CLOUD_MAX_TOKENS` and both are set together in `serve_local.py` (1600 / 180s).

## Rejected alternatives

**Bounding the candidate list by the verdict budget.** Tried first; `test_synthesis.py` rejected it in one run. With 5 findings pushed at an 800-token budget the trim left **zero** established merge partners — precisely the starvation the ranked union exists to prevent. The reasoning error: a candidate is an *input* cost, and input is not the constraint (2110 tokens against a 128K window). It becomes an output cost only in the minority of cases where it actually merges. Trimming candidates therefore destroys merge quality to save tokens it was not spending.

**A reactive retry on overflow.** Discovering the overflow after the fact and re-merging costs a second request against a 20/hour ceiling — and the provider already spends its one internal retry there. The shed must be decided before the call, or not at all.

**A single larger fixed interval.** 480s was the first answer and it satisfies neither goal: too slow for a teammate waiting on the memory, and still over budget under sustained load. Latency and spend are genuinely different limits and need different instruments.

**Removing `merge()`'s own upsert** — the deeper fix for the duplicate-append cost. Still out of scope: all 19 `merge(store, ...)` call sites in `test_synthesis.py` land findings nobody upserted first. It is no longer costing anything.
