ROOT = `/Users/siddharthsingh/Dev/synapse` (audited via the worktree checkout; content identical). All paths below are absolute.

# W10 — DOCS AUDIT

## A. Inventory and verdicts

### Root
| Doc | Verdict | Reason |
|---|---|---|
| `/Users/siddharthsingh/Dev/synapse/README.md` | **contradicted-by-code** | Says Codex is unbuilt (L59), documents only 2 of 6 MCP tools (L86), and its teammate-join command drops `--service-url`, which silently creates a second isolated memory. |
| `/Users/siddharthsingh/Dev/synapse/CONTEXT.md` | **fresh** (2 defects) | The 2026-08-06 re-key is recorded correctly (L122–126); `provenance` enum is under-stated (L128) and the Orchestrator/Producer entries overstate ownership (L48, L52). |

### `docs/` top level
| Doc | Verdict | Reason |
|---|---|---|
| `/Users/siddharthsingh/Dev/synapse/docs/STATE.md` | **contradicted-by-code + self-contradictory** | L90 says CodexSource + compaction merged; L102–103 and L151 say both unbuilt/parked. Test counts (526/387/565) are all stale; header still "2026-08-05". |
| `/Users/siddharthsingh/Dev/synapse/docs/demo-script.md` | **stale** | §B is written as "the branch" vs "main" (L3 vs L160); pre-dates the 60s merge debounce, the budget governor, `_asking_contributor`, and session_meta purpose retention (L304). Beats use `agent_session=` only — now the legacy path. |
| `/Users/siddharthsingh/Dev/synapse/docs/JOIN.md` | **fresh** | Best doc in the repo. Covers the six lifecycle tools, both id kinds, ambiguity refusal, `--listen`, troubleshooting. Only nit: no mention of the merge debounce (why memory lags ~60s). |
| `/Users/siddharthsingh/Dev/synapse/docs/JOIN-WINDOWS.md` | **fresh** | PowerShell mirror of JOIN.md; ARM64 + `mcp==1.9.4` traps intact. Must be re-checked whenever JOIN.md changes (they are declared "meant to stay in step", nothing enforces it). |
| `/Users/siddharthsingh/Dev/synapse/docs/NPU-RUNBOOK.md` | **stale** | Gate 1 asserts "717 passed" (L35); 846 test functions exist statically today. Otherwise accurate, incl. the `/completions` vs GenieX trap. |
| `/Users/siddharthsingh/Dev/synapse/docs/DEMO-READINESS-CHECKLIST.md` | **fresh** | Dated 2026-08-06, 4 open items, all still genuinely open. |
| `/Users/siddharthsingh/Dev/synapse/docs/2026-08-04-implementation-report.md` | **orphaned (historical)** | Explicitly a point-in-time report; its "orchestrator is a transport shell" (Part 1) is long false but the doc is dated and referenced as history. |
| `/Users/siddharthsingh/Dev/synapse/docs/2026-08-05-service-implementation-report.md` | **orphaned (historical)** | Branch report for `feat/shared-memory-store`, 266 tests. Superseded by the merge; still the only prose on lanes/fold internals. |
| `/Users/siddharthsingh/Dev/synapse/docs/architecture.html` | **stale** (unaudited in detail) | 90 KB, README's "architecture deep-dive" (README L100) and INSTALL.md's anchor source (`#awareness`). Pre-dates lifecycle, debounce, ADR 0004/0005. Highest-visibility doc with the least review. |
| `/Users/siddharthsingh/Dev/synapse/docs/demo-transcripts.txt` | **orphaned** | Referenced by nothing. |

