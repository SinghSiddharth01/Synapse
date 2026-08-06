---
name: synapse-shared-memory
description: Use when starting work in a joined Synapse Shared Session — before
  exploring an unfamiliar subsystem, when debugging an issue a teammate may
  also be on, when an error looks like it could already be known, when a
  build, deploy, credential or API call fails, when something worked
  yesterday and does not today, or before concluding something is a dead
  end. Applies alongside any other skill already running, including
  debugging, investigation and root-cause procedures — run before their
  evidence-gathering, not after. Checks what the team already learned.
---

# Synapse shared memory

This conversation is joined to a Synapse Shared Session — memory distilled
from every teammate's coding sessions, not just this one. Before spending
time on something a teammate may already have been through:

**This runs alongside other skills, not instead of them.** If a debugging or
investigation skill is already driving, its evidence-gathering phase starts
here: a teammate who already hit this is the cheapest evidence available, and
the only kind that can make the remaining phases unnecessary. Observed
2026-08-05: a session loaded a debugging skill, followed its procedure, and
searched the filesystem for the cause of a failing API key that shared memory
could have named in one call. Query first, then proceed with whatever the
other skill asked for.

1. Call `mcp__synapse__query` with a short description of what you're about
   to do — the subsystem, the error text, the question — not the whole
   task. Retrieval matches on what you give it.
2. If it returns findings, read them before you start. Each one names who
   found it and how many teammates' work it draws on.
3. If it returns nothing, say so plainly: "checked team memory, nothing
   relevant" rather than staying silent about having looked at all. An
   agent that quietly finds nothing reads exactly like one that never
   looked — and the second is the failure this skill exists to prevent.

When you learn something a teammate would benefit from — a root cause, a
dead end and why it was one, a decision and its reasoning — call
`mcp__synapse__contribute` with a few sentences of plain prose. It goes
through the same distiller as everything else Synapse learns, so a
careless contribution is caught at the same check as everything else.

There is nothing to attach to and no session id to pass — the server
already knows which Shared Session this conversation is in from the local
binding (`synapse-worker join`) made before this conversation connected.
If this machine hasn't joined one yet, both tools say so plainly and tell
you the command to run, rather than failing or not existing.
