# Synapse

**Shared working memory for AI-assisted teams.** Every engineer on the team works with a coding agent; each agent is blind to the others. Synapse pools what each agent learns so the team builds on shared knowledge instead of rediscovering it.

One person hosts a **Synapse Service** on the LAN — that is the shared memory. Everyone else runs their own **orchestrator** locally and points their agent at it. A teammate creates a **Shared Session** with a purpose; the others join with one command. From then on, each agent can ask what the team already knows (`query`) and put back what it learns (`contribute`), with the contributor's name attached to every Finding.

Nothing is injected unprompted, and raw transcript content never leaves the machine it was written on.

## The architecture in five lines

1. **Worker** (`packages/worker`) tails the local agent's transcript, triages it, and a small model distils it into **Findings** — key learnings, decisions, dead ends, open questions — each stamped with its Attribution. It is the only component that ever sees raw transcript content.
2. **Orchestrator** (`packages/orchestrator`) is the single egress: one per laptop, on `127.0.0.1:8787`. It serves the MCP tools and the producer endpoint, and rejects any body that is not `{"findings": [Finding…]}` with a 422 — never forwarded, never a 500 (`app.py:7-9`).
3. **Service** (`packages/service`) holds the Shared Session. The Finding Log is append-only; the Shared Memory is a fold over it (`adr/0004`), produced by a large model. Synthesis is debounced — at most one merge per 60s by default (`api.py:48`), with `POST /v1/sessions/{sid}/synthesize` as the force-now override.
4. **Retrieval** ranks over the Finding Log, not the working memory, across several lanes (`lanes.py`); the topic lane ships off (`lanes.py:79` `DEFAULT_TOPIC_LANE = False`). The service does the ranking, so reading costs you no model locally.
5. **Agents** reach all of it through six MCP tools — `query`, `contribute`, `create_session`, `join_session`, `leave_session`, `end_session` (`server.py:472, 569, 640, 731, 794, 856`).

Ports, once running: service `8899`, orchestrator `8787`, local model seam or `geniex serve` `18181` (`scripts/serve_local.py:65-68`).

## Start here

- [**First run**](first-run.md) — zero to a working stack on your own machine, no NPU and no credentials.
- [**Joining the team's Synapse**](JOIN.md) — the multi-machine path: point your orchestrator at someone else's service.
- [**Joining on Windows / Snapdragon X Elite**](JOIN-WINDOWS.md) — PowerShell throughout, with the ARM64 traps written out.
- [**NPU runbook**](NPU-RUNBOOK.md) — on-hardware work against GenieX.
- [**Demo script**](demo-script.md) and the [**demo readiness checklist**](DEMO-READINESS-CHECKLIST.md).

Design and vocabulary: `CONTEXT.md` at the repo root is the vocabulary authority; the decisions live in [`docs/adr/`](adr/0004-the-log-is-append-only-and-state-is-a-fold.md). Code is the behaviour authority — where a doc and the code disagree, the code is right and the doc is a bug.

## What is honestly true today

- **The memory is in-process.** Restart the host's service and the Shared Session is genuinely empty; the shared-id changes too.
- **The service has no auth.** `serve_local.py` binds `0.0.0.0` by default so teammates can reach it — anyone who can reach port 8899 can read and write the team's memory (`serve_local.py:364-367`). Pass `--host 127.0.0.1` to keep it local.
- **Distillation abstracts, it does not redact.** Measured verbatim overlap is 0.10 (`docs/JOIN.md:170`). Point the passive worker only at a conversation you would be happy to read aloud.
- **Codex.** The adapter exists and is registered (`packages/worker/src/synapse_worker/sources/codex.py`, `discovery.py:279-284`), but it was built from the Codex source tree rather than against a live transcript — see `fixtures/raw_lines/codex/README.md` for exactly what was and was not verified. Claude Code is the path with live evidence behind it.
