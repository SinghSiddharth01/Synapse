# Amendment 2026-08-03 (D): Agent auto-detection, pre-recorded A/B demo, per-model segment budget, shared-memory storage

**Status:** Adopted 2026-08-03 (review of the architecture overview page)
**Amends:** `2026-07-25-synapse-design.md` §2/§4/§5/§11/§12 · Plan A (source layer, segmenter) · Plan B (budget derivation, prompt status) · Plan C (storage seam, demo metrics) · awareness layer (architecture page)
**Context:** Doc-review session notes. Six decisions; the connecting thread is **nothing in Synapse may be shaped around one particular agent** — per-agent knowledge lives in exactly two thin, registered places (capture adapters and awareness packs), and everything between them is agent-blind.

---

## Part 1 — Agent auto-detection (capture side)

The worker must not be *configured* for Claude Code; it must **identify which agent is being used** and pull that agent's transcripts (or compacted summaries) from the path pertaining to it.

- **Agent registry.** A small static table: agent name → transcript root(s) + transcript dialect + `Source` class. Claude Code: `~/.claude/projects/<slug>/*.jsonl`. Codex: its session dir (confirm exact path during build). New agent = one registry entry + one adapter; nothing downstream changes.
- **Detection = fresh transcript activity.** The worker watches registered roots and treats a recently-growing transcript as an active agent session. Multiple agents active at once → multiple follower+adapter instances; the pipeline from `AgentEvent` onward never knows which agent produced what.
- **Compaction artifacts count as capture.** Where an agent writes compaction summaries into (or alongside) its transcript, the adapter ingests them as events — they are high-density signal, per the compaction-weighting principle in the hybrid amendment.
- **`CodexSource` is elevated from stretch to the demo pair.** The hackathon/demo scope is exactly two agents — Claude Code and Codex — with the design applicable to any coding agent regardless. The agent-agnostic claim is now *shown* in the demo, not asserted.

## Part 2 — Agent-agnostic awareness (retrieval side)

The awareness layer (architecture page) is re-framed the same way. The four signals — trigger-voiced tool descriptions, arrival briefing, freshness pointer, relevance skill — are **mechanism-neutral**; what is per-agent is only the *delivery vehicle*, packaged as an **integration pack**:

| Tier | Surface | Agent-specific? |
|---|---|---|
| Floor | MCP tool descriptions + server `instructions` + `join_session` briefing return | No — works in any MCP client |
| Pack | Claude Code: `SessionStart`/`UserPromptSubmit` hooks + auto-triggering skill | Yes — one pack per agent |
| Pack | Codex: equivalents via its config/instructions surface (investigate during build) | Yes |

Packs mirror `Source` adapters: thin, registered, replaceable. No awareness behavior may live in the service that assumes a specific agent.

## Part 3 — Demo strategy: pre-recorded, A/B

The demo is **pre-recorded**, not live. Two recordings of the *same task*:

1. **Baseline:** the team works without Synapse — duplicated exploration visible on screen.
2. **With Synapse:** same task, shared memory active — the reuse moment visible on screen.

Shown side-by-side with measured deltas: wall-clock to resolution, tokens spent (from `ModelResult.usage`), duplicated tool-call/exploration count, findings reused. The eval harness already produces these numbers; the demo just points a camera at them.

Consequences: the **venue-WiFi risk collapses** — it now only threatens the optional live encore, not the demo; the offline fallback configs remain for that encore. Recording day becomes the real deadline: everything on the demo path (including `CodexSource`) must work by then.

## Part 4 — Segment token budget is per-model, not a constant

The ⟨C⟩ amendment bounded segments at "~2–2.5K tokens, final number from the spike." Sharpened: there is **no single final number**. The budget is **derived per model** from its measured capabilities at startup:

```
budget(model) = usable_context(model)            # measured, not advertised
              − system_prompt_tokens
              − few_shot_tokens
              − response_reserve
   …clamped by a prefill-latency ceiling: budget ≤ prefill_toks_per_sec(model) × max_seconds_per_call
```

- Capability numbers (`usable_context`, `prefill_toks_per_sec`) live in per-model config next to the provider entry, filled from spike/eval measurements — the ⟨C⟩ working assumption (~2–2.5K on a 4K qairt bundle) becomes one row of that table, not a global.
- The segmenter reads the budget from the resolved distiller's capability record; swapping distiller models (4B → 1.7B → 8B, or Ollama fallback) automatically re-budgets with **no segmenter change**.

## Part 5 — Distiller & synthesis logic: first pass, expect fine-tuning

Both prompts (and the synthesis merge strategy) are **explicitly first-pass**: we go with Claude-suggested drafts, and they are *not* contracts. The tuning mechanism already exists — the eval harness plus the failure-analysis → prompt-revision loop (hybrid amendment Part 1). Recorded so nobody treats the first prompt as a frozen interface or is surprised when it changes mid-week.

## Part 6 — Shared-memory storage: open question, seamed off

How the service stores `SessionContext`/findings for **convenient semantic retrieval by agents** is an open design question. Candidates, in rough order of likelihood:

| Option | Sketch | Note |
|---|---|---|
| Vector RAG | embed findings (bge-large already on our key) + rerank | Cheapest to reach from current infra |
| Findings graph | findings as nodes; edges = topic, refs, conflicts, provenance | Natural fit for conflict/contradiction traversal |
| Hierarchy / tree | purpose → topic → finding decision-tree organization | Closest to how the briefing already summarizes |

**Decision now:** none of the above. First pass stays **in-memory + LLM-as-retriever**, but behind a narrow storage interface (store findings / get context / query candidates) so a backend can swap in without touching synthesis or MCP. The backend choice is a stretch/scaling decision made on evidence, not up front.

---

## Plan impact summary

| Plan item | Before | Now |
|---|---|---|
| Plan A source layer | `ClaudeCodeSource` core; `CodexSource` stretch | + agent registry & auto-detection; `CodexSource` on the demo path |
| Plan A segmenter | budget "~2–2.5K, final number from spike" | budget computed from the resolved distiller's capability record (Part 4 formula) |
| Plan B Task 4/6 | spike measures one bundle's numbers | measurements populate per-model capability rows; budget derivation tested per model |
| Plan B Task 1 / Plan C synthesis prompt | implied stable | explicitly first-pass; eval loop is the tuning mechanism |
| Plan C service | `InMemorySynthesizer` state is the store | same, behind a storage interface; RAG/graph/tree deferred (Part 6) |
| Demo | live end-to-end at the venue | pre-recorded A/B with measured deltas; live run is the encore |
| Scope §11 | CodexSource, vector RAG in stretch list | CodexSource → in; storage backends replace "vector RAG" as the retrieval stretch |
| Awareness layer | Claude Code hook pack (tier 2) | per-agent integration packs; Claude Code pack is the demo instance |