### `docs/adr/`
| Doc | Verdict | Reason |
|---|---|---|
| `0001-local-orchestrator.md` | **contradicted-by-code** | L13 "stamps Attribution onto every Finding from any Producer" — removed in round-2 review. |
| `0002-semantic-merge-and-tombstones.md` | **contradicted-by-code** | L36 asserts suppression is keyed on Agent Session. Header already self-flags the 0004 supersession, but not this. |
| `0003-distiller-compresses-rather-than-judges.md` | **fresh** | Matches `config/synapse.toml` (`prompt_pack = "v4-condense"`) and triage placement. |
| `0004-the-log-is-append-only-and-state-is-a-fold.md` | **fresh** | Option A projection is what `store.py`/`fold.py` actually do; `SessionStatus` follows the same shape. |
| `0005-the-synthesis-output-budget-is-derived.md` | **fresh** | Every claim verified in code (see §C). The most accurate doc in the repo. |

### `docs/plans/`
| Doc | Verdict | Reason |
|---|---|---|
| `plans/README.md` | **contradicted-by-code** | The single worst offender — invariant 3 wording, "no CodexSource", "A.5 compaction still unbuilt", "still parked: Codex/compaction/freshness pointer + relevance skill", "the three decisions worth a record" (five exist), E5 "pending merge". |
| `plans/2026-08-03-plan-0-foundation.md` | **stale (spec, historical)** | Contract freeze; `SessionStatus`, nullable `created_by`, `SessionEnded` all landed after it. |
| `plans/2026-08-03-plan-a-capture.md` | **contradicted-by-code** | L11 "Not built: A.5 compaction. `CodexSource` is also still missing" vs `packages/worker/src/synapse_worker/compaction.py` + `sources/codex.py`. |
| `plans/2026-08-03-plan-b-model.md` | **stale (spec)** | Superseded in part by the 2026-08-04 report and ADR 0003, as its own status note says. |
| `plans/2026-08-03-plan-c-service.md` | **stale (spec)** | Status block still "pending merge to main"; route list pre-dates `/end` and `DELETE /members/{c}`. |
| `plans/2026-08-03-plan-d-orchestrator.md` | **contradicted-by-code** | L68 "Tools: `query(nl)` · `contribute(text)`" — D.3's tool list is amended by the lifecycle spec; the plan itself never says so. |
| `plans/2026-08-05-plan-e-brain.md` | **stale (spec)** | Restates invariant 3 in Agent-Session terms (L305) throughout. |
| `plans/exec/*.md` (E1–E9, 9 files) | **orphaned (execution records)** | Completed TDD scripts; valuable as history, actively misleading if read as current. E2 still carries a known-false "Task 4 remains blocked" sentence (`docs/STATE.md:109`). |

### `docs/brainstorming/` (12 files)
**orphaned (historical)** — declared so by `docs/plans/README.md:3` ("Do not execute them"). Two are still load-bearing references and should not be deleted: `2026-07-30-npu-llm-benchmarks-and-geniex-findings.md` (the only measured hardware evidence) and `2026-08-03-local-orchestrator-domain-model-amendment.md` (amendment F, cited by ADR 0001 and the plans).

