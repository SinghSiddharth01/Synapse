# Amendment 2026-08-03: Hybrid frontier/local strategy — Part 1 local-only runtime, Part 2 contribute module

**Status:** Adopted 2026-08-03. Part 1 in scope for the build week; Part 2 deferred (first post-walking-skeleton stretch). One contract action lands now (`Finding.provenance`, see Part 2).
**Amends:** `2026-07-25-synapse-design.md` §5 (Finding contract) / §11 (scope) · Plan B Tasks 1 & 6 · MCP tool surface (Part 2, deferred)
**Context:** Resolves how frontier models (Claude) and the local models divide labor. The rule that fell out of the analysis: **the Finding boundary is the trust boundary.** Raw work never crosses it; below it, any model is safe to use.

---

## Decision rule — the four seams

| Seam | Pattern | Verdict |
|---|---|---|
| Design-time | Frontier as teacher/judge offline; only its artifacts (few-shots, prompt revisions, eval verdicts) run at runtime | **Adopted — Part 1** |
| Below the Finding boundary | Frontier synthesis/retrieval over already-redacted findings | Legal by construction; stays a config line (`synthesizer: claude`); AI-100 remains the demo path |
| Raw-segment escalation | Local distiller escalates hard segments up to a frontier API | **Rejected** — ships raw work off-device; deletes the privacy invariant that is the product. Teams without the privacy need already have `distiller: claude` as a config tier |
| Contribution | Agent self-distills in NL; the local model gates it into shared memory | **Adopted — Part 2 (deferred)** |

---

## Part 1 — Everything local at runtime (build week)

Runtime dataflow is unchanged: passive JSONL observation → segmenter → NPU distiller → findings → AI-100 synthesis. **No frontier model in the runtime path.** Frontier leverage is design-time only:

1. **Claude-authored few-shots (Plan B Task 1).** Run Claude over the fixture segments; freeze 1–2 of its best outputs into the distiller prompt — including one example that *abstracts* instead of quoting. Directly attacks the 4B's two worst expected failure modes (verbatim copying, triviality). Budget ~300–400 tokens inside the segment context budget.
2. **Eval loop as prompt optimizer (Plan B Task 6).** After a benchmark run, feed the low-scoring cases (segment → 4B output vs golden) back to Claude for failure diagnosis and a proposed prompt revision. ~50-line script on top of the existing harness. Frontier as prompt engineer, never as runtime dependency.
3. **Passive frontier leverage via compaction weighting (Plan A segmenter).** The agent's final per-turn text is already frontier-authored distillation of its own work. Compaction weights segment prompts toward assistant text and head/tail-truncates raw tool_results — the 4B summarizes Claude's summaries plus the error signal, not 30K tokens of raw logs. (Full segment token budget + compaction rules: `2026-08-03-segment-budget-compaction-amendment.md`; this item only fixes the *weighting* principle.)
4. **Deterministic quality axes (Plan B Task 6).** Two checks that need no LLM: **verbatim-copy rate** (n-gram overlap between finding text and segment content — this is the privacy metric, first-class in the benchmark table) and **empty-segment discipline** (one all-noise fixture segment whose golden is an empty array).

---

## Part 2 — Contribute module (deferred, toggleable)

**Concept.** Synapse's MCP is already connected for retrieval, so the agent can push as well as pull. A `contribute(text)` tool lets Claude distill a complex learning into a few sentences of free-form natural language; the **local** model converts that NL into structured findings. Frontier does the hardcore thinking; the local gate does the structuring.

**Why this is not the rejected escalation seam.** The direction is inverted and no new data crosses any boundary: the agent already holds the raw session in its own context, so self-summarization exposes nothing that isn't already at the provider by virtue of the session existing. Only the abstracted digest enters the Synapse pipeline. The invariant *strengthens*: not just "raw work stays on device" but **"the local distiller is the sole gate into shared memory,"** regardless of how capable the source is.

**Mechanism (small by design — no second distiller).**

- MCP tool `contribute(text: str)` — free-form NL; no schema exposure to agents.
- Handler wraps the text as a synthetic `Segment` → existing `Segment → Finding[]` distiller path → findings tagged `provenance: contributed`.
- **Toggle = tool availability.** A per-session config exposes or hides the tool. No complexity router in code — the tool description carries the judgment: *"call this when you've learned something non-obvious a teammate would benefit from."* The agent's own tool-selection judgment is the complexity detector.
- A CLAUDE.md line / hook nudges usage. Compliance is probabilistic and that is acceptable — the passive path is always-on underneath.

**Why NL → local → JSON instead of the agent filling the schema directly** (a decoupling choice, not a necessity — recorded honestly):

1. The agent-facing contract stays free text → any agent can contribute; the schema evolves without touching agent integrations.
2. Never trust external conformance — the local gate normalizes and validates uniformly.
3. The deterministic redaction check (Part 1 item 4) runs on contributed findings too: a careless digest that quotes a secret is caught at the same choke point as everything else.

**Interaction with the passive path.** The same insight may arrive twice (contributed + passively distilled). Dedup is synthesis's existing job; the provenance tag lets it prefer the contributed version on conflict.

**Contract action NOW — the one Part-2 cost paid during Part 1:** add `provenance: distilled | contributed` (default `distilled`) to the `Finding` contract before Day-0 freeze. One optional field today; a contract break across all three tracks later.

**Positioning.** Tiers, not replacement: the passive floor works with any unmodified agent (the pitch's sharpest edge stays intact); contribution mode is the cooperative tier for agents that support MCP — which is already Synapse's retrieval plane, so no new integration surface.

**Demo note.** If the core pipeline lands by mid-week, this is the strongest available demo beat: Claude visibly hands an insight to team memory, and a teammate's agent retrieves it seconds later — the product thesis in one visible moment.

**Risks (Part 2):**

| Risk | Mitigation |
|---|---|
| Agent doesn't reliably call the tool | tool description + CLAUDE.md nudge; measure call rate; passive path is the floor |
| Digest quotes secrets / verbatim code | same n-gram redaction gate as passive findings |
| Dilutes the "passive listener" pitch | frame as tiers; passive remains the default and the demo path |
| Double-counting insights | provenance tag + synthesis dedup (existing responsibility) |

---

## Plan impact summary

| Plan item | Before | Now |
|---|---|---|
| Plan B Task 1 (distiller prompt) | rules-only system prompt | + 1–2 Claude-authored few-shot examples (incl. abstraction example) |
| Plan B Task 6 (eval harness) | judge score, cost, latency | + verbatim-copy rate (deterministic), + empty-segment fixture, + failure-analysis → prompt-revision loop |
| Plan A (segmenter/compaction) | turn-boundary segments | compaction weights assistant text over raw tool_results (budget details in pending compaction amendment) |
| `Finding` contract (§5) | `{type, text, contributor, ts, source_session, refs?}` | + `provenance: distilled \| contributed` (default `distilled`) — added at freeze, used in Part 2 |
| MCP tools (§5) | `create_session` / `join_session` / `query` | unchanged now; `contribute(text)` reserved for Part 2 (stretch) |
| Scope §11 | — | contribute module added to stretch goals |
