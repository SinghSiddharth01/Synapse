---
description: Is Synapse actually working here? Checks this session's MCP
  connection, the orchestrator, the Edge Worker, and pings the Service.
allowed-tools: Bash(synapse health:*), Bash(uv run synapse health:*)
---

Answer one question: **is Synapse working in this session right now?** Two
halves, and the first is the half the CLI cannot answer.

## 1. This session's MCP connection

Check whether `mcp__synapse__query` and `mcp__synapse__contribute` are
available to you *at this moment*. Say plainly whether they are.

This is not the same question as "is the orchestrator listening", and
conflating the two is the most common wrong diagnosis here. The port can
answer while this session has no tools at all, for two reasons worth telling
them apart:

- **the session predates the server** — `.mcp.json` is read at session start,
  so a session opened before `synapse up` ran never picked it up. A new
  session fixes it; restarting the orchestrator does not.
- **the project never approved it** — project-scoped servers are gated on
  approval by design. `/mcp` shows the connection and approval state.

If the tools *are* present, that is the authoritative answer for the MCP half.
Do not go looking for a port to confirm it.

## 2. The processes

Run `synapse health` from the Synapse checkout (`uv run synapse health` if it
is not on PATH) and report each line as it printed it — keep its own
PASS/WARN/FAIL, do not re-grade them. It covers:

- **`config service.url`** — a live HTTP ping of the Synapse Service, so this
  line is the service-up answer. Unset means this client was never pointed at
  one; unreachable means it was, and the host is not answering.
- **`orchestrator :8787`** — what this session's MCP tools talk to.
- **`edge worker :8790`** — passive capture, probed through its debug server.
  WARN here is narrow and worth saying precisely: findings stop being distilled
  from *this machine's* transcripts, while querying the team's memory is
  unaffected. A line reading `not started, by configuration` is not a problem —
  it means this machine chose `client.worker=off` or the read-only
  `client.distiller=listen` arm.
- **`session binding`** — whether this machine has joined a Shared Session.
  Without one, both tools return "not joined" rather than erroring.
- **prerequisites** — only the ones the configured distiller actually needs.

Exit code 1 means at least one FAIL. WARN means it runs, but the user should
know.

## Report

Lead with the verdict in one line — working, or the single thing that is
broken. Then the evidence. If something is wrong, give the remedy: `synapse
health` prints a `->` line under every result that has one, so quote that
rather than inventing a fix.

Two things not to do. Do not start, restart or stop anything to make a check
pass — this command reads state, and `synapse up` is the user's to run. And if
a port is reported as held by something that is not ours, say which port and
stop; a second orchestrator would stamp a different developer's identity onto
this machine's findings.