### `docs/misc/`, `docs/superpowers/`, `docs/overnight/`
| Doc | Verdict | Reason |
|---|---|---|
| `misc/hackathon-info.md` | **fresh** | Event facts; carries an explicit "do not push, contains credentials" warning. |
| `misc/synapse-proposal.md` | **orphaned** | The submitted proposal; near-duplicate of README §1–5, drifting independently. |
| `superpowers/specs/2026-08-06-session-lifecycle-design.md` | **fresh** | Matches shipped behaviour exactly; the de-facto reference for the four lifecycle tools. |
| `overnight/PLAN.md`, `LOG.md`, `STATE.md` | *(skipped — tonight's live journal)* | |

### `packs/`, `fixtures/`, `packages/`
| Doc | Verdict | Reason |
|---|---|---|
| `/Users/siddharthsingh/Dev/synapse/packs/claude-code/INSTALL.md` | **stale** | "The tools appear as `mcp__synapse__query` and `mcp__synapse__contribute`" (L53–54); join framed as terminal-only (L58); prerequisite line conflates orchestrator port with service URL (L75). |
| `/Users/siddharthsingh/Dev/synapse/packs/claude-code/skills/synapse-shared-memory/SKILL.md` | **contradicted-by-code** | L44–46 "There is nothing to attach to and no session id to pass" — `join_session(shared_id)` and `create_session(purpose)` are exactly that. Content is test-pinned by `tests/test_awareness_pack_content.py`. |
| `/Users/siddharthsingh/Dev/synapse/fixtures/README.md` | **fresh** | Golden co-sign gate still genuinely open. |
| `/Users/siddharthsingh/Dev/synapse/fixtures/raw_lines/codex/README.md` | **fresh** | Research trail with honest residual-risk disclosure; NPU-RUNBOOK Phase 5 depends on it. |
| `packages/*/README*` | **absent** | Six packages, zero package-level READMEs. Every entry point is documented only in module docstrings. |

---

## B. Contradicted claims — doc line → code that contradicts it

1. **Invariant 3 is written in the wrong identity.**
   `/Users/siddharthsingh/Dev/synapse/docs/plans/README.md:71` — *"Awareness suppresses a Finding only when every Attribution is the asking agent's own Agent Session. **Scoped to Agent Session, never Contributor.**"*
   → `/Users/siddharthsingh/Dev/synapse/packages/service/src/synapse_service/retrieval.py:69-75` (`return attribution.contributor == asking_contributor`), `retrieval.py:7-15`, and `/Users/siddharthsingh/Dev/synapse/CONTEXT.md:122`. **This is the exact failure class that burned the team: the plans' canonical invariant list now asserts the opposite of shipped behaviour, in bold.**

2. Same claim, second location: `/Users/siddharthsingh/Dev/synapse/docs/adr/0002-semantic-merge-and-tombstones.md:36` → same code.

3. Same claim, third location — **in the frozen contract itself**: `/Users/siddharthsingh/Dev/synapse/packages/contracts/src/synapse_contracts/schemas.py:121` (*"`agent_session` for awareness suppression"*) and `schemas.py:141-142` → `retrieval.py:69-75`. A reader of `schemas.py` gets the pre-2026-08-06 rule with no amendment marker.

4. **The Orchestrator does not stamp Attribution.**
   `/Users/siddharthsingh/Dev/synapse/docs/adr/0001-local-orchestrator.md:13` and `/Users/siddharthsingh/Dev/synapse/docs/plans/README.md:62` (diagram: *"stamps Attribution"*)
   → `/Users/siddharthsingh/Dev/synapse/packages/orchestrator/src/synapse_orchestrator/app.py:16-27` (*"Attribution is no longer re-stamped"*) and `app.py:126-137`. Attribution is stamped by the worker's distiller and preserved as sent.

5. **CodexSource "missing".**
   `/Users/siddharthsingh/Dev/synapse/docs/plans/README.md:14`, `/Users/siddharthsingh/Dev/synapse/docs/plans/2026-08-03-plan-a-capture.md:11`, `/Users/siddharthsingh/Dev/synapse/docs/STATE.md:102`, `/Users/siddharthsingh/Dev/synapse/docs/STATE.md:151`, `/Users/siddharthsingh/Dev/synapse/README.md:59`
   → `/Users/siddharthsingh/Dev/synapse/packages/worker/src/synapse_worker/sources/codex.py:1` (616 lines) and `/Users/siddharthsingh/Dev/synapse/packages/worker/src/synapse_worker/discovery.py:279-284` (registry entry, `CodexSource`, `codex-rollout-jsonl`). `docs/STATE.md:90` says the opposite of `docs/STATE.md:102` in the same file.

6. **Compaction (A.5) "unbuilt".**
   `/Users/siddharthsingh/Dev/synapse/docs/plans/README.md:14`, `README.md:41`, `/Users/siddharthsingh/Dev/synapse/docs/plans/2026-08-03-plan-a-capture.md:11`, `/Users/siddharthsingh/Dev/synapse/docs/STATE.md:103`, `docs/STATE.md:151`
   → `/Users/siddharthsingh/Dev/synapse/packages/worker/src/synapse_worker/compaction.py` (318 lines) and `/Users/siddharthsingh/Dev/synapse/packages/worker/src/synapse_worker/loop.py:38,236-267` (wired into `tick()`, after triage).

7. **"Freshness pointer + relevance skill" listed as parked.**
   `/Users/siddharthsingh/Dev/synapse/docs/plans/README.md:41` → `/Users/siddharthsingh/Dev/synapse/packs/claude-code/hooks/freshness_pointer.py` and `/Users/siddharthsingh/Dev/synapse/packs/claude-code/skills/synapse-shared-memory/SKILL.md` both ship and are test-pinned (`/Users/siddharthsingh/Dev/synapse/tests/test_awareness_pack.py`, `tests/test_awareness_pack_content.py`).

8. **"The three decisions worth a record."**
   `/Users/siddharthsingh/Dev/synapse/docs/plans/README.md:47` → five files in `/Users/siddharthsingh/Dev/synapse/docs/adr/` (0004 and 0005 both Accepted).

9. **E5 "pending merge".**
   `/Users/siddharthsingh/Dev/synapse/docs/plans/README.md:18` and `README.md:34` → `/Users/siddharthsingh/Dev/synapse/docs/STATE.md:3` ("The brain is on `main`") and the merged code (`packages/service/src/synapse_service/fold.py`, `lanes.py`, `log.py` on main).

10. **Only two MCP tools documented.**
    `/Users/siddharthsingh/Dev/synapse/README.md:86`, `/Users/siddharthsingh/Dev/synapse/packs/claude-code/INSTALL.md:53-54`
    → six `@server.tool` registrations at `/Users/siddharthsingh/Dev/synapse/packages/orchestrator/src/synapse_orchestrator/server.py:472, 569, 640, 731, 794, 856`.

11. **"There is nothing to attach to and no session id to pass."**
    `/Users/siddharthsingh/Dev/synapse/packs/claude-code/skills/synapse-shared-memory/SKILL.md:44-46` → `server.py:653` (`create_session(purpose, agent_session_id=None)`) and `server.py:741` (`join_session(shared_id, agent_session_id=None)`); the "not joined" text itself now names both tools (`server.py:125-130`).

12. **README's teammate-join command produces a private, empty memory.**
    `/Users/siddharthsingh/Dev/synapse/README.md:86` — *"A second teammate joins the same Shared Session by re-running `serve_local.py --shared-id <the id printed above>` from their own machine"*
    → `/Users/siddharthsingh/Dev/synapse/scripts/serve_local.py:306-320`: without `--service-url`, `serve_local` starts its **own** service on 8899 and `POST`s the id into that empty store. The correct form is `/Users/siddharthsingh/Dev/synapse/docs/JOIN.md:53-58` (`--service-url http://<host-ip>:8899 --shared-id … --contributor …`).

13. **Resync purpose expectation.**
    `/Users/siddharthsingh/Dev/synapse/docs/demo-script.md:304` — expects `purpose: "(recovered by resync)"`
    → `/Users/siddharthsingh/Dev/synapse/packages/orchestrator/src/synapse_orchestrator/cli.py:341,348-349`: the retained `SessionMeta.purpose` is sent when known, and `/Users/siddharthsingh/Dev/synapse/scripts/serve_local.py:405-406` now records it on the documented path — so the placeholder is the exception, not the expected output.

14. **Test-count gates are all stale.** `/Users/siddharthsingh/Dev/synapse/docs/NPU-RUNBOOK.md:35` ("Gate: 717 passed"), `/Users/siddharthsingh/Dev/synapse/docs/STATE.md:3` (526), `docs/STATE.md:7` (387), `docs/STATE.md:74` (565), `/Users/siddharthsingh/Dev/synapse/docs/plans/README.md:34-35` (520/526/549/565) → 846 `def test_*` functions collected statically across `packages/` + `tests/` today. Phase 1 of the NPU runbook will "pass" a gate that is ~130 tests below reality.

15. **CONTEXT.md under-states the provenance enum.** `/Users/siddharthsingh/Dev/synapse/CONTEXT.md:128` (`distilled | contributed`) → `/Users/siddharthsingh/Dev/synapse/packages/contracts/src/synapse_contracts/schemas.py:81-86` has three members; `SYNTHESIZED` is written at `/Users/siddharthsingh/Dev/synapse/packages/service/src/synapse_service/synthesis.py:426`.

16. **INSTALL.md port confusion.** `/Users/siddharthsingh/Dev/synapse/packs/claude-code/INSTALL.md:75` — *"`synapse-orchestrator` reachable at its default `http://127.0.0.1:8899` Synapse Service URL"* → orchestrator binds 8787 (`cli.py:186`), `--service-url` defaults to 8899 (`cli.py:189`). Reads as "the orchestrator is on 8899".

---

## C. CONTEXT.md invariants, checked one by one

CONTEXT.md is a glossary + notes; the numbered invariants live in `docs/plans/README.md:67-74`. Both checked.

**The six numbered invariants (`docs/plans/README.md:69-74`):**
1. **Egress rule** — HELD. `app.py:97-117` (`_trust_violation`) + 422 on anything that is not a `Finding`; `contribute()` round-trips agent prose through the real distiller (`server.py:595-607`). Transcript-derived raw content never enters the orchestrator.
2. **Retrieval reads the Finding Log, not the Working Memory** — HELD. `api.py:684-724` ranks over `store.retrievable`/`store.candidates`; working memory appears only as prompt context (`retrieval.py:103`).
3. **Suppression** — **the statement is wrong, the code is right.** `retrieval.py:41-75` keys on Contributor with the empty-attributions guard intact; `api.py:104-146` keeps the legacy Agent-Session path for un-upgraded clients. The invariant text at `docs/plans/README.md:71` must be rewritten (see B1).
4. **Durable before send, retained after** — HELD. `app.py:190-197` records to the Relay before `flush()`; `cli.py:335-380` re-pushes the whole retained log. Caveat recorded honestly in `docs/STATE.md:110` (worker-side WAL re-join envelope).
5. **Merge produces a new record; originals are tombstones** — HELD, mechanism changed per ADR 0004: `synthesis.py:426` writes a new SYNTHESIZED Finding; supersession is derived from a `Merged` log entry, and `merged_into`/`status` are projected on egress only.
6. **Distiller compresses, does not judge** — HELD. `config/synapse.toml` pins `prompt_pack = "v4-condense"`; triage runs before the model (`loop.py:342-345`) and compaction after the keep decision.

**CONTEXT.md's own asserted facts:**
- L36 "Edge Worker … the only component that ever sees raw transcript content" — HELD (orchestrator sees agent-authored prose only, which the egress rule permits).
- L48 "Orchestrator … owns the LocalBinding" — LOOSE. Binding files are written by `packages/worker/src/synapse_worker/discovery.py` (`join_session`), which the orchestrator *calls* (`server.py:220-226`) rather than owns. Combined with ADR 0001's "stamps Attribution", the ownership story in the docs is one generation behind.
- L52 "All Producers post the same shape" — LOOSE. `contribute()` bypasses `POST /producer/findings` and calls `relay.record()` in-process (`server.py:615`).
- L70/L129 Tombstone as a derived condition, Option A projection — HELD (`fold.py`, `store.py`).
- L112 "as shipped, the governing [topic] lane is off" — HELD: `lanes.py:79` `DEFAULT_TOPIC_LANE = False`.
- L120–126 LocalBinding, Contributor-keyed suppression and watermark, `retrieval.visible_to`'s keyword-only `asking_agent_session`, `api._legacy_agent_session` — ALL HELD, verbatim accurate.
- L128 provenance enum — INCOMPLETE (see B15).
- L130 "The Finding Log is append-only" — HELD (`log.py`), with the ADR 0005 nuance that an *identical* resend is now skipped (`adr/0005` §8) — worth one clause in CONTEXT.md.
- Vocabulary integrity is machine-enforced: `/Users/siddharthsingh/Dev/synapse/tests/test_vocabulary.py` requires ten bolded terms to exist. It checks *presence*, not *truth* — which is why L122 is right and `docs/plans/README.md:71` is wrong with no test failing.

---

## D. Prioritized fix list

**P0 — actively dangerous (a reader acts on it and is wrong)**
1. `docs/plans/README.md:71` — rewrite invariant 3 to Contributor scope, with the back-compat path named. Every plan in the repo is written against this list.
2. `packages/contracts/src/synapse_contracts/schemas.py:121,141-142` — amend the Attribution/Finding docstrings to Contributor scope. The frozen contract is the file a new integrator reads first.
3. `README.md:86` — fix the teammate-join command to include `--service-url`; today it silently creates a second, empty memory.
4. `docs/NPU-RUNBOOK.md:35` — replace "717 passed" with a command that derives the number (`uv run pytest -q` tail), or re-measure. A gate whose number is stale is not a gate.
5. `docs/adr/0002:36` and `docs/adr/0001:13` — add dated amendment lines (do not rewrite history): suppression re-keyed 2026-08-06; Attribution is stamped by the worker, not re-stamped at the orchestrator.

**P1 — wrong-but-recoverable**
6. `docs/STATE.md:102,103,151` — delete or mark the Codex/compaction "parked" bullets that contradict `docs/STATE.md:90`.
7. `docs/plans/README.md:14,41,47` — CodexSource built, compaction built, awareness pack shipped, five ADRs.
8. `docs/plans/README.md:18,34` — E5 merged, not pending.
9. `README.md:59` — drop "Codex support is unbuilt — see Stretch Goals" (also a dangling pointer: Stretch Goals never mentions Codex); replace with the honest caveat from `fixtures/raw_lines/codex/README.md` ("confirmed from source, not yet a live transcript").
10. `README.md:86` + `packs/claude-code/INSTALL.md:53-54` + `SKILL.md:44-46` — six tools, not two; the skill must stop saying there is no session id to pass.
11. `packs/claude-code/INSTALL.md:75` — separate orchestrator :8787 from service :8899.

**P2 — structural gaps (no doc exists at all)**
12. No MCP tool reference anywhere; the only complete description is the `description=` strings in `server.py`.
13. No service HTTP reference; the eight routes exist only as `Route(...)` lines at `api.py:762-772`.
14. No troubleshooting page outside `docs/JOIN.md`'s table; the operational failure modes discovered 2026-08-06 (synthesis truncation, budget deferral, GenieX has no `/completions`) are recorded only in ADR 0005 and code comments.
15. No contributor guide; no `packages/*/README`.
16. `docs/architecture.html` un-audited and linked from README as the deep-dive — it is the largest unverified surface in the repo.

**P3 — hygiene**
17. `docs/demo-script.md` — reconcile §A/§B branch language, drop the `purpose: "(recovered by resync)"` expectation, add the 60s debounce (or `POST /synthesize` as the force-now override) so a live demo does not read as a hang.
18. `docs/plans/exec/2026-08-04-e2-triage.md` — the known-false "Task 4 remains blocked" sentence (tracked at `docs/STATE.md:109`).
19. `docs/misc/synapse-proposal.md` and README §1–5 have diverged; make one the source.
20. Add a doc-truth test alongside `tests/test_vocabulary.py` that asserts the invariant-3 sentence in `docs/plans/README.md` contains "Contributor" — cheap, and it is exactly the class of failure that recurred three times tonight.

---

## E. Writer plan — six areas

Common rule for every writer: **`CONTEXT.md` is the vocabulary authority, code is the behaviour authority, and no number goes in a doc unless the command that produces it is next to it.**

### 1. README / first-run story
- **Read:** `/Users/siddharthsingh/Dev/synapse/README.md` (current), `/Users/siddharthsingh/Dev/synapse/scripts/serve_local.py:1-28` (docstring — the intended first-run narrative) and `:196-330` (every flag, the `--service-url` join branch, the `--listen` branch), `/Users/siddharthsingh/Dev/synapse/docs/JOIN.md:23-136` (the correct multi-machine sequence), `/Users/siddharthsingh/Dev/synapse/pyproject.toml:1-12` (`requires-python`, workspace members), `/Users/siddharthsingh/Dev/synapse/packages/worker/src/synapse_worker/sources/codex.py:1-40` + `/Users/siddharthsingh/Dev/synapse/fixtures/raw_lines/codex/README.md` (what to say about Codex).
- **Build on:** the existing README's problem/solution framing (L5–27) is good and should be left alone. Rewrite only "Getting Started" (L53–102).
- **Must fix:** L59 Codex, L86 two-tool list + the broken teammate-join command; add the six tools and a pointer to JOIN.md for the second machine.

### 2. Architecture (current, not aspirational)
- **Read:** `/Users/siddharthsingh/Dev/synapse/CONTEXT.md` (whole), `/Users/siddharthsingh/Dev/synapse/docs/adr/0001`, `0003`, `0004`, `0005`, `/Users/siddharthsingh/Dev/synapse/packages/orchestrator/src/synapse_orchestrator/app.py:1-74` (single-egress, per-agent routing), `relay.py:1-60`, `/Users/siddharthsingh/Dev/synapse/packages/service/src/synapse_service/api.py:36-86` (debounce + governor), `synthesis.py:60-145` (`SynthesisBudget`), `lanes.py:50-80` (the five lanes and their shipped defaults), `fold.py`, `/Users/siddharthsingh/Dev/synapse/packages/worker/src/synapse_worker/loop.py:230-350` (tick: compact → triage → distil → push), `discovery.py:20-47,272-284` (agent registry).
- **Build on:** `docs/plans/README.md:54-65` (the ASCII diagram — correct except "stamps Attribution") and `docs/architecture.html`, which must be read in full and treated as suspect.
- **Must state:** attribution is stamped in the worker; suppression is Contributor-keyed; synthesis is rate-governed and its output budget is derived; the topic lane ships off.

### 3. Service HTTP reference
- **Read:** `/Users/siddharthsingh/Dev/synapse/packages/service/src/synapse_service/api.py` — routes at `:762-772`, and per handler: `create_session:320`, `add_member:346`, `leave_session:454`, `end_session:375`, `push_findings:469`, `synthesize:563`, `watermark:601`, `query:665`; the liveness gate `_unavailable:266` and the 422 contract `_missing:89`; `/Users/siddharthsingh/Dev/synapse/packages/service/src/synapse_service/debug.py:142-160` (`/debug`, `/debug/stats.json`); `cli.py:61-89` (`--host`, `--port 8899`, `--no-debug`).
- **Nothing exists today** beyond `Route(...)` lines and curl examples scattered in `docs/demo-script.md`.
- **Must document:** the eight `/v1` routes + two debug routes; the 404/409/422/403 matrix; `{"accepted", "memory_version", "synthesized", "deferred", "pending"}` on push and what `deferred` means; which routes are deliberately *not* gated on ENDED (`add_member`, `POST /end`, `DELETE /members/{c}`) and why.

### 4. MCP tool reference
- **Read:** `/Users/siddharthsingh/Dev/synapse/packages/orchestrator/src/synapse_orchestrator/server.py` in full — it is the spec; `/Users/siddharthsingh/Dev/synapse/docs/superpowers/specs/2026-08-06-session-lifecycle-design.md`; `briefing.py` (the `instructions` arrival briefing, signals ①/②); `/Users/siddharthsingh/Dev/synapse/packs/claude-code/INSTALL.md` (signals ③/④).
- **The complete surface today is six MCP tools, not ten** — enumerated from the code:

| # | Tool | Signature | server.py |
|---|---|---|---|
| 1 | `query` | `(question: str) -> str` | `:472` (decorator) / `:486` |
| 2 | `contribute` | `(text: str) -> str` | `:569` / `:573` |
| 3 | `create_session` | `(purpose: str, agent_session_id: str \| None = None)` | `:640` / `:653` |
| 4 | `join_session` | `(shared_id: str, agent_session_id: str \| None = None)` | `:731` / `:741` |
| 5 | `leave_session` | `() -> str` | `:794` / `:802` |
| 6 | `end_session` | `() -> str` | `:856` / `:864` |

  Plus two non-tool surfaces that belong on the same page: the **arrival briefing** on the initialize `instructions` field (`server.py:104-114`, `briefing.py`) and the **producer endpoint** `POST /producer/findings` (`app.py:142-198`). If "about ten" was expected, the missing four are HTTP routes, not tools — say so explicitly rather than inventing them.
- **Must document per tool:** the trigger-voice description verbatim, every failure string a user can see (`_NOT_JOINED:125`, `_SESSION_ENDED*:144-162`, `_NO_STATE_DIR:168`, the ambiguity refusal `:441-452`, the unmatched-`agent_session_id` refusal `:456-469`), and the rule that no tool ever raises.

### 5. Troubleshooting
- **Read:** `/Users/siddharthsingh/Dev/synapse/docs/JOIN.md:220-234` (the existing symptom table — the seed), `/Users/siddharthsingh/Dev/synapse/docs/adr/0005` (the whole outage post-mortem: truncation, timeout, governor, duplicate appends), `/Users/siddharthsingh/Dev/synapse/scripts/serve_local.py:316-345` (GenieX has no `/completions` → `aic100` 410s and queries come back empty with a 200), `/Users/siddharthsingh/Dev/synapse/packages/service/src/synapse_service/api.py:504-522` (the two distinct deferral log lines), `/Users/siddharthsingh/Dev/synapse/packages/providers/src/synapse_providers/aic100.py:254-318` (truncation detection), `/Users/siddharthsingh/Dev/synapse/packages/orchestrator/src/synapse_orchestrator/server.py:125-171`, `/Users/siddharthsingh/Dev/synapse/docs/NPU-RUNBOOK.md:13-17,124-129` (env traps + fallback ladder), `/Users/siddharthsingh/Dev/synapse/docs/JOIN-WINDOWS.md` (ARM64).
- **Must add over JOIN.md's table:** "findings land but memory never moves" (three distinct causes: debounce, budget governor, truncated verdict), "everything is 409", "the pointer is silent in one window", "`--npu` + synthesis returns empty", ports 8787/8899/18181/8790/8791.

### 6. Contributor guide
- **Read:** `/Users/siddharthsingh/Dev/synapse/pyproject.toml` (workspace, `pytest` config, coverage exclusion), `/Users/siddharthsingh/Dev/synapse/docs/plans/README.md:67-74` (the six invariants — fix #1 first, then cite), `/Users/siddharthsingh/Dev/synapse/CONTEXT.md`, `/Users/siddharthsingh/Dev/synapse/tests/test_vocabulary.py` and `tests/test_awareness_pack_content.py` (docs are test-pinned — a writer editing SKILL.md will break tests), `/Users/siddharthsingh/Dev/synapse/docs/adr/0004:1-40` (the ADR format in use), `/Users/siddharthsingh/Dev/synapse/config/synapse.toml` (the "every knob is env-overridable, every capability is measured" discipline), `/Users/siddharthsingh/Dev/synapse/packages/distiller/src/synapse_distiller/capability.py:41-124`.
- **Build on:** nothing exists — this is greenfield. The house rules are real but live only in code comments: fail-open in every MCP tool; no partial verdict application; the log is the record; budgets are derived, never typed in twice; docs get dated `⟨AMENDED …⟩` markers rather than silent rewrites.
- **Must state:** the six packages and what each owns, `uv sync` / `uv run pytest`, where to add a new agent adapter (registry entry + finder, `discovery.py:20-24`), and the ADR-when-you-change-a-decision rule.